"""Durable dynamic-capacity observation and PBS launch reconciliation."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Protocol

from ...core.config import MembershipSection, ScalingSection
from ...protocol.scheduler import SchedulerOperatorRequest
from ...storage.atomic_io import fsync_directory, publish_immutable_bytes
from ...storage.authority import LeaderAuthority, LeaderSession, MembershipFenceError
from ...storage.paths import RunPaths
from ..pbs_scheduler import PBSJobObservation


PLAN03_REQUIREMENTS = frozenset({"P5-ARCH", "SCHED-01", "SCHED-02", "SCHED-03", "SCHED-04"})


class SchedulerAdapter(Protocol):
    def submit_learner(self, **kwargs: Any) -> dict[str, Any]: ...

    def find_by_launch_request(
        self, launch_request_id: str, *, historical: bool = False
    ) -> PBSJobObservation | None: ...

    def query(self, job_id: str, *, historical: bool = False) -> PBSJobObservation: ...


class DynamicCapacityService:
    """One leader-fenced reconcile pass, safe to recreate after every failover."""

    def __init__(
        self,
        *,
        authority: LeaderAuthority,
        leader: LeaderSession,
        scheduler: SchedulerAdapter,
        scaling: ScalingSection,
        membership: MembershipSection,
        shared_root: Path,
        descriptor_sha256: str,
        wall_clock: Any = time.time,
    ) -> None:
        self.authority = authority
        self.leader = leader
        self.scheduler = scheduler
        self.scaling = scaling
        self.membership = membership
        self.shared_root = shared_root
        self.descriptor_sha256 = descriptor_sha256
        self.wall_clock = wall_clock

    def tick(
        self,
        *,
        global_version: int,
        eligible_contributors: int,
        selected_contributors: int,
    ) -> tuple[str, ...]:
        """Reconcile existing reservations, then persist and act on one capacity window."""

        if not self.scaling.enabled:
            return ()
        controller = self.authority.read.controller_status()
        if controller["state"] != "open":
            return ("controller_closed",)
        actions = list(self._apply_operator_requests())
        actions.extend(self._reconcile_launches())
        now = float(self.wall_clock())
        prior = self.authority.read.capacity_observations()
        if prior and now - float(prior[-1]["observed_at"]) < float(
            self.scaling.scheduler_reconcile_interval_seconds
        ):
            return tuple(actions)

        instances = self.authority.read.dynamic_instances()
        launches = self.authority.read.dynamic_launch_requests()
        reserved = sum(
            row["role"] != "bootstrap" and row["reservation_released_at"] is None
            for row in launches
        )
        productive = self._productive_instance_count(instances, now=now)
        key_material = f"{self.leader.token.epoch}\0{len(prior) + 1}\0{global_version}\0{now:.9f}"
        observation_key = "capacity-" + hashlib.sha256(key_material.encode()).hexdigest()[:32]
        prior_productive_windows = max(0, self.scaling.productive_window_count - 1)
        recent_productive = (
            []
            if prior_productive_windows == 0
            else [int(row["productive_instances"]) for row in prior[-prior_productive_windows:]]
        )
        stable_productive = min([productive, *recent_productive])
        low = stable_productive + reserved <= self.scaling.low_contributor_threshold
        action = "low" if low else "sufficient"
        self.leader.record_capacity_observation(
            command_id=f"observe-{observation_key}",
            observation_key=observation_key,
            global_version=global_version,
            eligible_contributors=eligible_contributors,
            selected_contributors=selected_contributors,
            productive_instances=productive,
            reserved_launch_capacity=reserved,
            desired_contributors=self.scaling.desired_contributors,
            action=action,
            retention_count=self.scaling.capacity_observation_retention_count,
        )
        observations = self.authority.read.capacity_observations()

        replacement = self._confirmed_lost_instance(instances, now=now)
        if replacement is not None and self._cooldown_elapsed(launches, now=now):
            instance, job_id = replacement
            planned = self._plan(
                observation_key=observation_key,
                stream_id=int(instance["stream_id"]),
                replace_instance_id=str(instance["instance_id"]),
                reason="confirmed_scheduler_terminal_after_progress_stall",
                expected_scheduler_job_id=job_id,
                now=now,
            )
            if planned:
                actions.extend(("replacement_planned", *self._reconcile_launches()))
            return tuple(actions)

        low_windows = observations[-self.scaling.consecutive_low_windows :]
        persistently_low = len(low_windows) == self.scaling.consecutive_low_windows and all(
            int(row["productive_instances"]) + int(row["reserved_launch_capacity"])
            <= self.scaling.low_contributor_threshold
            for row in low_windows
        )
        total_capacity = productive + reserved
        if (
            persistently_low
            and total_capacity < self.scaling.desired_contributors
            and self._cooldown_elapsed(launches, now=now)
        ):
            reserved_streams = {
                int(row["stream_id"])
                for row in launches
                if row["role"] != "bootstrap" and row["reservation_released_at"] is None
            }
            available = next(
                (
                    row
                    for row in self.authority.read.dynamic_streams()
                    if row["state"] == "available"
                    and row["current_instance_id"] is None
                    and int(row["stream_id"]) not in reserved_streams
                ),
                None,
            )
            if available is not None and self._plan(
                observation_key=observation_key,
                stream_id=int(available["stream_id"]),
                replace_instance_id=None,
                reason="persistent_low_productive_capacity",
                expected_scheduler_job_id=None,
                now=now,
            ):
                actions.extend(("scale_out_planned", *self._reconcile_launches()))
        return tuple(actions)

    def _productive_instance_count(
        self, instances: tuple[dict[str, Any], ...], *, now: float
    ) -> int:
        grace = min(
            float(self.scaling.productive_upload_grace_max_seconds),
            max(
                float(self.scaling.productive_upload_grace_min_seconds),
                float(self.scaling.productive_upload_grace_factor)
                * float(self.scaling.starvation_observation_seconds),
            ),
        )
        productive = 0
        for instance in instances:
            if instance["status"] != "admitted":
                continue
            admitted_at = float(instance["admitted_at"])
            if now - admitted_at <= float(self.scaling.startup_grace_seconds):
                productive += 1
                continue
            progress = self.authority.read.contributor_progress(str(instance["stream_id"]))
            if progress is not None and now - float(progress.updated_at) <= grace:
                productive += 1
        return productive

    def _confirmed_lost_instance(
        self, instances: tuple[dict[str, Any], ...], *, now: float
    ) -> tuple[dict[str, Any], str] | None:
        for instance in instances:
            if instance["status"] != "admitted" or instance["pbs_job_id"] is None:
                continue
            progress = self.authority.read.contributor_progress(str(instance["stream_id"]))
            last_progress = float(
                instance["admitted_at"] if progress is None else progress.updated_at
            )
            if now - last_progress <= float(self.membership.heartbeat_dead_after_seconds):
                continue
            job_id = str(instance["pbs_job_id"])
            live = self.scheduler.query(job_id)
            historical = self.scheduler.query(job_id, historical=True)
            terminal = live.classification == "finished" or (
                live.classification == "no_record" and historical.classification == "finished"
            )
            if terminal:
                return instance, job_id
        return None

    def _cooldown_elapsed(self, launches: tuple[dict[str, Any], ...], *, now: float) -> bool:
        prior = [float(row["created_at"]) for row in launches if row["role"] != "bootstrap"]
        return not prior or now - max(prior) >= float(self.scaling.cooldown_seconds)

    def _plan(
        self,
        *,
        observation_key: str,
        stream_id: int,
        replace_instance_id: str | None,
        reason: str,
        expected_scheduler_job_id: str | None,
        now: float,
    ) -> bool:
        request_material = f"{observation_key}\0{stream_id}\0{replace_instance_id or ''}\0{reason}"
        request_id = "launch-" + hashlib.sha256(request_material.encode()).hexdigest()[:32]
        try:
            self.leader.plan_dynamic_launch_request(
                command_id=f"plan-{request_id}",
                request_id=request_id,
                observation_key=observation_key,
                stream_id=stream_id,
                replace_instance_id=replace_instance_id,
                reason=reason,
                expires_at=now + float(self.scaling.launch_request_ttl_seconds),
                max_pending_requests=self.scaling.max_pending_launch_requests,
                max_total_requests=self.scaling.max_total_launch_requests,
                expected_scheduler_job_id=expected_scheduler_job_id,
            )
        except MembershipFenceError:
            return False
        except RuntimeError as exc:
            if "budget is exhausted" in str(exc):
                return False
            raise
        return True

    def _reconcile_launches(self) -> tuple[str, ...]:
        actions: list[str] = []
        for row in self.authority.read.dynamic_launch_requests():
            if row["role"] == "bootstrap":
                continue
            state = str(row["state"])
            request_id = str(row["request_id"])
            if state == "planned":
                if row["expires_at"] is not None and float(self.wall_clock()) >= float(
                    row["expires_at"]
                ):
                    self._transition(
                        row=row,
                        state="expired",
                        job_id=None,
                        scheduler_state="not_submitted",
                        evidence="launch_request_ttl",
                    )
                    actions.append("expired")
                    continue
                self.leader.transition_dynamic_launch_request(
                    command_id=f"submit-start-{request_id}",
                    request_id=request_id,
                    expected_state="planned",
                    state="submitting",
                    pbs_job_id=None,
                    scheduler_state="qsub_started",
                    evidence_source="qsub_started",
                )
                result = self.scheduler.submit_learner(
                    script=self.scaling.learner_pbs_script,
                    launch_request_id=request_id,
                    stream_id=int(row["stream_id"]),
                    replace_instance_id=row["replace_instance_id"],
                    shared_root=self.shared_root,
                    descriptor_sha256=self.descriptor_sha256,
                    walltime=str(self.scaling.learner_walltime),
                    queue=self.scaling.learner_queue,
                )
                job_id = result.get("job_id_normalized")
                if result.get("returncode") == 0 and isinstance(job_id, str):
                    self._transition(
                        row={**row, "state": "submitting"},
                        state="submitted",
                        job_id=job_id,
                        scheduler_state="queued",
                        evidence="qsub_receipt",
                    )
                    actions.append("submitted")
                else:
                    found = self._find_by_launch_request(request_id)
                    if found is not None and found.classification not in {
                        "query_failed",
                        "no_record",
                        "unknown",
                    }:
                        self._transition_observation(
                            {**row, "state": "submitting"}, found, "request_scan"
                        )
                        actions.append("submission_recovered")
                    else:
                        self._transition(
                            row={**row, "state": "submitting"},
                            state="submission_unknown",
                            job_id=None,
                            scheduler_state="no_receipt",
                            evidence="qsub_result_uncertain",
                            last_error=str(result.get("stderr", ""))[:512],
                        )
                        actions.append("submission_unknown")
                continue
            if state == "submitting":
                found = self._find_by_launch_request(request_id)
                if found is None:
                    self._transition(
                        row=row,
                        state="submission_unknown",
                        job_id=None,
                        scheduler_state="no_record",
                        evidence="successor_request_scan",
                    )
                else:
                    self._transition_observation(row, found, "successor_request_scan")
                actions.append("submitting_reconciled")
                continue
            if state in {"submission_unknown", "terminal_uncertain"} and row["pbs_job_id"] is None:
                found = self._find_by_launch_request(request_id)
                if found is not None and found.classification not in {
                    "query_failed",
                    "no_record",
                    "unknown",
                }:
                    self._transition_observation(row, found, "request_scan")
                    actions.append("uncertain_rearmed")
                elif state == "submission_unknown":
                    self._transition(
                        row=row,
                        state="terminal_uncertain",
                        job_id=None,
                        scheduler_state="no_record",
                        evidence="live+historical:no_record",
                    )
                    actions.append("terminal_uncertain")
                elif self._deadline_elapsed(row):
                    self._transition(
                        row=row,
                        state="manual_review",
                        job_id=None,
                        scheduler_state="no_record",
                        evidence="uncertainty_deadline",
                    )
                    actions.append("manual_review")
                continue
            if state in {"submitted", "started", "terminal_uncertain"} and row["pbs_job_id"]:
                job_id = str(row["pbs_job_id"])
                live = self.scheduler.query(job_id)
                observation = live
                evidence = "live_qstat"
                if live.classification == "no_record":
                    observation = self.scheduler.query(job_id, historical=True)
                    evidence = "historical_qstat"
                if observation.classification == "finished":
                    self._transition(
                        row=row,
                        state="failed",
                        job_id=job_id,
                        scheduler_state="finished",
                        evidence=evidence,
                        terminal_evidence=True,
                    )
                    actions.append("failed")
                elif observation.classification in {"queued", "prologue", "running", "suspended"}:
                    target = "started" if observation.classification == "running" else "submitted"
                    if target != state and not (state == "started" and target == "submitted"):
                        self._transition_observation(row, observation, evidence)
                        actions.append("positive_evidence")
                elif state != "terminal_uncertain":
                    self._transition(
                        row=row,
                        state="terminal_uncertain",
                        job_id=job_id,
                        scheduler_state=observation.classification,
                        evidence=evidence,
                    )
                    actions.append("terminal_uncertain")
                elif self._deadline_elapsed(row):
                    self._transition(
                        row=row,
                        state="manual_review",
                        job_id=job_id,
                        scheduler_state=observation.classification,
                        evidence="uncertainty_deadline",
                    )
                    actions.append("manual_review")
        return tuple(actions)

    def _apply_operator_requests(self) -> tuple[str, ...]:
        actions: list[str] = []
        root = RunPaths(self.shared_root).scheduler_operator_requests
        if not root.is_dir():
            return ()
        for path in sorted(root.glob("*.json")):
            relative_path = path.name
            if path.is_symlink():
                raw = f"symlink:{relative_path}".encode()
                rejection_reason = "scheduler operator request must not be a symlink"
            else:
                try:
                    raw = path.read_bytes()
                except OSError:
                    actions.append("operator_request_transient_io")
                    continue
                rejection_reason = None
            content_sha256 = hashlib.sha256(raw).hexdigest()
            if (
                self.authority.read.scheduler_operator_file_disposition(
                    relative_path, content_sha256
                )
                is not None
            ):
                self._archive_operator_file(
                    path=path,
                    raw=raw,
                    content_sha256=content_sha256,
                    was_symlink=path.is_symlink(),
                )
                continue
            request: SchedulerOperatorRequest | None = None
            try:
                if rejection_reason is not None:
                    raise ValueError(rejection_reason)
                if len(raw) > 1_048_576:
                    raise ValueError("scheduler operator request exceeds 1 MiB")
                payload = json.loads(raw)
                request = SchedulerOperatorRequest.from_dict(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                reason = str(exc) or type(exc).__name__
                self._record_operator_file_disposition(
                    relative_path=relative_path,
                    content_sha256=content_sha256,
                    disposition="rejected",
                    reason=reason,
                )
                if not self._archive_operator_file(
                    path=path,
                    raw=raw,
                    content_sha256=content_sha256,
                    was_symlink=rejection_reason is not None,
                ):
                    actions.append("operator_request_archive_deferred")
                actions.append("invalid_operator_request")
                continue
            assert request is not None
            result = self.leader.apply_scheduler_operator_request(
                command_id=f"apply-{request.immutable_sha256()}",
                operator_request=request,
            )
            self._record_operator_file_disposition(
                relative_path=relative_path,
                content_sha256=content_sha256,
                disposition="applied",
                reason=str(result["request_state"]),
            )
            if not self._archive_operator_file(
                path=path,
                raw=raw,
                content_sha256=content_sha256,
                was_symlink=False,
            ):
                actions.append("operator_request_archive_deferred")
            actions.append(f"operator_{result['request_state']}")
        return tuple(actions)

    @staticmethod
    def _archive_operator_file(
        *,
        path: Path,
        raw: bytes,
        content_sha256: str,
        was_symlink: bool,
    ) -> bool:
        """Move a disposed request out of the hot scan without losing its bytes."""

        archive = path.parent / "processed"
        archive.mkdir(parents=True, exist_ok=True)
        suffix = ".symlink" if was_symlink else ""
        target = archive / f"{content_sha256}-{path.name}{suffix}"
        publish_immutable_bytes(target, raw)
        try:
            if was_symlink:
                if not path.is_symlink():
                    return False
            elif (
                path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != content_sha256
            ):
                return False
            path.unlink()
            fsync_directory(path.parent)
        except FileNotFoundError:
            pass
        return True

    def _record_operator_file_disposition(
        self,
        *,
        relative_path: str,
        content_sha256: str,
        disposition: str,
        reason: str,
    ) -> None:
        identity = hashlib.sha256(f"{relative_path}\0{content_sha256}".encode()).hexdigest()
        self.leader.record_scheduler_operator_file_disposition(
            command_id=f"operator-file-{identity}",
            relative_path=relative_path,
            content_sha256=content_sha256,
            disposition=disposition,
            reason=reason[:512],
        )

    def _find_by_launch_request(self, request_id: str) -> PBSJobObservation | None:
        live = self.scheduler.find_by_launch_request(request_id)
        if live is not None and live.classification not in {
            "query_failed",
            "no_record",
            "unknown",
        }:
            return live
        return self.scheduler.find_by_launch_request(request_id, historical=True) or live

    def _transition_observation(
        self, row: dict[str, Any], observation: PBSJobObservation, evidence: str
    ) -> None:
        if observation.classification == "finished":
            self._transition(
                row=row,
                state="failed",
                job_id=observation.job_id,
                scheduler_state="finished",
                evidence=evidence,
                terminal_evidence=True,
            )
            return
        state = (
            "started"
            if observation.classification == "running"
            and row["state"] in {"submitted", "terminal_uncertain"}
            else "submitted"
        )
        self._transition(
            row=row,
            state=state,
            job_id=observation.job_id,
            scheduler_state=observation.classification,
            evidence=evidence,
        )

    def _transition(
        self,
        *,
        row: dict[str, Any],
        state: str,
        job_id: str | None,
        scheduler_state: str,
        evidence: str,
        terminal_evidence: bool = False,
        last_error: str | None = None,
    ) -> None:
        uncertain = state in {"submission_unknown", "terminal_uncertain"}
        transition_identity = json.dumps(
            {
                "request_id": row["request_id"],
                "expected_state": row["state"],
                "expected_updated_at": row["updated_at"],
                "state": state,
                "job_id": job_id,
                "scheduler_state": scheduler_state,
                "evidence": evidence,
                "terminal_evidence": terminal_evidence,
                "last_error": last_error,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.leader.transition_dynamic_launch_request(
            command_id="launch-transition-"
            + hashlib.sha256(transition_identity.encode()).hexdigest(),
            request_id=str(row["request_id"]),
            expected_state=str(row["state"]),
            state=state,
            pbs_job_id=job_id,
            scheduler_state=scheduler_state,
            evidence_source=evidence,
            uncertainty_timeout_seconds=(
                float(self.scaling.scheduler_uncertainty_timeout_seconds) if uncertain else None
            ),
            terminal_evidence=terminal_evidence,
            last_error=last_error,
        )

    def _deadline_elapsed(self, row: dict[str, Any]) -> bool:
        deadline = row.get("uncertainty_deadline")
        return deadline is not None and float(self.wall_clock()) >= float(deadline)
