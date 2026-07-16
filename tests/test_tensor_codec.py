import torch
from safetensors.torch import load_file

from fs_diloco.storage.tensor_codec import load_update_vector, save_update_vector


def test_update_vector_bfloat16_storage_loads_as_float32(tmp_path):
    path = tmp_path / "update.params.safetensors"
    source = torch.tensor([1.0, -2.0, 3.5], dtype=torch.bfloat16)
    save_update_vector(path, source, dtype=torch.bfloat16)

    assert load_file(str(path))["local_params"].dtype == torch.bfloat16
    loaded = load_update_vector(path)
    assert loaded.dtype == torch.float32
    assert torch.equal(loaded, source.float())
