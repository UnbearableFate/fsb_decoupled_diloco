from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]

REMOVED_SOURCE = (
    "fs_diloco/observability/metrics.py",
    "fs_diloco/protocol/admission_v4.py",
    "fs_diloco/protocol/control_epoch.py",
    "fs_diloco/protocol/control_v4.py",
    "fs_diloco/protocol/dynamic_terminal.py",
    "fs_diloco/protocol/fragment_codec.py",
    "fs_diloco/protocol/fragment_index.py",
    "fs_diloco/protocol/fragment_scheduler.py",
    "fs_diloco/protocol/liveness.py",
    "fs_diloco/protocol/membership.py",
    "fs_diloco/runtime/failure_sim.py",
    "fs_diloco/runtime/launch_outbox.py",
    "fs_diloco/runtime/learner.py",
    "fs_diloco/runtime/syncer.py",
    "fs_diloco/runtime/syncer_ha.py",
    "fs_diloco/storage/fenced_store.py",
    "fs_diloco/storage/maintenance.py",
    "fs_diloco/storage/schema.sql",
    "fs_diloco/storage/schema_bootstrap.py",
    "fs_diloco/storage/sqlite_store.py",
)

FRAGMENT_CONFIGS = (
    "configs/fs_diloco_gpt2_wikitext2_1l_fragment_debug.yaml",
    "configs/fs_diloco_gpt2_wikitext2_8l_fragment_5000steps.yaml",
    "configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x10.yaml",
    "configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x4.yaml",
    "configs/fs_diloco_tiny_fragment_local.yaml",
    "configs/fs_diloco_tiny_fragment_terminal_local.yaml",
    "configs/fs_diloco_tiny_scheduler_fragment_local.yaml",
    "configs/fs_diloco_tiny_watchdog_fragment_local.yaml",
)

FRAGMENT_PBS = (
    "scripts/miyabi/run_1node_fragment_debug.pbs",
    "scripts/miyabi/run_2node_fragment_debug.pbs",
    "scripts/miyabi/run_9node_fragment_gpt2_wikitext2_5000steps.pbs",
    "scripts/miyabi/run_9node_fragment_gpt2_wikitext2_50x10.pbs",
    "scripts/miyabi/run_9node_fragment_gpt2_wikitext2_50x4.pbs",
)

HISTORICAL_CONTROL = (
    "configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml",
    "scripts/miyabi/run_9node_no_fragment_gpt2_wikitext2_50x10.pbs",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add(("." * node.level) + (node.module or ""))
    return result


def test_classic_and_fragment_production_surfaces_are_deleted() -> None:
    assert len(FRAGMENT_CONFIGS) == 8
    assert len(FRAGMENT_PBS) == 5
    for relative in (*REMOVED_SOURCE, *FRAGMENT_CONFIGS, *FRAGMENT_PBS, *HISTORICAL_CONTROL):
        assert not (ROOT / relative).exists(), relative


def test_retained_configs_cannot_express_removed_runtime_modes() -> None:
    for path in sorted((ROOT / "configs").rglob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert "fragments" not in payload, path
        assert "init" not in payload, path
        coordination = payload.get("coordination", {})
        assert "syncer_ha" not in coordination, path


def test_runtime_and_baselines_have_no_legacy_or_classic_dependency() -> None:
    for path in sorted((ROOT / "fs_diloco/runtime").glob("*.py")):
        imported = _imports(path)
        assert not any("legacy" in item for item in imported), path
        assert not any("sqlite_store" in item or "fenced_store" in item for item in imported), path
    for path in sorted((ROOT / "fs_diloco/baselines").glob("*.py")):
        assert not any("runtime" in item for item in _imports(path)), path
    for path in sorted((ROOT / "fs_diloco/protocol").glob("*.py")):
        imported = _imports(path)
        assert not any(
            item.startswith(("..runtime", "..storage", "pathlib")) for item in imported
        ), path


def test_v4_ddl_has_no_fragment_v0_tables() -> None:
    ddl = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8").lower()
        for relative in (
            "fs_diloco/storage/schema_v4.sql",
            "fs_diloco/storage/schema_v4_dynamic.sql",
        )
    )
    for table in (
        "fragment_proposal_frontiers",
        "fragments",
        "fragment_versions",
        "fragment_updates",
    ):
        assert f"create table {table}" not in ddl


def test_archive_tags_are_immutable_and_share_the_frozen_full_commit() -> None:
    targets = {
        tag: subprocess.run(
            ["git", "rev-parse", f"{tag}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for tag in ("archive/classic-full-v1-final", "archive/fragment-v0-final")
    }
    assert set(targets.values()) == {"a00a3d64a50f10a2478c3f4fe795e658d1b3b52f"}
