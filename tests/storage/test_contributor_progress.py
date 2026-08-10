from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority
from tests.support.protocol import receipt_payload


def leader_for(tmp_path: Path):
    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=lambda: 100.0)
    authority = LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0)
    token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
    leader = authority.open_leader(token)
    binding = leader.bind_or_replace_static_attempt(
        command_id="bind-1",
        learner_id="learner-0",
        logical_launch_id="launch-0",
        attempt_id="attempt-1",
    )
    fence = StaticContributorFence(
        "static",
        binding.learner_id,
        binding.logical_launch_id,
        binding.attempt_id,
        binding.binding_generation,
    )
    return authority, leader, fence


def test_contributor_progress_advances_only_contiguous_receipt_chain(tmp_path: Path) -> None:
    authority, leader, fence = leader_for(tmp_path)
    try:
        first = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
        first_progress = leader.ingest_cycle_receipt(command_id="receipt-1", receipt=first)
        second = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=2,
                previous_receipt_id=first.receipt_id,
                previous_receipt_sha256=first.immutable_sha256(),
                cursor_start=8,
                cursor_end=16,
                fence=fence.as_dict(),
            )
        )
        second_progress = leader.ingest_cycle_receipt(command_id="receipt-2", receipt=second)

        assert first_progress.last_cycle_seq == 1
        assert second_progress.last_cycle_seq == 2
        assert second_progress.last_receipt_sha256 == second.immutable_sha256()
        assert second_progress.data_cursor == 16
        assert authority.read.contributor_progress("learner-0") == second_progress
    finally:
        authority.close()


def test_contributor_progress_rejects_sequence_hole_and_cursor_mismatch(tmp_path: Path) -> None:
    authority, leader, fence = leader_for(tmp_path)
    try:
        first = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=first)
        hole = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=3,
                previous_receipt_id=first.receipt_id,
                previous_receipt_sha256=first.immutable_sha256(),
                cursor_start=8,
                cursor_end=16,
                fence=fence.as_dict(),
            )
        )
        with pytest.raises(ValueError, match="contiguous"):
            leader.ingest_cycle_receipt(command_id="receipt-hole", receipt=hole)

        mismatch = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=2,
                previous_receipt_id=first.receipt_id,
                previous_receipt_sha256=first.immutable_sha256(),
                cursor_start=9,
                cursor_end=17,
                fence=fence.as_dict(),
            )
        )
        with pytest.raises(ValueError, match="cursor"):
            leader.ingest_cycle_receipt(command_id="receipt-cursor", receipt=mismatch)
    finally:
        authority.close()
