"""Tests for the standalone baseline configuration contract."""

from pathlib import Path

import pytest

from torch_ddp_baselines.config import load_config


CONFIG_PATH = Path("torch_ddp_baselines/configs/gpt2_wikitext2_8n_500steps.yaml")


def test_500_step_experiment_config_is_complete_and_pinned() -> None:
    """The submitted experiment must resolve one immutable 8-node workload."""

    config = load_config(CONFIG_PATH)

    assert config.training.max_steps == 500
    assert config.distributed.world_size == 8
    assert config.distributed.backend == "nccl"
    assert config.distributed.periodic_average_interval == 100
    assert len(config.model.revision) == 40
    assert len(config.data.revision) == 40


def test_unknown_or_duplicated_config_surface_is_rejected(tmp_path: Path) -> None:
    """Strict parsing prevents old or alternate configuration spellings from surviving."""

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text(
        CONFIG_PATH.read_text(encoding="utf-8") + "legacy_mode: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown config keys"):
        load_config(malformed)
