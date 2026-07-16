import time

import pytest

from fs_diloco.storage.sqlite_store import SQLiteStore


def _metadata(
    update_id="u1",
    *,
    learner_id="learner_000",
    local_step_end=1,
    base_version=0,
    file_path="/tmp/update.safetensors",
):
    now = time.time()
    return {
        "update_id": update_id,
        "learner_id": learner_id,
        "hostname": "host",
        "base_global_version": base_version,
        "local_step_start": max(0, local_step_end - 1),
        "local_step_end": local_step_end,
        "inner_steps": 1,
        "tokens_this_update": 10,
        "tokens_since_global_load": 10,
        "num_examples_this_update": 1,
        "train_loss": 1.0,
        "grad_norm": None,
        "param_norm": 1.0,
        "delta_norm": None,
        "training_cpu_utilization_peak_percent": 40.0,
        "training_gpu_utilization_peak_percent": 90.0,
        "local_cycle_cpu_utilization_peak_percent": 30.0,
        "local_cycle_gpu_utilization_peak_percent": 80.0,
        "local_cycle_step_time_seconds_mean": 1.5,
        "local_cycle_step_count": 1,
        "local_cycle_resource_sample_count": 2,
        "file_path": file_path,
        "file_size_bytes": 1,
        "sha256": None,
        "created_at": now,
        "committed_at": now,
    }


def _initialize(store):
    return store.initialize_full_run(
        weight_path="/run/weights/global_v000000.safetensors",
        optim_path="/run/optim/outer_v000000.safetensors",
        outer_optimizer="nesterov",
        identity={"run_id": "test", "protocol_version": 2},
        config_snapshot={"test": True},
    )


def test_persistent_pragmas_and_reopen(tmp_path):
    path = tmp_path / "control" / "syncer_metadata.sqlite3"
    store = SQLiteStore(path)
    assert store.pragma_settings() == {"journal_mode": "delete", "synchronous": 2}
    _initialize(store)
    store.integrity_check()
    store.close()

    reopened = SQLiteStore(path)
    assert reopened.latest_global_version()["version"] == 0
    assert reopened.get_run_state("identity")["run_id"] == "test"
    reopened.integrity_check()
    reopened.close()


def test_fixed_pointer_latest_wins_and_frontier_survives_pruning(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    assert store.insert_update_metadata(
        _metadata("old", local_step_end=1), pointer_path="learner_000.json"
    )
    assert store.insert_update_metadata(
        _metadata("new", local_step_end=2), pointer_path="learner_000.json"
    )
    assert store.get_update("old")["drop_reason"] == "superseded"
    assert store.get_update("new")["status"] == "pending"
    assert store.insert_update_metadata(
        _metadata("new", local_step_end=2), pointer_path="learner_000.json"
    ) is False
    assert store.proposal_frontiers() == {"learner_000": "new"}
    store.close()


def test_eligibility_rejects_future_and_honors_staleness_boundary(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    for index, (update_id, base) in enumerate(
        (("at_max", 2), ("too_old", 1), ("future", 4))
    ):
        store.insert_update_metadata(
            _metadata(update_id, learner_id=f"learner_{index:03d}", base_version=base)
        )
    eligible = store.eligible_updates(current_version=3, max_staleness_versions=1)
    assert {row["update_id"] for row in eligible} == {"at_max"}
    assert store.drop_ineligible_updates(3, 1) == 2
    assert store.get_update("too_old")["drop_reason"] == "too_stale"
    assert store.get_update("future")["drop_reason"] == "future_base"
    store.close()


def test_full_merge_is_one_transaction_and_validates_selected_set(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    _initialize(store)
    store.insert_update_metadata(_metadata("u0", learner_id="learner_000"))
    store.insert_update_metadata(_metadata("u1", learner_id="learner_001"))
    selected = [store.get_update("u0"), store.get_update("u1")]
    store.mark_updates_selected(["u0", "u1"], "selection")
    row = store.commit_full_merge(
        predecessor_version=0,
        target_version=1,
        weight_path="/run/weights/global_v000001.safetensors",
        optim_path="/run/optim/outer_v000001.safetensors",
        selected_updates=selected,
        effective_weights={"u0": 0.25, "u1": 0.75},
        total_update_tokens=20,
        total_seen_tokens=20,
        outer_optimizer="nesterov",
        max_staleness_versions=0,
    )
    assert row["version"] == 1
    assert row["total_seen_tokens"] == 20
    assert store.get_update("u0")["status"] == "applied"
    assert store.get_update("u0")["effective_weight"] == 0.25

    with pytest.raises(ValueError, match="does not follow"):
        store.commit_full_merge(
            predecessor_version=1,
            target_version=3,
            weight_path="w3",
            optim_path="o3",
            selected_updates=selected,
            effective_weights={"u0": 0.25, "u1": 0.75},
            total_update_tokens=20,
            total_seen_tokens=40,
            outer_optimizer="nesterov",
            max_staleness_versions=0,
        )
    store.close()


def test_full_merge_failure_rolls_back_global_and_update_state(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    _initialize(store)
    store.insert_update_metadata(_metadata("u0"))
    selected = [store.get_update("u0")]
    store.mark_updates_selected(["u0"], "selection")

    def fail():
        raise RuntimeError("injected transaction failure")

    with pytest.raises(RuntimeError, match="injected"):
        store.commit_full_merge(
            predecessor_version=0,
            target_version=1,
            weight_path="w1",
            optim_path="o1",
            selected_updates=selected,
            effective_weights={"u0": 1.0},
            total_update_tokens=10,
            total_seen_tokens=10,
            outer_optimizer="nesterov",
            max_staleness_versions=0,
            before_commit=fail,
        )
    assert store.get_global_version(1) is None
    assert store.get_update("u0")["status"] == "selected"
    assert store.reset_all_selected_to_pending() == 1
    assert store.get_update("u0")["status"] == "pending"
    store.close()


def test_full_merge_rejects_duplicate_learner_and_non_selected(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    _initialize(store)
    store.insert_update_metadata(_metadata("u0", local_step_end=1))
    first = store.get_update("u0")
    store.mark_updates_selected(["u0"], "selection")
    store.insert_update_metadata(_metadata("u1", local_step_end=2))
    second = store.get_update("u1")
    with pytest.raises(ValueError, match="duplicate learner"):
        store.commit_full_merge(
            predecessor_version=0,
            target_version=1,
            weight_path="w1",
            optim_path="o1",
            selected_updates=[first, second],
            effective_weights={"u0": 0.5, "u1": 0.5},
            total_update_tokens=20,
            total_seen_tokens=20,
            outer_optimizer="nesterov",
            max_staleness_versions=0,
        )
    with pytest.raises(RuntimeError, match="not selected"):
        store.commit_full_merge(
            predecessor_version=0,
            target_version=1,
            weight_path="w1",
            optim_path="o1",
            selected_updates=[second],
            effective_weights={"u1": 1.0},
            total_update_tokens=10,
            total_seen_tokens=10,
            outer_optimizer="nesterov",
            max_staleness_versions=0,
        )
    assert store.latest_global_version()["version"] == 0
    store.close()


def test_normal_shutdown_finalizes_selected_and_pending_updates(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.insert_update_metadata(_metadata("selected", local_step_end=1))
    store.mark_updates_selected(["selected"], "selection")
    store.insert_update_metadata(_metadata("pending", local_step_end=2))
    assert store.finalize_unconsumed_updates(
        fragment_mode=False, reason="stop_after_outer_steps"
    ) == 2
    assert store.get_update("selected")["status"] == "dropped"
    assert store.get_update("pending")["drop_reason"] == "stop_after_outer_steps"
    store.close()
