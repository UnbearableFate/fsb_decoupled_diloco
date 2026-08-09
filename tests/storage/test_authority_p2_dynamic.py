from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from fs_diloco.protocol.authority import MergeFenceConflict
from fs_diloco.protocol.contributor import DynamicContributorFence, DynamicMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    MembershipFenceError,
    initialize_authority_v4,
)
from tests.support.v4_protocol import (
    proposal_payload,
    publish_checkpoint_pair,
    publish_proposal_payload,
    receipt_payload,
)


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def open_dynamic(tmp_path: Path, clock: Clock, *, streams: int = 2) -> LeaderAuthority:
    identity = AuthorityIdentity(
        "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = DynamicMembershipScope(streams)
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=clock)
    return LeaderAuthority(database, identity, scope, wall_clock=clock)


def admit(leader, *, index: int, replace: str | None = None) -> DynamicContributorFence:
    admission = leader.admit_dynamic_incarnation(
        command_id=f"admit-{index}",
        instance_id=f"instance-{index}",
        placement_id=f"placement-{index % 2}",
        stream_id=index % 2,
        admission_token_sha256=hashlib.sha256(f"token-{index}".encode()).hexdigest(),
        hostname="host",
        pid=index + 1,
        launch_request_id=(f"replacement-launch-{index}" if replace is not None else None),
        replace_instance_id=replace,
        replacement_reason="authorized_replacement" if replace is not None else None,
    )
    assert isinstance(admission.fence, DynamicContributorFence)
    return admission.fence


def ingest_update(
    leader,
    run_root: Path,
    fence: DynamicContributorFence,
    *,
    sequence: int = 1,
    ordinal: int = 0,
) -> FullUpdateProposalV2:
    update_id = f"00000000-0000-4000-8000-{ordinal * 1000 + sequence:012d}"
    receipt = CycleReceiptV1.from_dict(
        receipt_payload(
            cycle_seq=sequence,
            stable_contributor_key=fence.stable_contributor_key,
            update_id=update_id,
            fence=fence.as_dict(),
        )
    )
    leader.ingest_cycle_receipt(
        command_id=f"receipt-{fence.instance_id}-{sequence}", receipt=receipt
    )
    proposal = FullUpdateProposalV2.from_dict(
        proposal_payload(
            cycle_seq=sequence,
            stable_contributor_key=fence.stable_contributor_key,
            update_id=update_id,
            receipt_sha256=receipt.immutable_sha256(),
            fence=fence.as_dict(),
        )
    )
    publish_proposal_payload(run_root, proposal)
    leader.ingest_proposal(command_id=f"proposal-{fence.instance_id}-{sequence}", proposal=proposal)
    return proposal


def initialize_v0(leader, run_root: Path) -> None:
    leader.initialize_v0(
        command_id="initialize-v0",
        publication_id="publication-v0",
        **publish_checkpoint_pair(run_root, version=0),
    )


def test_revoke_before_selection_leaves_current_quorum_progressing(tmp_path: Path) -> None:
    clock = Clock()
    with open_dynamic(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        leader.initialize_dynamic_membership(command_id="initialize-membership")
        stale_fence = admit(leader, index=0)
        current_fence = admit(leader, index=1)
        stale = ingest_update(leader, tmp_path, stale_fence, ordinal=0)
        current = ingest_update(leader, tmp_path, current_fence, ordinal=1)
        initialize_v0(leader, tmp_path)

        retired = leader.retire_incarnation(
            command_id="retire-stale",
            fence=stale_fence,
            reason="heartbeat_dead",
            final_status="expired",
        )
        attempt = leader.try_select_batch(command_id="select-v1", quorum_min=1, quorum_max=2)

        assert retired == (stale.update_id,)
        assert attempt.invalid_update_ids == ()
        assert attempt.batch is not None
        assert [candidate.proposal.update_id for candidate in attempt.batch.candidates] == [
            current.update_id
        ]
        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert dict(connection.execute("SELECT update_id, status FROM updates")) == {
                stale.update_id: "dropped",
                current.update_id: "selected",
            }
        finally:
            connection.close()


def test_selection_classifies_stale_rows_without_partial_abort(tmp_path: Path) -> None:
    clock = Clock()
    with open_dynamic(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        leader.initialize_dynamic_membership(command_id="initialize-membership")
        stale_fence = admit(leader, index=0)
        current_fence = admit(leader, index=1)
        stale = ingest_update(leader, tmp_path, stale_fence, ordinal=0)
        current = ingest_update(leader, tmp_path, current_fence, ordinal=1)
        initialize_v0(leader, tmp_path)
        fault = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            fault.execute(
                "UPDATE learner_instances SET status='revoked', stopped_at=101, "
                "status_reason='fault_injection' WHERE instance_id=?",
                (stale_fence.instance_id,),
            )
            fault.commit()
        finally:
            fault.close()

        attempt = leader.try_select_batch(command_id="select-v1", quorum_min=1, quorum_max=2)

        assert attempt.invalid_update_ids == (stale.update_id,)
        assert attempt.batch is not None
        assert [candidate.proposal.update_id for candidate in attempt.batch.candidates] == [
            current.update_id
        ]


def test_revoke_after_selection_returns_per_row_conflict_then_retry_commits(
    tmp_path: Path,
) -> None:
    clock = Clock()
    with open_dynamic(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        leader.initialize_dynamic_membership(command_id="initialize-membership")
        stale_fence = admit(leader, index=0)
        current_fence = admit(leader, index=1)
        stale = ingest_update(leader, tmp_path, stale_fence, ordinal=0)
        current = ingest_update(leader, tmp_path, current_fence, ordinal=1)
        initialize_v0(leader, tmp_path)
        first_attempt = leader.try_select_batch(
            command_id="select-first", quorum_min=2, quorum_max=2
        )
        assert first_attempt.batch is not None
        first_intent = leader.prepare_publication(
            command_id="prepare-first",
            publication_id="publication-first-v1",
            target_version=1,
            selection_batch_id=first_attempt.batch.batch_id,
            **publish_checkpoint_pair(tmp_path, version=1, epoch=1),
        )
        leader.retire_incarnation(
            command_id="retire-after-select",
            fence=stale_fence,
            reason="heartbeat_dead",
            final_status="expired",
        )

        conflict = leader.commit_merge(
            command_id="commit-first", publication_id=first_intent.publication_id
        )

        assert conflict == MergeFenceConflict(
            publication_id=first_intent.publication_id,
            invalid_update_ids=(stale.update_id,),
            reset_pending_update_ids=(current.update_id,),
        )
        retry = leader.try_select_batch(command_id="select-retry", quorum_min=1, quorum_max=1)
        assert retry.batch is not None
        retry_intent = leader.prepare_publication(
            command_id="prepare-retry",
            publication_id="publication-retry-v1",
            target_version=1,
            selection_batch_id=retry.batch.batch_id,
            **publish_checkpoint_pair(tmp_path, version=1, epoch=2),
        )
        committed = leader.commit_merge(
            command_id="commit-retry", publication_id=retry_intent.publication_id
        )
        assert not isinstance(committed, MergeFenceConflict)
        assert committed.version == 1
        assert committed.direct_weight_tokens_applied == current.effective_tokens_this_update


def test_draining_preserves_only_declared_final_update_and_stop_terminalizes_it(
    tmp_path: Path,
) -> None:
    clock = Clock()
    with open_dynamic(tmp_path, clock, streams=1) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        leader.initialize_dynamic_membership(command_id="initialize-membership")
        fence = admit(leader, index=0)
        final_update_id = "00000000-0000-4000-8000-000000000001"

        leader.retire_incarnation(
            command_id="begin-drain",
            fence=fence,
            reason="orderly_drain",
            final_status="draining",
            final_update_id=final_update_id,
        )
        final = ingest_update(leader, tmp_path, fence, ordinal=0)
        assert final.update_id == final_update_id

        wrong_receipt = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=2,
                previous_receipt_id="receipt-0-1",
                previous_receipt_sha256=authority.read.contributor_progress(
                    "0"
                ).last_receipt_sha256,
                cursor_start=8,
                cursor_end=16,
                stable_contributor_key="0",
                update_id="00000000-0000-4000-8000-000000000002",
                fence=fence.as_dict(),
            )
        )
        with pytest.raises(MembershipFenceError):
            leader.ingest_cycle_receipt(command_id="wrong-final-receipt", receipt=wrong_receipt)

        terminalized = leader.retire_incarnation(
            command_id="finish-drain",
            fence=fence,
            reason="drain_complete",
            final_status="stopped",
        )
        assert terminalized == (final.update_id,)
        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM updates WHERE status IN ('pending', 'selected')"
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.close()


def test_authorized_replacement_atomically_retires_old_incarnation(tmp_path: Path) -> None:
    clock = Clock()
    with open_dynamic(tmp_path, clock, streams=1) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        leader.initialize_dynamic_membership(command_id="initialize-membership")
        old_fence = admit(leader, index=0)
        old = ingest_update(leader, tmp_path, old_fence)

        new_fence = admit(leader, index=2, replace=old_fence.instance_id)

        assert new_fence.placement_epoch == old_fence.placement_epoch + 1
        assert new_fence.stream_epoch == old_fence.stream_epoch + 1
        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert (
                connection.execute(
                    "SELECT status FROM updates WHERE update_id=?", (old.update_id,)
                ).fetchone()[0]
                == "dropped"
            )
            assert (
                connection.execute(
                    "SELECT current_instance_id FROM streams WHERE stream_id=0"
                ).fetchone()[0]
                == new_fence.instance_id
            )
        finally:
            connection.close()
