from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from fs_diloco.protocol.contributor import DynamicMembershipScope, StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    CommandConflictError,
    LeaderAuthority,
    initialize_authority_v4,
)
from tests.support.v4_protocol import (
    proposal_payload,
    publish_checkpoint_pair,
    publish_proposal_payload,
    receipt_payload,
)


PLAN03_REQUIREMENTS = frozenset(
    {
        "AUTH-11",
        "P6-ACCEPTANCE",
        "P6-DYNAMIC-9N",
        "P6-PERF-CLASSIC",
        "P6-PERF-DYNAMIC",
        "P6-QUALITY",
        "P6-STATIC-9N",
    }
)

pytestmark = [
    pytest.mark.state_machine,
    pytest.mark.p6_formal,
    pytest.mark.skipif(
        os.environ.get("FS_DILOCO_RUN_P6_FORMAL_GATES") != "1",
        reason="formal P6 generated gate runs in its dedicated compute job",
    ),
]

ACTIONS = (
    "ingest",
    "replay",
    "conflict",
    "select",
    "retire",
    "dynamic_replace",
    "static_attempt_bind_replace",
    "commit",
    "crash",
    "restart",
    "drain",
    "fs_fault",
    "scheduler_ambiguity",
)


@dataclass
class _ProtocolModel:
    leader_epoch: int = 1
    leader_alive: bool = True
    version: int = 0
    static_generation: int = 1
    dynamic_generation: int = 1
    next_update: int = 1
    pending: dict[str, int] = field(default_factory=dict)
    selected: dict[str, int] = field(default_factory=dict)
    applied: set[int] = field(default_factory=set)
    draining: bool = False
    scheduler_uncertain: bool = False

    def apply(self, action: str) -> None:
        if action == "crash":
            self.leader_alive = False
        elif action == "restart":
            if not self.leader_alive:
                self.leader_epoch += 1
                self.leader_alive = True
        elif action == "drain":
            self.draining = True
            self.pending.clear()
            self.selected.clear()
        elif action == "scheduler_ambiguity":
            self.scheduler_uncertain = not self.scheduler_uncertain
        elif action == "fs_fault" or not self.leader_alive:
            pass
        elif action == "ingest" and not self.draining:
            self.pending["static"] = self.next_update
            self.next_update += 1
        elif action in {"replay", "conflict"}:
            pass
        elif action == "select" and self.pending and not self.draining:
            contributor, update = next(iter(self.pending.items()))
            self.selected[contributor] = update
            del self.pending[contributor]
        elif action == "commit" and self.selected and not self.draining:
            for update in self.selected.values():
                assert update not in self.applied
                self.applied.add(update)
            self.selected.clear()
            self.version += 1
        elif action in {"retire", "dynamic_replace"}:
            self.dynamic_generation += 1
            self.pending.pop("dynamic", None)
            self.selected.pop("dynamic", None)
        elif action == "static_attempt_bind_replace":
            self.static_generation += 1
            self.pending.pop("static", None)
            self.selected.pop("static", None)
        self.assert_invariants()

    def assert_invariants(self) -> None:
        assert set(self.pending).isdisjoint(self.selected)
        assert set(self.applied).isdisjoint(self.pending.values())
        assert set(self.applied).isdisjoint(self.selected.values())
        assert len(self.pending) <= 1
        assert len(self.selected) <= 1
        assert self.static_generation >= 1 and self.dynamic_generation >= 1
        assert self.leader_epoch >= 1 and self.version >= 0
        if self.draining:
            assert not self.pending and not self.selected


def _action_sequences(maximum: int) -> st.SearchStrategy[list[str]]:
    return st.builds(
        lambda required, tail: [*required, *tail],
        st.permutations(ACTIONS),
        st.lists(st.sampled_from(ACTIONS), max_size=maximum - len(ACTIONS)),
    )


@settings(
    max_examples=1000,
    deadline=None,
    derandomize=True,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(_action_sequences(300))
def test_p6_gate_pure_protocol_model(actions: list[str]) -> None:
    model = _ProtocolModel()
    for action in actions:
        model.apply(action)


class _SQLiteAdapter:
    def __init__(self, root: Path) -> None:
        self.clock = [100.0]
        self.sequence = 0
        self.static_generation = 1
        self.static_attempt = 0
        self.dynamic_generation = 0
        self.dynamic_job = 0
        self.dynamic_instance = "instance-0"
        self.draining = False
        self.leader_alive = True
        self.last_receipt: CycleReceiptV1 | None = None
        self.last_proposal: FullUpdateProposalV2 | None = None
        self.static_identity = AuthorityIdentity(
            "p6-static", "p6-source", hashlib.sha256(b"p6-static").hexdigest()
        )
        self.dynamic_identity = AuthorityIdentity(
            "p6-dynamic", "p6-source", hashlib.sha256(b"p6-dynamic").hexdigest()
        )
        self.static_scope = StaticMembershipScope(("learner-0",))
        self.dynamic_scope = DynamicMembershipScope(1)
        self.static_database = root / "static/authority.sqlite3"
        self.dynamic_database = root / "dynamic/authority.sqlite3"
        initialize_authority_v4(
            self.static_database,
            self.static_identity,
            self.static_scope,
            wall_clock=self._now,
        )
        initialize_authority_v4(
            self.dynamic_database,
            self.dynamic_identity,
            self.dynamic_scope,
            wall_clock=self._now,
        )
        self.static = self._open_static()
        self.dynamic = self._open_dynamic()
        self._acquire_sessions()
        binding = self.static_leader.bind_or_replace_static_attempt(
            command_id="static-bind-0",
            learner_id="learner-0",
            logical_launch_id="logical-0",
            attempt_id="attempt-0",
        )
        self.static_generation = binding.binding_generation
        self.static_fence = {
            "kind": "static",
            "learner_id": binding.learner_id,
            "logical_launch_id": binding.logical_launch_id,
            "attempt_id": binding.attempt_id,
            "binding_generation": binding.binding_generation,
        }
        self.static_leader.initialize_v0(
            command_id="static-v0",
            publication_id="static-publication-v0",
            **publish_checkpoint_pair(self.static_database.parent, version=0),
        )
        self.dynamic_leader.initialize_dynamic_membership(command_id="dynamic-initialize")
        admission = self.dynamic_leader.admit_dynamic_incarnation(
            command_id="dynamic-admit-0",
            instance_id=self.dynamic_instance,
            placement_id="placement-0",
            stream_id=0,
            admission_token_sha256=hashlib.sha256(b"token-0").hexdigest(),
            hostname="host",
            pid=1,
            pbs_job_id="0.opbs",
        )
        self.dynamic_generation = admission.fence.stream_epoch

    def _now(self) -> float:
        return self.clock[0]

    def _open_static(self) -> LeaderAuthority:
        return LeaderAuthority(
            self.static_database,
            self.static_identity,
            self.static_scope,
            wall_clock=self._now,
        )

    def _open_dynamic(self) -> LeaderAuthority:
        return LeaderAuthority(
            self.dynamic_database,
            self.dynamic_identity,
            self.dynamic_scope,
            wall_clock=self._now,
        )

    def _acquire_sessions(self) -> None:
        self.static_token = self.static.acquire_leader(
            owner_id=f"static-owner-{self.sequence}", hostname="host", pid=1
        )
        self.dynamic_token = self.dynamic.acquire_leader(
            owner_id=f"dynamic-owner-{self.sequence}", hostname="host", pid=1
        )
        self.static_leader = self.static.open_leader(self.static_token)
        self.dynamic_leader = self.dynamic.open_leader(self.dynamic_token)

    def close(self) -> None:
        self.static.close()
        self.dynamic.close()

    def _static_active_count(self) -> int:
        connection = sqlite3.connect(self.static_database)
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM updates WHERE status IN ('pending','selected')"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def _ingest_static(self) -> None:
        if self._static_active_count() != 0:
            return
        sequence = 1 if self.last_receipt is None else self.last_receipt.cycle_seq + 1
        update_id = f"00000000-0000-4000-8000-{sequence:012d}"
        receipt = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=sequence,
                previous_receipt_id=(
                    None if self.last_receipt is None else self.last_receipt.receipt_id
                ),
                previous_receipt_sha256=(
                    None if self.last_receipt is None else self.last_receipt.immutable_sha256()
                ),
                cursor_start=8 * (sequence - 1),
                cursor_end=8 * sequence,
                fence=self.static_fence,
                update_id=update_id,
                run_id="p6-static",
            )
        )
        self.static_leader.ingest_cycle_receipt(
            command_id=f"static-receipt-{sequence}", receipt=receipt
        )
        latest = self.static.read.latest_committed_version()
        assert latest is not None
        proposal_data = proposal_payload(
            cycle_seq=sequence,
            receipt_sha256=receipt.immutable_sha256(),
            fence=self.static_fence,
            update_id=update_id,
            run_id="p6-static",
        )
        proposal_data["base_global_version"] = latest.version
        proposal_data["retained_tokens_since_base"] = 6
        proposal = FullUpdateProposalV2.from_dict(proposal_data)
        publish_proposal_payload(self.static_database.parent, proposal)
        self.static_leader.ingest_proposal(
            command_id=f"static-proposal-{sequence}", proposal=proposal
        )
        self.last_receipt = receipt
        self.last_proposal = proposal

    def _commit_static(self) -> None:
        connection = sqlite3.connect(self.static_database)
        try:
            row = connection.execute(
                "SELECT batch_id FROM selection_batches WHERE state='selected' "
                "ORDER BY target_version LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            selection = self.static_leader.try_select_batch(
                command_id=f"select-for-commit-{self.sequence}",
                quorum_min=1,
                quorum_max=1,
            )
            if selection.batch is None:
                return
            batch_id = selection.batch.batch_id
        else:
            batch_id = str(row[0])
        latest = self.static.read.latest_committed_version()
        assert latest is not None
        version = latest.version + 1
        publication_id = f"static-publication-v{version}"
        self.static_leader.prepare_publication(
            command_id=f"prepare-{publication_id}",
            publication_id=publication_id,
            target_version=version,
            selection_batch_id=batch_id,
            **publish_checkpoint_pair(self.static_database.parent, version=version),
        )
        committed = self.static_leader.commit_merge(
            command_id=f"commit-{publication_id}", publication_id=publication_id
        )
        assert committed.version == version

    def step(self, action: str) -> None:
        self.sequence += 1
        self.clock[0] += 0.01
        if action == "crash":
            if self.leader_alive:
                self.static.fail_leader(self.static_token)
                self.dynamic.fail_leader(self.dynamic_token)
                self.leader_alive = False
        elif action == "restart" and not self.leader_alive:
            self.static.close()
            self.dynamic.close()
            self.static = self._open_static()
            self.dynamic = self._open_dynamic()
            self._acquire_sessions()
            self.leader_alive = True
        elif not self.leader_alive:
            pass
        elif action == "static_attempt_bind_replace" and not self.draining:
            self.static_attempt += 1
            binding = self.static_leader.bind_or_replace_static_attempt(
                command_id=f"static-replace-{self.sequence}",
                learner_id="learner-0",
                logical_launch_id="logical-0",
                attempt_id=f"attempt-{self.static_attempt}",
                expected_generation=self.static_generation,
                replacement_reason="generated-state-machine",
            )
            assert binding.binding_generation > self.static_generation
            self.static_generation = binding.binding_generation
            self.static_fence = {
                "kind": "static",
                "learner_id": binding.learner_id,
                "logical_launch_id": binding.logical_launch_id,
                "attempt_id": binding.attempt_id,
                "binding_generation": binding.binding_generation,
            }
        elif action in {"retire", "dynamic_replace"} and not self.draining:
            generation = self.dynamic_generation + 1
            next_job = self.dynamic_job + 1
            observation = f"capacity-{self.sequence}"
            request = f"replacement-{self.sequence}"
            self.dynamic_leader.record_capacity_observation(
                command_id=f"observe-{self.sequence}",
                observation_key=observation,
                global_version=0,
                eligible_contributors=0,
                selected_contributors=0,
                productive_instances=0,
                reserved_launch_capacity=0,
                desired_contributors=1,
                action="replace",
                retention_count=16,
            )
            planned = self.dynamic_leader.plan_dynamic_launch_request(
                command_id=f"plan-{self.sequence}",
                request_id=request,
                observation_key=observation,
                stream_id=0,
                replace_instance_id=self.dynamic_instance,
                reason="generated-state-machine",
                expires_at=1000.0,
                max_pending_requests=256,
                max_total_requests=256,
                expected_scheduler_job_id=f"{self.dynamic_job}.opbs",
            )
            submitting = self.dynamic_leader.transition_dynamic_launch_request(
                command_id=f"submitting-{self.sequence}",
                request_id=request,
                expected_state=planned["state"],
                state="submitting",
                pbs_job_id=None,
                scheduler_state="qsub_started",
                evidence_source="generated",
            )
            self.dynamic_leader.transition_dynamic_launch_request(
                command_id=f"submitted-{self.sequence}",
                request_id=request,
                expected_state=submitting["state"],
                state="submitted",
                pbs_job_id=f"{next_job}.opbs",
                scheduler_state="queued",
                evidence_source="generated",
            )
            next_instance = f"instance-{generation}"
            admission = self.dynamic_leader.admit_dynamic_incarnation(
                command_id=f"admit-{self.sequence}",
                instance_id=next_instance,
                placement_id="placement-0",
                stream_id=0,
                admission_token_sha256=hashlib.sha256(next_instance.encode()).hexdigest(),
                hostname="host",
                pid=generation + 1,
                pbs_job_id=f"{next_job}.opbs",
                launch_request_id=request,
                replace_instance_id=self.dynamic_instance,
                replacement_reason="generated-state-machine",
            )
            assert admission.fence.stream_epoch > self.dynamic_generation
            self.dynamic_generation = admission.fence.stream_epoch
            self.dynamic_job = next_job
            self.dynamic_instance = next_instance
        elif action == "ingest" and not self.draining:
            self._ingest_static()
        elif action == "select" and not self.draining:
            self.static_leader.try_select_batch(
                command_id=f"select-{self.sequence}", quorum_min=1, quorum_max=1
            )
        elif action == "commit" and not self.draining:
            self._commit_static()
        elif action == "drain" and not self.draining:
            self.static_leader.begin_terminal_preclose(
                command_id=f"static-preclose-{self.sequence}",
                reason="generated-drain",
                registration_visibility_grace_seconds=1.0,
            )
            self.dynamic_leader.begin_terminal_preclose(
                command_id=f"dynamic-preclose-{self.sequence}",
                reason="generated-drain",
                registration_visibility_grace_seconds=1.0,
            )
            self.draining = True
        elif action in {"fs_fault", "scheduler_ambiguity"} and not self.draining:
            key = f"capacity-action-{self.sequence}"
            self.dynamic_leader.record_capacity_observation(
                command_id=f"capacity-action-{self.sequence}",
                observation_key=key,
                global_version=0,
                eligible_contributors=1,
                selected_contributors=0,
                productive_instances=1,
                reserved_launch_capacity=0,
                desired_contributors=1,
                action=action,
                retention_count=16,
            )
        elif action == "replay" and not self.draining:
            command = f"replay-{self.sequence}"
            arguments = {
                "command_id": command,
                "observation_key": f"replay-observation-{self.sequence}",
                "global_version": 0,
                "eligible_contributors": 1,
                "selected_contributors": 0,
                "productive_instances": 1,
                "reserved_launch_capacity": 0,
                "desired_contributors": 1,
                "action": "replay",
                "retention_count": 16,
            }
            first = self.dynamic_leader.record_capacity_observation(**arguments)
            assert self.dynamic_leader.record_capacity_observation(**arguments) == first
        elif action == "conflict" and not self.draining:
            command = f"conflict-{self.sequence}"
            arguments = {
                "command_id": command,
                "observation_key": f"conflict-observation-{self.sequence}",
                "global_version": 0,
                "eligible_contributors": 1,
                "selected_contributors": 0,
                "productive_instances": 1,
                "reserved_launch_capacity": 0,
                "desired_contributors": 1,
                "action": "first",
                "retention_count": 16,
            }
            self.dynamic_leader.record_capacity_observation(**arguments)
            with pytest.raises(CommandConflictError):
                self.dynamic_leader.record_capacity_observation(
                    **{**arguments, "action": "different"}
                )
        self.assert_invariants()

    def assert_invariants(self) -> None:
        for database in (self.static_database, self.dynamic_database):
            connection = sqlite3.connect(database)
            try:
                active = connection.execute(
                    "SELECT stable_contributor_key, status, COUNT(*) FROM updates "
                    "WHERE status IN ('pending','selected') "
                    "GROUP BY stable_contributor_key, status"
                ).fetchall()
                assert all(int(row[2]) <= 1 for row in active)
                assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                connection.close()
        connection = sqlite3.connect(self.dynamic_database)
        try:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM learner_instances WHERE status='admitted'"
                ).fetchone()[0]
                <= 1
            )
            stream = connection.execute(
                "SELECT current_instance_id FROM streams WHERE stream_id=0"
            ).fetchone()[0]
            assert stream == self.dynamic_instance
        finally:
            connection.close()


@settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(_action_sequences(150))
def test_p6_gate_sqlite_adapter(actions: list[str]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        adapter = _SQLiteAdapter(Path(directory))
        try:
            for action in actions:
                adapter.step(action)
        finally:
            adapter.close()
