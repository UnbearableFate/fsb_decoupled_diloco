"""Verify bounded proposal-visibility observation and quarantine policy."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from fs_diloco.protocol.authority import ReadResult, ReadStatus
from fs_diloco.protocol.contributor import MembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority
from tests.support.protocol import admit_contributor, receipt_payload


@dataclass
class Clock:
    """Provide mutable deterministic time for visibility grace periods."""

    now: float = 100.0

    def __call__(self) -> float:
        """Return the current deterministic timestamp."""

        return self.now


def open_leader(tmp_path: Path, clock: Clock):
    """Open a leader and ingest the receipt required by visibility observations."""

    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = MembershipScope(1)
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=clock)
    authority = LeaderAuthority(
        database,
        identity,
        scope,
        wall_clock=clock,
        lease_duration_seconds=1000.0,
    )
    token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
    leader = authority.open_leader(token)
    fence = admit_contributor(leader)
    receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
    leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
    return authority, leader


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
    """Record one visibility observation with stable fixture identities."""

    result = (
        ReadResult(status, value=object(), fingerprint=fingerprint)
        if status is ReadStatus.OK
        else ReadResult(status, diagnostic=f"diagnostic-{status.value}", fingerprint=fingerprint)
    )
    return leader.observe_proposal_visibility(
        command_id=command,
        stable_contributor_key="0",
        cycle_seq=1,
        update_id="00000000-0000-4000-8000-000000000001",
        object_identity="proposal-object-1",
        pointer_signature=signature,
        pointer_sequence=sequence,
        source_relative_path="updates/latest/0.json",
        result=result,
        grace_seconds=grace,
        operator_deadline_seconds=deadline,
        max_archived_signatures=archive_limit,
    )


def query(tmp_path: Path, sql: str):
    """Read one durable visibility result from the authority database."""

    connection = sqlite3.connect(tmp_path / "authority.sqlite3")
    try:
        return connection.execute(sql).fetchone()
    finally:
        connection.close()


def test_not_found_requires_three_stable_observations_and_grace(tmp_path: Path) -> None:
    """Missing proposals require repeated stable observations across the grace window."""

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
    """Malformed content becomes terminal only after its exact fingerprint is stable."""

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
    """Transient recovery clears failures while an unresolved deadline requires review."""

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
    """An identity mismatch quarantines immediately without deleting the source object."""

    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    source = tmp_path / "updates/latest/0.json"
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
    """Pointer successor observations archive old signatures within a fixed bound."""

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


def test_visibility_requires_receipt_and_pointer_sequence_collision_fails_closed(
    tmp_path: Path,
) -> None:
    """Visibility requires a receipt and rejects conflicting pointer sequence identities."""

    clock = Clock()
    authority, leader = open_leader(tmp_path, clock)
    try:
        first = observe(
            leader,
            command="pointer-first",
            status=ReadStatus.OK,
            signature="pointer-a",
            sequence=1,
        )
        collision = observe(
            leader,
            command="pointer-collision",
            status=ReadStatus.OK,
            signature="pointer-b",
            sequence=1,
        )
        old_replay = observe(
            leader,
            command="pointer-old-replay",
            status=ReadStatus.OK,
            signature="pointer-old",
            sequence=0,
        )

        assert first.terminal_disposition is None
        assert collision.status is ReadStatus.IDENTITY_MISMATCH
        assert collision.terminal_disposition == "identity_mismatch"
        assert old_replay.terminal_disposition is None
        assert (
            query(tmp_path, "SELECT pointer_signature FROM proposal_visibility")[0] == "pointer-a"
        )
        assert query(tmp_path, "SELECT COUNT(*) FROM proposal_quarantine")[0] == 1

        with pytest.raises(ValueError, match="matching contiguous receipt"):
            leader.observe_proposal_visibility(
                command_id="missing-receipt",
                stable_contributor_key="0",
                cycle_seq=2,
                update_id="00000000-0000-4000-8000-000000000002",
                object_identity="proposal-object-2",
                pointer_signature="pointer-2",
                pointer_sequence=2,
                source_relative_path="updates/latest/0.json",
                result=ReadResult(ReadStatus.NOT_FOUND, diagnostic="missing"),
                grace_seconds=0.0,
                operator_deadline_seconds=1.0,
            )
        assert query(tmp_path, "SELECT last_terminal_cycle_seq FROM proposal_frontiers")[0] == 1
    finally:
        authority.close()
