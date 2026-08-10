from __future__ import annotations

import sys
from pathlib import Path
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
    )

    assert load_text_split(config, "train") is expected
    assert all(call[2]["revision"] == DATA_REVISION for call in calls)
    assert calls[0][0] == "wikitext"
    if primary_fails:
        assert calls[1][0] == "Salesforce/wikitext"


@pytest.mark.parametrize("use_symlink", [False, True])
def test_hf_model_loader_rejects_an_ambiguous_existing_local_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_symlink: bool,
) -> None:
    local_model = tmp_path / "local-model"
    local_model.mkdir()
    reference = local_model
    if use_symlink:
        reference = tmp_path / "organization" / "model"
        reference.parent.mkdir()
        reference.symlink_to(local_model, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    class UnexpectedProducer:
        @staticmethod
        def from_pretrained(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("local model reference reached the Hugging Face producer")

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoModelForCausalLM=UnexpectedProducer,
            AutoTokenizer=UnexpectedProducer,
        ),
    )
    config = SimpleNamespace(
        name_or_path=reference.relative_to(tmp_path).as_posix(),
        revision=MODEL_REVISION,
        tokenizer_revision=MODEL_REVISION,
        trust_remote_code=False,
        dtype="float32",
        compile=False,
    )

    with pytest.raises(ValueError, match="local model reference.*content identity"):
        load_causal_lm_and_tokenizer(config)


@pytest.mark.parametrize("use_environment_override", [False, True])
def test_hf_dataset_loader_rejects_an_existing_local_reference_or_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_environment_override: bool,
) -> None:
    local_dataset = tmp_path / "Salesforce" / "wikitext"
    local_dataset.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    reference = "Salesforce/wikitext"
    configured_name = reference
    if use_environment_override:
        configured_name = "wikitext"
        monkeypatch.setenv("FS_DILOCO_HF_WIKITEXT_REPO", str(local_dataset))
    else:
        monkeypatch.delenv("FS_DILOCO_HF_WIKITEXT_REPO", raising=False)

    def unexpected_load(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("local dataset reference reached the Hugging Face producer")

    monkeypatch.setitem(sys.modules, "datasets", SimpleNamespace(load_dataset=unexpected_load))
    config = SimpleNamespace(
        dataset_name=configured_name,
        dataset_config_name="wikitext-2-raw-v1",
        revision=DATA_REVISION,
        cache_dir=None,
    )

    with pytest.raises(ValueError, match="local dataset reference.*content identity"):
        load_text_split(config, "train")
