import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.modeling.param_index import flatten_trainable_params
from fs_diloco.protocol.fragment_index import build_fragment_index
from fs_diloco.runtime import learner as learner_runtime
from fs_diloco.runtime.learner import (
    adopt_fragment_updates,
    load_fragment_latest_into_model,
)
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs


def _model_and_indexes():
    model = torch.nn.Module()
    model.register_parameter("a", torch.nn.Parameter(torch.zeros(4)))
    model.register_parameter("b", torch.nn.Parameter(torch.zeros(3)))
    model.register_parameter("c", torch.nn.Parameter(torch.zeros(3)))
    param_index = {
        "format_version": 1,
        "model_name_or_path": "tiny",
        "trainable_only": True,
        "total_numel": 10,
        "params": [
            {
                "name": "a",
                "shape": [4],
                "dtype": "torch.float32",
                "numel": 4,
                "offset": 0,
            },
            {
                "name": "b",
                "shape": [3],
                "dtype": "torch.float32",
                "numel": 3,
                "offset": 4,
            },
            {
                "name": "c",
                "shape": [3],
                "dtype": "torch.float32",
                "numel": 3,
                "offset": 7,
            },
        ],
    }
    return model, param_index, build_fragment_index(
        param_index, strategy="balanced_tensor", num_fragments=2
    )


def _latest(event, version, prefix):
    return {
        "latest_kind": "fragment",
        "global_merge_event": event,
        "version": event,
        "fragments": {
            "0": {"version": version, "weight_path": f"{prefix}-f0"},
            "1": {"version": version, "weight_path": f"{prefix}-f1"},
        },
    }


def test_fragment_initial_load_retries_one_consistent_new_snapshot(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    config = resolve_config("configs/fs_diloco_tiny_fragment_local.yaml")
    config.learner.prediction.reconcile_timeout_seconds = 0.01
    old_latest = _latest(1, 1, "old")
    new_latest = _latest(2, 2, "new")
    atomic_write_json(paths.latest_json, new_latest)
    model, param_index, fragment_index = _model_and_indexes()
    fragment_numels = {
        int(fragment["fragment_id"]): int(fragment["numel"])
        for fragment in fragment_index["fragments"]
    }
    calls = []

    def load(path):
        calls.append(path)
        if path.startswith("old"):
            raise FileNotFoundError(path)
        fragment_id = int(path[-1])
        offset = 0 if fragment_id == 0 else fragment_numels[0]
        return torch.arange(fragment_numels[fragment_id], dtype=torch.float32) + offset

    monkeypatch.setattr(learner_runtime, "load_fragment_weight", load)
    event, versions = load_fragment_latest_into_model(
        model=model,
        latest=old_latest,
        param_index=param_index,
        fragment_index=fragment_index,
        device=torch.device("cpu"),
        paths=paths,
        config=config,
    )

    assert event == 2
    assert versions == {0: 2, 1: 2}
    assert torch.equal(flatten_trainable_params(model, param_index), torch.arange(10.0))
    assert calls == ["old-f0", "new-f0", "new-f1"]


def test_fragment_incremental_retry_does_not_commit_mixed_versions(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    config = resolve_config("configs/fs_diloco_tiny_fragment_local.yaml")
    config.learner.prediction.reconcile_timeout_seconds = 0.01
    old_latest = _latest(1, 1, "old")
    new_latest = _latest(2, 2, "new")
    atomic_write_json(paths.latest_json, new_latest)
    model, param_index, fragment_index = _model_and_indexes()
    fragment_numels = {
        int(fragment["fragment_id"]): int(fragment["numel"])
        for fragment in fragment_index["fragments"]
    }
    calls = []

    def load(path):
        calls.append(path)
        if path == "old-f1":
            raise FileNotFoundError(path)
        base = 10 if path.startswith("old") else 20
        fragment_id = int(path[-1])
        value = base + fragment_id
        return torch.full((fragment_numels[fragment_id],), value, dtype=torch.float32)

    monkeypatch.setattr(learner_runtime, "load_fragment_weight", load)
    loaded_versions = {0: 0, 1: 0}
    event, versions, changed = adopt_fragment_updates(
        model=model,
        latest=old_latest,
        param_index=param_index,
        fragment_index=fragment_index,
        last_loaded_fragment_versions=loaded_versions,
        device=torch.device("cpu"),
        paths=paths,
        config=config,
    )

    assert event == 2
    assert versions is loaded_versions
    assert versions == {0: 2, 1: 2}
    assert changed == [0, 1]
    assert calls == ["old-f0", "old-f1", "new-f0", "new-f1"]
    assert set(flatten_trainable_params(model, param_index).tolist()) == {20.0, 21.0}
