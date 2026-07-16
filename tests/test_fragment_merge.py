from fs_diloco.protocol.merge import (
    normalized_fragment_update_weights,
    select_one_per_learner,
    stale_fragment_update_ids,
)


def test_fragment_weighting_and_selection_are_fragment_version_based():
    updates = [
        {
            "update_id": "a",
            "learner_id": "learner_000",
            "fragment_id": 0,
            "base_fragment_version": 4,
            "tokens_this_update": 100,
            "local_step_end": 1,
            "committed_at": 1.0,
        },
        {
            "update_id": "b",
            "learner_id": "learner_001",
            "fragment_id": 0,
            "base_fragment_version": 2,
            "tokens_this_update": 100,
            "local_step_end": 1,
            "committed_at": 1.0,
        },
    ]
    weights = normalized_fragment_update_weights(updates, current_fragment_version=4, staleness_lambda=0.5)
    assert weights["a"] > weights["b"]
    assert stale_fragment_update_ids(updates, current_fragment_version=5, max_staleness_versions=2) == ["b"]


def test_selection_filters_are_applied_before_one_per_learner():
    updates = [
        {"update_id": "f0", "learner_id": "learner_000", "fragment_id": 0, "local_step_end": 1, "committed_at": 1.0},
        {"update_id": "f1", "learner_id": "learner_000", "fragment_id": 1, "local_step_end": 2, "committed_at": 2.0},
    ]
    target_updates = [row for row in updates if row["fragment_id"] == 0]
    assert [row["update_id"] for row in select_one_per_learner(target_updates)] == ["f0"]
