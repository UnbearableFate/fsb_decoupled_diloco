import torch

from fs_diloco.protocol.merge import (
    normalized_update_weights,
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
