"""Verify terminal-service close, drain, merge, and completion policy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.core.config import Config
from fs_diloco.core.run_descriptor import (
    DescriptorAuthorityIdentity,
    LoadedRunDescriptor,
)
from fs_diloco.observability.logging_utils import ActorTelemetryWriter
from fs_diloco.protocol.contributor import MembershipScope
from fs_diloco.runtime.services.merge import MergeAttemptStatus
from fs_diloco.runtime.services.terminal import (
    TerminalService,
    terminal_close_reason,
)
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    initialize_authority,
)
from fs_diloco.storage.control import ControlPublisher, publish_terminal_ack
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.terminal_request import publish_manual_terminal_request
from tests.support import VirtualClock
from tests.support.protocol import admit_contributor, publish_checkpoint_pair


class Telemetry:
    """Record terminal telemetry events without external I/O."""

    def __init__(self) -> None:
        """Start with an empty event sequence."""

        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        """Append one structured terminal event."""

        self.events.append((name, fields))


class MergeProbe:
    """Record terminal merge requests and return configured outcomes."""

    def __init__(self, result=MergeAttemptStatus.NO_BATCH) -> None:
        """Configure the default or queued merge outcomes."""

        self.calls: list[dict[str, object]] = []
        self.result = result

    def merge_once(self, **kwargs):
        """Record one merge call and return the next configured outcome."""

        self.calls.append(dict(kwargs))
        if isinstance(self.result, list):
            return self.result.pop(0)
        return self.result


def _runtime(tmp_path: Path, clock: VirtualClock, *, max_terminal_merges: int):
    """Create one initialized terminal runtime with an admitted stream."""

    identity = AuthorityIdentity("run-current", "source", hashlib.sha256(b"config").hexdigest())
    scope = MembershipScope(1)
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=clock.wall)
    authority = LeaderAuthority(database, identity, scope, wall_clock=clock.wall)
    leader = authority.open_leader(
        authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
    )
    leader.initialize_genesis(
        command_id="v0",
        publication_id="publication-v0",
        **publish_checkpoint_pair(tmp_path, version=0),
    )
    admit_contributor(leader)
    shared = Config()
    shared.sync.scan_interval_seconds = 0.1
    shared.terminal.drain_ack_timeout_seconds = 1.0
    shared.terminal.registration_visibility_grace_seconds = 0.25
    shared.terminal.proposal_visibility_grace_seconds = 0.25
    shared.terminal.max_terminal_merges = max_terminal_merges
    config = shared
    loaded = LoadedRunDescriptor(
        paths=RunPaths(tmp_path),
        descriptor={"run_id": "run-current", "descriptor_sha256": "d" * 64},
        config=config,
        identity=DescriptorAuthorityIdentity(
            run_id="run-current",
            source_fingerprint="source",
            config_sha256=hashlib.sha256(b"config").hexdigest(),
        ),
    )
    return authority, leader, loaded


@pytest.mark.parametrize(("maximum", "expected_calls"), [(0, 0), (1, 1)])
def test_terminal_service_honors_bounded_final_merge_policy(
    tmp_path: Path, maximum: int, expected_calls: int
) -> None:
    """Terminal draining performs no more than its configured final merge budget."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=maximum)
    merge = MergeProbe()
    try:
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=ControlPublisher(loaded.paths, leader.token),
            telemetry=Telemetry(),
            merge=merge,
            ingest=lambda: None,
            admit_preclose=lambda _cutoff: None,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )

        terminal = service.finalize(reason="configured_target")

        assert len(merge.calls) == expected_calls
        if merge.calls:
            assert merge.calls == [
                {
                    "quorum_min": 1,
                    "quorum_max": loaded.config.sync.quorum_max,
                    "purpose": "terminal",
                }
            ]
        assert terminal["state"] == "finalized"
        assert authority.read.controller_status()["state"] == "finalized"
    finally:
        authority.close()


def test_terminal_preclose_visibility_uses_one_frozen_wall_cutoff(tmp_path: Path) -> None:
    """All preclose admission scans use the one durable wall-clock cutoff."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    loaded.config.terminal.allow_preclose_admission_during_drain = True
    cutoffs: list[float] = []
    try:
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=ControlPublisher(loaded.paths, leader.token),
            telemetry=Telemetry(),
            merge=MergeProbe(),
            ingest=lambda: None,
            admit_preclose=cutoffs.append,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )

        service.finalize(reason="manual")

        assert cutoffs == [100.0, 100.0, 100.0, 100.0]
        controller = authority.read.controller_status()
        assert controller["requested_at"] == pytest.approx(100.0)
        assert controller["registration_visibility_deadline"] == pytest.approx(100.25)
    finally:
        authority.close()


def test_terminal_wait_does_not_sleep_past_durable_ack_deadline(tmp_path: Path) -> None:
    """Terminal polling never sleeps beyond the durable drain-ack deadline."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    loaded.config.sync.scan_interval_seconds = 10.0
    loaded.config.terminal.drain_ack_timeout_seconds = 0.25
    try:
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=ControlPublisher(loaded.paths, leader.token),
            telemetry=Telemetry(),
            merge=MergeProbe(),
            ingest=lambda: None,
            admit_preclose=lambda _cutoff: None,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )

        service.finalize(reason="configured_target")

        assert clock.wall() == pytest.approx(100.25)
    finally:
        authority.close()


def test_terminal_ack_telemetry_does_not_override_writer_identity(tmp_path: Path) -> None:
    """Ack metrics cannot override the telemetry writer's actor identity fields."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    telemetry_path = tmp_path / "metrics/syncer.jsonl"
    telemetry = ActorTelemetryWriter(
        telemetry_path,
        actor_kind="syncer",
        actor_id="syncer-owner",
        attempt_id="syncer-attempt",
    )
    try:
        fence = authority.read.current_contributor_fences()[0]
        leader.begin_terminal_close(
            command_id="terminal-close",
            reason="test",
            hard_crash_cycle_token_budget=1,
            drain_ack_timeout_seconds=1.0,
        )
        controller = authority.read.controller_status()
        publish_terminal_ack(
            loaded.paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            generation=int(controller["generation"]),
            actor_id=fence.instance_id,
            attempt_id=fence.instance_id,
            fence=fence,
            final_cycle_seq=0,
            final_update_id=None,
        )
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=ControlPublisher(loaded.paths, leader.token),
            telemetry=telemetry,
            merge=MergeProbe(),
            ingest=lambda: None,
            admit_preclose=lambda _cutoff: None,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )

        service._ingest_acks(controller)

        events = [json.loads(line) for line in telemetry_path.read_text().splitlines()]
        assert [event["event_type"] for event in events] == ["terminal_ack_ingested"]
        assert events[0]["actor_id"] == "syncer-owner"
        assert events[0]["contributor_actor_id"] == fence.instance_id
    finally:
        authority.close()


def test_terminal_merge_conflict_does_not_consume_terminal_budget(tmp_path: Path) -> None:
    """A fenced merge conflict does not consume the bounded terminal merge budget."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=1)
    merge = MergeProbe([MergeAttemptStatus.FENCE_CONFLICT, MergeAttemptStatus.NO_BATCH])
    try:
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=ControlPublisher(loaded.paths, leader.token),
            telemetry=Telemetry(),
            merge=merge,
            ingest=lambda: None,
            admit_preclose=lambda _cutoff: None,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )

        service.finalize(reason="configured_target")

        assert len(merge.calls) == 2
    finally:
        authority.close()


def test_manual_reason_is_not_used_as_a_protocol_command_identity(tmp_path: Path) -> None:
    """Untrusted manual close text never becomes a protocol command identity."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    loaded.config.terminal.admission_close_policy = "manual"
    request = publish_manual_terminal_request(
        loaded.paths,
        run_id="run-current",
        descriptor_sha256="d" * 64,
        reason="operator maintenance: 日本語",
        created_at=100.0,
    )
    try:
        reason = terminal_close_reason(loaded, authority, version=0, now=100.0)
        assert reason == f"manual:{request['request_id']}:operator maintenance: 日本語"
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=ControlPublisher(loaded.paths, leader.token),
            telemetry=Telemetry(),
            merge=MergeProbe(),
            ingest=lambda: None,
            admit_preclose=lambda _cutoff: None,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )

        assert service.finalize(reason=str(reason))["state"] == "finalized"
    finally:
        authority.close()


def test_preclose_cutoff_and_deadline_survive_successor_takeover(tmp_path: Path) -> None:
    """A successor resumes the durable preclose cutoff and ack deadline exactly."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    loaded.config.terminal.allow_preclose_admission_during_drain = True
    first_cutoffs: list[float] = []

    def crash_after_first_sleep(seconds: float) -> None:
        """Simulate process loss after the first durable terminal wait."""

        clock.advance(seconds)
        raise RuntimeError("injected preclose crash")

    first = TerminalService(
        loaded=loaded,
        authority=authority,
        leader=leader,
        control=ControlPublisher(loaded.paths, leader.token),
        telemetry=Telemetry(),
        merge=MergeProbe(),
        ingest=lambda: None,
        admit_preclose=first_cutoffs.append,
        monotonic_clock=clock.monotonic,
        wall_clock=clock.wall,
        sleep=crash_after_first_sleep,
    )
    try:
        with pytest.raises(RuntimeError, match="injected preclose crash"):
            first.finalize(reason="configured_target")
        frozen = authority.read.controller_status()
        assert frozen["state"] == "preclosing"
        assert frozen["requested_at"] == 100.0
        deadline = frozen["registration_visibility_deadline"]

        authority.release_leader(leader.token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="successor", hostname="host", pid=2)
        )
        resumed_cutoffs: list[float] = []
        resumed = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=successor,
            control=ControlPublisher(loaded.paths, successor.token),
            telemetry=Telemetry(),
            merge=MergeProbe(),
            ingest=lambda: None,
            admit_preclose=resumed_cutoffs.append,
            monotonic_clock=clock.monotonic,
            wall_clock=clock.wall,
            sleep=clock.advance,
        )
        resumed.finalize(reason="configured_target")

        assert resumed_cutoffs and set(resumed_cutoffs) == {100.0}
        assert frozen["requested_at"] == 100.0
        assert deadline == pytest.approx(100.25)
    finally:
        authority.close()


def test_terminal_close_reason_consumes_global_target_and_deadline_policy(
    tmp_path: Path,
) -> None:
    """Close-reason selection honors global targets and optional deadlines."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, _leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    try:
        loaded.config.sync.stop_after_outer_steps = 2
        loaded.config.terminal.admission_close_policy = "global_target"
        assert terminal_close_reason(loaded, authority, version=1, now=100.0) is None
        assert terminal_close_reason(loaded, authority, version=2, now=100.0) == (
            "configured_target"
        )

        loaded.config.terminal.admission_close_policy = "deadline"
        loaded.config.terminal.deadline_seconds = 5.0
        assert terminal_close_reason(loaded, authority, version=0, now=104.9) is None
        assert terminal_close_reason(loaded, authority, version=0, now=105.0) == (
            "configured_deadline"
        )

        loaded.config.terminal.admission_close_policy = "manual"
        request = publish_manual_terminal_request(
            loaded.paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            reason="operator maintenance",
            created_at=105.0,
        )
        assert terminal_close_reason(loaded, authority, version=0, now=105.0) == (
            f"manual:{request['request_id']}:operator maintenance"
        )
    finally:
        authority.close()


@pytest.mark.parametrize(
    ("reservation_released_at", "productive", "reserved", "expected"),
    [
        (None, 0, 1, None),
        (101.0, 1, 0, None),
        (101.0, 0, 0, "launch_budget_exhausted"),
    ],
)
def test_launch_budget_close_requires_released_reservations_and_low_capacity(
    tmp_path: Path,
    reservation_released_at: float | None,
    productive: int,
    reserved: int,
    expected: str | None,
) -> None:
    """Launch-budget close requires released reservations and proven low capacity."""

    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, _leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    loaded.config.terminal.admission_close_policy = "global_target_or_launch_budget"
    loaded.config.sync.stop_after_outer_steps = None
    loaded.config.sync.stop_after_direct_weight_tokens_applied = None
    loaded.config.scaling.enabled = True
    loaded.config.scaling.max_total_launch_requests = 1
    read = SimpleNamespace(
        controller_status=lambda: {"state": "open"},
        token_ledger_summary=lambda: SimpleNamespace(direct_applied=0),
        launch_requests=lambda: (
            {
                "role": "scale_out",
                "reservation_released_at": reservation_released_at,
            },
        ),
        capacity_observations=lambda: (
            {
                "productive_instances": productive,
                "reserved_launch_capacity": reserved,
                "desired_contributors": 1,
            },
        ),
    )
    try:
        assert (
            terminal_close_reason(
                loaded,
                SimpleNamespace(read=read),
                version=0,
                now=100.0,
            )
            == expected
        )
    finally:
        authority.close()
