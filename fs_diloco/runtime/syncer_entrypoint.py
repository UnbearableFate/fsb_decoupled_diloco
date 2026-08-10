"""Candidate/lease composition entrypoint for the Full Protocol."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core.run_descriptor import LoadedRunDescriptor, load_run_descriptor
from ..observability.logging_utils import ActorTelemetryWriter


class _LeaseRenewer:
    def __init__(
        self,
        *,
        authority_factory: Callable[[], Any],
        token: Any,
        control: Any,
        interval_seconds: float,
    ) -> None:
        self._factory = authority_factory
        self._token = token
        self._control = control
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self._failed: BaseException | None = None
        self._renewal_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="lease-renewer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(5.0, self._interval * 2.0))
        if self._thread.is_alive():
            raise RuntimeError("lease renewer did not stop")

    def raise_if_failed(self) -> None:
        if self._failed is not None:
            raise RuntimeError("leader lease renewal failed") from self._failed

    @contextmanager
    def quiesce_for_fault_injection(self) -> Any:
        """Exclude an in-flight renewal transaction around a fault-harness SIGSTOP.

        The runtime loop calls this only when the explicit fault injection
        environment is present. Holding the lock across SIGSTOP
        proves that neither the main authority connection nor the renewer can
        retain a SQLite write transaction while the candidate is paused.
        """

        with self._renewal_lock:
            yield

    def _run(self) -> None:
        authority = None
        try:
            authority = self._factory()
            while not self._stop.wait(self._interval):
                with self._renewal_lock:
                    lease = authority.renew_leader(self._token)
                self._control.publish_heartbeat(lease)
        except BaseException as exc:
            self._failed = exc
            self._stop.set()
        finally:
            if authority is not None:
                authority.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--shared-root", required=True)
    return parser


def _scope(loaded: LoadedRunDescriptor) -> Any:
    from ..protocol.contributor import DynamicMembershipScope, StaticMembershipScope

    if loaded.config.membership.mode == "dynamic":
        return DynamicMembershipScope(int(loaded.descriptor["stream_pool_size"]))
    return StaticMembershipScope(tuple(loaded.descriptor["static_learner_ids"]))


def _initial_admission_ready(loaded: LoadedRunDescriptor, authority: Any) -> bool:
    fences = authority.read.current_contributor_fences()
    if loaded.config.membership.mode == "static":
        return {fence.stable_contributor_key for fence in fences} == set(
            loaded.descriptor["static_learner_ids"]
        )
    return len(fences) >= int(loaded.descriptor["bootstrap_slots"])


def _admit_before_runtime_import(
    loaded: LoadedRunDescriptor,
    authority: Any,
    leader: Any,
    telemetry: ActorTelemetryWriter,
    *,
    admit: Callable[..., None],
    monotonic_clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Briefly admit initial peers before the syncer imports Torch/model code.

    This is only a startup overlap window.  The main loop keeps scanning the
    same durable request directory, so late requests are neither required here
    nor lost when the bounded window expires.
    """

    shared = loaded.config
    poll_seconds = min(
        float(shared.sync.scan_interval_seconds),
        float(shared.membership.registration_scan_interval_seconds),
    )
    window_seconds = min(5.0, max(1.0, poll_seconds * 5.0))
    deadline = monotonic_clock() + window_seconds
    while True:
        admit(loaded, authority, leader, telemetry)
        if _initial_admission_ready(loaded, authority):
            telemetry.event(
                "initial_admission_ready_before_runtime_import",
                admitted_count=len(authority.read.current_contributor_fences()),
                waited_seconds=window_seconds - max(0.0, deadline - monotonic_clock()),
            )
            return
        remaining = deadline - monotonic_clock()
        if remaining <= 0.0:
            telemetry.event(
                "initial_admission_window_expired",
                admitted_count=len(authority.read.current_contributor_fences()),
                window_seconds=window_seconds,
            )
            return
        sleep(min(poll_seconds, remaining))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    loaded = load_run_descriptor(
        args.shared_root,
        expected_descriptor_sha256=os.environ.get("FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"),
        expected_git_commit=os.environ.get("FS_DILOCO_EXPECTED_GIT_COMMIT"),
        expected_source_fingerprint=os.environ.get("FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"),
    )
    if Path(args.config).resolve() != loaded.paths.resolved_config_yaml.resolve():
        raise RuntimeError("syncer candidate must use the immutable descriptor config")
    # Opening the strict authority is deliberately after the immutable
    # descriptor/bootstrap gate. It is the only writable runtime adapter.
    from ..storage.control import ControlPublisher
    from ..storage.authority import AuthorityIdentity, LeaderAuthority
    from ..storage.leader_lease import LeaseUnavailableError

    config = loaded.config
    scope = _scope(loaded)
    identity = AuthorityIdentity(**loaded.identity.as_dict())

    def authority_factory() -> LeaderAuthority:
        return LeaderAuthority(
            loaded.paths.sqlite_db,
            identity,
            scope,
            marker_path=loaded.paths.bootstrap_complete_json,
            lease_duration_seconds=config.leader.lease_duration_seconds,
            max_clock_skew_seconds=config.leader.max_clock_skew_seconds,
            busy_timeout_ms=config.leader.business_busy_timeout_ms,
            run_root=loaded.paths.shared_root,
            orphan_grace_seconds=config.maintenance.publication_orphan_grace_seconds,
            max_quarantine_records_per_contributor=(
                config.maintenance.quarantine_records_per_contributor
            ),
        )

    authority = authority_factory()
    token = None
    renewer = None
    candidate_failed = False
    owner_id = f"syncer-{uuid.uuid4()}"
    deadline = time.monotonic() + config.leader.candidate_wait_seconds
    try:
        while token is None:
            try:
                token = authority.acquire_leader(
                    owner_id=owner_id,
                    pbs_job_id=os.environ.get("PBS_JOBID"),
                )
            except LeaseUnavailableError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("syncer candidate timed out waiting for leader lease")
                time.sleep(config.leader.candidate_acquire_poll_seconds)
        leader = authority.open_leader(token)
        control = ControlPublisher(loaded.paths, token)
        control.publish_heartbeat(authority.committed_leader_lease(token))
        renewer = _LeaseRenewer(
            authority_factory=authority_factory,
            token=token,
            control=control,
            interval_seconds=config.leader.renew_interval_seconds,
        )
        renewer.start()
        attempt_id = f"attempt-{uuid.uuid4()}"
        telemetry = ActorTelemetryWriter(
            loaded.paths.actor_metrics_path("syncer", token.owner_id, attempt_id),
            actor_kind="syncer",
            actor_id=token.owner_id,
            attempt_id=attempt_id,
        )
        telemetry.event("leadership_acquired", epoch=token.epoch)
        from .syncer import _admit_requests, run_fenced_syncer

        try:
            if config.membership.mode == "dynamic":
                leader.initialize_dynamic_membership(
                    command_id=f"initialize-dynamic-membership-e{token.epoch}"
                )
            _admit_before_runtime_import(
                loaded,
                authority,
                leader,
                telemetry,
                admit=_admit_requests,
            )
            run_fenced_syncer(
                loaded,
                authority,
                leader,
                control,
                attempt_id=attempt_id,
                renewer=renewer,
                telemetry=telemetry,
            )
        except BaseException as exc:
            try:
                control.publish_error(
                    attempt_id=attempt_id,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            except Exception as publication_error:
                exc.add_note(
                    f"candidate error-control publication also failed: {publication_error!r}"
                )
            try:
                authority.fail_leader(token)
                candidate_failed = True
            except Exception as failure_error:
                exc.add_note(f"candidate error fencing also failed: {failure_error!r}")
            raise
    finally:
        if renewer is not None:
            renewer.stop()
        if token is not None and not candidate_failed:
            try:
                authority.release_leader(token)
            except Exception:
                # A successor may already have fenced an expired/failed owner.
                pass
        authority.close()


if __name__ == "__main__":
    main()
