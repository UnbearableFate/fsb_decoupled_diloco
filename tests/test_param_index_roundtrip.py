import torch

from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
    trainable_params_l2_norm,
)


def test_flatten_load_roundtrip_preserves_values():
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    flat = flatten_trainable_params(model, index)
    modified = flat + torch.arange(flat.numel(), dtype=torch.float32) * 1e-4
    load_flat_into_model(model, modified, index)
    roundtrip = flatten_trainable_params(model, index)
    assert torch.allclose(roundtrip, modified)


def test_flatten_dtype_and_norm_without_flattening():
    model = TinyCausalLM(vocab_size=16, hidden_size=8).to(dtype=torch.bfloat16)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    flat = flatten_trainable_params(model, index, dtype=torch.bfloat16)
    expected = torch.linalg.vector_norm(flat, ord=2, dtype=torch.float32)
    assert flat.dtype == torch.bfloat16
    assert torch.allclose(trainable_params_l2_norm(model), expected)
