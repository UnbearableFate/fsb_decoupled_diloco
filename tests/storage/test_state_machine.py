"""Exercise bounded authority state machines across generated event sequences."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from fs_diloco.protocol.contributor import MembershipScope
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority
from tests.storage.test_proposal_adjudication import (
    build_cycle,
    open_authority,
)


@pytest.mark.state_machine
@given(st.integers(min_value=1, max_value=12))
def test_proposal_state_machine_keeps_one_pending_and_monotonic_frontier(
    cycles: int,
) -> None:
    """Accepted cycles retain one pending update and a monotonic terminal frontier."""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        authority, leader, fence = open_authority(root)
        try:
            previous = None
            for sequence in range(1, cycles + 1):
                previous, proposal = build_cycle(
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
def test_membership_state_machine_has_one_current_incarnation(
    replacements: int,
) -> None:
    """Every authorized replacement leaves exactly one current stream incarnation."""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        now = [100.0]
        identity = AuthorityIdentity(
            "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
        )
        scope = MembershipScope(1)
        database = root / "authority.sqlite3"
        initialize_authority(database, identity, scope, wall_clock=lambda: now[0])
        with LeaderAuthority(database, identity, scope, wall_clock=lambda: now[0]) as authority:
            token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
            leader = authority.open_leader(token)
            leader.initialize_membership(command_id="initialize-membership")
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
                    planned = leader.plan_launch_request(
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
                    submitting = leader.transition_launch_request(
                        command_id=f"submit-replacement-{generation}",
                        request_id=launch_request_id,
                        expected_state=planned["state"],
                        state="submitting",
                        pbs_job_id=None,
                        scheduler_state="qsub_started",
                        evidence_source="qsub_started",
                    )
                    leader.transition_launch_request(
                        command_id=f"submitted-replacement-{generation}",
                        request_id=launch_request_id,
                        expected_state=submitting["state"],
                        state="submitted",
                        pbs_job_id=f"{generation}.opbs",
                        scheduler_state="queued",
                        evidence_source="qsub_receipt",
                    )
                admission = leader.admit_incarnation(
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
                    bootstrap_slot=0 if current_instance is None else None,
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
