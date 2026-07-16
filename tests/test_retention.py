import json

from fs_diloco.paths import RunPaths
from fs_diloco.retention import cleanup_global_artifacts, cleanup_learner_update_artifacts


def test_cleanup_global_artifacts_keeps_newest_versions(tmp_path):
    paths = RunPaths(tmp_path)
    paths.weights.mkdir(parents=True)
    paths.optim.mkdir(parents=True)
    for version in range(5):
        paths.global_weight_path(version).write_text("weight")
        paths.outer_optim_path(version).write_text("optim")

    deleted = cleanup_global_artifacts(paths, keep_last=3)

    assert deleted == 4
    assert sorted(path.name for path in paths.weights.glob("*.safetensors")) == [
        "global_v000002.safetensors",
        "global_v000003.safetensors",
        "global_v000004.safetensors",
    ]
    assert sorted(path.name for path in paths.optim.glob("*.safetensors")) == [
        "outer_v000002.safetensors",
        "outer_v000003.safetensors",
        "outer_v000004.safetensors",
    ]


def test_cleanup_learner_update_artifacts_keeps_newest_pairs(tmp_path):
    update_dir = tmp_path / "updates" / "pending" / "learner_000"
    update_dir.mkdir(parents=True)
    for step in range(100, 600, 100):
        tensor_path = update_dir / f"update_{step}.params.safetensors"
        meta_path = update_dir / f"update_{step}.meta.json"
        tensor_path.write_text("tensor")
        meta_path.write_text(
            json.dumps(
                {
                    "local_step_end": step,
                    "committed_at": float(step),
                    "file_path": str(tensor_path),
                }
            )
        )

    deleted = cleanup_learner_update_artifacts(update_dir, keep_last=3)

    assert deleted == 4
    assert sorted(path.name for path in update_dir.glob("*.meta.json")) == [
        "update_300.meta.json",
        "update_400.meta.json",
        "update_500.meta.json",
    ]
    assert sorted(path.name for path in update_dir.glob("*.params.safetensors")) == [
        "update_300.params.safetensors",
        "update_400.params.safetensors",
        "update_500.params.safetensors",
    ]
