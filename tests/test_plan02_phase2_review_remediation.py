from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from fs_diloco.core.config import Config
from fs_diloco.protocol.dynamic_terminal import read_current_drain
from fs_diloco.protocol.membership import new_learner_instance_id, write_registration_request
from fs_diloco.runtime import syncer as syncer_runtime
from fs_diloco.storage.fenced_store import FencedSQLiteStore
from fs_diloco.storage.leader_lease import LeaderLeaseStore, LeaseSafetyTracker
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from fs_diloco.storage.schema_bootstrap import BootstrapIdentity, initialize_new_run
from scripts.miyabi import check_plan02_phase2


def _identity() -> BootstrapIdentity:
    return BootstrapIdentity(
        run_id="phase2-review-test",
        source_fingerprint="sha256:phase2-review-source",
        config_sha256="phase2-review-config",
        mode="full_dynamic",
    )


def _dynamic_store(tmp_path: Path):
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    identity = _identity()
    initialize_new_run(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
    )
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
    )
    token = lease.acquire(owner_id="phase2-review-owner", hostname="host", pid=1)
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
    )
    fenced = FencedSQLiteStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
        lease_safety_check=tracker.assert_safe,
    )
    return paths, lease, fenced, fenced.bind(token)


def _initialize_member(store: Any, paths: RunPaths) -> dict[str, Any]:
    launch = store.initialize_dynamic_membership(
        stream_pool_size=1,
        bootstrap_instances=1,
        config_fingerprint="phase2-review-descriptor",
        created_at=100.0,
    )[0]
    instance_id = new_learner_instance_id()
    request = write_registration_request(
        paths,
        run_id=_identity().run_id,
        instance_id=instance_id,
        placement="host:gpu0",
        launch_request_id=str(launch["request_id"]),
        source_fingerprint=_identity().source_fingerprint,
        config_sha256=_identity().config_sha256,
        ttl_seconds=60.0,
        hostname="host",
        pid=7,
        gpu_identity="gpu0",
        now=100.0,
    )
    admission = store.admit_registration(
        request,
        stream_pool_size=1,
        desired_contributors=1,
        allow_unsolicited_registration=False,
        allow_healthy_placement_replacement=False,
        reuse_stream_for_same_placement=True,
        now=100.0,
    )
    paths.registration_request_path(instance_id).unlink(missing_ok=True)
    return admission


def _update_metadata(admission: dict[str, Any]) -> dict[str, Any]:
    return {
        "update_id": "phase2-review-update",
        "learner_id": admission["instance_id"],
        "learner_instance_id": admission["instance_id"],
        "placement_id": admission["placement_id"],
        "placement_epoch": admission["placement_epoch"],
        "stream_id": admission["stream_id"],
        "stream_epoch": admission["stream_epoch"],
        "admission_generation": admission["admission_generation"],
        "admission_token_hash": hashlib.sha256(
            admission["admission_token"].encode("utf-8")
        ).hexdigest(),
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": 1,
        "inner_steps": 1,
        "tokens_this_update": 16,
        "tokens_since_global_load": 16,
        "file_path": "updates/phase2-review.safetensors",
        "created_at": 100.0,
        "committed_at": 100.0,
    }


def _capacity_kwargs(
    *,
    observation_key: str,
    kind: str,
    global_version: int,
    selected_instance_ids: list[str],
    eligible_contributors: int,
) -> dict[str, Any]:
    return {
        "observation_key": observation_key,
        "kind": kind,
        "global_version": global_version,
        "eligible_contributors": eligible_contributors,
        "selected_instance_ids": selected_instance_ids,
        "low_contributor_threshold": 0,
        "consecutive_low_windows": 2,
        "productive_window_count": 2,
        "startup_grace_seconds": 1.0,
        "heartbeat_stale_after_seconds": 10.0,
        "productive_upload_grace_factor": 2.0,
        "productive_upload_grace_min_seconds": 1.0,
        "productive_upload_grace_max_seconds": 10.0,
        "desired_contributors": 1,
        "stream_pool_size": 1,
        "scaling_enabled": False,
        "initial_membership_deadline_seconds": 10.0,
        "cooldown_seconds": 10.0,
        "max_pending_launch_requests": 0,
        "max_total_launch_requests": 0,
        "launch_request_ttl_seconds": 20.0,
        "config_fingerprint": "phase2-review-descriptor",
        "now": 100.0,
    }


def _raise_before_commit() -> None:
    raise RuntimeError("phase2-review-before-commit")


def test_merge_and_capacity_observation_share_one_rollback_boundary(tmp_path: Path) -> None:
    paths, lease, fenced, store = _dynamic_store(tmp_path)
    try:
        admission = _initialize_member(store, paths)
        store.initialize_full_run(
            weight_path="weights/v0.safetensors",
            optim_path="optim/v0.safetensors",
            outer_optimizer="sgd",
            identity={"kind": "phase2-review"},
            config_snapshot={"kind": "phase2-review"},
            publication_id="phase2-review-v0",
            weight_size_bytes=1,
            optim_size_bytes=1,
        )
        metadata = _update_metadata(admission)
        assert store.insert_update_metadata(metadata)
        store.mark_updates_selected([metadata["update_id"]], "phase2-review-selection")
        capacity = _capacity_kwargs(
            observation_key="merge:1",
            kind="merge",
            global_version=1,
            selected_instance_ids=[admission["instance_id"]],
            eligible_contributors=1,
        )
        merge = {
            "predecessor_version": 0,
            "target_version": 1,
            "weight_path": "weights/v1.safetensors",
            "optim_path": "optim/v1.safetensors",
            "selected_updates": [metadata],
            "effective_weights": {metadata["update_id"]: 1.0},
            "total_update_tokens": 16,
            "total_seen_tokens": 16,
            "outer_optimizer": "sgd",
            "max_staleness_versions": 0,
            "publication_id": "phase2-review-v1",
            "weight_size_bytes": 1,
            "optim_size_bytes": 1,
            "capacity_observation": capacity,
        }
        without_capacity = {
            key: value for key, value in merge.items() if key != "capacity_observation"
        }
        with pytest.raises(RuntimeError, match="atomic capacity observation"):
            store.commit_full_merge(**without_capacity)
        for malformed in (
            {**capacity, "kind": "synthetic"},
            {**capacity, "observation_key": "merge:2"},
            {**capacity, "global_version": 2},
        ):
            with pytest.raises(RuntimeError, match="exact merge capacity observation"):
                store.commit_full_merge(**{**merge, "capacity_observation": malformed})
        with pytest.raises(RuntimeError, match="phase2-review-before-commit"):
            store.commit_full_merge(**merge, before_commit=_raise_before_commit)
        assert int(store.latest_global_version()["version"]) == 0
        assert store.capacity_observations() == []
        assert store.get_update(metadata["update_id"])["status"] == "selected"

        store.commit_full_merge(**merge)
        observations = store.capacity_observations()
        assert [row["observation_key"] for row in observations] == ["merge:1"]
        assert int(store.latest_global_version()["version"]) == 1
    finally:
        fenced.close()
        lease.close()


def test_merge_and_starvation_observations_reject_non_atomic_public_writes(
    tmp_path: Path,
) -> None:
    _paths, lease, fenced, store = _dynamic_store(tmp_path)
    try:
        store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=0,
            config_fingerprint="phase2-review-descriptor",
            created_at=100.0,
        )
        for kind, key in (
            ("merge", "merge:1"),
            ("starvation", "starvation:1"),
            ("synthetic", "merge:1"),
            ("synthetic", "starvation:1"),
        ):
            with pytest.raises(RuntimeError, match="atomic state-transition API"):
                store.record_capacity_observation(
                    **_capacity_kwargs(
                        observation_key=key,
                        kind=kind,
                        global_version=1,
                        selected_instance_ids=[],
                        eligible_contributors=0,
                    )
                )
        assert store.capacity_observations() == []
    finally:
        fenced.close()
        lease.close()


def test_starvation_generation_and_observation_share_one_rollback_boundary(
    tmp_path: Path,
) -> None:
    _paths, lease, fenced, store = _dynamic_store(tmp_path)
    try:
        store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=0,
            config_fingerprint="phase2-review-descriptor",
            created_at=100.0,
        )
        capacity = _capacity_kwargs(
            observation_key="",
            kind="starvation",
            global_version=0,
            selected_instance_ids=[],
            eligible_contributors=0,
        )
        capacity.pop("observation_key")
        with pytest.raises(RuntimeError, match="phase2-review-before-commit"):
            store.record_starvation_capacity_observation(
                interval_seconds=10.0,
                capacity_observation=capacity,
                before_commit=_raise_before_commit,
            )
        assert store.get_run_state("starvation_generation") is None
        assert store.get_run_state("next_starvation_observation_at") is None
        assert store.capacity_observations() == []

        result = store.record_starvation_capacity_observation(
            interval_seconds=10.0,
            capacity_observation=capacity,
        )
        assert result is not None
        assert result["observation"]["observation_key"] == "starvation:1"
        assert store.get_run_state("starvation_generation") == 1
        assert (
            store.record_starvation_capacity_observation(
                interval_seconds=10.0,
                capacity_observation=capacity,
                now=105.0,
            )
            is None
        )
    finally:
        fenced.close()
        lease.close()


def test_token_target_freezes_dynamic_terminal_version_at_committed_head(
    tmp_path: Path,
) -> None:
    _paths, lease, fenced, store = _dynamic_store(tmp_path)
    try:
        store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=0,
            config_fingerprint="phase2-review-descriptor",
            created_at=100.0,
        )
        controller = store.begin_dynamic_drain(
            reason="stop_after_global_tokens",
            current_version=3,
            global_target=20,
            max_terminal_merges=1,
            requested_at=100.0,
        )
        assert int(controller["max_terminal_version"]) == 3
    finally:
        fenced.close()
        lease.close()


def test_dynamic_no_progress_enters_persisted_drain_and_open_terminal_is_rejected(
    tmp_path: Path,
) -> None:
    paths, lease, fenced, store = _dynamic_store(tmp_path)
    try:
        store.initialize_dynamic_membership(
            stream_pool_size=1,
            bootstrap_instances=0,
            config_fingerprint="phase2-review-descriptor",
            created_at=100.0,
        )
        config = Config()
        config.run.run_id = _identity().run_id
        config.membership.mode = "dynamic"
        config.sync.stop_after_outer_steps = 20
        config.terminal.max_terminal_merges = 1

        with pytest.raises(RuntimeError, match="closed.*controller"):
            syncer_runtime.require_closed_dynamic_terminal(
                store,
                dynamic_mode=True,
                stop_reason="no_progress_timeout",
            )
        controller = syncer_runtime.start_dynamic_drain(
            config=config,
            store=store,
            paths=paths,
            reason="no_progress_timeout",
            current_version=3,
            requested_at=100.0,
        )
        assert controller["state"] == "draining"
        assert int(controller["max_terminal_version"]) == 4
        assert (
            syncer_runtime.resumed_dynamic_stop_reason(store, default="completed")
            == "no_progress_timeout"
        )
        directive = read_current_drain(paths, run_id=_identity().run_id)
        assert directive is not None
        assert directive["reason"] == "no_progress_timeout"
        closed = store.advance_dynamic_drain(
            drain_ack_timeout_seconds=0.0,
            registration_visibility_grace_seconds=0.0,
            proposal_visibility_grace_seconds=0.0,
            now=100.0,
        )
        assert closed["state"] == "closed"
        syncer_runtime.require_closed_dynamic_terminal(
            store,
            dynamic_mode=True,
            stop_reason="no_progress_timeout",
        )
    finally:
        fenced.close()
        lease.close()


def test_completed_checker_requires_one_merge_observation_per_committed_version() -> None:
    versions = [{"version": version} for version in range(4)]
    observations = [
        {"observation_key": "merge:1", "kind": "merge", "global_version": 1},
        {"observation_key": "merge:3", "kind": "merge", "global_version": 3},
        {"observation_key": "starvation:1", "kind": "starvation", "global_version": 1},
    ]
    assert check_plan02_phase2.missing_merge_observation_versions(
        versions,
        observations,
    ) == [2]
