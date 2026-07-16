import torch

from fs_diloco.merge import (
    normalized_update_weights,
    select_one_per_learner,
    stale_update_ids,
    weighted_average_tensors,
)


def test_token_staleness_weighting_and_average():
    updates = [
        {"update_id": "a", "base_global_version": 4, "tokens_this_update": 100},
        {"update_id": "b", "base_global_version": 2, "tokens_this_update": 100},
    ]
    weights = normalized_update_weights(updates, current_version=4, staleness_lambda=0.5)
    assert weights["a"] > weights["b"]
    result = weighted_average_tensors(
        [torch.tensor([1.0, 3.0]), torch.tensor([3.0, 5.0])],
        [weights["a"], weights["b"]],
    )
    assert result.shape == (2,)
    assert torch.all(result > torch.tensor([1.0, 3.0]))


def test_selection_one_update_per_learner_and_stale_drop():
    updates = [
        {"update_id": "old", "learner_id": "learner_000", "local_step_end": 1, "committed_at": 1.0, "base_global_version": 0},
        {"update_id": "new", "learner_id": "learner_000", "local_step_end": 2, "committed_at": 2.0, "base_global_version": 1},
        {"update_id": "other", "learner_id": "learner_001", "local_step_end": 1, "committed_at": 3.0, "base_global_version": 3},
    ]
    selected = select_one_per_learner(updates, policy="most_recent_per_learner")
    assert [row["update_id"] for row in selected] == ["new", "other"]
    assert stale_update_ids(updates, current_version=3, max_staleness_versions=2) == ["old"]
