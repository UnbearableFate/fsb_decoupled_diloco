"""Verify bootstrap and scheduler-bound launch authorization invariants."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import MembershipScope
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    MembershipFenceError,
    initialize_authority,
)


def _authority(tmp_path: Path) -> LeaderAuthority:
    """Create one initialized single-stream authority for launch tests."""

    identity = AuthorityIdentity("run-current", "source", hashlib.sha256(b"config").hexdigest())
    scope = MembershipScope(1)
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=lambda: 100.0)
    return LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0)


def test_bootstrap_slot_is_one_use_and_replacement_requires_exact_qsub_job(
    tmp_path: Path,
) -> None:
    """Bootstrap slots are one-use and replacement admission binds the qsub receipt."""

    with _authority(tmp_path) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="initialize-membership")
        leader.admit_incarnation(
            command_id="bootstrap",
            instance_id="instance-1",
            placement_id="placement-1",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="a" * 64,
            hostname="host",
            pid=1,
            pbs_job_id="100.opbs",
        )
        with pytest.raises(MembershipFenceError, match="already consumed"):
            leader.admit_incarnation(
                command_id="duplicate-bootstrap",
                instance_id="instance-unsolicited",
                placement_id="placement-2",
                stream_id=0,
                bootstrap_slot=0,
                admission_token_sha256="b" * 64,
                hostname="host",
                pid=2,
                pbs_job_id="101.opbs",
            )

        leader.record_capacity_observation(
            command_id="observe",
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
        launch = leader.plan_launch_request(
            command_id="plan",
            request_id="launch-replacement",
            observation_key="capacity-replacement",
            stream_id=0,
            replace_instance_id="instance-1",
            reason="scheduler_terminal",
            expires_at=200.0,
            max_pending_requests=1,
            max_total_requests=1,
            expected_scheduler_job_id="100.opbs",
        )
        submitting = leader.transition_launch_request(
            command_id="submitting",
            request_id=launch["request_id"],
            expected_state="planned",
            state="submitting",
            pbs_job_id=None,
            scheduler_state="qsub_started",
            evidence_source="qsub_started",
        )
        with pytest.raises(RuntimeError, match="evidence is pending"):
            leader.admit_incarnation(
                command_id="admission-before-qsub-evidence",
                instance_id="instance-2",
                placement_id="placement-2",
                stream_id=0,
                launch_request_id="launch-replacement",
                replace_instance_id="instance-1",
                replacement_reason="authorized replacement",
                admission_token_sha256="c" * 64,
                hostname="host",
                pid=3,
                pbs_job_id="200.opbs",
            )
        leader.transition_launch_request(
            command_id="submitted",
            request_id=launch["request_id"],
            expected_state=submitting["state"],
            state="submitted",
            pbs_job_id="200",
            scheduler_state="queued",
            evidence_source="qsub_receipt",
        )
        with pytest.raises(MembershipFenceError, match="scheduler job"):
            leader.admit_incarnation(
                command_id="wrong-job",
                instance_id="instance-2",
                placement_id="placement-2",
                stream_id=0,
                launch_request_id="launch-replacement",
                replace_instance_id="instance-1",
                replacement_reason="authorized replacement",
                admission_token_sha256="c" * 64,
                hostname="host",
                pid=3,
                pbs_job_id="999.opbs",
            )

        admitted = leader.admit_incarnation(
            command_id="replacement",
            instance_id="instance-2",
            placement_id="placement-2",
            stream_id=0,
            launch_request_id="launch-replacement",
            replace_instance_id="instance-1",
            replacement_reason="authorized replacement",
            admission_token_sha256="c" * 64,
            hostname="host",
            pid=3,
            pbs_job_id="200.opbs",
        )

        assert admitted.fence.instance_id == "instance-2"
        launch_row = authority.read.launch_requests()[-1]
        assert launch_row["state"] == "admitted"
        assert launch_row["admitted_instance_id"] == "instance-2"
        assert launch_row["reservation_released_at"] == 100.0

        with pytest.raises(MembershipFenceError, match="replayed with different admission"):
            leader.admit_incarnation(
                command_id="replacement-identity-mismatch",
                instance_id="instance-2",
                placement_id="placement-2",
                stream_id=0,
                launch_request_id="launch-replacement",
                replace_instance_id="instance-1",
                replacement_reason="authorized replacement",
                admission_token_sha256="c" * 64,
                hostname="different-host",
                pid=3,
                pbs_job_id="200.opbs",
            )


def test_admission_requires_one_explicit_authorization_source(tmp_path: Path) -> None:
    """The final writer rejects implicit or ambiguous admission authorization."""

    with _authority(tmp_path) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="initialize-membership")
        common = {
            "instance_id": "instance-1",
            "placement_id": "placement-1",
            "stream_id": 0,
            "admission_token_sha256": "a" * 64,
            "hostname": "host",
            "pid": 1,
        }

        with pytest.raises(ValueError, match="exactly one bootstrap slot or launch request ID"):
            leader.admit_incarnation(command_id="implicit", **common)
        with pytest.raises(ValueError, match="exactly one bootstrap slot or launch request ID"):
            leader.admit_incarnation(
                command_id="ambiguous",
                bootstrap_slot=0,
                launch_request_id="launch-1",
                **common,
            )

        assert authority.read.instances() == ()


def test_stream_cannot_hold_two_unreleased_launch_reservations(tmp_path: Path) -> None:
    """A stream can reserve capacity for only one outstanding authorized launch."""

    with _authority(tmp_path) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="initialize-membership")
        for sequence in (1, 2):
            leader.record_capacity_observation(
                command_id=f"observe-{sequence}",
                observation_key=f"capacity-{sequence}",
                global_version=0,
                eligible_contributors=0,
                selected_contributors=0,
                productive_instances=0,
                reserved_launch_capacity=sequence - 1,
                desired_contributors=1,
                action="low",
                retention_count=4,
            )
        leader.plan_launch_request(
            command_id="plan-1",
            request_id="launch-1",
            observation_key="capacity-1",
            stream_id=0,
            replace_instance_id=None,
            reason="low capacity",
            expires_at=200.0,
            max_pending_requests=2,
            max_total_requests=2,
        )

        with pytest.raises(MembershipFenceError, match="already has a launch reservation"):
            leader.plan_launch_request(
                command_id="plan-2",
                request_id="launch-2",
                observation_key="capacity-2",
                stream_id=0,
                replace_instance_id=None,
                reason="same stream is still reserved",
                expires_at=200.0,
                max_pending_requests=2,
                max_total_requests=2,
            )


def test_launch_capacity_observation_survives_hot_history_retention(tmp_path: Path) -> None:
    """A launch keeps its causal capacity observation after later windows are pruned."""

    with _authority(tmp_path) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        )
        leader.initialize_membership(command_id="initialize-membership")
        leader.record_capacity_observation(
            command_id="observe-launch",
            observation_key="capacity-launch",
            global_version=0,
            eligible_contributors=0,
            selected_contributors=0,
            productive_instances=0,
            reserved_launch_capacity=0,
            desired_contributors=1,
            action="low",
            retention_count=1,
        )
        leader.plan_launch_request(
            command_id="plan-launch",
            request_id="launch-1",
            observation_key="capacity-launch",
            stream_id=0,
            replace_instance_id=None,
            reason="low capacity",
            expires_at=200.0,
            max_pending_requests=1,
            max_total_requests=1,
        )
        for sequence in range(2, 5):
            leader.record_capacity_observation(
                command_id=f"observe-{sequence}",
                observation_key=f"capacity-{sequence}",
                global_version=0,
                eligible_contributors=0,
                selected_contributors=0,
                productive_instances=0,
                reserved_launch_capacity=1,
                desired_contributors=1,
                action="sufficient",
                retention_count=1,
            )

        assert {row["observation_key"] for row in authority.read.capacity_observations()} == {
            "capacity-launch",
            "capacity-4",
        }
