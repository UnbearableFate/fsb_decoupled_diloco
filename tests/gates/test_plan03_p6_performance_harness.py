from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HARNESS = PROJECT_ROOT / "scripts/miyabi/plan03_p6_performance.py"


def _load_harness():
    specification = importlib.util.spec_from_file_location("plan03_p6_performance", HARNESS)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_dynamic_comparison_adds_omitted_static_terminal_defaults(tmp_path: Path) -> None:
    module = _load_harness()
    source = PROJECT_ROOT / "configs/fs_diloco_tiny_ha_static.yaml"
    original = source.read_bytes()

    configs = module._prepare_configs(
        current_root=PROJECT_ROOT,
        classic_root=None,
        scratch=tmp_path,
        comparison="dynamic",
    )

    baseline = yaml.safe_load(configs["baseline"].read_text(encoding="utf-8"))
    assert baseline["terminal"]["max_terminal_merges"] == 0
    assert source.read_bytes() == original


def test_frozen_classic_ref_records_tag_object_and_peeled_commit() -> None:
    module = _load_harness()

    identity = module._classic_ref_identity(PROJECT_ROOT)

    assert identity["ref"] == "archive/classic-full-v1-final"
    assert identity["object_type"] == "tag"
    assert identity["object_id"] != identity["commit"]
    object_type = subprocess.run(
        ["git", "cat-file", "-t", identity["commit"]],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert object_type == "commit"
