from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from fs_diloco.protocol.authority import ReadResult, ReadStatus
from fs_diloco.protocol.contributor import (
    DynamicMembershipScope,
    StaticContributorFence,
    StaticMembershipScope,
)
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority_v4
from tests.storage.test_proposal_adjudication_v4 import (
    build_cycle as _build_static_cycle,
    open_static as _open_static,
)
from tests.support.v4_protocol import receipt_payload


@pytest.mark.state_machine
@given(
    st.lists(
        st.sampled_from(
            [
                ReadStatus.OK,
                ReadStatus.NOT_FOUND,
                ReadStatus.TRANSIENT_IO,
                ReadStatus.MALFORMED,
                ReadStatus.IDENTITY_MISMATCH,
            ]
        ),
        min_size=1,
        max_size=40,
    )
)
def test_visibility_state_machine_is_bounded_and_terminal_is_sticky(
    events: list[ReadStatus],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        now = [100.0]
        identity = AuthorityIdentity(
            "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
        )
        scope = StaticMembershipScope(("learner-0",))
        database = root / "authority.sqlite3"
        initialize_authority_v4(database, identity, scope, wall_clock=lambda: now[0])
        with LeaderAuthority(database, identity, scope, wall_clock=lambda: now[0]) as authority:
            token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
            leader = authority.open_leader(token)
            binding = leader.bind_or_replace_static_attempt(
                command_id="bind",
                learner_id="learner-0",
                logical_launch_id="launch-0",
                attempt_id="attempt-0",
            )
            fence = StaticContributorFence(
                "static",
                binding.learner_id,
                binding.logical_launch_id,
                binding.attempt_id,
                binding.binding_generation,
            )
            receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
            leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
            terminal_observation: int | None = None
            for index, status in enumerate(events):
                now[0] += 1.0
                result = (
                    ReadResult(status, value=object())
                    if status is ReadStatus.OK
                    else ReadResult(
                        status,
                        diagnostic=status.value,
                        fingerprint=("a" * 64 if status is ReadStatus.MALFORMED else None),
                    )
                )
                decision = leader.observe_proposal_visibility(
                    command_id=f"observe-{index}",
                    stable_contributor_key="learner-0",
                    cycle_seq=1,
                    update_id="00000000-0000-4000-8000-000000000001",
                    object_identity="proposal-object",
                    pointer_signature="pointer-signature",
                    pointer_sequence=1,
                    source_relative_path="updates/latest/learner-0.json",
                    result=result,
                    grace_seconds=2.0,
                    operator_deadline_seconds=5.0,
                    max_archived_signatures=4,
                )
                if terminal_observation is None and decision.observation_id is not None:
                    terminal_observation = decision.observation_id
                if terminal_observation is not None:
                    assert decision.observation_id == terminal_observation
                connection = sqlite3.connect(database)
                try:
                    assert (
                        connection.execute("SELECT COUNT(*) FROM proposal_visibility").fetchone()[0]
                        <= 1
                    )
                    assert (
                        connection.execute(
                            "SELECT COUNT(*) FROM proposal_visibility_archive"
                        ).fetchone()[0]
                        <= 4
                    )
                    assert (
                        connection.execute("SELECT COUNT(*) FROM proposal_quarantine").fetchone()[0]
                        <= 1
                    )
                finally:
                    connection.close()


@pytest.mark.state_machine
@given(st.integers(min_value=1, max_value=12))
def test_proposal_state_machine_keeps_one_pending_and_monotonic_frontier(
    cycles: int,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        authority, leader, fence = _open_static(root)
        try:
            previous = None
            for sequence in range(1, cycles + 1):
                previous, proposal = _build_static_cycle(
                    leader,
                    root,
                    fence,
                    cycle_seq=sequence,
                    previous=previous,
                )
                leader.ingest_proposal(command_id=f"accept-{sequence}", proposal=proposal)
                connection = sqlite3.connect(root / "authority.sqlite3")
                try:
                    assert (
                        connection.execute(
                            "SELECT COUNT(*) FROM updates WHERE status='pending'"
                        ).fetchone()[0]
                        == 1
                    )
                    assert (
                        connection.execute(
                            "SELECT last_terminal_cycle_seq FROM proposal_frontiers"
                        ).fetchone()[0]
                        == sequence
                    )
                finally:
                    connection.close()
        finally:
            authority.close()


@pytest.mark.state_machine
@given(st.integers(min_value=0, max_value=8))
def test_dynamic_membership_state_machine_has_one_current_incarnation(
    replacements: int,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        now = [100.0]
        identity = AuthorityIdentity(
            "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
        )
        scope = DynamicMembershipScope(1)
        database = root / "authority.sqlite3"
        initialize_authority_v4(database, identity, scope, wall_clock=lambda: now[0])
        with LeaderAuthority(database, identity, scope, wall_clock=lambda: now[0]) as authority:
            token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
            leader = authority.open_leader(token)
            leader.initialize_dynamic_membership(command_id="initialize-membership")
            current_instance: str | None = None
            previous_stream_epoch = 0
            for generation in range(replacements + 1):
                instance_id = f"instance-{generation}"
                launch_request_id = None
                if current_instance is not None:
                    launch_request_id = f"replacement-launch-{generation}"
                    observation_key = f"capacity-replacement-{generation}"
                    leader.record_capacity_observation(
                        command_id=f"observe-replacement-{generation}",
                        observation_key=observation_key,
                        global_version=0,
                        eligible_contributors=0,
                        selected_contributors=0,
                        productive_instances=0,
                        reserved_launch_capacity=0,
                        desired_contributors=1,
                        action="replace",
                        retention_count=16,
                    )
                    planned = leader.plan_dynamic_launch_request(
                        command_id=f"plan-replacement-{generation}",
                        request_id=launch_request_id,
                        observation_key=observation_key,
                        stream_id=0,
                        replace_instance_id=current_instance,
                        reason="scheduler_terminal",
                        expires_at=1000.0,
                        max_pending_requests=16,
                        max_total_requests=16,
                        expected_scheduler_job_id=f"{generation - 1}.opbs",
                    )
                    submitting = leader.transition_dynamic_launch_request(
                        command_id=f"submit-replacement-{generation}",
                        request_id=launch_request_id,
                        expected_state=planned["state"],
                        state="submitting",
                        pbs_job_id=None,
                        scheduler_state="qsub_started",
                        evidence_source="qsub_started",
                    )
                    leader.transition_dynamic_launch_request(
                        command_id=f"submitted-replacement-{generation}",
                        request_id=launch_request_id,
                        expected_state=submitting["state"],
                        state="submitted",
                        pbs_job_id=f"{generation}.opbs",
                        scheduler_state="queued",
                        evidence_source="qsub_receipt",
                    )
                admission = leader.admit_dynamic_incarnation(
                    command_id=f"admit-{generation}",
                    instance_id=instance_id,
                    placement_id="placement-0",
                    stream_id=0,
                    admission_token_sha256=hashlib.sha256(
                        f"token-{generation}".encode()
                    ).hexdigest(),
                    hostname="host",
                    pid=generation + 1,
                    pbs_job_id=f"{generation}.opbs",
                    launch_request_id=launch_request_id,
                    replace_instance_id=current_instance,
                    replacement_reason=(
                        None if current_instance is None else "state_machine_replacement"
                    ),
                )
                current_instance = instance_id
                assert admission.fence.stream_epoch > previous_stream_epoch
                previous_stream_epoch = admission.fence.stream_epoch
                connection = sqlite3.connect(database)
                try:
                    assert (
                        connection.execute(
                            "SELECT COUNT(*) FROM learner_instances WHERE status='admitted'"
                        ).fetchone()[0]
                        == 1
                    )
                    assert (
                        connection.execute(
                            "SELECT current_instance_id FROM streams WHERE stream_id=0"
                        ).fetchone()[0]
                        == current_instance
                    )
                finally:
                    connection.close()
