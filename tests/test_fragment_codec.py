import torch

from fs_diloco.fragment_codec import (
    extract_fragment,
    load_fragment_update,
    materialize_full_from_fragments,
    save_fragment_update,
    scatter_fragment,
)
from fs_diloco.fragment_index import build_fragment_index


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
