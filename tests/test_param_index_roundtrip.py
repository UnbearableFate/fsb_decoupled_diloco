import torch

from fs_diloco.hf_model import TinyCausalLM
from fs_diloco.param_index import build_param_index, flatten_trainable_params, load_flat_into_model


def test_flatten_load_roundtrip_preserves_values():
    model = TinyCausalLM(vocab_size=16, hidden_size=8)
    index = build_param_index(model, model_name_or_path="synthetic-tiny")
    flat = flatten_trainable_params(model, index)
    modified = flat + torch.arange(flat.numel(), dtype=torch.float32) * 1e-4
    load_flat_into_model(model, modified, index)
    roundtrip = flatten_trainable_params(model, index)
    assert torch.allclose(roundtrip, modified)
