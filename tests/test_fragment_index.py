import pytest

from fs_diloco.fragment_index import build_fragment_index, fragment_size_summary, validate_fragment_index


def _param_index():
    return {
        "format_version": 1,
        "model_name_or_path": "tiny",
        "trainable_only": True,
        "total_numel": 20,
        "params": [
            {"name": "a", "shape": [10], "dtype": "torch.float32", "numel": 10, "offset": 0},
            {"name": "b", "shape": [6], "dtype": "torch.float32", "numel": 6, "offset": 10},
            {"name": "c", "shape": [4], "dtype": "torch.float32", "numel": 4, "offset": 16},
        ],
    }


def test_full_fragment_strategy_covers_all_params():
    index = build_fragment_index(_param_index(), strategy="full", num_fragments=1)
    assert index["num_fragments"] == 1
    assert index["fragments"][0]["numel"] == 20
    validate_fragment_index(index, _param_index())


def test_balanced_tensor_fragments_are_nonempty_and_cover_vector():
    index = build_fragment_index(_param_index(), strategy="balanced_tensor", num_fragments=2)
    assert [fragment["fragment_id"] for fragment in index["fragments"]] == [0, 1]
    assert all(fragment["numel"] > 0 for fragment in index["fragments"])
    assert sum(fragment["numel"] for fragment in index["fragments"]) == 20
    summary = fragment_size_summary(index)
    assert summary["max"] >= summary["min"] > 0


def test_balanced_tensor_rejects_more_fragments_than_tensors():
    with pytest.raises(ValueError, match="num_fragments <= trainable tensor count"):
        build_fragment_index(_param_index(), strategy="balanced_tensor", num_fragments=4)
