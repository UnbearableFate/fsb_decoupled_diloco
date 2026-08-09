from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fs_diloco.core.config import Config
from fs_diloco.core.config_v4 import ConfigV4
from fs_diloco.core.run_descriptor import (
    DescriptorAuthorityIdentity,
    LoadedRunDescriptor,
)
from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.runtime.services.terminal import TerminalService, terminal_close_reason
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    initialize_authority_v4,
)
from fs_diloco.storage.control import V4ControlPublisher
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.terminal_request import publish_manual_terminal_request
from tests.support import VirtualClock
from tests.support.v4_protocol import publish_checkpoint_pair


PLAN03_REQUIREMENTS = frozenset({"P5-ARCH", "TERM-01", "TERM-02", "TERM-03"})


class Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


class MergeProbe:
    def __init__(self, result=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result

    def merge_once(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result


def _runtime(tmp_path: Path, clock: VirtualClock, *, max_terminal_merges: int):
    identity = AuthorityIdentity("run-v4", "source", hashlib.sha256(b"config").hexdigest())
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=clock.wall)
    authority = LeaderAuthority(database, identity, scope, wall_clock=clock.wall)
    leader = authority.open_leader(
        authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
    )
    leader.initialize_v0(
        command_id="v0",
        publication_id="publication-v0",
        **publish_checkpoint_pair(tmp_path, version=0),
    )
    shared = Config()
    shared.sync.scan_interval_seconds = 0.1
    shared.terminal.drain_ack_timeout_seconds = 1.0
    shared.terminal.registration_visibility_grace_seconds = 0.25
    shared.terminal.proposal_visibility_grace_seconds = 0.25
    shared.terminal.max_terminal_merges = max_terminal_merges
    config = ConfigV4(shared=shared)
    loaded = LoadedRunDescriptor(
        paths=RunPaths(tmp_path),
        descriptor={"run_id": "run-v4", "descriptor_sha256": "d" * 64},
        config=config,
        identity=DescriptorAuthorityIdentity(
            run_id="run-v4",
            source_fingerprint="source",
            config_sha256=hashlib.sha256(b"config").hexdigest(),
        ),
    )
    return authority, leader, loaded


@pytest.mark.parametrize(("maximum", "expected_calls"), [(0, 0), (1, 1)])
def test_terminal_service_honors_bounded_final_merge_policy(
    tmp_path: Path, maximum: int, expected_calls: int
) -> None:
    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=maximum)
    merge = MergeProbe()
    try:
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=V4ControlPublisher(loaded.paths, leader.token),
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
                    "quorum_max": loaded.config.shared.sync.quorum_max,
                    "purpose": "terminal",
                }
            ]
        assert terminal["state"] == "finalized"
        assert authority.read.controller_status()["state"] == "finalized"
    finally:
        authority.close()


def test_terminal_preclose_visibility_uses_one_frozen_wall_cutoff(tmp_path: Path) -> None:
    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    loaded.config.shared.terminal.allow_preclose_admission_during_drain = True
    cutoffs: list[float] = []
    try:
        service = TerminalService(
            loaded=loaded,
            authority=authority,
            leader=leader,
            control=V4ControlPublisher(loaded.paths, leader.token),
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
        assert authority.read.controller_status()["requested_at"] == pytest.approx(100.25)
    finally:
        authority.close()


def test_terminal_close_reason_consumes_global_target_and_deadline_policy(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(monotonic_seconds=0.0, wall_seconds=100.0)
    authority, _leader, loaded = _runtime(tmp_path, clock, max_terminal_merges=0)
    try:
        loaded.config.shared.sync.stop_after_outer_steps = 2
        loaded.config.shared.terminal.admission_close_policy = "global_target"
        assert terminal_close_reason(loaded, authority, version=1, now=100.0) is None
        assert terminal_close_reason(loaded, authority, version=2, now=100.0) == (
            "configured_target"
        )

        loaded.config.shared.terminal.admission_close_policy = "deadline"
        loaded.config.shared.terminal.deadline_seconds = 5.0
        assert terminal_close_reason(loaded, authority, version=0, now=104.9) is None
        assert terminal_close_reason(loaded, authority, version=0, now=105.0) == (
            "configured_deadline"
        )

        loaded.config.shared.terminal.admission_close_policy = "manual"
        request = publish_manual_terminal_request(
            loaded.paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            reason="operator maintenance",
            created_at=105.0,
        )
        assert terminal_close_reason(loaded, authority, version=0, now=105.0) == (
            f"manual:{request['request_id']}:operator maintenance"
        )
    finally:
        authority.close()
