from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fs_diloco.core.config import MembershipSection, ScalingSection
from fs_diloco.protocol.scheduler import (
    SchedulerOperatorAction,
    SchedulerOperatorRequest,
    scheduler_state_sha256,
)
from fs_diloco.protocol.contributor import DynamicMembershipScope
from fs_diloco.runtime.pbs_scheduler import PBSJobObservation
from fs_diloco.runtime.services.dynamic_capacity import DynamicCapacityService
from fs_diloco.storage.atomic_io import publish_immutable_bytes
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    initialize_authority_v4,
)
from fs_diloco.storage.paths import RunPaths
from tests.support import FakePBS


PLAN03_REQUIREMENTS = frozenset({"P5-ARCH", "SCHED-01", "SCHED-02", "SCHED-03", "SCHED-04"})


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _identity() -> AuthorityIdentity:
    return AuthorityIdentity("run-v4", "source", hashlib.sha256(b"config").hexdigest())


def _runtime(tmp_path: Path, clock: Clock, *, streams: int = 2):
    scope = DynamicMembershipScope(streams)
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, _identity(), scope, wall_clock=clock)
    authority = LeaderAuthority(database, _identity(), scope, wall_clock=clock)
    leader = authority.open_leader(
        authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
    )
    leader.initialize_dynamic_membership(command_id="initialize-membership")
    scaling = ScalingSection(
        enabled=True,
        desired_contributors=2,
        low_contributor_threshold=1,
        consecutive_low_windows=2,
        productive_window_count=1,
        startup_grace_seconds=2.0,
        productive_upload_grace_factor=2.0,
        productive_upload_grace_min_seconds=1.0,
        productive_upload_grace_max_seconds=4.0,
        cooldown_seconds=1.0,
        max_pending_launch_requests=1,
        max_total_launch_requests=4,
        launch_request_ttl_seconds=20.0,
        capacity_observation_retention_count=8,
        scheduler_reconcile_interval_seconds=1.0,
        scheduler_uncertainty_timeout_seconds=3.0,
        starvation_observation_seconds=1.0,
        learner_pbs_script="scripts/miyabi/run_dynamic_learner.pbs",
        learner_walltime="00:10:00",
        learner_queue="debug-g",
    )
    membership = MembershipSection(
        mode="dynamic",
        stream_pool_size=streams,
        bootstrap_instances=0,
        heartbeat_stale_after_seconds=2.0,
        heartbeat_dead_after_seconds=4.0,
        max_active_instance_records=max(4, streams),
    )
    scheduler = FakePBS()
    service = DynamicCapacityService(
        authority=authority,
        leader=leader,
        scheduler=scheduler,
        scaling=scaling,
        membership=membership,
        shared_root=tmp_path,
        descriptor_sha256="d" * 64,
        wall_clock=clock,
    )
    return authority, leader, scheduler, service


def _observation(job_id: str, classification: str) -> PBSJobObservation:
    return PBSJobObservation(job_id, classification, {}, 0, "")


def test_persistent_low_windows_plan_and_submit_exact_stream(tmp_path: Path) -> None:
    clock = Clock()
    authority, _leader, scheduler, service = _runtime(tmp_path, clock)
    try:
        assert (
            service.tick(global_version=0, eligible_contributors=0, selected_contributors=0) == ()
        )
        clock.now += 1.1
        actions = service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)

        launches = [
            row for row in authority.read.dynamic_launch_requests() if row["role"] != "bootstrap"
        ]
        assert "scale_out_planned" in actions
        assert len(launches) == 1
        assert launches[0]["state"] == "submitted"
        assert launches[0]["stream_id"] == 0
        assert scheduler.submissions == [
            {
                "script": "scripts/miyabi/run_dynamic_learner.pbs",
                "launch_request_id": launches[0]["request_id"],
                "stream_id": 0,
                "replace_instance_id": None,
                "shared_root": tmp_path,
                "descriptor_sha256": "d" * 64,
                "walltime": "00:10:00",
                "queue": "debug-g",
            }
        ]
    finally:
        authority.close()


def test_lost_qsub_result_never_resubmits_and_ends_in_manual_review(tmp_path: Path) -> None:
    class LostResultPBS(FakePBS):
        def submit_learner(self, **kwargs):
            self.submissions.append(dict(kwargs))
            return {"returncode": -1, "stderr": "timeout"}

    clock = Clock()
    authority, leader, _scheduler, service = _runtime(tmp_path, clock)
    scheduler = LostResultPBS()
    service.scheduler = scheduler
    try:
        service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)
        clock.now += 1.1
        service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)
        request = [
            row for row in authority.read.dynamic_launch_requests() if row["role"] != "bootstrap"
        ][0]
        assert request["state"] == "submission_unknown"
        assert len(scheduler.submissions) == 1

        clock.now += 1.1
        service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)
        uncertain = authority.read.dynamic_launch_requests()[-1]
        assert uncertain["state"] == "terminal_uncertain"
        deadline = uncertain["uncertainty_deadline"]
        assert deadline == pytest.approx(104.1)

        clock.now = 104.3
        authority.release_leader(leader.token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="successor", hostname="host", pid=2)
        )
        successor_service = DynamicCapacityService(
            authority=authority,
            leader=successor,
            scheduler=scheduler,
            scaling=service.scaling,
            membership=service.membership,
            shared_root=tmp_path,
            descriptor_sha256="d" * 64,
            wall_clock=clock,
        )
        successor_service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)
        reviewed = authority.read.dynamic_launch_requests()[-1]
        assert reviewed["state"] == "manual_review"
        assert reviewed["uncertainty_deadline"] == deadline
        assert reviewed["reservation_released_at"] is None
        assert len(scheduler.submissions) == 1

        request = SchedulerOperatorRequest(
            format_version=1,
            request_id="operator-mark-failed",
            launch_request_id=str(reviewed["request_id"]),
            action=SchedulerOperatorAction.MARK_FAILED,
            expected_state_sha256=scheduler_state_sha256(reviewed),
            reason="operator verified that qsub did not create a job",
            created_at=clock.now,
            scheduler_job_id=None,
            evidence_source="operator_scheduler_audit",
        )
        target = RunPaths(tmp_path).scheduler_operator_requests / "operator-mark-failed.json"
        publish_immutable_bytes(target, request.canonical_bytes() + b"\n")
        actions = successor_service.tick(
            global_version=0,
            eligible_contributors=0,
            selected_contributors=0,
        )
        resolved = authority.read.dynamic_launch_requests()[-1]
        assert "operator_applied" in actions
        assert resolved["state"] == "failed"
        assert resolved["reservation_released_at"] == clock.now
        assert len(scheduler.submissions) == 1
    finally:
        authority.close()


def test_lost_qsub_result_uses_historical_request_scan_without_resubmission(
    tmp_path: Path,
) -> None:
    class LostButHistoricallyFinishedPBS(FakePBS):
        def submit_learner(self, **kwargs):
            self.submissions.append(dict(kwargs))
            request_id = str(kwargs["launch_request_id"])
            self.bind_historical_request(
                request_id,
                _observation("historical-123", "finished"),
            )
            return {"returncode": -1, "stderr": "qsub connection lost"}

    clock = Clock()
    authority, _leader, _scheduler, service = _runtime(tmp_path, clock)
    scheduler = LostButHistoricallyFinishedPBS()
    service.scheduler = scheduler
    try:
        service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)
        clock.now += 1.1
        actions = service.tick(
            global_version=0,
            eligible_contributors=0,
            selected_contributors=0,
        )

        request = [
            row for row in authority.read.dynamic_launch_requests() if row["role"] != "bootstrap"
        ][0]
        assert "submission_recovered" in actions
        assert request["state"] == "failed"
        assert request["pbs_job_id"] == "historical-123"
        assert request["reservation_released_at"] == clock.now
        assert len(scheduler.submissions) == 1
    finally:
        authority.close()


def test_successor_recovers_submitting_request_without_duplicate_qsub(tmp_path: Path) -> None:
    clock = Clock()
    authority, leader, scheduler, service = _runtime(tmp_path, clock)
    try:
        observation = leader.record_capacity_observation(
            command_id="observe",
            observation_key="capacity-successor",
            global_version=0,
            eligible_contributors=0,
            selected_contributors=0,
            productive_instances=0,
            reserved_launch_capacity=0,
            desired_contributors=2,
            action="low",
            retention_count=8,
        )
        row = leader.plan_dynamic_launch_request(
            command_id="plan",
            request_id="launch-successor",
            observation_key=observation["observation_key"],
            stream_id=0,
            replace_instance_id=None,
            reason="low capacity",
            expires_at=120.0,
            max_pending_requests=1,
            max_total_requests=4,
        )
        leader.transition_dynamic_launch_request(
            command_id="submitting",
            request_id=row["request_id"],
            expected_state="planned",
            state="submitting",
            pbs_job_id=None,
            scheduler_state="qsub_started",
            evidence_source="qsub_started",
        )
        scheduler.bind_request(row["request_id"], _observation("123", "running"))
        authority.release_leader(leader.token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="successor", hostname="host", pid=2)
        )
        service.leader = successor

        service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)

        recovered = authority.read.dynamic_launch_requests()[-1]
        assert recovered["state"] == "submitted"
        assert recovered["pbs_job_id"] == "123"
        assert scheduler.submissions == []
    finally:
        authority.close()


def test_replacement_requires_stale_progress_and_terminal_scheduler_evidence(
    tmp_path: Path,
) -> None:
    clock = Clock()
    authority, leader, scheduler, service = _runtime(tmp_path, clock, streams=1)
    service.scaling.desired_contributors = 1
    service.scaling.low_contributor_threshold = 0
    try:
        leader.admit_dynamic_incarnation(
            command_id="bootstrap",
            instance_id="instance-old",
            placement_id="placement-old",
            stream_id=0,
            admission_token_sha256="a" * 64,
            hostname="host",
            pid=1,
            pbs_job_id="old.opbs",
        )
        clock.now = 105.0
        scheduler.queue_query("old.opbs", _observation("old", "no_record"))
        scheduler.queue_query("old.opbs", _observation("old", "finished"), historical=True)

        actions = service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)

        launch = [
            row for row in authority.read.dynamic_launch_requests() if row["role"] != "bootstrap"
        ]
        assert "replacement_planned" in actions
        assert len(launch) == 1
        assert launch[0]["replace_instance_id"] == "instance-old"
        assert launch[0]["stream_id"] == 0
        old = next(
            row
            for row in authority.read.dynamic_instances()
            if row["instance_id"] == "instance-old"
        )
        assert old["status"] == "expired"
    finally:
        authority.close()


def test_suspended_or_unknown_scheduler_state_never_authorizes_replacement(
    tmp_path: Path,
) -> None:
    clock = Clock()
    authority, leader, scheduler, service = _runtime(tmp_path, clock, streams=1)
    service.scaling.desired_contributors = 1
    service.scaling.low_contributor_threshold = 0
    try:
        leader.admit_dynamic_incarnation(
            command_id="bootstrap",
            instance_id="instance-paused",
            placement_id="placement-paused",
            stream_id=0,
            admission_token_sha256="b" * 64,
            hostname="host",
            pid=2,
            pbs_job_id="paused.opbs",
        )
        clock.now = 105.0
        scheduler.queue_query("paused.opbs", _observation("paused", "suspended"))
        scheduler.queue_query("paused.opbs", _observation("paused", "unknown"), historical=True)

        service.tick(global_version=0, eligible_contributors=0, selected_contributors=0)

        assert [
            row for row in authority.read.dynamic_launch_requests() if row["role"] != "bootstrap"
        ] == []
        assert authority.read.dynamic_instances()[0]["status"] == "admitted"
    finally:
        authority.close()
