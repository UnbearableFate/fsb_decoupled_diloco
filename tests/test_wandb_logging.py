from fs_diloco.core.config import resolve_config
from fs_diloco.observability.wandb_logging import (
    selected_update_summary,
    syncer_wandb_project_name,
    syncer_wandb_run_name,
)


def test_syncer_wandb_names_are_derived_from_config(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="cli_run_id_should_not_be_visible_name",
        shared_root=str(tmp_path / "run"),
        sqlite_local_dir=str(tmp_path / "db"),
        num_learners=2,
    )
    run_name = syncer_wandb_run_name(config, timestamp="20260709_120000")
    assert syncer_wandb_project_name(config) == "fs-diloco-miyabi-syncer"
    assert run_name.startswith("20260709_120000_")
    assert "cli_run_id_should_not_be_visible_name" not in run_name
    assert "_L2_q" in run_name
    assert "_outer-" in run_name


def test_selected_update_summary_skips_missing_and_nonfinite_values():
    summary = selected_update_summary(
        [
            {"base_global_version": 3, "train_loss": 2.0, "param_norm": 4.0},
            {"base_global_version": 4, "train_loss": None, "param_norm": float("nan")},
        ],
        current_version=5,
    )
    assert summary["selected/train_loss_mean"] == 2.0
    assert summary["selected/param_norm_mean"] == 4.0
    assert summary["selected/staleness_mean"] == 1.5
    assert summary["selected/staleness_max"] == 2.0
