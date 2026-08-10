import torch
from safetensors.torch import load_file

from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.param_index import build_param_index, flatten_trainable_params
from fs_diloco.storage.tensor_codec import (
    load_global_weights_into_model,
    load_update_vector,
    save_global_weights,
    save_update_vector,
)


def test_update_vector_bfloat16_storage_loads_as_float32(tmp_path):
    path = tmp_path / "update.params.safetensors"
    source = torch.tensor([1.0, -2.0, 3.5], dtype=torch.bfloat16)
    save_update_vector(path, source, dtype=torch.bfloat16)

    assert load_file(str(path))["local_params"].dtype == torch.bfloat16
    loaded = load_update_vector(path)
    assert loaded.dtype == torch.float32
    assert torch.equal(loaded, source.float())

    loaded_bf16 = load_update_vector(path, dtype=torch.bfloat16)
    assert loaded_bf16.dtype == torch.bfloat16
    assert torch.equal(loaded_bf16, source)


def test_bfloat16_global_weights_load_directly_into_bfloat16_model(tmp_path):
    source_model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    param_index = build_param_index(source_model, model_name_or_path="synthetic-tiny")
    source_flat = flatten_trainable_params(source_model, param_index, dtype=torch.bfloat16)
    path = tmp_path / "global.safetensors"
    save_global_weights(path, source_flat, param_index, dtype=torch.bfloat16)

    target_model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    with torch.no_grad():
        for param in target_model.parameters():
            param.zero_()

    load_global_weights_into_model(path, target_model, param_index)

    loaded_flat = flatten_trainable_params(target_model, param_index, dtype=torch.bfloat16)
    assert loaded_flat.dtype == torch.bfloat16
    assert torch.equal(loaded_flat, source_flat)
