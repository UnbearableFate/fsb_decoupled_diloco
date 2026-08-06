from types import SimpleNamespace

import pytest
import torch

from fs_diloco.baselines.artifacts import BaselineRunPaths, initialize_run_root
from fs_diloco.baselines.train import _save_final_checkpoint
from fs_diloco.core.config import resolve_config
from fs_diloco.modeling import hf_data


def _runtimes(size: int):
    return [
        {
            "rank": rank,
            "world_size": size,
            "local_rank": 0,
            "hostname": f"host-{rank}",
            "backend": "gloo",
            "device": "cpu",
            "device_type": "cpu",
        }
        for rank in range(size)
    ]


def test_formal_and_smoke_baseline_configs_resolve():
    formal = resolve_config("configs/torch_baseline_gpt2_wikitext2_8n_5000steps.yaml")
    smoke = resolve_config("configs/torch_baseline_tiny_2rank.yaml")

    assert formal.torch_baseline.enabled
    assert formal.torch_baseline.backend == "nccl"
    assert formal.sync.num_learners == 8
    assert formal.training.max_local_steps == 5000
    assert formal.training.inner_steps == 100
    assert formal.inner_optimizer.scheduler_total_steps == 5000
    assert smoke.torch_baseline.backend == "gloo"
    assert not smoke.torch_baseline.require_distinct_hosts


def test_existing_training_manifest_or_metrics_refuses_overwrite(tmp_path):
    config = resolve_config(
        "configs/torch_baseline_tiny_2rank.yaml",
        run_id="exclusive",
        shared_root=str(tmp_path),
    )
    paths = BaselineRunPaths(tmp_path)
    initialize_run_root(
        paths,
        config=config,
        mode="ddp",
        backend="gloo",
        max_steps=4,
        average_interval=2,
        runtimes=_runtimes(2),
    )

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        initialize_run_root(
            paths,
            config=config,
            mode="ddp",
            backend="gloo",
            max_steps=4,
            average_interval=2,
            runtimes=_runtimes(2),
        )


def test_existing_source_identity_must_match_launcher_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FS_DILOCO_GIT_COMMIT", "expected-commit")
    monkeypatch.setenv("FS_DILOCO_GIT_DIRTY", "0")
    monkeypatch.setenv("FS_DILOCO_SOURCE_FINGERPRINT", "sha256:expected")
    config = resolve_config(
        "configs/torch_baseline_tiny_2rank.yaml",
        run_id="source-mismatch",
        shared_root=str(tmp_path),
    )
    paths = BaselineRunPaths(tmp_path)
    paths.source_identity.write_text(
        '{"git_commit":"different","git_dirty":false,'
        '"source_fingerprint":"sha256:different"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match runtime"):
        initialize_run_root(
            paths,
            config=config,
            mode="ddp",
            backend="gloo",
            max_steps=4,
            average_interval=2,
            runtimes=_runtimes(2),
        )


class DummyDataset:
    def __init__(self, values):
        self.values = list(values)

    def shard(self, *, num_shards, index, contiguous):
        assert contiguous is True
        shard_size = len(self.values) // num_shards
        start = index * shard_size
        end = len(self.values) if index + 1 == num_shards else start + shard_size
        return DummyDataset(self.values[start:end])

    def __iter__(self):
        return iter({"text": str(value)} for value in self.values)


class NumericTokenizer:
    eos_token_id = None

    def __call__(self, text, add_special_tokens):
        assert add_special_tokens is False
        return {"input_ids": [int(text)]}


def test_rank_data_shards_are_deterministic_and_nonoverlapping(monkeypatch):
    data_config = SimpleNamespace(
        train_split="train",
        shuffle_blocks=False,
    )
    monkeypatch.setattr(
        hf_data,
        "load_text_split",
        lambda _config, _split: DummyDataset(range(8)),
    )
    rank_batches = []
    for rank in range(2):
        iterator = hf_data.wikitext_batches(
            data_config,
            NumericTokenizer(),
            learner_index=rank,
            num_learners=2,
            micro_batch_size=1,
            block_size=1,
            seed=1337,
        )
        rank_batches.append([int(next(iterator).input_ids.item()) for _ in range(4)])

    assert rank_batches == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert set(rank_batches[0]).isdisjoint(rank_batches[1])


class SaveableModel(torch.nn.Module):
    def save_pretrained(self, path, *, safe_serialization):
        assert safe_serialization is True
        (path / "model.safetensors").write_bytes(b"model")


class SaveableTokenizer:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def save_pretrained(self, path):
        if self.fail:
            raise RuntimeError("injected tokenizer save failure")
        (path / "tokenizer.json").write_text("{}", encoding="utf-8")


def test_final_checkpoint_is_published_from_staging(tmp_path):
    paths = BaselineRunPaths(tmp_path)
    paths.final_checkpoint.parent.mkdir(parents=True)

    _save_final_checkpoint(paths, SaveableModel(), SaveableTokenizer())

    assert (paths.final_checkpoint / "model.safetensors").read_bytes() == b"model"
    assert (paths.final_checkpoint / "tokenizer.json").read_text(encoding="utf-8") == "{}"
    assert not list(paths.final_checkpoint.parent.glob(".final.*"))


def test_final_checkpoint_failure_cleans_staging(tmp_path):
    paths = BaselineRunPaths(tmp_path)
    paths.final_checkpoint.parent.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="injected tokenizer save failure"):
        _save_final_checkpoint(
            paths,
            SaveableModel(),
            SaveableTokenizer(fail=True),
        )

    assert not paths.final_checkpoint.exists()
    assert not list(paths.final_checkpoint.parent.glob(".final.*"))
