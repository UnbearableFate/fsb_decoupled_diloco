import torch

from fs_diloco.protocol.fragment_codec import (
    extract_fragment,
    extract_fragment_from_model,
    load_fragment_update,
    materialize_full_from_fragments,
    save_fragment_update,
    scatter_fragment,
)
from fs_diloco.protocol.fragment_index import build_fragment_index


def _param_index():
    return {
        "format_version": 1,
        "model_name_or_path": "tiny",
        "trainable_only": True,
        "total_numel": 10,
        "params": [
            {"name": "a", "shape": [4], "dtype": "torch.float32", "numel": 4, "offset": 0},
            {"name": "b", "shape": [3], "dtype": "torch.float32", "numel": 3, "offset": 4},
            {"name": "c", "shape": [3], "dtype": "torch.float32", "numel": 3, "offset": 7},
        ],
    }


def test_fragment_extract_scatter_round_trip():
    fragment_index = build_fragment_index(_param_index(), strategy="balanced_tensor", num_fragments=2)
    flat = torch.arange(10, dtype=torch.float32)
    fragments = {
        int(fragment["fragment_id"]): extract_fragment(flat, fragment_index, int(fragment["fragment_id"]))
        for fragment in fragment_index["fragments"]
    }
    round_trip = materialize_full_from_fragments(fragments, fragment_index, total_numel=10)
    assert torch.equal(round_trip, flat)

    changed = scatter_fragment(flat, fragment_index, 0, fragments[0] + 100)
    assert not torch.equal(changed, flat)
    unchanged = scatter_fragment(changed, fragment_index, 0, fragments[0])
    assert torch.equal(unchanged, flat)


def test_fragment_update_safetensors_round_trip(tmp_path):
    tensor = torch.tensor([1.0, 2.0, 3.0])
    path = tmp_path / "fragment.safetensors"
    save_fragment_update(path, tensor, torch.float32)
    loaded = load_fragment_update(path)
    assert torch.equal(loaded, tensor)


def test_extract_fragment_from_model_matches_flat_extraction():
    model = torch.nn.Module()
    model.register_parameter("a", torch.nn.Parameter(torch.arange(4, dtype=torch.float32)))
    model.register_parameter("b", torch.nn.Parameter(torch.arange(4, 7, dtype=torch.float32)))
    model.register_parameter("c", torch.nn.Parameter(torch.arange(7, 10, dtype=torch.float32)))
    fragment_index = build_fragment_index(
        _param_index(), strategy="balanced_tensor", num_fragments=2
    )
    flat = torch.arange(10, dtype=torch.float32)
    for fragment_id in range(2):
        expected = extract_fragment(flat, fragment_index, fragment_id).to(torch.bfloat16)
        actual = extract_fragment_from_model(
            model,
            fragment_index,
            fragment_id,
            dtype=torch.bfloat16,
        )
        assert actual.dtype == torch.bfloat16
        assert torch.equal(actual, expected)


def test_fragment_update_can_store_bfloat16(tmp_path):
    tensor = torch.tensor([1.0, 2.0, 3.0], dtype=torch.bfloat16)
    path = tmp_path / "fragment_bf16.safetensors"
    save_fragment_update(path, tensor, torch.bfloat16)
    from safetensors.torch import load_file

    stored = load_file(str(path))["fragment_params"]
    assert stored.dtype == torch.bfloat16
    assert load_fragment_update(path).dtype == torch.float32
    assert load_fragment_update(path, dtype=torch.bfloat16).dtype == torch.bfloat16
