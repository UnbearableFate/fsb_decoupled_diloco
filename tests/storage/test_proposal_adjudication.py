"""Verify proposal replay, conflict, quarantine, and frontier adjudication."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from fs_diloco.protocol.authority import ProposalDisposition
from fs_diloco.protocol.contributor import ContributorFence, MembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority
from tests.support.protocol import (
    admit_contributor,
    proposal_payload,
    publish_proposal_payload,
    receipt_payload,
)


def open_authority(tmp_path: Path):
    """Open an authority leader with one admitted stream incarnation."""

    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = MembershipScope(1)
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=lambda: 100.0)
    authority = LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0)
    token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
    leader = authority.open_leader(token)
    fence = admit_contributor(leader)
    return authority, leader, fence


def build_cycle(
    leader,
    run_root: Path,
    fence: ContributorFence,
    *,
    cycle_seq: int,
    previous: CycleReceiptV1 | None = None,
) -> tuple[CycleReceiptV1, FullUpdateProposalV2]:
    """Ingest one receipt and publish its matching proposal payload."""

    receipt = CycleReceiptV1.from_dict(
        receipt_payload(
            cycle_seq=cycle_seq,
            previous_receipt_id=None if previous is None else previous.receipt_id,
            previous_receipt_sha256=(None if previous is None else previous.immutable_sha256()),
            cursor_start=8 * (cycle_seq - 1),
            cursor_end=8 * cycle_seq,
            fence=fence.as_dict(),
        )
    )
    leader.ingest_cycle_receipt(command_id=f"receipt-{cycle_seq}", receipt=receipt)
    proposal = FullUpdateProposalV2.from_dict(
        proposal_payload(
            cycle_seq=cycle_seq,
            receipt_sha256=receipt.immutable_sha256(),
            fence=fence.as_dict(),
        )
    )
    publish_proposal_payload(run_root, proposal)
    return receipt, proposal


def query_one(database: Path, sql: str, parameters: tuple = ()):
    """Read one SQLite result row for durable adjudication assertions."""

    connection = sqlite3.connect(database)
    try:
        return connection.execute(sql, parameters).fetchone()
    finally:
        connection.close()


def test_replay_collision_and_logical_conflict_are_explicit_and_audited(
    tmp_path: Path,
) -> None:
    """Exact replays remain idempotent while collisions are durably audited."""

    authority, leader, fence = open_authority(tmp_path)
    database = tmp_path / "authority.sqlite3"
    try:
        receipt, proposal = build_cycle(leader, tmp_path, fence, cycle_seq=1)
        assert (
            leader.ingest_proposal(command_id="accept", proposal=proposal)
            is ProposalDisposition.ACCEPTED
        )
        assert (
            leader.ingest_proposal(command_id="replay", proposal=proposal)
            is ProposalDisposition.EXACT_REPLAY
        )

        collision = replace(proposal, base_global_version=7)
        assert (
            leader.ingest_proposal(command_id="collision", proposal=collision)
            is ProposalDisposition.IDENTITY_COLLISION
        )
        assert (
            leader.ingest_proposal(command_id="collision-repeat", proposal=collision)
            is ProposalDisposition.IDENTITY_COLLISION
        )

        conflict_payload = proposal_payload(
            cycle_seq=1,
            update_id="00000000-0000-4000-8000-000000000099",
            receipt_sha256=receipt.immutable_sha256(),
            fence=fence.as_dict(),
        )
        conflict = FullUpdateProposalV2.from_dict(conflict_payload)
        publish_proposal_payload(tmp_path, conflict)
        assert (
            leader.ingest_proposal(command_id="logical-conflict", proposal=conflict)
            is ProposalDisposition.CONFLICT
        )
        assert (
            leader.ingest_proposal(command_id="logical-conflict-repeat", proposal=conflict)
            is ProposalDisposition.CONFLICT
        )

        assert query_one(database, "SELECT COUNT(*) FROM updates")[0] == 1
        assert query_one(database, "SELECT COUNT(*) FROM proposal_conflicts")[0] == 4
        assert query_one(database, "SELECT COUNT(*) FROM proposal_quarantine")[0] == 2
        frontier = query_one(
            database,
            """
            SELECT f.last_terminal_cycle_seq, o.disposition
            FROM proposal_frontiers AS f
            JOIN proposal_observations AS o
                ON o.observation_id=f.terminal_observation_id
            """,
        )
        assert frontier == (1, "conflict")
        assert (
            query_one(
                database, "SELECT status FROM updates WHERE update_id=?", (proposal.update_id,)
            )[0]
            == "pending"
        )
    finally:
        authority.close()


def test_conflict_cannot_cross_receipt_gap_and_command_replay_needs_no_object(
    tmp_path: Path,
) -> None:
    """An accepted command replay is identified without rereading its payload."""

    """Logical conflicts cannot jump a receipt gap or depend on replayed objects."""

    authority, leader, fence = open_authority(tmp_path)
    database = tmp_path / "authority.sqlite3"
    try:
        _receipt, proposal = build_cycle(leader, tmp_path, fence, cycle_seq=1)
        assert (
            leader.ingest_proposal(command_id="accept", proposal=proposal)
            is ProposalDisposition.ACCEPTED
        )
        (tmp_path / proposal.payload_relative_path).unlink()
        assert (
            leader.ingest_proposal(command_id="accept", proposal=proposal)
            is ProposalDisposition.EXACT_REPLAY
        )
        publish_proposal_payload(tmp_path, proposal)

        missing_receipt_collision = replace(
            proposal,
            cycle_receipt_id="missing-receipt",
            cycle_receipt_sha256="f" * 64,
            base_global_version=9,
        )
        with pytest.raises(ValueError, match="receipt reference is missing"):
            leader.ingest_proposal(
                command_id="missing-receipt-collision",
                proposal=missing_receipt_collision,
            )
        assert query_one(database, "SELECT COUNT(*) FROM proposal_conflicts")[0] == 0
        assert query_one(database, "SELECT last_terminal_cycle_seq FROM proposal_frontiers")[0] == 1
    finally:
        authority.close()


def test_quarantine_hot_rows_are_bounded_for_distinct_conflicts(tmp_path: Path) -> None:
    """Per-contributor conflict quarantine remains bounded under unique collisions."""

    authority, leader, fence = open_authority(tmp_path)
    database = tmp_path / "authority.sqlite3"
    try:
        receipt, accepted = build_cycle(leader, tmp_path, fence, cycle_seq=1)
        leader.ingest_proposal(command_id="accept", proposal=accepted)
        for index in range(100, 170):
            conflict = FullUpdateProposalV2.from_dict(
                proposal_payload(
                    cycle_seq=1,
                    update_id=f"00000000-0000-4000-8000-{index:012d}",
                    receipt_sha256=receipt.immutable_sha256(),
                    fence=fence.as_dict(),
                )
            )
            publish_proposal_payload(tmp_path, conflict)
            assert (
                leader.ingest_proposal(command_id=f"conflict-{index}", proposal=conflict)
                is ProposalDisposition.CONFLICT
            )

        assert query_one(database, "SELECT COUNT(*) FROM proposal_quarantine")[0] == 64
        assert query_one(database, "SELECT COUNT(*) FROM proposal_conflicts")[0] == 70
    finally:
        authority.close()


@pytest.mark.crash_matrix
def test_insert_supersede_and_frontier_failures_roll_back_as_one_unit(tmp_path: Path) -> None:
    """Proposal insertion, supersession, and frontier advance share one transaction."""

    authority, leader, fence = open_authority(tmp_path)
    database = tmp_path / "authority.sqlite3"
    try:
        first_receipt, first = build_cycle(leader, tmp_path, fence, cycle_seq=1)
        fault = sqlite3.connect(database)
        fault.execute(
            """
            CREATE TRIGGER fail_update_insert BEFORE INSERT ON updates
            BEGIN SELECT RAISE(ABORT, 'insert fault'); END
            """
        )
        fault.commit()
        fault.close()
        with pytest.raises(sqlite3.IntegrityError, match="insert fault"):
            leader.ingest_proposal(command_id="failed-insert", proposal=first)
        assert query_one(database, "SELECT COUNT(*) FROM updates")[0] == 0
        assert query_one(database, "SELECT COUNT(*) FROM proposal_observations")[0] == 0
        assert query_one(database, "SELECT COUNT(*) FROM proposal_frontiers")[0] == 0

        fault = sqlite3.connect(database)
        fault.execute("DROP TRIGGER fail_update_insert")
        fault.commit()
        fault.close()
        leader.ingest_proposal(command_id="accept-first", proposal=first)
        second_receipt, second = build_cycle(
            leader, tmp_path, fence, cycle_seq=2, previous=first_receipt
        )
        fault = sqlite3.connect(database)
        fault.execute(
            f"""
            CREATE TRIGGER fail_supersede BEFORE UPDATE OF status ON updates
            WHEN OLD.update_id='{first.update_id}' AND NEW.status='dropped'
            BEGIN SELECT RAISE(ABORT, 'supersede fault'); END
            """
        )
        fault.commit()
        fault.close()
        with pytest.raises(sqlite3.IntegrityError, match="supersede fault"):
            leader.ingest_proposal(command_id="failed-supersede", proposal=second)
        assert query_one(database, "SELECT COUNT(*) FROM updates")[0] == 1
        assert (
            query_one(database, "SELECT status FROM updates WHERE update_id=?", (first.update_id,))[
                0
            ]
            == "pending"
        )

        fault = sqlite3.connect(database)
        fault.execute("DROP TRIGGER fail_supersede")
        fault.execute(
            """
            CREATE TRIGGER fail_frontier BEFORE UPDATE ON proposal_frontiers
            BEGIN SELECT RAISE(ABORT, 'frontier fault'); END
            """
        )
        fault.commit()
        fault.close()
        with pytest.raises(sqlite3.IntegrityError, match="frontier fault"):
            leader.ingest_proposal(command_id="failed-frontier", proposal=second)
        assert query_one(database, "SELECT COUNT(*) FROM updates")[0] == 1
        assert (
            query_one(database, "SELECT status FROM updates WHERE update_id=?", (first.update_id,))[
                0
            ]
            == "pending"
        )
        assert query_one(database, "SELECT last_terminal_cycle_seq FROM proposal_frontiers")[0] == 1
        del second_receipt
    finally:
        authority.close()


def test_frontier_foreign_key_and_active_proposal_bound(tmp_path: Path) -> None:
    """The active proposal frontier remains referentially valid and bounded."""

    authority, leader, fence = open_authority(tmp_path)
    database = tmp_path / "authority.sqlite3"
    try:
        previous = None
        proposals: list[FullUpdateProposalV2] = []
        for sequence in range(1, 7):
            previous, proposal = build_cycle(
                leader,
                tmp_path,
                fence,
                cycle_seq=sequence,
                previous=previous,
            )
            proposals.append(proposal)
            leader.ingest_proposal(command_id=f"accept-{sequence}", proposal=proposal)
        assert (
            query_one(
                database,
                "SELECT COUNT(*) FROM updates WHERE status IN ('pending', 'selected')",
            )[0]
            == 1
        )
        assert query_one(database, "SELECT last_terminal_cycle_seq FROM proposal_frontiers")[0] == 6

        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO proposal_frontiers(
                        run_id, stable_contributor_key, last_terminal_cycle_seq,
                        terminal_observation_id, updated_by_epoch, updated_at
                    ) VALUES ('run-current', 'orphan', 1, 999999, 1, 100)
                    """
                )
        finally:
            connection.close()
        assert proposals[-1].update_id
    finally:
        authority.close()
