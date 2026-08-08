from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.protocol import membership
from fs_diloco.protocol.merge import select_one_per_learner
from fs_diloco.protocol.membership import new_learner_instance_id, write_registration_request
from fs_diloco.runtime.launch_outbox import LearnerLaunchOutbox
from fs_diloco.runtime.pbs_scheduler import PBSJobObservation
from fs_diloco.storage.sqlite_store import DynamicMembershipFenceError
from tests.support import DynamicAuthorityHarness, FakePBS


pytestmark = pytest.mark.plan03_red


def _heartbeat(admission: dict[str, object], *, timestamp: float) -> dict[str, object]:
    return {
        "instance_id": admission["instance_id"],
        "learner_id": admission["instance_id"],
        "placement_id": admission["placement_id"],
        "placement_epoch": admission["placement_epoch"],
        "stream_id": admission["stream_id"],
        "stream_epoch": admission["stream_epoch"],
        "admission_generation": admission["admission_generation"],
        "admission_token": admission["admission_token"],
        "timestamp": timestamp,
        "status": "active",
    }


def _capacity_observation(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "observation_key": "merge:1",
        "kind": "merge",
        "global_version": 1,
        "desired_contributors": len(rows),
        "current_contributors": len(rows),
        "eligible_contributors": len(rows),
        "selected_contributors": len(rows),
        "selected_instance_ids": [str(row["learner_instance_id"]) for row in rows],
        "low_count_streak": 0,
        "now": 200.0,
        "scale_out_enabled": False,
        "min_contributors": 1,
        "max_contributors": len(rows),
        "scale_out_low_watermark": 1,
        "scale_out_consecutive_observations": 2,
        "scale_out_cooldown_seconds": 60.0,
        "launch_request_ttl_seconds": 60.0,
        "max_total_launch_requests": len(rows),
        "max_active_launch_requests": len(rows),
    }


@pytest.mark.xfail(
    strict=True,
    reason="H-01a: eligible_updates includes revoked incarnations and selection aborts the batch",
)
def test_h01a_revoke_before_select_does_not_abort_current_batch(tmp_path: Path) -> None:
    authority = DynamicAuthorityHarness.create(tmp_path / "authority-h01a")
    try:
        stale, current = authority.initialize(members=2)
        stale_row = authority.proposal(stale, "stale-before-select", committed_at=100.0)
        current_row = authority.proposal(current, "current-before-select", committed_at=101.0)
        assert authority.store.insert_update_metadata(stale_row)
        assert authority.store.insert_update_metadata(current_row)
        authority.store.update_instance_heartbeat(
            _heartbeat(current, timestamp=195.0),
            heartbeat_path="hb.json",
        )
        revoked = authority.store.revoke_dead_instances(
            heartbeat_dead_after_seconds=10.0,
            now=200.0,
        )
        assert [row["instance_id"] for row in revoked] == [stale["instance_id"]]

        eligible = authority.store.eligible_updates(0, 0)
        authority.store.mark_updates_selected(
            [str(row["update_id"]) for row in eligible],
            "h01a-selection",
        )
        assert authority.store.get_update("stale-before-select")["status"] == "dropped"
        assert authority.store.get_update("current-before-select")["status"] == "selected"
    finally:
        authority.close()


@pytest.mark.xfail(
    strict=True,
    reason="H-01b: commit-time membership retry resets the stale row with the valid batch",
)
def test_h01b_commit_conflict_terminalizes_only_invalid_rows(tmp_path: Path) -> None:
    authority = DynamicAuthorityHarness.create(tmp_path / "authority-h01b")
    try:
        stale, current = authority.initialize(members=2)
        rows = [
            authority.proposal(stale, "stale-after-select", committed_at=100.0),
            authority.proposal(current, "current-after-select", committed_at=101.0),
        ]
        for row in rows:
            assert authority.store.insert_update_metadata(row)
        authority.store.mark_updates_selected(
            [str(row["update_id"]) for row in rows],
            "h01b-selection",
        )
        authority.store.update_instance_heartbeat(
            _heartbeat(current, timestamp=195.0),
            heartbeat_path="hb.json",
        )
        authority.store.revoke_dead_instances(heartbeat_dead_after_seconds=10.0, now=200.0)
        try:
            authority.store.commit_full_merge(
                predecessor_version=0,
                target_version=1,
                weight_path="weights/v1.safetensors",
                optim_path="optim/v1.safetensors",
                selected_updates=rows,
                effective_weights={str(row["update_id"]): 0.5 for row in rows},
                total_update_tokens=32,
                total_seen_tokens=32,
                outer_optimizer="sgd",
                max_staleness_versions=0,
                publication_id="publication-v1",
                weight_size_bytes=1,
                optim_size_bytes=1,
                capacity_observation=_capacity_observation(rows),
            )
        except DynamicMembershipFenceError:
            authority.store.reset_selected_to_pending([str(row["update_id"]) for row in rows])
        states = {
            str(row["update_id"]): str(authority.store.get_update(str(row["update_id"]))["status"])
            for row in rows
        }
        assert states == {
            "stale-after-select": "dropped",
            "current-after-select": "pending",
        }
    finally:
        authority.close()


@pytest.mark.xfail(
    strict=True,
    reason="H-05: quorum truncation has no persistent contributor service credit",
)
def test_h05_quorum_truncation_serves_every_continuously_ready_contributor() -> None:
    updates = [
        {
            "update_id": f"u-{index}",
            "learner_id": f"learner_{index:03d}",
            "local_step_end": 1,
            "committed_at": 1.0,
        }
        for index in range(8)
    ]
    selected_contributors: set[str] = set()
    for _ in range(1000):
        selected_contributors.update(
            str(row["learner_id"]) for row in select_one_per_learner(updates, quorum_max=3)
        )
    assert selected_contributors == {str(row["learner_id"]) for row in updates}


@pytest.mark.xfail(
    strict=True,
    reason="H-06: one transient registration read is collapsed into malformed and unlinked",
)
def test_h06_transient_registration_eio_preserves_request_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = DynamicAuthorityHarness.create(tmp_path / "authority-h06")
    try:
        launch = authority.store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=1,
            config_fingerprint="descriptor-digest",
            created_at=100.0,
        )[0]
        instance_id = new_learner_instance_id()
        write_registration_request(
            authority.paths,
            run_id=authority.identity.run_id,
            instance_id=instance_id,
            placement="host:gpu0",
            launch_request_id=str(launch["request_id"]),
            source_fingerprint=authority.identity.source_fingerprint,
            config_sha256=authority.identity.config_sha256,
            ttl_seconds=60.0,
            now=100.0,
        )
        request_path = authority.paths.registration_request_path(instance_id)
        original = membership.safe_read_json
        calls = 0

        def transient_once(path: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original(path)

        monkeypatch.setattr(membership, "safe_read_json", transient_once)
        result = membership.ingest_registration_requests(
            authority.store,
            authority.paths,
            token=authority.store.token,
            run_id=authority.identity.run_id,
            source_fingerprint=authority.identity.source_fingerprint,
            config_sha256=authority.identity.config_sha256,
            stream_pool_size=1,
            desired_contributors=1,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=101.0,
        )
        assert result == []
        assert request_path.is_file()
        assert (
            membership.ingest_registration_requests(
                authority.store,
                authority.paths,
                token=authority.store.token,
                run_id=authority.identity.run_id,
                source_fingerprint=authority.identity.source_fingerprint,
                config_sha256=authority.identity.config_sha256,
                stream_pool_size=1,
                desired_contributors=1,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=102.0,
            )[0]["state"]
            == "admitted"
        )
    finally:
        authority.close()


@pytest.mark.xfail(
    strict=True,
    reason="H-07: live+historical no-record immediately fails a known accepted PBS job",
)
def test_h07_known_job_no_record_enters_bounded_uncertainty(tmp_path: Path) -> None:
    authority = DynamicAuthorityHarness.create(tmp_path / "authority-h07")
    try:
        launch = authority.store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=1,
            config_fingerprint="descriptor-digest",
            created_at=100.0,
        )[0]
        authority.store.record_external_launch_jobs(
            [
                {
                    "bootstrap_slot": 0,
                    "request_id": str(launch["request_id"]),
                    "pbs_job_id": "known.opbs",
                }
            ],
            observed_at=100.0,
        )
        scheduler = FakePBS()
        no_record = PBSJobObservation("known.opbs", "no_record", None, 1, "missing")
        scheduler.queue_query("known.opbs", no_record)
        scheduler.queue_query("known.opbs", no_record, historical=True)
        outbox = LearnerLaunchOutbox(
            paths=authority.paths,
            config=SimpleNamespace(
                scheduler_reconcile_interval_seconds=1.0,
                learner_pbs_script="learner.pbs",
                learner_walltime="00:01:00",
            ),
            scheduler=scheduler,
            descriptor_sha256="descriptor-digest",
            wall_clock=lambda: 101.0,
        )
        outbox.reconcile(authority.store)
        row = next(
            item
            for item in authority.store.launch_requests()
            if item["request_id"] == launch["request_id"]
        )
        assert row["state"] == "terminal_uncertain"
        assert row["reservation_released_at"] is None
        assert row["uncertainty_deadline"] is not None
    finally:
        authority.close()
