from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import (
    DynamicMembershipScope,
    StaticContributorFence,
    StaticMembershipScope,
)
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.protocol.scheduler import (
    SchedulerOperatorAction,
    SchedulerOperatorRequest,
    scheduler_state_sha256,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.audit_archive import (
    build_audit_batch,
    build_audit_partition,
    delete_claimed_audit_batch_object,
    publish_audit_batch,
    publish_audit_partition,
)
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    MembershipFenceError,
    initialize_authority_v4,
)
from fs_diloco.storage.leader_lease import (
    LeaderToken,
    LeaseSafetyTracker,
    StaleLeaderTokenError,
)
from fs_diloco.storage.paths import RunPaths
from tests.support.v4_protocol import (
    proposal_payload,
    publish_checkpoint_pair,
    publish_proposal_payload,
    receipt_payload,
)


PLAN03_REQUIREMENTS = frozenset(
    {
        "AUDIT-02",
        "AUDIT-04",
        "CLOCK-01",
        "DATA-02",
        "DATA-03",
        "DMB-09",
        "DMB-10",
        "SCHED-01",
        "SCHED-02",
        "SCHED-03",
        "SCHED-04",
        "SCHED-05",
        "SEL-01",
        "SEL-02",
        "SEL-03",
        "SEL-04",
        "SEL-05",
        "TERM-01",
        "TERM-02",
        "TERM-03",
        "TOK-05",
        "TOK-08",
    }
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _identity() -> AuthorityIdentity:
    return AuthorityIdentity("run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest())


def _open_static(tmp_path: Path, clock: Clock) -> LeaderAuthority:
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    return LeaderAuthority(database, _identity(), scope, wall_clock=clock)


def _static_fence(leader) -> dict[str, object]:
    binding = leader.bind_or_replace_static_attempt(
        command_id="bind-static",
        learner_id="learner-0",
        logical_launch_id="launch-0",
        attempt_id="attempt-0",
    )
    return {
        "kind": "static",
        "learner_id": binding.learner_id,
        "logical_launch_id": binding.logical_launch_id,
        "attempt_id": binding.attempt_id,
        "binding_generation": binding.binding_generation,
    }


def _ingest_static_cycle(
    leader,
    run_root: Path,
    fence: dict[str, object],
    *,
    sequence: int,
    previous: CycleReceiptV1 | None,
    stable_contributor_key: str = "learner-0",
    update_ordinal: int | None = None,
) -> tuple[CycleReceiptV1, FullUpdateProposalV2]:
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


def _commit_next(leader, run_root: Path, *, version: int) -> None:
    attempt = leader.try_select_batch(command_id=f"select-{version}", quorum_min=1, quorum_max=1)
    assert attempt.batch is not None
    leader.prepare_publication(
        command_id=f"prepare-{version}",
        publication_id=f"publication-{version}",
        target_version=version,
        selection_batch_id=attempt.batch.batch_id,
        **publish_checkpoint_pair(run_root, version=version),
    )
    leader.commit_merge(command_id=f"commit-{version}", publication_id=f"publication-{version}")


def test_authority_token_rollup_balances_receipt_only_and_applied_fates(tmp_path: Path) -> None:
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _static_fence(leader)
        receipt, _ = _ingest_static_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        leader.initialize_v0(
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
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _static_fence(leader)
        _ingest_static_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        leader.initialize_v0(
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
        assert retry.batch.candidates[0].stable_key == "learner-0"
        assert retry.batch.candidates[0].selection_credit == 0


def test_sql_fair_selection_uses_committed_count_before_version_ties(tmp_path: Path) -> None:
    clock = Clock()
    keys = tuple(f"learner-{index}" for index in range(8))
    database = tmp_path / "authority.sqlite3"
    scope = StaticMembershipScope(keys)
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fences: dict[str, dict[str, object]] = {}
        receipts: dict[str, CycleReceiptV1] = {}
        sequences = {key: 1 for key in keys}
        for index, key in enumerate(keys):
            binding = leader.bind_or_replace_static_attempt(
                command_id=f"bind-{index}",
                learner_id=key,
                logical_launch_id=f"launch-{index}",
                attempt_id=f"attempt-{index}",
            )
            fence = StaticContributorFence(
                "static",
                binding.learner_id,
                binding.logical_launch_id,
                binding.attempt_id,
                binding.binding_generation,
            )
            fences[key] = fence.as_dict()
            receipt, _ = _ingest_static_cycle(
                leader,
                tmp_path,
                fences[key],
                sequence=1,
                previous=None,
                stable_contributor_key=key,
                update_ordinal=index * 100 + 1,
            )
            receipts[key] = receipt
        leader.initialize_v0(
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
                index = int(key.rsplit("-", 1)[1])
                sequences[key] += 1
                receipt, _ = _ingest_static_cycle(
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
            ("learner-0", "learner-1", "learner-2"),
            ("learner-3", "learner-4", "learner-5"),
            ("learner-0", "learner-6", "learner-7"),
            ("learner-1", "learner-2", "learner-3"),
            ("learner-4", "learner-5", "learner-6"),
            ("learner-0", "learner-1", "learner-7"),
            ("learner-2", "learner-3", "learner-4"),
            ("learner-5", "learner-6", "learner-7"),
        ]


def test_process_elapsed_safety_is_monotonic_despite_wall_clock_jumps() -> None:
    wall_clock = Clock()
    monotonic_now = [100.0]
    token = LeaderToken(run_id="run-v4", epoch=1, owner_id="owner")
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        monotonic_clock=lambda: monotonic_now[0],
    )

    baseline = tracker.remaining_safe_seconds(token)
    wall_clock.now += 3600.0
    assert tracker.remaining_safe_seconds(token) == baseline
    wall_clock.now -= 7200.0
    assert tracker.remaining_safe_seconds(token) == baseline
    monotonic_now[0] += 88.001
    with pytest.raises(StaleLeaderTokenError, match="monotonic safety boundary"):
        tracker.assert_safe(token)


def test_dynamic_replacement_returns_full_contiguous_resume_state(tmp_path: Path) -> None:
    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(1)
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_dynamic_membership(command_id="init-membership")
        first = leader.admit_dynamic_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
        )
        receipt = CycleReceiptV1.from_dict(
            receipt_payload(
                fence=first.fence.as_dict(),
                stable_contributor_key="0",
                update_id="00000000-0000-4000-8000-000000000001",
            )
        )
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        second = leader.admit_dynamic_incarnation(
            command_id="admit-2",
            instance_id="instance-2",
            placement_id="placement-0",
            stream_id=0,
            admission_token_sha256="2" * 64,
            hostname="host",
            pid=2,
            replace_instance_id="instance-1",
            replacement_reason="authorized replacement",
        )

        assert second.resume_cursor == 8
        assert second.last_receipt_id == receipt.receipt_id
        assert second.last_receipt_sha256 == receipt.immutable_sha256()
        assert second.next_cycle_seq == 2
        assert second.resume.stream_epoch == second.fence.stream_epoch == 2
        assert authority.read.token_ledger_summary().direct_dropped == 6


def test_terminal_close_freezes_fence_blocks_admission_and_accounts_hard_crash(
    tmp_path: Path,
) -> None:
    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(1)
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_dynamic_membership(command_id="init-membership")
        admission = leader.admit_dynamic_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
        )
        leader.initialize_v0(
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
            leader.admit_dynamic_incarnation(
                command_id="late-admit",
                instance_id="instance-2",
                placement_id="placement-1",
                stream_id=0,
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


def test_terminal_hard_crash_gap_is_summed_per_lost_incarnation(tmp_path: Path) -> None:
    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(2)
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_dynamic_membership(command_id="init-membership")
        admissions = tuple(
            leader.admit_dynamic_incarnation(
                command_id=f"admit-{index}",
                instance_id=f"instance-{index}",
                placement_id=f"placement-{index}",
                stream_id=index,
                admission_token_sha256=str(index + 1) * 64,
                hostname="host",
                pid=index + 1,
            )
            for index in range(2)
        )
        leader.initialize_v0(
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


def test_static_restart_recovers_authoritative_cursor_and_receipt_chain(tmp_path: Path) -> None:
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence_payload = _static_fence(leader)
        receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence_payload))
        progress = leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        fence = StaticContributorFence.from_dict(fence_payload)
        leader.mark_static_attempt_terminal(command_id="terminal-1", fence=fence)
        replacement = leader.bind_or_replace_static_attempt(
            command_id="restart",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1-restart",
            expected_generation=1,
        )

        assert replacement.binding_generation == 2
        recovered = authority.read.contributor_progress("learner-0")
        assert recovered == progress
        assert recovered.data_cursor == 8
        assert recovered.last_receipt_id == receipt.receipt_id
        assert recovered.last_receipt_sha256 == receipt.immutable_sha256()


def test_telemetry_deletion_cannot_change_authoritative_token_summary(tmp_path: Path) -> None:
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _static_fence(leader)
        _ingest_static_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        before = authority.read.token_ledger_summary()
        telemetry = tmp_path / "metrics/learner/learner-0/attempt.jsonl"
        telemetry.parent.mkdir(parents=True)
        telemetry.write_text('{"processed_tokens": 999999}\n', encoding="utf-8")
        telemetry.unlink()

        assert authority.read.token_ledger_summary() == before


def test_terminal_final_receipt_ack_preserves_zero_gap_and_balanced_tokens(
    tmp_path: Path,
) -> None:
    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(1)
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_dynamic_membership(command_id="init-membership")
        admission = leader.admit_dynamic_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
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
        leader.initialize_v0(
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


def test_terminal_close_accepts_only_one_contiguous_current_cycle_and_matching_update(
    tmp_path: Path,
) -> None:
    clock = Clock()
    database = tmp_path / "authority.sqlite3"
    scope = DynamicMembershipScope(1)
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    with LeaderAuthority(database, _identity(), scope, wall_clock=clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_dynamic_membership(command_id="init-membership")
        admission = leader.admit_dynamic_incarnation(
            command_id="admit-1",
            instance_id="instance-1",
            placement_id="placement-0",
            stream_id=0,
            admission_token_sha256="1" * 64,
            hostname="host",
            pid=1,
        )
        leader.initialize_v0(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        leader.begin_terminal_close(command_id="close", reason="target reached")
        receipt, proposal = _ingest_static_cycle(
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
        _commit_next(leader, tmp_path, version=1)
        assert leader.finalize_terminal(command_id="finalize", reason="done").value == "finalized"


def test_scheduler_operator_request_is_expected_state_cas_and_audited(tmp_path: Path) -> None:
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        row = leader.record_candidate_launch_request(
            command_id="record-launch",
            request_id="candidate-request-1",
            observation_key="heartbeat-1",
            request_sha256="a" * 64,
        )
        with pytest.raises(ValueError, match="invalid scheduler transition"):
            leader.transition_candidate_launch_request(
                command_id="invalid-direct-admit",
                request_id=row["request_id"],
                expected_state="planned",
                state="admitted",
                evidence_source="invalid",
            )
        submitting = leader.transition_candidate_launch_request(
            command_id="submitting",
            request_id=row["request_id"],
            expected_state="planned",
            state="submitting",
            evidence_source="qsub_started",
        )
        with pytest.raises(ValueError, match="persistent timeout"):
            leader.transition_candidate_launch_request(
                command_id="missing-deadline",
                request_id=submitting["request_id"],
                expected_state="submitting",
                state="submission_unknown",
                evidence_source="qsub_receipt_missing",
            )
        unknown = leader.transition_candidate_launch_request(
            command_id="submission-unknown",
            request_id=submitting["request_id"],
            expected_state="submitting",
            state="submission_unknown",
            evidence_source="qsub_receipt_missing",
            uncertainty_timeout_seconds=30.0,
        )
        uncertain = leader.transition_candidate_launch_request(
            command_id="uncertain",
            request_id=unknown["request_id"],
            expected_state="submission_unknown",
            state="terminal_uncertain",
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


def test_scheduler_uncertainty_deadline_survives_leader_change_and_bounds_resolution(
    tmp_path: Path,
) -> None:
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        row = leader.record_candidate_launch_request(
            command_id="record-launch",
            request_id="candidate-request-1",
            observation_key="heartbeat-1",
            request_sha256="a" * 64,
        )
        submitting = leader.transition_candidate_launch_request(
            command_id="submitting",
            request_id=row["request_id"],
            expected_state="planned",
            state="submitting",
            evidence_source="qsub_started",
        )
        unknown = leader.transition_candidate_launch_request(
            command_id="unknown",
            request_id=submitting["request_id"],
            expected_state="submitting",
            state="submission_unknown",
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
        uncertain = successor.transition_candidate_launch_request(
            command_id="terminal-uncertain",
            request_id=unknown["request_id"],
            expected_state="submission_unknown",
            state="terminal_uncertain",
            evidence_source="live+historical:no_record",
            uncertainty_timeout_seconds=999.0,
        )
        assert uncertain["first_uncertain_at"] == 100.0
        assert uncertain["uncertainty_deadline"] == 130.0
        with pytest.raises(RuntimeError, match="deadline has not elapsed"):
            successor.transition_candidate_launch_request(
                command_id="too-early-review",
                request_id=unknown["request_id"],
                expected_state="terminal_uncertain",
                state="manual_review",
                evidence_source="deadline",
            )
        clock.now = 131.0
        reviewed = successor.transition_candidate_launch_request(
            command_id="manual-review",
            request_id=unknown["request_id"],
            expected_state="terminal_uncertain",
            state="manual_review",
            evidence_source="deadline",
        )
        assert reviewed["state"] == "manual_review"
        assert reviewed["uncertainty_deadline"] == 130.0


def test_immutable_audit_batch_precedes_exact_history_prune_and_preserves_rollup(
    tmp_path: Path,
) -> None:
    clock = Clock()
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _static_fence(leader)
        leader.initialize_v0(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        first, _ = _ingest_static_cycle(leader, tmp_path, fence, sequence=1, previous=None)
        _commit_next(leader, tmp_path, version=1)
        _ingest_static_cycle(leader, tmp_path, fence, sequence=2, previous=first)
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
        assert authority.read.contributor_progress("learner-0").last_cycle_seq == 2
        assert authority.read.token_ledger_summary() == before
        assert path.stat().st_mode & 0o222 == 0


def test_active_leader_compacts_audit_batches_before_exact_source_gc(tmp_path: Path) -> None:
    clock = Clock()
    paths = RunPaths(tmp_path)
    with _open_static(tmp_path, clock) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        fence = _static_fence(leader)
        leader.initialize_v0(
            command_id="v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        _ingest_static_cycle(leader, tmp_path, fence, sequence=1, previous=None)
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
