"""Model construction for real Hugging Face and tiny synthetic smoke runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import torch

from .hf_identity import reject_local_reference


@dataclass
class CausalLMOutput:
    loss: torch.Tensor | None = None
    logits: torch.Tensor | None = None


class TinyCausalLM(torch.nn.Module):
    def __init__(self, vocab_size: int = 128, hidden_size: int = 32) -> None:
        super().__init__()
        self.embed = torch.nn.Embedding(vocab_size, hidden_size)
        self.lm_head = torch.nn.Linear(hidden_size, vocab_size, bias=False)
        self.config = type("TinyConfig", (), {"vocab_size": vocab_size})()

    def forward(
        self, input_ids: torch.Tensor, labels: torch.Tensor | None = None
    ) -> CausalLMOutput:
        hidden = self.embed(input_ids)
        logits = self.lm_head(hidden)
        loss = None
        if labels is not None:
            shift_logits = logits[:, :-1, :].contiguous()
            shift_labels = labels[:, 1:].contiguous()
            loss = torch.nn.functional.cross_entropy(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_labels.view(-1),
            )
        return CausalLMOutput(loss=loss, logits=logits)


class SyntheticTokenizer:
    def __init__(self, vocab_size: int) -> None:
        self.vocab_size = vocab_size
        self.eos_token = "<eos>"
        self.pad_token = "<eos>"


def is_synthetic_model(name_or_path: str) -> bool:
    return name_or_path == "synthetic-tiny"


def model_dtype(dtype_name: str) -> torch.dtype:
    if dtype_name == "bfloat16":
        return torch.bfloat16
    if dtype_name == "float32":
        return torch.float32
    raise ValueError(f"unsupported model dtype: {dtype_name}")


def load_causal_lm_and_tokenizer(config: Any) -> tuple[torch.nn.Module, Any]:
    name = config.name_or_path
    if is_synthetic_model(name):
        model = TinyCausalLM(
            vocab_size=int(getattr(config, "synthetic_vocab_size", 128)),
            hidden_size=int(getattr(config, "synthetic_hidden_size", 32)),
        ).to(dtype=model_dtype(getattr(config, "dtype", "float32")))
        return model, SyntheticTokenizer(model.config.vocab_size)

    reject_local_reference(name, kind="model")
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        name,
        revision=getattr(config, "tokenizer_revision", None) or getattr(config, "revision", None),
        trust_remote_code=bool(getattr(config, "trust_remote_code", False)),
    )
    if (
        getattr(tokenizer, "pad_token", None) is None
        and getattr(tokenizer, "eos_token", None) is not None
    ):
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        name,
        revision=getattr(config, "revision", None),
        trust_remote_code=bool(getattr(config, "trust_remote_code", False)),
        dtype=model_dtype(getattr(config, "dtype", "float32")),
    )
    if bool(getattr(config, "compile", False)):
        model = torch.compile(model)
    return model, tokenizer


def choose_device() -> torch.device:
    return torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
