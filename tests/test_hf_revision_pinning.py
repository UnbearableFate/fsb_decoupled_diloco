from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from fs_diloco.modeling.hf_data import load_text_split
from fs_diloco.modeling.hf_model import load_causal_lm_and_tokenizer


MODEL_REVISION = "607a30d783dfa663caf39e06633721c8d4cfcd7e"
TOKENIZER_REVISION = "1111111111111111111111111111111111111111"
DATA_REVISION = "b08601e04326c79dfdd32d625aee71d232d685c3"


@pytest.mark.parametrize(
    ("tokenizer_revision", "expected_tokenizer_revision"),
    [(TOKENIZER_REVISION, TOKENIZER_REVISION), (None, MODEL_REVISION)],
)
def test_hf_model_and_tokenizer_load_the_declared_revisions(
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_revision: str | None,
    expected_tokenizer_revision: str,
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Tokenizer:
        pad_token = None
        eos_token = "<eos>"

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(name: str, **kwargs: object) -> Tokenizer:
            calls.append(("tokenizer", name, kwargs))
            return Tokenizer()

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(name: str, **kwargs: object) -> object:
            calls.append(("model", name, kwargs))
            return object()

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoModelForCausalLM=AutoModelForCausalLM,
            AutoTokenizer=AutoTokenizer,
        ),
    )
    config = SimpleNamespace(
        name_or_path="gpt2",
        revision=MODEL_REVISION,
        tokenizer_revision=tokenizer_revision,
        trust_remote_code=False,
        dtype="float32",
        compile=False,
    )

    load_causal_lm_and_tokenizer(config)

    assert calls[0][0:2] == ("tokenizer", "gpt2")
    assert calls[0][2]["revision"] == expected_tokenizer_revision
    assert calls[1][0:2] == ("model", "gpt2")
    assert calls[1][2]["revision"] == MODEL_REVISION


@pytest.mark.parametrize("primary_fails", [False, True])
def test_hf_dataset_primary_and_wikitext_fallback_use_the_declared_revision(
    monkeypatch: pytest.MonkeyPatch,
    primary_fails: bool,
) -> None:
    calls: list[tuple[str, str | None, dict[str, object]]] = []
    expected = object()

    def load_dataset(name: str, config_name: str | None, **kwargs: object) -> object:
        calls.append((name, config_name, kwargs))
        if primary_fails and len(calls) == 1:
            raise RuntimeError("primary unavailable")
        return expected

    monkeypatch.delenv("FS_DILOCO_HF_WIKITEXT_REPO", raising=False)
    monkeypatch.setitem(sys.modules, "datasets", SimpleNamespace(load_dataset=load_dataset))
    config = SimpleNamespace(
        dataset_name="wikitext",
        dataset_config_name="wikitext-2-raw-v1",
        revision=DATA_REVISION,
        cache_dir=None,
        streaming=False,
    )

    assert load_text_split(config, "train") is expected
    assert all(call[2]["revision"] == DATA_REVISION for call in calls)
    assert calls[0][0] == "wikitext"
    if primary_fails:
        assert calls[1][0] == "Salesforce/wikitext"
