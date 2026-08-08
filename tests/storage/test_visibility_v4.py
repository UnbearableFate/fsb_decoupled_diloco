from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fs_diloco.protocol.authority import ReadResult, ReadStatus
from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority_v4


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def open_leader(tmp_path: Path, clock: Clock):
    identity = AuthorityIdentity(
        "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=clock)
    authority = LeaderAuthority(
        database,
        identity,
        scope,
        wall_clock=clock,
        lease_duration_seconds=1000.0,
    )
    token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
    return authority, authority.open_leader(token)


def observe(
    leader,
    *,
    command: str,
    status: ReadStatus,
    signature: str = "pointer-1",
    sequence: int = 1,
    fingerprint: str | None = None,
    grace: float = 5.0,
    deadline: float = 20.0,
    archive_limit: int = 8,
):
    result = (
        ReadResult(status, value=object(), fingerprint=fingerprint)
        if status is ReadStatus.OK
        else ReadResult(status, diagnostic=f"diagnostic-{status.value}", fingerprint=fingerprint)
    )
    return leader.observe_proposal_visibility(
        command_id=command,
        stable_contributor_key="learner-0",
        cycle_seq=1,
        update_id="00000000-0000-4000-8000-000000000001",
        object_identity="proposal-object-1",
        pointer_signature=signature,
        pointer_sequence=sequence,
        source_relative_path="updates/latest/learner-0.json",
        result=result,
        grace_seconds=grace,
        operator_deadline_seconds=deadline,
        max_archived_signatures=archive_limit,
    )


def query(tmp_path: Path, sql: str):
    connection = sqlite3.connect(tmp_path / "authority.sqlite3")
    try:
        return connection.execute(sql).fetchone()
    finally:
        connection.close()


def test_not_found_requires_three_stable_observations_and_grace(tmp_path: Path) -> None:
    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    try:
        first = observe(leader, command="missing-1", status=ReadStatus.NOT_FOUND)
        clock.now = 104.0
        second = observe(leader, command="missing-2", status=ReadStatus.NOT_FOUND)
        clock.now = 105.0
        third = observe(leader, command="missing-3", status=ReadStatus.NOT_FOUND)

        assert first.terminal_disposition is None
        assert second.terminal_disposition is None
        assert third.terminal_disposition == "missing"
        assert third.stable_failure_count == 3
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_quarantine")[0] == 1
        assert (
            query(
                tmp_path,
                """
            SELECT COUNT(*) FROM proposal_frontiers AS f
            JOIN proposal_observations AS o
                ON o.observation_id=f.terminal_observation_id
            WHERE o.disposition='missing'
            """,
            )[0]
            == 1
        )
    finally:
        authority.close()


def test_malformed_requires_same_fingerprint_twice_across_grace(tmp_path: Path) -> None:
    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    try:
        observe(
            leader,
            command="malformed-a",
            status=ReadStatus.MALFORMED,
            fingerprint="a" * 64,
        )
        clock.now = 106.0
        reset = observe(
            leader,
            command="malformed-b",
            status=ReadStatus.MALFORMED,
            fingerprint="b" * 64,
        )
        clock.now = 111.0
        terminal = observe(
            leader,
            command="malformed-b-again",
            status=ReadStatus.MALFORMED,
            fingerprint="b" * 64,
        )

        assert reset.stable_failure_count == 1
        assert reset.terminal_disposition is None
        assert terminal.stable_failure_count == 2
        assert terminal.terminal_disposition == "malformed"
        replay = observe(
            leader,
            command="malformed-terminal-replay",
            status=ReadStatus.MALFORMED,
            fingerprint="b" * 64,
        )
        assert replay.observation_id == terminal.observation_id
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_quarantine")[0] == 1
    finally:
        authority.close()


def test_transient_recovery_never_drops_and_deadline_enters_manual_review(
    tmp_path: Path,
) -> None:
    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    try:
        transient = observe(
            leader,
            command="transient-1",
            status=ReadStatus.TRANSIENT_IO,
            deadline=10.0,
        )
        clock.now = 101.0
        recovered = observe(leader, command="recovered", status=ReadStatus.OK)
        assert transient.terminal_disposition is None
        assert recovered.terminal_disposition is None
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_quarantine")[0] == 0

        clock.now = 200.0
        observe(
            leader,
            command="transient-2",
            status=ReadStatus.TRANSIENT_IO,
            deadline=10.0,
        )
        clock.now = 210.0
        deadline = observe(
            leader,
            command="transient-deadline",
            status=ReadStatus.TRANSIENT_IO,
            deadline=10.0,
        )
        assert deadline.terminal_disposition == "manual_review"
        assert (
            query(
                tmp_path,
                "SELECT COUNT(*) FROM proposal_observations WHERE disposition='missing'",
            )[0]
            == 0
        )
    finally:
        authority.close()


def test_identity_mismatch_is_immediate_fail_closed_without_unlink(tmp_path: Path) -> None:
    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    source = tmp_path / "updates/latest/learner-0.json"
    source.parent.mkdir(parents=True)
    source.write_text("do-not-delete", encoding="utf-8")
    try:
        decision = observe(
            leader,
            command="identity-mismatch",
            status=ReadStatus.IDENTITY_MISMATCH,
        )
        assert decision.terminal_disposition == "identity_mismatch"
        assert source.read_text(encoding="utf-8") == "do-not-delete"
    finally:
        authority.close()


def test_visibility_upsert_and_pointer_archive_are_bounded(tmp_path: Path) -> None:
    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    try:
        for index in range(100):
            clock.now += 0.01
            observe(
                leader,
                command=f"poll-{index}",
                status=ReadStatus.OK,
                signature="pointer-1",
                sequence=1,
            )
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_visibility")[0] == 1
        for sequence in range(2, 42):
            clock.now += 0.01
            observe(
                leader,
                command=f"pointer-{sequence}",
                status=ReadStatus.OK,
                signature=f"pointer-{sequence}",
                sequence=sequence,
                archive_limit=8,
            )
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_visibility")[0] == 1
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_visibility_archive")[0] == 8
    finally:
        authority.close()
