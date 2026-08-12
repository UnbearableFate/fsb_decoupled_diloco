"""Verify operational authority transactions, terminal accounting, and audit cleanup."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.core.config import MaintenanceSection
from fs_diloco.protocol.contributor import ContributorFence, MembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.protocol.scheduler import (
    SchedulerOperatorAction,
    SchedulerOperatorRequest,
    scheduler_state_sha256,
)
from fs_diloco.runtime.services.maintenance import (
    MaintenanceService,
    delete_claimed_artifact_object,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.audit_archive import (
    build_audit_batch,
    build_audit_partition,
    command_receipt_path,
    delete_claimed_audit_batch_object,
    publish_audit_batch,
    publish_audit_partition,
)
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    CommandConflictError,
    LeaderAuthority,
    MembershipFenceError,
    initialize_authority,
)
from fs_diloco.storage.paths import RunPaths
from tests.support.protocol import (
    admit_contributor,
    proposal_payload,
    publish_checkpoint_pair,
    publish_proposal_payload,
    receipt_payload,
)


class Clock:
    """Provide mutable deterministic wall time for operational tests."""

    def __init__(self) -> None:
        """Start the clock at a stable nonzero timestamp."""

        self.now = 100.0

    def __call__(self) -> float:
        """Return the current deterministic timestamp."""

        return self.now


def _identity() -> AuthorityIdentity:
    """Return the stable authority identity used by operational fixtures."""

    return AuthorityIdentity(
        "run-current",
        "source-fingerprint",
        hashlib.sha256(b"config").hexdigest(),
    )


def _open_authority(tmp_path: Path, clock: Clock, *, pool_size: int = 1) -> LeaderAuthority:
    """Open the sole authority schema with a fixed logical stream pool."""

    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(pool_size)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    return LeaderAuthority(database, _identity(), scope, wall_clock=clock)


def _plan_launch(leader, *, request_id: str = "learner-request-1"):
    """Create one launch reservation from a durable capacity observation."""

    leader.initialize_membership(command_id="initialize-membership")
    observation = leader.record_capacity_observation(
        command_id="capacity-observation",
        observation_key="capacity-window-1",
        global_version=0,
        eligible_contributors=0,
        selected_contributors=0,
        productive_instances=0,
        reserved_launch_capacity=0,
        desired_contributors=1,
        action="low",
        retention_count=4,
    )
    return leader.plan_launch_request(
        command_id="record-launch",
        request_id=request_id,
        observation_key=str(observation["observation_key"]),
        stream_id=0,
        replace_instance_id=None,
        reason="persistent low capacity",
        expires_at=1000.0,
        max_pending_requests=1,
        max_total_requests=2,
    )


def _current_fence(leader) -> dict[str, object]:
    """Admit and return the first stream's current contributor fence payload."""

    return admit_contributor(leader).as_dict()


def _replace_fence(leader, old_fence: ContributorFence) -> ContributorFence:
    """Authorize and admit one replacement for an existing stream incarnation."""

    observation = leader.record_capacity_observation(
        command_id="observe-replacement",
        observation_key="capacity-replacement",
        global_version=0,
        eligible_contributors=0,
        selected_contributors=0,
        productive_instances=0,
        reserved_launch_capacity=0,
        desired_contributors=1,
        action="replace",
        retention_count=4,
    )
    planned = leader.plan_launch_request(
        command_id="plan-replacement",
        request_id="replacement-launch",
        observation_key=str(observation["observation_key"]),
        stream_id=old_fence.stream_id,
        replace_instance_id=old_fence.instance_id,
        reason="scheduler_terminal",
        expires_at=1000.0,
        max_pending_requests=1,
        max_total_requests=1,
        expected_scheduler_job_id="bootstrap-0.opbs",
    )
    submitting = leader.transition_launch_request(
        command_id="replacement-submitting",
        request_id=planned["request_id"],
        expected_state="planned",
        state="submitting",
        pbs_job_id=None,
        scheduler_state="qsub_started",
        evidence_source="qsub_started",
    )
    leader.transition_launch_request(
        command_id="replacement-submitted",
        request_id=planned["request_id"],
        expected_state=submitting["state"],
        state="submitted",
        pbs_job_id="replacement.opbs",
        scheduler_state="queued",
        evidence_source="qsub_receipt",
    )
    return leader.admit_incarnation(
        command_id="admit-replacement",
        instance_id="instance-replacement",
        placement_id="placement-replacement",
        stream_id=old_fence.stream_id,
        launch_request_id=planned["request_id"],
        replace_instance_id=old_fence.instance_id,
        replacement_reason="authorized replacement",
        admission_token_sha256="b" * 64,
        hostname="host",
        pid=2,
        pbs_job_id="replacement.opbs",
    ).fence


def _ingest_cycle(
    leader,
    run_root: Path,
    fence: dict[str, object],
    *,
    sequence: int,
    previous: CycleReceiptV1 | None,
    stable_contributor_key: str = "0",
    update_ordinal: int | None = None,
) -> tuple[CycleReceiptV1, FullUpdateProposalV2]:
    """Ingest one contiguous receipt and its published proposal."""

    update_id = (
        f"00000000-0000-4000-8000-{(sequence if update_ordinal is None else update_ordinal):012d}"
    )
    receipt = CycleReceiptV1.from_dict(
        receipt_payload(
            cycle_seq=sequence,
            previous_receipt_id=None if previous is None else previous.receipt_id,
            previous_receipt_sha256=(None if previous is None else previous.immutable_sha256()),
            cursor_start=8 * (sequence - 1),
            cursor_end=8 * sequence,
            fence=fence,
            update_id=update_id,
            stable_contributor_key=stable_contributor_key,
        )
    )
    leader.ingest_cycle_receipt(
        command_id=f"receipt-{stable_contributor_key}-{sequence}", receipt=receipt
    )
    proposal = FullUpdateProposalV2.from_dict(
        proposal_payload(
            cycle_seq=sequence,
            receipt_sha256=receipt.immutable_sha256(),
            fence=fence,
            update_id=update_id,
            stable_contributor_key=stable_contributor_key,
        )
    )
    publish_proposal_payload(run_root, proposal)
    leader.ingest_proposal(
        command_id=f"proposal-{stable_contributor_key}-{sequence}", proposal=proposal
    )
    return receipt, proposal


def _commit_next(leader, run_root: Path, *, version: int, terminal: bool = False) -> None:
    """Select one pending proposal and commit the next checkpoint version."""

    attempt = leader.try_select_batch(command_id=f"select-{version}", quorum_min=1, quorum_max=1)
    assert attempt.batch is not None
    leader.prepare_publication(
        command_id=f"prepare-{version}",
        publication_id=f"publication-{version}",
        target_version=version,
        selection_batch_id=attempt.batch.batch_id,
        **publish_checkpoint_pair(run_root, version=version),
    )
    leader.commit_merge(
        command_id=f"commit-{version}",
        publication_id=f"publication-{version}",
        terminal_generation=1 if terminal else None,
        terminal_merge_limit=1 if terminal else None,
    )


def test_authority_token_rollup_balances_receipt_only_and_applied_fates(tmp_path: Path) -> None:
    """Token rollup balances unpublished receipt tokens and applied proposal tokens."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _current_fence(leader)
        receipt, _ = _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        before = authority.read.token_ledger_summary()
        assert before.adjudicated_processed == 8
        assert before.local_discarded == 2
        assert before.direct_outstanding == 6
        _commit_next(leader, tmp_path, version=1)

        skipped_payload = receipt_payload(
            cycle_seq=2,
            previous_receipt_id=receipt.receipt_id,
            previous_receipt_sha256=receipt.immutable_sha256(),
            cursor_start=8,
            cursor_end=16,
            fence=fence,
        )
        skipped_payload.update(
            processed_tokens_this_cycle=8,
            effective_tokens_this_cycle=0,
            local_discarded_tokens_this_cycle=8,
            retained_tokens_since_base=0,
            proposal_expected=False,
            planned_update_id=None,
            planned_payload_sha256=None,
        )
        leader.ingest_cycle_receipt(
            command_id="receipt-only-2",
            receipt=CycleReceiptV1.from_dict(skipped_payload),
        )
        summary = authority.read.token_ledger_summary()
        assert summary.adjudicated_processed == 16
        assert summary.local_discarded == 10
        assert summary.direct_applied == 6
        assert summary.direct_reported_unpublished == 0
        assert summary.balance == 0


def test_abandoned_selection_does_not_consume_persistent_service_credit(
    tmp_path: Path,
) -> None:
    """Abandoning a selected publication restores its contributor service credit."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _current_fence(leader)
        _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        first = leader.try_select_batch(command_id="select-1", quorum_min=1, quorum_max=1)
        assert first.batch is not None
        assert first.batch.candidates[0].selection_credit == 0
        leader.prepare_publication(
            command_id="prepare-1",
            publication_id="publication-1",
            target_version=1,
            selection_batch_id=first.batch.batch_id,
            **publish_checkpoint_pair(tmp_path, version=1),
        )
        leader.abandon_publication(
            command_id="abandon-1",
            publication_id="publication-1",
            reason="injected publish failure",
        )

        retry = leader.try_select_batch(command_id="select-retry", quorum_min=1, quorum_max=1)
        assert retry.batch is not None
        assert retry.batch.candidates[0].stable_key == "0"
        assert retry.batch.candidates[0].selection_credit == 0


def test_sql_fair_selection_uses_committed_count_before_version_ties(tmp_path: Path) -> None:
    """Selection fairness orders streams by committed count before version ties."""

    clock = Clock()
    keys = tuple(str(index) for index in range(8))
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(len(keys))
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fences: dict[str, dict[str, object]] = {}
        receipts: dict[str, CycleReceiptV1] = {}
        sequences = {key: 1 for key in keys}
        for index, key in enumerate(keys):
            fence = admit_contributor(
                leader,
                stream_id=index,
                instance_id=f"instance-{index}",
            )
            fences[key] = fence.as_dict()
            receipt, _ = _ingest_cycle(
                leader,
                tmp_path,
                fences[key],
                sequence=1,
                previous=None,
                stable_contributor_key=key,
                update_ordinal=index * 100 + 1,
            )
            receipts[key] = receipt
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        lineage: list[tuple[str, ...]] = []
        for version in range(1, 9):
            attempt = leader.try_select_batch(
                command_id=f"select-{version}", quorum_min=3, quorum_max=3
            )
            assert attempt.batch is not None
            selected = tuple(candidate.stable_key for candidate in attempt.batch.candidates)
            lineage.append(selected)
            leader.prepare_publication(
                command_id=f"prepare-{version}",
                publication_id=f"publication-{version}",
                target_version=version,
                selection_batch_id=attempt.batch.batch_id,
                **publish_checkpoint_pair(tmp_path, version=version),
            )
            leader.commit_merge(
                command_id=f"commit-{version}", publication_id=f"publication-{version}"
            )
            if version == 8:
                continue
            for key in selected:
                index = int(key)
                sequences[key] += 1
                receipt, _ = _ingest_cycle(
                    leader,
                    tmp_path,
                    fences[key],
                    sequence=sequences[key],
                    previous=receipts[key],
                    stable_contributor_key=key,
                    update_ordinal=index * 100 + sequences[key],
                )
                receipts[key] = receipt

        assert lineage == [
            ("0", "1", "2"),
            ("3", "4", "5"),
            ("0", "6", "7"),
            ("1", "2", "3"),
            ("4", "5", "6"),
            ("0", "1", "7"),
            ("2", "3", "4"),
            ("5", "6", "7"),
        ]


def test_replacement_returns_full_contiguous_resume_state(tmp_path: Path) -> None:
    """Replacement admission returns the complete authoritative stream resume state."""

    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="init-membership")
        first = leader.admit_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
            pbs_job_id="1.opbs",
        )
        receipt = CycleReceiptV1.from_dict(
            receipt_payload(
                fence=first.fence.as_dict(),
                stable_contributor_key="0",
                update_id="00000000-0000-4000-8000-000000000001",
            )
        )
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        proposal = FullUpdateProposalV2.from_dict(
            proposal_payload(
                fence=first.fence.as_dict(),
                stable_contributor_key="0",
                update_id=receipt.planned_update_id,
                receipt_sha256=receipt.immutable_sha256(),
            )
        )
        publish_proposal_payload(tmp_path, proposal)
        leader.ingest_proposal(command_id="proposal-1", proposal=proposal)
        leader.record_capacity_observation(
            command_id="observe-replacement",
            observation_key="capacity-replacement-2",
            global_version=0,
            eligible_contributors=0,
            selected_contributors=0,
            productive_instances=0,
            reserved_launch_capacity=0,
            desired_contributors=1,
            action="replace",
            retention_count=4,
        )
        planned = leader.plan_launch_request(
            command_id="plan-replacement-2",
            request_id="replacement-launch-2",
            observation_key="capacity-replacement-2",
            stream_id=0,
            replace_instance_id="instance-1",
            reason="scheduler_terminal",
            expires_at=1000.0,
            max_pending_requests=1,
            max_total_requests=2,
            expected_scheduler_job_id="1.opbs",
        )
        submitting = leader.transition_launch_request(
            command_id="submit-replacement-2",
            request_id="replacement-launch-2",
            expected_state=planned["state"],
            state="submitting",
            pbs_job_id=None,
            scheduler_state="qsub_started",
            evidence_source="qsub_started",
        )
        leader.transition_launch_request(
            command_id="submitted-replacement-2",
            request_id="replacement-launch-2",
            expected_state=submitting["state"],
            state="submitted",
            pbs_job_id="2.opbs",
            scheduler_state="queued",
            evidence_source="qsub_receipt",
        )
        second = leader.admit_incarnation(
            command_id="admit-2",
            instance_id="instance-2",
            placement_id="placement-0",
            stream_id=0,
            admission_token_sha256="2" * 64,
            hostname="host",
            pid=2,
            pbs_job_id="2.opbs",
            launch_request_id="replacement-launch-2",
            replace_instance_id="instance-1",
            replacement_reason="authorized replacement",
        )

        assert second.resume.cursor == 8
        assert second.resume.last_receipt_id == receipt.receipt_id
        assert second.resume.last_receipt_sha256 == receipt.immutable_sha256()
        assert second.resume.last_update_id == receipt.planned_update_id
        assert second.resume.next_cycle_seq == 2
        assert second.resume.stream_epoch == second.fence.stream_epoch == 2
        assert authority.read.update_status(proposal.update_id) == "dropped"
        assert authority.read.token_ledger_summary().direct_dropped == 6
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="target reached")
        with sqlite3.connect(tmp_path / "authority.sqlite3") as connection:
            connection.execute(
                "UPDATE updates SET status='pending' WHERE update_id=?",
                (proposal.update_id,),
            )
        with pytest.raises(MembershipFenceError, match="contributor lineage"):
            leader.acknowledge_terminal_contributor(
                command_id="stale-live-update-ack",
                fence=second.fence,
                final_cycle_seq=1,
                final_update_id=second.resume.last_update_id,
            )
        with sqlite3.connect(tmp_path / "authority.sqlite3") as connection:
            connection.execute(
                "UPDATE updates SET status='dropped' WHERE update_id=?",
                (proposal.update_id,),
            )
        assert (
            leader.acknowledge_terminal_contributor(
                command_id="replacement-ack",
                fence=second.fence,
                final_cycle_seq=1,
                final_update_id=second.resume.last_update_id,
            )
            == "acked"
        )


def test_terminal_close_freezes_fence_blocks_admission_and_accounts_hard_crash(
    tmp_path: Path,
) -> None:
    """Terminal close freezes membership and bounds a declared hard-crash token gap."""

    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="init-membership")
        admission = leader.admit_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
        )
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(
            command_id="close",
            reason="target reached",
            hard_crash_cycle_token_budget=64,
        )
        with pytest.raises(MembershipFenceError, match="closed"):
            leader.admit_incarnation(
                command_id="late-admit",
                instance_id="instance-2",
                placement_id="placement-1",
                stream_id=0,
                bootstrap_slot=0,
                admission_token_sha256="2" * 64,
                hostname="host",
                pid=2,
            )
        with pytest.raises(ValueError, match="one-cycle"):
            leader.acknowledge_terminal_contributor(
                command_id="too-large-gap",
                fence=admission.fence,
                final_cycle_seq=None,
                hard_crash_gap_tokens_upper_bound=65,
            )
        assert (
            leader.acknowledge_terminal_contributor(
                command_id="hard-crash",
                fence=admission.fence,
                final_cycle_seq=None,
                hard_crash_gap_tokens_upper_bound=64,
            )
            == "hard_crash"
        )
        assert authority.read.token_ledger_summary().hard_crash_gap_tokens_upper_bound == 64
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"


def test_terminal_preclose_admits_only_requests_before_durable_cutoff(
    tmp_path: Path,
) -> None:
    """Terminal preclose admits only requests created before its durable cutoff."""

    clock = Clock()
    with _open_authority(tmp_path, clock, pool_size=2) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="init-membership")
        leader.begin_terminal_preclose(
            command_id="preclose",
            reason="target reached",
            registration_visibility_grace_seconds=10.0,
        )
        admitted = leader.admit_incarnation(
            command_id="admit-before-cutoff",
            instance_id="instance-before-cutoff",
            placement_id="placement-before-cutoff",
            stream_id=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
            bootstrap_slot=0,
            registration_created_at=99.0,
        )
        assert admitted.fence.stream_id == 0
        with pytest.raises(MembershipFenceError, match="after the preclose cutoff"):
            leader.admit_incarnation(
                command_id="admit-after-cutoff",
                instance_id="instance-after-cutoff",
                placement_id="placement-after-cutoff",
                stream_id=1,
                admission_token_sha256="2" * 64,
                hostname="host",
                pid=2,
                bootstrap_slot=1,
                registration_created_at=100.1,
            )


def test_terminal_hard_crash_gap_is_summed_per_lost_incarnation(tmp_path: Path) -> None:
    """Terminal accounting sums independent hard-crash gaps across lost instances."""

    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(2)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="init-membership")
        admissions = tuple(
            leader.admit_incarnation(
                command_id=f"admit-{index}",
                instance_id=f"instance-{index}",
                placement_id=f"placement-{index}",
                stream_id=index,
                admission_token_sha256=str(index + 1) * 64,
                hostname="host",
                pid=index + 1,
                bootstrap_slot=index,
            )
            for index in range(2)
        )
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(
            command_id="close",
            reason="target reached",
            hard_crash_cycle_token_budget=64,
        )
        for index, (admission, gap) in enumerate(zip(admissions, (64, 32), strict=True)):
            assert (
                leader.acknowledge_terminal_contributor(
                    command_id=f"hard-crash-{index}",
                    fence=admission.fence,
                    final_cycle_seq=None,
                    hard_crash_gap_tokens_upper_bound=gap,
                )
                == "hard_crash"
            )

        assert authority.read.token_ledger_summary().hard_crash_gap_tokens_upper_bound == 96
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"


def test_replacement_recovers_authoritative_cursor_and_receipt_chain(tmp_path: Path) -> None:
    """An authorized replacement resumes the stream cursor and receipt chain exactly."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence_payload = _current_fence(leader)
        receipt, proposal = _ingest_cycle(
            leader,
            tmp_path,
            fence_payload,
            sequence=1,
            previous=None,
        )
        progress = authority.read.contributor_progress("0")
        assert progress is not None
        fence = ContributorFence.from_dict(fence_payload)
        replacement_fence = _replace_fence(leader, fence)

        assert replacement_fence.stream_epoch == fence.stream_epoch + 1
        recovered = authority.read.contributor_progress("0")
        assert recovered == progress
        assert recovered.data_cursor == 8
        assert recovered.last_receipt_id == receipt.receipt_id
        assert recovered.last_receipt_sha256 == receipt.immutable_sha256()
        assert recovered.last_update_id == receipt.planned_update_id
        assert authority.read.update_status(proposal.update_id) == "dropped"

        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="target reached")
        assert (
            leader.acknowledge_terminal_contributor(
                command_id="replacement-ack",
                fence=replacement_fence,
                final_cycle_seq=1,
                final_update_id=recovered.last_update_id,
            )
            == "acked"
        )


def test_telemetry_deletion_cannot_change_authoritative_token_summary(tmp_path: Path) -> None:
    """Deleting non-authoritative telemetry cannot alter durable token accounting."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _current_fence(leader)
        _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        before = authority.read.token_ledger_summary()
        telemetry = tmp_path / "metrics/learner/instance-0/attempt.jsonl"
        telemetry.parent.mkdir(parents=True)
        telemetry.write_text('{"processed_tokens": 999999}\n', encoding="utf-8")
        telemetry.unlink()

        assert authority.read.token_ledger_summary() == before


def test_terminal_final_receipt_ack_preserves_zero_gap_and_balanced_tokens(
    tmp_path: Path,
) -> None:
    """A final receipt ack preserves zero crash gap and exact token balance."""

    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="init-membership")
        admission = leader.admit_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
        )
        payload = receipt_payload(
            fence=admission.fence.as_dict(),
            stable_contributor_key="0",
            update_id="00000000-0000-4000-8000-000000000001",
        )
        payload.update(
            effective_tokens_this_cycle=0,
            local_discarded_tokens_this_cycle=8,
            retained_tokens_since_base=0,
            proposal_expected=False,
            planned_update_id=None,
            planned_payload_sha256=None,
        )
        leader.ingest_cycle_receipt(
            command_id="receipt-1",
            receipt=CycleReceiptV1.from_dict(payload),
        )
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="target reached")

        assert (
            leader.acknowledge_terminal_contributor(
                command_id="ack-final",
                fence=admission.fence,
                final_cycle_seq=1,
            )
            == "acked"
        )
        summary = authority.read.token_ledger_summary()
        assert summary.hard_crash_gap_tokens_upper_bound == 0
        assert summary.adjudicated_processed == summary.local_discarded == 8
        assert summary.balance == 0
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"


def test_terminal_zero_cycle_ack_requires_no_receipt_and_preserves_zero_gap(
    tmp_path: Path,
) -> None:
    """A zero-cycle contributor may ack without a receipt and adds no token gap."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = ContributorFence.from_dict(_current_fence(leader))
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="zero work")

        assert (
            leader.acknowledge_terminal_contributor(
                command_id="ack-zero",
                fence=fence,
                final_cycle_seq=0,
            )
            == "acked"
        )
        summary = authority.read.token_ledger_summary()
        assert summary.hard_crash_gap_tokens_upper_bound == 0
        assert summary.balance == 0
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"
        terminal = authority.read.terminal_record()
        assert terminal is not None
        assert terminal["direct_weight_tokens_applied"] == 0


def test_terminal_close_snapshot_cannot_be_rewritten_by_a_second_command(
    tmp_path: Path,
) -> None:
    """A terminal snapshot is immutable under a different close command identity."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = ContributorFence.from_dict(_current_fence(leader))
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        assert (
            leader.begin_terminal_close(
                command_id="close", reason="first reason", hard_crash_cycle_token_budget=8
            ).value
            == "closing"
        )
        assert (
            leader.begin_terminal_close(
                command_id="close", reason="first reason", hard_crash_cycle_token_budget=8
            ).value
            == "closing"
        )
        with pytest.raises(RuntimeError, match="already active"):
            leader.begin_terminal_close(
                command_id="rewrite-close",
                reason="rewritten reason",
                hard_crash_cycle_token_budget=0,
            )
        assert (
            leader.acknowledge_terminal_contributor(
                command_id="hard-crash",
                fence=fence,
                final_cycle_seq=None,
                hard_crash_gap_tokens_upper_bound=8,
            )
            == "hard_crash"
        )


def test_terminal_ack_rejects_a_missing_proposal_promised_by_final_receipt(
    tmp_path: Path,
) -> None:
    """Terminal ack rejects a final receipt whose promised proposal never appeared."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence_payload = _current_fence(leader)
        fence = ContributorFence.from_dict(fence_payload)
        receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence_payload))
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(
            command_id="close", reason="target reached", hard_crash_cycle_token_budget=8
        )

        with pytest.raises(MembershipFenceError, match="promised a proposal"):
            leader.acknowledge_terminal_contributor(
                command_id="invalid-final-ack",
                fence=fence,
                final_cycle_seq=1,
                final_update_id=None,
            )
        assert (
            leader.acknowledge_terminal_contributor(
                command_id="hard-crash-ack",
                fence=fence,
                final_cycle_seq=None,
                hard_crash_gap_tokens_upper_bound=8,
            )
            == "hard_crash"
        )
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"


def test_terminal_close_accepts_only_one_contiguous_current_cycle_and_matching_update(
    tmp_path: Path,
) -> None:
    """Closing accepts only the bounded current cycle and its matching final update."""

    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(1)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="init-membership")
        admission = leader.admit_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
        )
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="target reached")
        receipt, proposal = _ingest_cycle(
            leader,
            tmp_path,
            admission.fence.as_dict(),
            sequence=1,
            previous=None,
            stable_contributor_key="0",
        )
        extra = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=2,
                previous_receipt_id=receipt.receipt_id,
                previous_receipt_sha256=receipt.immutable_sha256(),
                cursor_start=8,
                cursor_end=16,
                fence=admission.fence.as_dict(),
                stable_contributor_key="0",
                update_id="00000000-0000-4000-8000-000000000002",
            )
        )
        with pytest.raises(MembershipFenceError, match="current-cycle bound"):
            leader.ingest_cycle_receipt(command_id="receipt-2", receipt=extra)
        with pytest.raises(MembershipFenceError, match="final update"):
            leader.acknowledge_terminal_contributor(
                command_id="wrong-final",
                fence=admission.fence,
                final_cycle_seq=1,
                final_update_id="00000000-0000-4000-8000-999999999999",
            )
        assert (
            leader.acknowledge_terminal_contributor(
                command_id="ack-final",
                fence=admission.fence,
                final_cycle_seq=1,
                final_update_id=proposal.update_id,
            )
            == "acked"
        )
        _commit_next(leader, tmp_path, version=1, terminal=True)
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"


def test_scheduler_operator_request_is_expected_state_cas_and_audited(tmp_path: Path) -> None:
    """Operator resolution uses expected-state CAS and leaves durable audit evidence."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        row = _plan_launch(leader)
        with pytest.raises(ValueError, match="invalid scheduler transition"):
            leader.transition_launch_request(
                command_id="invalid-direct-admit",
                request_id=row["request_id"],
                expected_state="planned",
                state="admitted",
                pbs_job_id=None,
                scheduler_state=None,
                evidence_source="invalid",
            )
        submitting = leader.transition_launch_request(
            command_id="submitting",
            request_id=row["request_id"],
            expected_state="planned",
            state="submitting",
            pbs_job_id=None,
            scheduler_state="qsub_started",
            evidence_source="qsub_started",
        )
        with pytest.raises(ValueError, match="persistent timeout"):
            leader.transition_launch_request(
                command_id="missing-deadline",
                request_id=submitting["request_id"],
                expected_state="submitting",
                state="submission_unknown",
                pbs_job_id=None,
                scheduler_state="no_record",
                evidence_source="qsub_receipt_missing",
            )
        unknown = leader.transition_launch_request(
            command_id="submission-unknown",
            request_id=submitting["request_id"],
            expected_state="submitting",
            state="submission_unknown",
            pbs_job_id=None,
            scheduler_state="no_record",
            evidence_source="qsub_receipt_missing",
            uncertainty_timeout_seconds=30.0,
        )
        uncertain = leader.transition_launch_request(
            command_id="uncertain",
            request_id=unknown["request_id"],
            expected_state="submission_unknown",
            state="terminal_uncertain",
            pbs_job_id=None,
            scheduler_state="no_record",
            evidence_source="live+historical:no_record",
            uncertainty_timeout_seconds=30.0,
        )
        request = SchedulerOperatorRequest(
            format_version=1,
            request_id="scheduler-op-1",
            launch_request_id=row["request_id"],
            action=SchedulerOperatorAction.CONFIRM_JOB_ID,
            expected_state_sha256=scheduler_state_sha256(uncertain),
            reason="operator found accounting receipt",
            created_at=101.0,
            scheduler_job_id="123.opbs",
            evidence_source="accounting_record",
        )
        applied = leader.apply_scheduler_operator_request(
            command_id="apply-operator", operator_request=request
        )
        assert applied["request_state"] == "applied"
        assert applied["launch_state"] == "submitted"

        with sqlite3.connect(tmp_path / "authority.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            submitted = dict(
                connection.execute(
                    "SELECT * FROM launch_requests WHERE request_id=?",
                    (row["request_id"],),
                ).fetchone()
            )
        assert submitted["first_uncertain_at"] is None
        assert submitted["uncertainty_deadline"] is None
        assert submitted["reservation_released_at"] is None

        stale = SchedulerOperatorRequest(
            format_version=1,
            request_id="scheduler-op-stale",
            launch_request_id=row["request_id"],
            action=SchedulerOperatorAction.MARK_FAILED,
            expected_state_sha256="f" * 64,
            reason="stale view",
            created_at=102.0,
        )
        rejected = leader.apply_scheduler_operator_request(
            command_id="apply-stale", operator_request=stale
        )
        assert rejected["request_state"] == "stale_rejected"
        assert rejected["launch_state"] == "submitted"

        clock.now = 110.0
        second_uncertain = leader.transition_launch_request(
            command_id="second-uncertain",
            request_id=row["request_id"],
            expected_state="submitted",
            state="terminal_uncertain",
            pbs_job_id="123.opbs",
            scheduler_state="no_record",
            evidence_source="live+historical:no_record",
            uncertainty_timeout_seconds=30.0,
        )
        clock.now = 141.0
        reviewed = leader.transition_launch_request(
            command_id="manual-review",
            request_id=row["request_id"],
            expected_state="terminal_uncertain",
            state="manual_review",
            pbs_job_id="123.opbs",
            scheduler_state="no_record",
            evidence_source="deadline",
        )
        assert second_uncertain["uncertainty_deadline"] == 140.0
        failed_request = SchedulerOperatorRequest(
            format_version=1,
            request_id="scheduler-op-failed",
            launch_request_id=row["request_id"],
            action=SchedulerOperatorAction.MARK_FAILED,
            expected_state_sha256=scheduler_state_sha256(reviewed),
            reason="operator confirmed terminal failure",
            created_at=102.0,
        )
        failed = leader.apply_scheduler_operator_request(
            command_id="apply-failed", operator_request=failed_request
        )
        assert failed["request_state"] == "applied"
        assert failed["launch_state"] == "failed"
        with sqlite3.connect(tmp_path / "authority.sqlite3") as connection:
            released_at = connection.execute(
                "SELECT reservation_released_at FROM launch_requests WHERE request_id=?",
                (row["request_id"],),
            ).fetchone()[0]
        assert released_at == clock.now


def test_terminal_ack_can_precede_final_proposal_visibility_and_merge(tmp_path: Path) -> None:
    """A terminal ack may precede final proposal visibility and the bounded merge."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence_payload = _current_fence(leader)
        fence = ContributorFence.from_dict(fence_payload)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="target reached")
        update_id = "00000000-0000-4000-8000-000000000099"
        receipt = CycleReceiptV1.from_dict(
            receipt_payload(
                fence=fence_payload,
                stable_contributor_key="0",
                update_id=update_id,
            )
        )
        leader.ingest_cycle_receipt(command_id="receipt-final", receipt=receipt)

        assert (
            leader.acknowledge_terminal_contributor(
                command_id="ack-before-proposal",
                fence=fence,
                final_cycle_seq=1,
                final_update_id=update_id,
            )
            == "acked"
        )
        assert authority.read.update_status(update_id) is None

        proposal = FullUpdateProposalV2.from_dict(
            proposal_payload(
                fence=fence_payload,
                stable_contributor_key="0",
                update_id=update_id,
                receipt_sha256=receipt.immutable_sha256(),
            )
        )
        publish_proposal_payload(tmp_path, proposal)
        leader.ingest_proposal(command_id="proposal-final-visible", proposal=proposal)
        _commit_next(leader, tmp_path, version=1, terminal=True)
        assert authority.read.controller_status()["terminal_merge_count"] == 1
        assert authority.read.update_status(update_id) == "applied"
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == ("finalized")


def test_scheduler_uncertainty_deadline_survives_leader_change_and_bounds_resolution(
    tmp_path: Path,
) -> None:
    """Scheduler uncertainty retains one deadline across takeover until review."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        row = _plan_launch(leader)
        submitting = leader.transition_launch_request(
            command_id="submitting",
            request_id=row["request_id"],
            expected_state="planned",
            state="submitting",
            pbs_job_id=None,
            scheduler_state="qsub_started",
            evidence_source="qsub_started",
        )
        unknown = leader.transition_launch_request(
            command_id="unknown",
            request_id=submitting["request_id"],
            expected_state="submitting",
            state="submission_unknown",
            pbs_job_id=None,
            scheduler_state="no_record",
            evidence_source="qsub_receipt_missing",
            uncertainty_timeout_seconds=30.0,
        )
        assert unknown["first_uncertain_at"] == 100.0
        assert unknown["uncertainty_deadline"] == 130.0
        authority.release_leader(token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        )
        clock.now = 129.0
        uncertain = successor.transition_launch_request(
            command_id="terminal-uncertain",
            request_id=unknown["request_id"],
            expected_state="submission_unknown",
            state="terminal_uncertain",
            pbs_job_id=None,
            scheduler_state="no_record",
            evidence_source="live+historical:no_record",
            uncertainty_timeout_seconds=999.0,
        )
        assert uncertain["first_uncertain_at"] == 100.0
        assert uncertain["uncertainty_deadline"] == 130.0
        with pytest.raises(RuntimeError, match="deadline has not elapsed"):
            successor.transition_launch_request(
                command_id="too-early-review",
                request_id=unknown["request_id"],
                expected_state="terminal_uncertain",
                state="manual_review",
                pbs_job_id=None,
                scheduler_state="no_record",
                evidence_source="deadline",
            )
        clock.now = 131.0
        reviewed = successor.transition_launch_request(
            command_id="manual-review",
            request_id=unknown["request_id"],
            expected_state="terminal_uncertain",
            state="manual_review",
            pbs_job_id=None,
            scheduler_state="no_record",
            evidence_source="deadline",
        )
        assert reviewed["state"] == "manual_review"
        assert reviewed["uncertainty_deadline"] == 130.0


def test_immutable_audit_batch_precedes_exact_history_prune_and_preserves_rollup(
    tmp_path: Path,
) -> None:
    """Immutable audit publication precedes exact pruning without changing rollups."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _current_fence(leader)
        version_zero = publish_checkpoint_pair(tmp_path, version=0)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **version_zero,
        )
        first, _ = _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        _commit_next(leader, tmp_path, version=1)
        _ingest_cycle(leader, tmp_path, fence, sequence=2, previous=first)
        _commit_next(leader, tmp_path, version=2)
        before = authority.read.token_ledger_summary()
        records = authority.read.audit_history_records(cutoff_version=1)
        payload = build_audit_batch(
            batch_id="batch-through-v1",
            record_kind="authority_history",
            cutoff_version=1,
            records=records,
        )
        path, digest = publish_audit_batch(RunPaths(tmp_path), payload)
        archived = leader.archive_audit_batch(
            command_id="archive-v1",
            batch_id="batch-through-v1",
            cutoff_version=1,
            relative_path=RunPaths(tmp_path).relative(path),
            sha256=digest,
        )

        assert archived["row_count"] == len(records) > 0
        assert authority.read.latest_committed_version().version == 2
        assert authority.read.contributor_progress("0").last_cycle_seq == 2
        assert authority.read.token_ledger_summary() == before
        assert path.stat().st_mode & 0o222 == 0
        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM command_records WHERE command_id='v0-prepare'"
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.close()
        assert command_receipt_path(RunPaths(tmp_path), "v0-prepare").is_file()
        replayed_genesis = leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **version_zero,
        )
        assert replayed_genesis.version == 0
        with pytest.raises(CommandConflictError, match="different kind or request"):
            leader.initialize_genesis(
                command_id="v0",
                publication_id="different-publication-v0",
                **version_zero,
            )


def test_online_archive_retains_each_current_receipt_until_terminal_ack(
    tmp_path: Path,
) -> None:
    """Online archiving retains each current receipt until its terminal ack."""

    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = MembershipScope(2)
    initialize_authority(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fences: dict[str, ContributorFence] = {}
        for index in range(2):
            fences[str(index)] = admit_contributor(
                leader,
                stream_id=index,
                instance_id=f"instance-{index}",
            )
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        receipts: dict[str, CycleReceiptV1] = {}
        proposals: dict[str, FullUpdateProposalV2] = {}
        for index in range(2):
            key = str(index)
            receipts[key], proposals[key] = _ingest_cycle(
                leader,
                tmp_path,
                fences[key].as_dict(),
                sequence=1,
                previous=None,
                stable_contributor_key=key,
                update_ordinal=index + 1,
            )
            _commit_next(leader, tmp_path, version=index + 1)

        records = authority.read.audit_history_records(cutoff_version=1)
        first_payload = build_audit_batch(
            batch_id="open-history-through-v1",
            record_kind="authority_history",
            cutoff_version=1,
            records=records,
        )
        first_path, first_sha = publish_audit_batch(RunPaths(tmp_path), first_payload)
        leader.archive_audit_batch(
            command_id="archive-open-v1",
            batch_id="open-history-through-v1",
            cutoff_version=1,
            relative_path=RunPaths(tmp_path).relative(first_path),
            sha256=first_sha,
        )

        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM cycle_receipts WHERE receipt_id=?",
                    (receipts["0"].receipt_id,),
                ).fetchone()[0]
                == 1
            )

        leader.begin_terminal_close(command_id="close", reason="target reached")
        for index in range(2):
            key = str(index)
            assert (
                leader.acknowledge_terminal_contributor(
                    command_id=f"ack-{index}",
                    fence=fences[key],
                    final_cycle_seq=1,
                    final_update_id=proposals[key].update_id,
                )
                == "acked"
            )
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"
        terminal = authority.read.terminal_record()
        rollup = authority.read.token_ledger_summary()
        assert terminal is not None
        assert terminal["direct_weight_tokens_applied"] == rollup.direct_applied == 12

        terminal_records = authority.read.audit_history_records(cutoff_version=1)
        assert any(
            record["table"] == "cycle_receipts"
            and record["primary_key"] == receipts["0"].receipt_id
            for record in terminal_records
        )


def test_audit_archive_never_prunes_latest_version_or_blocks_next_commit(tmp_path: Path) -> None:
    """Audit pruning retains the latest version and permits the next merge commit."""

    clock = Clock()
    with _open_authority(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _current_fence(leader)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        first, _ = _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        _commit_next(leader, tmp_path, version=1)
        records = authority.read.audit_history_records(cutoff_version=1)
        payload = build_audit_batch(
            batch_id="archive-with-latest-cutoff",
            record_kind="authority_history",
            cutoff_version=1,
            records=records,
        )
        path, digest = publish_audit_batch(RunPaths(tmp_path), payload)
        leader.archive_audit_batch(
            command_id="archive-with-latest-cutoff",
            batch_id="archive-with-latest-cutoff",
            cutoff_version=1,
            relative_path=RunPaths(tmp_path).relative(path),
            sha256=digest,
        )

        assert authority.read.latest_committed_version().version == 1
        _ingest_cycle(leader, tmp_path, fence, sequence=2, previous=first)
        _commit_next(leader, tmp_path, version=2)
        assert authority.read.latest_committed_version().version == 2


def test_active_leader_compacts_audit_batches_before_exact_source_gc(tmp_path: Path) -> None:
    """Audit compaction publishes a partition before claiming exact source batches."""

    clock = Clock()
    paths = RunPaths(tmp_path)
    with _open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        fence = _current_fence(leader)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        _commit_next(leader, tmp_path, version=1)

        first_payload = build_audit_batch(
            batch_id="history-1",
            record_kind="authority_history",
            cutoff_version=1,
            records=authority.read.audit_history_records(cutoff_version=1),
        )
        first_path, first_sha = publish_audit_batch(paths, first_payload)
        leader.archive_audit_batch(
            command_id="archive-1",
            batch_id="history-1",
            cutoff_version=1,
            relative_path=paths.relative(first_path),
            sha256=first_sha,
        )
        second_payload = build_audit_batch(
            batch_id="history-2",
            record_kind="authority_history",
            cutoff_version=1,
            records=authority.read.audit_history_records(cutoff_version=1),
        )
        second_path, second_sha = publish_audit_batch(paths, second_payload)
        leader.archive_audit_batch(
            command_id="archive-2",
            batch_id="history-2",
            cutoff_version=1,
            relative_path=paths.relative(second_path),
            sha256=second_sha,
        )
        partition = build_audit_partition(
            partition_id="partition-1",
            record_kind="authority_history",
            batches=(read_json(first_path), read_json(second_path)),
        )
        partition_path, partition_sha, manifest_path, manifest_sha = publish_audit_partition(
            paths, partition
        )
        compacted = leader.compact_audit_batches(
            command_id="compact-1",
            partition_id="partition-1",
            record_kind="authority_history",
            batch_ids=("history-2", "history-1"),
            partition_relative_path=paths.relative(partition_path),
            partition_sha256=partition_sha,
            manifest_relative_path=paths.relative(manifest_path),
            manifest_sha256=manifest_sha,
        )

        assert compacted["source_batch_count"] == 2
        assert authority.read.audit_archive_summary() == {
            "hot_batches": 0,
            "partitions": 1,
            "folded_batches": 2,
            "folded_batch_index_rows": 2,
            "pending_gc": 2,
            "claimed_gc": 0,
        }
        claims = leader.claim_audit_gc(command_id="claim-audit-gc")
        assert {item["relative_path"] for item in claims} == {
            paths.relative(first_path),
            paths.relative(second_path),
        }
        authority.release_leader(token)
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="successor", hostname="host", pid=2)
        )
        assert leader.claim_audit_gc(command_id="reclaim-audit-gc") == claims
        for item in claims:
            delete_claimed_audit_batch_object(
                paths,
                relative_path=item["relative_path"],
                expected_sha256=item["sha256"],
            )
        collision = paths.shared_root / claims[0]["relative_path"]
        collision.symlink_to(tmp_path / "missing-audit-object")
        with pytest.raises(RuntimeError, match="still exists"):
            leader.complete_audit_gc(
                command_id="complete-audit-gc",
                relative_paths=tuple(item["relative_path"] for item in claims),
            )
        collision.unlink()
        assert leader.complete_audit_gc(
            command_id="complete-audit-gc",
            relative_paths=tuple(item["relative_path"] for item in claims),
        ) == tuple(sorted(item["relative_path"] for item in claims))
        assert authority.read.audit_archive_summary() == {
            "hot_batches": 0,
            "partitions": 1,
            "folded_batches": 2,
            "folded_batch_index_rows": 0,
            "pending_gc": 0,
            "claimed_gc": 0,
        }
        replay = leader.archive_audit_batch(
            command_id="archive-1-after-compaction",
            batch_id="history-1",
            cutoff_version=1,
            relative_path=paths.relative(first_path),
            sha256=first_sha,
        )
        assert replay["state"] == "compacted"
        assert replay["partition_id"] == "partition-1"


class _MaintenanceTelemetry:
    """Record maintenance service events for ownership assertions."""

    def __init__(self) -> None:
        """Start with no recorded maintenance events."""

        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        """Append one named maintenance event and its structured fields."""

        self.events.append((name, fields))


def test_fenced_maintenance_archives_history_and_successor_reclaims_artifact_gc(
    tmp_path: Path,
) -> None:
    """Fenced maintenance archives history before a successor reclaims artifact GC."""

    clock = Clock()
    paths = RunPaths(tmp_path)
    telemetry = _MaintenanceTelemetry()
    config = MaintenanceSection(archive_batch_rows=1, recent_batch_dedup_count=2)
    with _open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        fence = _current_fence(leader)
        version_zero = publish_checkpoint_pair(tmp_path, version=0)
        leader.initialize_genesis(
            command_id="v0",
            publication_id="publication-v0",
            **version_zero,
        )
        _ingest_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        version_one = publish_checkpoint_pair(tmp_path, version=1)
        attempt = leader.try_select_batch(command_id="select-1", quorum_min=1, quorum_max=1)
        assert attempt.batch is not None
        leader.prepare_publication(
            command_id="prepare-1",
            publication_id="publication-1",
            target_version=1,
            selection_batch_id=attempt.batch.batch_id,
            **version_one,
        )
        leader.commit_merge(command_id="commit-1", publication_id="publication-1")
        service = MaintenanceService(
            authority=authority,
            leader=leader,
            paths=paths,
            config=config,
            telemetry=telemetry,
        )

        archived = service.tick(force=True)

        assert archived["archived_batch"]["cutoff_version"] == 0
        old_paths = {
            str(version_zero["weight_relative_path"]),
            str(version_zero["optim_relative_path"]),
        }
        assert all((tmp_path / relative_path).is_file() for relative_path in old_paths)
        assert all(
            (tmp_path / str(version_one[field])).is_file()
            for field in ("weight_relative_path", "optim_relative_path")
        )
        clock.now += 60.0
        authority.renew_leader(token)
        clock.now += config.publication_orphan_grace_seconds - 59.0
        claimed = leader.claim_orphan_gc(command_id="old-leader-claim")
        assert {item["relative_path"] for item in claimed} == old_paths
        authority.release_leader(token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="successor", hostname="host", pid=2)
        )
        successor_service = MaintenanceService(
            authority=authority,
            leader=successor,
            paths=paths,
            config=config,
            telemetry=telemetry,
        )

        completed = successor_service.tick()

        assert set(completed["artifact_gc"]) == old_paths
        assert all(not (tmp_path / relative_path).exists() for relative_path in old_paths)
        assert authority.read.integrity_check() == ("ok",)
        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) FROM gc_candidates").fetchone()[0] == 0
        finally:
            connection.close()


def test_artifact_gc_refuses_symlinked_or_identity_changed_objects(tmp_path: Path) -> None:
    """Artifact GC refuses symlinks and objects that changed after their claim."""

    paths = RunPaths(tmp_path)
    target = tmp_path / "weights" / "epochs" / "e1" / "payload.safetensors"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"immutable")
    target.chmod(0o444)
    digest = hashlib.sha256(b"immutable").hexdigest()
    target.unlink()
    target.symlink_to(tmp_path / "outside")

    with pytest.raises(RuntimeError, match="immutable identity changed"):
        delete_claimed_artifact_object(
            paths,
            relative_path="weights/epochs/e1/payload.safetensors",
            expected_size=len(b"immutable"),
            expected_sha256=digest,
        )
