#!/usr/bin/env python3
"""Static inventory and frozen-evidence checker for Plan 03.

The checker performs no Torch/GPU work and is safe on a Miyabi login node when
run through the project environment. Runtime evidence remains a compute/PBS gate.
"""

from __future__ import annotations

import argparse
import ast
import copy
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
EXECUTABLE_SOURCE_SCOPES = (
    "fs_diloco",
    "configs",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)
ARCHIVE_TAGS = (
    "archive/classic-full-v1-final",
    "archive/fragment-v0-final",
)
BOUNDARY_COUNT_KEYS = (
    "bound_mutators",
    "fragment_enabled_configs",
    "fragment_pbs",
    "torch_baseline_configs",
    "torch_baseline_pbs",
    "torch_baseline_tests",
)
P1_BASELINE_COMPOSITION_MIGRATION = "fs_diloco/baselines/train.py"
P5_BASELINE_PROTOCOL_MIGRATION = "fs_diloco/baselines/protocol.py"
P5_BASELINE_PROTOCOL_SHA256 = "4919f2e26c8d6da22028f26d66286cff7f2f51004d230672bc51c0ad9bbc1fc9"
P5_ADDITIONAL_REMOVED_FULL_CONFIGS = frozenset(
    {"configs/fs_diloco_gpt2_wikitext2_8l_5000steps_terminal_capture.yaml"}
)
P6_ACCEPTANCE_CONFIG_PROJECTIONS: dict[str, dict[tuple[str, ...], Any]] = {
    "configs/fs_diloco_tiny_ha_static_acceptance.yaml": {
        ("data", "synthetic_num_batches"): 4096,
        ("sync", "stop_after_outer_steps"): 20,
        ("training", "inner_steps"): 60,
        ("training", "log_every_steps"): 60,
        ("wandb", "enabled"): False,
        ("syncer",): {
            "device": "cuda",
            "compute_dtype": "float32",
            "publish_dtype": "float32",
            "parallel_checkpoint_writes": True,
        },
    },
    "configs/fs_diloco_tiny_ha_dynamic_acceptance.yaml": {
        ("data", "synthetic_num_batches"): 16384,
        ("scaling", "learner_walltime"): "00:20:00",
        ("scaling", "learner_queue"): "regular-g",
        ("training", "inner_steps"): 60,
        ("training", "precision"): "bf16",
        ("training", "log_every_steps"): 60,
        ("io", "tensor_dtype"): "bfloat16",
        ("syncer",): {
            "device": "cpu",
            "compute_dtype": "float32",
            "publish_dtype": "bfloat16",
            "parallel_checkpoint_writes": True,
        },
    },
}
FROZEN_FULL_COMMIT = "a00a3d64a50f10a2478c3f4fe795e658d1b3b52f"
P5_REMOVED_SOURCE = (
    "fs_diloco/observability/metrics.py",
    "fs_diloco/protocol/admission_v4.py",
    "fs_diloco/protocol/control_v4.py",
    "fs_diloco/protocol/control_epoch.py",
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

# This retained P3 runtime result predates source-cleanliness fields. Its exact
# path is frozen and separately bound to the reviewed clean target; every newly
# produced runtime artifact must carry an explicit marker.
LEGACY_CLEAN_RUNTIME_EVIDENCE = frozenset(
    {
        "reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts/"
        "20260809-071821_p3-incremental-remediation-tests_pass.json"
    }
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository_files(root: Path, prefix: str, *, source_ref: str | None = None) -> list[str]:
    """List a commit tree, or the current cached plus untracked non-ignored worktree."""

    if source_ref is None:
        output = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", prefix)
    else:
        output = _git(root, "ls-tree", "-r", "--name-only", source_ref, "--", prefix)
    return sorted(
        line
        for line in output.splitlines()
        if line and (source_ref is not None or (root / line).exists())
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(root: Path, path: str, *, source_ref: str | None) -> str:
    if source_ref is None:
        return (root / path).read_text(encoding="utf-8")
    return subprocess.run(
        ["git", "show", f"{source_ref}:{path}"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")


def _content_sha256(root: Path, path: str, *, source_ref: str | None) -> str:
    if source_ref is None:
        return _sha256(root / path)
    content = subprocess.run(
        ["git", "show", f"{source_ref}:{path}"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(content).hexdigest()


def _literal_string_set(node: ast.expr) -> set[str] | None:
    candidate = node
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"set", "frozenset"}
        and len(node.args) == 1
        and not node.keywords
    ):
        candidate = node.args[0]
    try:
        value = ast.literal_eval(candidate)
    except (ValueError, TypeError):
        return None
    if isinstance(value, (set, frozenset)) and all(isinstance(item, str) for item in value):
        return set(value)
    return None


def _bound_mutators(root: Path, *, source_ref: str | None = None) -> list[str]:
    path = "fs_diloco/storage/fenced_store.py"
    if source_ref is None and not (root / path).is_file():
        return []
    tree = ast.parse(_read_text(root, path, source_ref=source_ref), filename=path)
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "_BOUND_MUTATORS" for target in targets
        ):
            value = _literal_string_set(node.value)
            if value is not None:
                return sorted(value)
            break
    raise RuntimeError("could not statically resolve _BOUND_MUTATORS")


def _fragment_enabled(content: str, *, path: str) -> bool:
    payload = yaml.safe_load(content)
    if not isinstance(payload, dict):
        raise RuntimeError(f"config is not an object: {path}")
    fragments = payload.get("fragments")
    if fragments is None:
        return False
    if not isinstance(fragments, dict):
        raise RuntimeError(f"fragments section is not an object: {path}")
    enabled = fragments.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RuntimeError(f"fragments.enabled is not boolean: {path}")
    return enabled


def inventory(root: Path, *, source_ref: str | None = None) -> dict[str, Any]:
    source = _repository_files(root, "fs_diloco", source_ref=source_ref)
    tests = [
        path
        for path in _repository_files(root, "tests", source_ref=source_ref)
        if Path(path).name.startswith("test_")
    ]
    configs = [
        path
        for path in _repository_files(root, "configs", source_ref=source_ref)
        if path.endswith((".yaml", ".yml"))
    ]
    pbs = [
        path
        for path in _repository_files(root, "scripts/miyabi", source_ref=source_ref)
        if path.endswith(".pbs")
    ]
    schemas = [path for path in source if path.endswith(".sql")]
    fragments = [
        path
        for path in configs
        if _fragment_enabled(_read_text(root, path, source_ref=source_ref), path=path)
    ]
    fragment_pbs = [
        path for path in pbs if "fragment" in Path(path).name and "no_fragment" not in path
    ]
    baseline_configs = [path for path in configs if Path(path).name.startswith("torch_baseline_")]
    baseline_pbs = [path for path in pbs if "torch_" in Path(path).name]
    baseline_tests = [path for path in tests if Path(path).name.startswith("test_torch_baseline_")]
    historical_config = "configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml"
    historical_pbs = "scripts/miyabi/run_9node_no_fragment_gpt2_wikitext2_50x10.pbs"
    recursive_anchor = "configs/5000/fs_diloco_gpt2_wikitext2_8l_200x25steps.yaml"
    if source_ref is not None and (historical_config not in configs or historical_pbs not in pbs):
        raise RuntimeError("historical full-control inventory boundary is missing")
    if recursive_anchor not in configs:
        raise RuntimeError("recursive config inventory anchor is missing")
    tag_targets: dict[str, str | None] = {}
    for tag in ARCHIVE_TAGS:
        try:
            tag_targets[tag] = _git(root, "rev-parse", f"{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            tag_targets[tag] = None
    files = source + tests + configs + pbs + schemas
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "status": "INVENTORY",
        "source_identity": {
            "branch": _git(root, "branch", "--show-current"),
            "commit": _git(root, "rev-parse", source_ref or "HEAD"),
            "archive_tag_targets": tag_targets,
        },
        "counts": {
            "source_files": len(source),
            "test_files": len(tests),
            "config_files": len(configs),
            "pbs_files": len(pbs),
            "schema_files": len(schemas),
            "bound_mutators": len(_bound_mutators(root, source_ref=source_ref)),
            "fragment_enabled_configs": len(fragments),
            "fragment_pbs": len(fragment_pbs),
            "torch_baseline_configs": len(baseline_configs),
            "torch_baseline_pbs": len(baseline_pbs),
            "torch_baseline_tests": len(baseline_tests),
        },
        "inventory": {
            "source": source,
            "tests": tests,
            "configs_recursive": configs,
            "pbs": pbs,
            "schemas": schemas,
            "bound_mutators": _bound_mutators(root, source_ref=source_ref),
        },
        "migration_boundaries": {
            "fragment_enabled_configs_delete_in_p5": fragments,
            "fragment_pbs_delete_in_p5": fragment_pbs,
            "historical_full_control_archive_separately": {
                "config": historical_config,
                "pbs": historical_pbs,
            },
            "torch_baseline_retain": {
                "configs": baseline_configs,
                "pbs": baseline_pbs,
                "tests": baseline_tests,
                "package": "fs_diloco/baselines",
            },
            "recursive_config_anchor": recursive_anchor,
        },
        "manifest_sha256": {
            path: _content_sha256(root, path, source_ref=source_ref) for path in sorted(set(files))
        },
    }


def verify_inventory(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    differences: list[str] = []
    comparisons = (
        (
            "source_identity.commit",
            actual["source_identity"]["commit"],
            expected["source_identity"]["commit"],
        ),
        (
            "source_identity.archive_tag_targets",
            actual["source_identity"]["archive_tag_targets"],
            expected["source_identity"]["archive_tag_targets"],
        ),
        ("counts", actual["counts"], expected["counts"]),
        ("inventory", actual["inventory"], expected["inventory"]),
        (
            "migration_boundaries",
            actual["migration_boundaries"],
            expected["migration_boundaries"],
        ),
        ("manifest_sha256", actual["manifest_sha256"], expected["manifest_sha256"]),
    )
    for label, actual_value, expected_value in comparisons:
        if actual_value != expected_value:
            differences.append(label)
    return differences


def _boundary_manifest(payload: dict[str, Any]) -> dict[str, str]:
    boundaries = payload["migration_boundaries"]
    # Full configs and the shared-schema marker in torch-baseline configs are
    # intentional P4 migration surfaces. Their semantics are checked against
    # the frozen source by ``verify_p4_migration_contracts`` below; hashing
    # them here would make the required migration impossible. Fragment,
    # historical-control, baseline PBS/test/code and archive-tag boundaries
    # remain byte-frozen until their owning phases.
    paths = {
        *boundaries["torch_baseline_retain"]["pbs"],
        *boundaries["torch_baseline_retain"]["tests"],
    }
    baseline_package = str(boundaries["torch_baseline_retain"]["package"]).rstrip("/") + "/"
    paths.update(
        path
        for path in payload["inventory"]["source"]
        if path.startswith(baseline_package)
        and path not in {P1_BASELINE_COMPOSITION_MIGRATION, P5_BASELINE_PROTOCOL_MIGRATION}
    )
    return {
        path: payload["manifest_sha256"][path]
        for path in sorted(paths)
        if path in payload["manifest_sha256"]
    }


def verify_p4_migration_contracts(root: Path, frozen_source_ref: str) -> list[str]:
    """Compare every retained config to its exact authorized P4 semantic migration."""

    from fs_diloco.core.config_v4 import migrate_v3_bytes_to_v4

    differences: list[str] = []
    frozen_configs = [
        path
        for path in _repository_files(root, "configs", source_ref=frozen_source_ref)
        if path.endswith((".yaml", ".yml"))
    ]
    current_configs = [
        path for path in _repository_files(root, "configs") if path.endswith((".yaml", ".yml"))
    ]

    def classify(path: str, payload: Any) -> str:
        if Path(path).name.startswith("torch_baseline_"):
            return "baseline"
        if path == "configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml":
            return "historical"
        if isinstance(payload, dict):
            fragments = payload.get("fragments")
            if isinstance(fragments, dict) and fragments.get("enabled") is True:
                return "fragment"
        return "full"

    frozen_payloads = {
        path: yaml.safe_load(_read_text(root, path, source_ref=frozen_source_ref)) or {}
        for path in frozen_configs
    }
    current_payloads = {
        path: yaml.safe_load(_read_text(root, path, source_ref=None)) or {}
        for path in current_configs
    }
    frozen_classes = {path: classify(path, payload) for path, payload in frozen_payloads.items()}
    current_classes = {path: classify(path, payload) for path, payload in current_payloads.items()}
    p5_dynamic_walltime_updates = {
        "configs/fs_diloco_tiny_ha_dynamic_2node.yaml": "00:10:00",
        "configs/fs_diloco_tiny_ha_dynamic_acceptance.yaml": "00:10:00",
    }

    def apply_projection(payload: dict[str, Any], projection: dict[tuple[str, ...], Any]) -> None:
        for path, value in projection.items():
            current: dict[str, Any] = payload
            for component in path[:-1]:
                nested = current.setdefault(component, {})
                if not isinstance(nested, dict):
                    raise RuntimeError(f"config projection crosses non-mapping field: {path}")
                current = nested
            current[path[-1]] = copy.deepcopy(value)

    for kind in ("full", "baseline", "fragment", "historical"):
        expected_paths = sorted(
            path
            for path, value in frozen_classes.items()
            if value == kind and path not in P5_ADDITIONAL_REMOVED_FULL_CONFIGS
        )
        if kind in {"fragment", "historical"}:
            expected_paths = []
        actual_paths = sorted(path for path, value in current_classes.items() if value == kind)
        if actual_paths != expected_paths:
            differences.append(f"config-migration.{kind}-path-inventory")

    for path, kind in sorted(frozen_classes.items()):
        if path not in current_payloads:
            continue
        frozen_payload = frozen_payloads[path]
        current_payload = current_payloads[path]
        if kind == "full":
            migrated_bytes, _report = migrate_v3_bytes_to_v4(
                _read_text(root, path, source_ref=frozen_source_ref).encode("utf-8")
            )
            expected_payload = yaml.safe_load(migrated_bytes) or {}
            if path in p5_dynamic_walltime_updates:
                expected_payload["scaling"]["learner_walltime"] = p5_dynamic_walltime_updates[path]
            if path in P6_ACCEPTANCE_CONFIG_PROJECTIONS:
                apply_projection(expected_payload, P6_ACCEPTANCE_CONFIG_PROJECTIONS[path])
            if current_payload != expected_payload:
                differences.append(f"config-migration.full-semantic:{path}")
        elif kind == "baseline":
            expected_payload = dict(frozen_payload)
            expected_payload["config_schema_version"] = 1
            if current_payload != expected_payload:
                differences.append(f"config-migration.baseline-semantic:{path}")

    retained_full_pbs = (
        "scripts/miyabi/run_1node_debug.pbs",
        "scripts/miyabi/run_2node_debug.pbs",
        "scripts/miyabi/run_2node_resume_regression.pbs",
        "scripts/miyabi/run_8node_colocated_gpt2_wikitext2_5000steps.pbs",
        "scripts/miyabi/run_9node_gpt2_wikitext2.pbs",
        "scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs",
        "scripts/miyabi/run_plan01_regression.pbs",
    )
    for path in retained_full_pbs:
        target = root / path
        if not target.is_file():
            differences.append(f"pbs-migration.missing:{path}")
            continue
        source = target.read_text(encoding="utf-8")
        if "#PBS -W group_list=xg24i002" not in source:
            differences.append(f"pbs-migration.literal-group:{path}")
        if path.endswith("run_2node_resume_regression.pbs"):
            if "fs_diloco.tools.init_run" not in source or "fs_diloco.syncer" not in source:
                differences.append(f"pbs-migration.v4-runtime:{path}")
        elif path.endswith("run_plan01_regression.pbs"):
            if "run_tiny_2proc_smoke.sh" not in source:
                differences.append(f"pbs-migration.v4-runtime:{path}")
        elif "run_v4_allocation.sh" not in source:
            differences.append(f"pbs-migration.v4-runtime:{path}")
    return differences


def verify_boundaries(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Compare retained migration boundaries, excluding planned P1 composition wiring."""
    differences: list[str] = []
    expected_counts = {key: expected["counts"][key] for key in BOUNDARY_COUNT_KEYS}
    expected_counts.update(bound_mutators=0, fragment_enabled_configs=0, fragment_pbs=0)
    expected_boundaries = copy.deepcopy(expected["migration_boundaries"])
    expected_boundaries["fragment_enabled_configs_delete_in_p5"] = []
    expected_boundaries["fragment_pbs_delete_in_p5"] = []
    comparisons = (
        (
            "source_identity.archive_tag_targets",
            actual["source_identity"]["archive_tag_targets"],
            expected["source_identity"]["archive_tag_targets"],
        ),
        (
            "boundary_counts",
            {key: actual["counts"][key] for key in BOUNDARY_COUNT_KEYS},
            expected_counts,
        ),
        (
            "inventory.bound_mutators",
            actual["inventory"]["bound_mutators"],
            [],
        ),
        (
            "migration_boundaries",
            actual["migration_boundaries"],
            expected_boundaries,
        ),
        ("boundary_manifest_sha256", _boundary_manifest(actual), _boundary_manifest(expected)),
    )
    for label, actual_value, expected_value in comparisons:
        if actual_value != expected_value:
            differences.append(label)
    if (
        actual["manifest_sha256"].get(P5_BASELINE_PROTOCOL_MIGRATION) != P5_BASELINE_PROTOCOL_SHA256
        and "boundary_manifest_sha256" not in differences
    ):
        differences.append("boundary_manifest_sha256")
    return differences


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(("." * node.level) + (node.module or ""))
    return modules


def verify_p5_contracts(root: Path, frozen: dict[str, Any]) -> list[str]:
    """Verify the exact P5 removal, compatibility, and dependency boundary."""

    differences: list[str] = []
    boundaries = frozen["migration_boundaries"]
    fragment_configs = tuple(boundaries["fragment_enabled_configs_delete_in_p5"])
    fragment_pbs = tuple(boundaries["fragment_pbs_delete_in_p5"])
    historical = boundaries["historical_full_control_archive_separately"]
    if len(fragment_configs) != 8:
        differences.append("deletion.fragment-config-frozen-count")
    if len(fragment_pbs) != 5:
        differences.append("deletion.fragment-pbs-frozen-count")
    for relative in (
        *P5_REMOVED_SOURCE,
        *fragment_configs,
        *fragment_pbs,
        historical["config"],
        historical["pbs"],
    ):
        if (root / relative).exists():
            differences.append(f"deletion.still-present:{relative}")

    for relative in (
        "fs_diloco/syncer.py",
        "fs_diloco/learner.py",
        "fs_diloco/analysis.py",
        "fs_diloco/eval_lm_harness.py",
        "fs_diloco/legacy/config_v1_v3.py",
        "fs_diloco/legacy/reader.py",
        "fs_diloco/legacy/fragment_v0.py",
        "fs_diloco/storage/admission.py",
        "fs_diloco/storage/control.py",
    ):
        if not (root / relative).is_file():
            differences.append(f"retained.missing:{relative}")

    for tag in ARCHIVE_TAGS:
        try:
            target = _git(root, "rev-parse", f"{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            target = None
        if target != FROZEN_FULL_COMMIT:
            differences.append(f"archive-tag.target:{tag}")

    for path in sorted((root / "configs").rglob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            differences.append(f"config.not-mapping:{path.relative_to(root)}")
            continue
        for key in ("init", "fragments", "failure_sim"):
            if key in payload:
                differences.append(f"config.removed-key:{path.relative_to(root)}:{key}")
        coordination = payload.get("coordination", {})
        if not isinstance(coordination, dict):
            differences.append(f"config.coordination-not-mapping:{path.relative_to(root)}")
        elif set(coordination) - {"leader"}:
            differences.append(f"config.removed-coordination:{path.relative_to(root)}")
        sync = payload.get("sync", {})
        if isinstance(sync, dict):
            if "stop_after_global_tokens" in sync:
                differences.append(f"config.ambiguous-token-stop:{path.relative_to(root)}")
            if "capture_terminal_predecessor_for_eval" in sync:
                differences.append(f"config.classic-terminal-capture:{path.relative_to(root)}")

    boundary_roots = {
        "protocol": root / "fs_diloco/protocol",
        "runtime": root / "fs_diloco/runtime",
        "baselines": root / "fs_diloco/baselines",
    }
    for area, base in boundary_roots.items():
        for path in sorted(base.glob("*.py")):
            modules = _imported_modules(path)
            relative = path.relative_to(root).as_posix()
            if area == "protocol" and any(
                module.startswith(("..runtime", "..storage", "pathlib")) for module in modules
            ):
                differences.append(f"imports.protocol-adapter:{relative}")
            if area == "runtime" and any("legacy" in module for module in modules):
                differences.append(f"imports.runtime-legacy:{relative}")
            if area == "baselines" and any("runtime" in module for module in modules):
                differences.append(f"imports.baseline-runtime:{relative}")

    for path in sorted((root / "fs_diloco/runtime").glob("*entrypoint.py")):
        source = path.read_text(encoding="utf-8").lower()
        relative = path.relative_to(root).as_posix()
        if "sqlite3" in _imported_modules(path):
            differences.append(f"entrypoint.sqlite:{relative}")
        if any(command in source for command in ("qsub", "qstat")):
            differences.append(f"entrypoint.scheduler-command:{relative}")
        if any(statement in source for statement in ("select ", "insert ", "update ", "delete ")):
            differences.append(f"entrypoint.sql:{relative}")

    for area in ("runtime", "storage", "protocol", "baselines"):
        for path in sorted((root / "fs_diloco" / area).glob("*.py")):
            if "fragment" in path.read_text(encoding="utf-8").lower():
                differences.append(f"writer.fragment-symbol:{path.relative_to(root)}")

    ddl = "\n".join(
        (root / relative).read_text(encoding="utf-8").lower()
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
        if f"create table {table}" in ddl:
            differences.append(f"ddl.fragment-table:{table}")

    legacy_reader = (root / "fs_diloco/legacy/reader.py").read_text(encoding="utf-8")
    normalized_reader = legacy_reader.lower().replace(" ", "")
    if "mode=ro" not in normalized_reader or "query_only=on" not in normalized_reader:
        differences.append("legacy.reader-not-query-only")
    for table in (
        "fragment_proposal_frontiers",
        "fragments",
        "fragment_versions",
        "fragment_updates",
    ):
        if table not in legacy_reader:
            differences.append(f"legacy.reader-missing-table:{table}")
    for relative in (
        "fs_diloco/tools/eval_lm_harness.py",
        "fs_diloco/tools/validation_eval.py",
        "fs_diloco/tools/publish_quality_gate.py",
    ):
        if "load_query_config_snapshot" not in (root / relative).read_text(encoding="utf-8"):
            differences.append(f"legacy.query-config-not-used:{relative}")
    return differences


def _declared_requirements(root: Path, prefix: str) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    base = root / prefix
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in tree.body:
            targets: list[ast.expr]
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "PLAN03_REQUIREMENTS"
                for target in targets
            ):
                continue
            declared = _literal_string_set(node.value)
            if declared is None or not declared:
                raise RuntimeError(f"{relative}: PLAN03_REQUIREMENTS must be a literal string set")
            for requirement in declared:
                owners.setdefault(requirement, []).append(relative)
            break
    return {key: sorted(paths) for key, paths in sorted(owners.items())}


def _evidence_source_matches_target(root: Path, source: Any, target: str | None) -> bool:
    if target is None:
        return True
    if not isinstance(source, str) or not source:
        return False
    if source == target:
        return True
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source, target],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode != 0:
        return False
    relevant_tree = (
        "fs_diloco",
        "tests",
        "scripts",
        "configs",
        "pyproject.toml",
        "uv.lock",
        "main.py",
    )
    difference = subprocess.run(
        ["git", "diff", "--quiet", source, target, "--", *relevant_tree],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return difference.returncode == 0


def verify_phase_requirements(
    root: Path,
    matrix_path: Path,
    phase: str,
    *,
    expected_source_commit: str | None = None,
    excluded_evidence_path: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bind every phase requirement to implementation, tests, and retained evidence."""

    with matrix_path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row["phase"] == phase]
    if not rows:
        raise RuntimeError(f"requirement matrix has no rows for phase: {phase}")
    identifiers = [row["invariant_id"] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise RuntimeError(f"requirement matrix has duplicate IDs in phase: {phase}")
    implementation = _declared_requirements(root, "fs_diloco")
    tests = _declared_requirements(root, "tests")
    checks: dict[str, Any] = {}
    differences: list[str] = []
    source_match_cache: dict[str | None, bool] = {}
    for row in rows:
        requirement = row["invariant_id"]
        implementation_owners = implementation.get(requirement, [])
        test_owners = tests.get(requirement, [])
        evidence_paths = [item.strip() for item in row["evidence_path"].split(";") if item.strip()]
        artifact_contracts = [
            item.strip() for item in row["artifact_contract"].split(";") if item.strip()
        ]
        checker_contract = f"checker requirements.{requirement}"
        requirement_differences: list[str] = []
        if row["status"] != "complete":
            requirement_differences.append("status")
        if checker_contract not in artifact_contracts:
            requirement_differences.append("artifact-contract")
        if not implementation_owners:
            requirement_differences.append("implementation")
        if not test_owners:
            requirement_differences.append("tests")
        if not evidence_paths or evidence_paths == ["TBD"]:
            requirement_differences.append("evidence")
        else:
            missing = [
                item
                for item in evidence_paths
                if item != excluded_evidence_path and not (root / item).exists()
            ]
            if missing:
                requirement_differences.extend(f"missing-evidence:{item}" for item in missing)
        structured_evidence: list[str] = []
        for item in evidence_paths:
            if item == excluded_evidence_path:
                continue
            path = root / item
            if path.suffix != ".json" or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            requirement_payload = (
                payload.get("checks", {}).get("requirements", {}).get(requirement, {})
                if isinstance(payload, dict)
                else {}
            )
            source_commit = payload.get("source_commit")
            if source_commit is None and isinstance(payload.get("checks"), dict):
                source_commit = payload["checks"].get("requirements_source_commit")
            cache_key = source_commit if isinstance(source_commit, str) else None
            if cache_key not in source_match_cache:
                source_match_cache[cache_key] = _evidence_source_matches_target(
                    root, source_commit, expected_source_commit
                )
            source_matches = source_match_cache[cache_key]
            covered_requirements = payload.get("requirements_covered", [])
            dirty_markers: list[Any] = []
            if "git_dirty" in payload:
                dirty_markers.append(payload["git_dirty"])
            for container_name in ("source_identity", "identity"):
                container = payload.get(container_name)
                if isinstance(container, dict) and "git_dirty" in container:
                    dirty_markers.append(container["git_dirty"])
            runtime_source_is_clean = (
                bool(dirty_markers) and all(marker is False for marker in dirty_markers)
            ) or (not dirty_markers and item in LEGACY_CLEAN_RUNTIME_EVIDENCE)
            runtime_evidence_pass = (
                payload.get("status") == "PASS"
                and isinstance(covered_requirements, list)
                and requirement in covered_requirements
                and runtime_source_is_clean
            )
            if source_matches and (
                runtime_evidence_pass
                or (
                    isinstance(requirement_payload, dict)
                    and requirement_payload.get("status") == "PASS"
                )
            ):
                structured_evidence.append(item)
        if checker_contract in artifact_contracts and not structured_evidence:
            requirement_differences.append("structured-checker-evidence")
        checks[requirement] = {
            "status": "PASS" if not requirement_differences else "BLOCKED",
            "implementation_owners": implementation_owners,
            "test_owners": test_owners,
            "evidence_paths": evidence_paths,
            "structured_evidence_paths": structured_evidence,
            "differences": requirement_differences,
        }
        differences.extend(
            f"requirements.{requirement}.{difference}" for difference in requirement_differences
        )
    return checks, differences


def verify_p3_operational_contracts(root: Path) -> list[str]:
    """Guard the cross-file scheduler/initializer invariants found during P3 review."""

    differences: list[str] = []
    authority = (root / "fs_diloco/storage/authority.py").read_text(encoding="utf-8")
    capacity = (root / "fs_diloco/runtime/services/dynamic_capacity.py").read_text(encoding="utf-8")
    initializer = (root / "fs_diloco/storage/run_initializer.py").read_text(encoding="utf-8")
    schema = (root / "fs_diloco/storage/schema_v4.sql").read_text(encoding="utf-8")
    dynamic_schema = (root / "fs_diloco/storage/schema_v4_dynamic.sql").read_text(encoding="utf-8")
    if "COALESCE(uncertainty_deadline, ?)" not in authority:
        differences.append("scheduler.deadline-not-first-write-wins")
    if (
        "first_uncertain_at=CASE WHEN ? THEN NULL" not in authority
        or "uncertainty_deadline=CASE WHEN ? THEN NULL" not in authority
    ):
        differences.append("scheduler.positive-evidence-does-not-rearm-deadline")
    if (
        "SchedulerOperatorAction.MARK_FAILED" not in authority
        or "reservation_released_at=COALESCE(reservation_released_at, ?)" not in authority
    ):
        differences.append("scheduler.manual-review-reservation-has-no-release-path")
    if (
        "reservation_released_at IS NULL" not in authority
        or "reservation_released_at REAL" not in dynamic_schema
    ):
        differences.append("scheduler.reservation-accounting-not-tombstone-based")
    if (
        'state == "submission_unknown"' not in capacity
        or 'state="terminal_uncertain"' not in capacity
        or 'state="manual_review"' not in capacity
    ):
        differences.append("scheduler.no-job-deadline-path-missing")
    validator = ast.parse(initializer, filename="fs_diloco/storage/run_initializer.py")
    validate_function = next(
        (
            node
            for node in validator.body
            if isinstance(node, ast.FunctionDef) and node.name == "_validate_completed_run"
        ),
        None,
    )
    if validate_function is None:
        differences.append("initializer.validator-missing")
    else:
        recursive_scans = [
            node
            for node in ast.walk(validate_function)
            if isinstance(node, ast.Attribute) and node.attr == "rglob"
        ]
        if recursive_scans:
            differences.append("initializer.startup-recursive-scan")
    if "claimed_by_epoch INTEGER" not in schema or "claimed_at REAL" not in schema:
        differences.append("audit.gc-claim-ownership-missing")
    if "reservation_released_at REAL" not in dynamic_schema:
        differences.append("scheduler.v4-reservation-tombstone-missing")
    if "candidate_launch_outbox" in schema or "candidate_launch_outbox" in authority:
        differences.append("scheduler.deleted-candidate-outbox-remains")
    return differences


def verify_tracked_evidence(root: Path, matrix_path: Path) -> list[str]:
    differences: list[str] = []
    with matrix_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["status"] == "pending":
            continue
        for evidence in (item.strip() for item in row["evidence_path"].split(";")):
            if not evidence or evidence == "TBD":
                continue
            try:
                tracked = _git(root, "ls-files", "--error-unmatch", "--", evidence)
            except subprocess.CalledProcessError:
                differences.append(f"{row['invariant_id']}:{evidence}:not-tracked")
                continue
            tracked_paths = tracked.splitlines()
            directory_evidence = evidence.endswith("/")
            if (
                not tracked_paths
                or (
                    directory_evidence
                    and not all(path.startswith(evidence) for path in tracked_paths)
                )
                or (not directory_evidence and tracked_paths != [evidence])
            ):
                differences.append(f"{row['invariant_id']}:{evidence}:ambiguous")
    return differences


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--inventory-output", type=Path)
    parser.add_argument("--output", type=Path, help="alias for --inventory-output")
    parser.add_argument("--source-ref")
    parser.add_argument("--phase", help="matrix phase ID for the unified phase checker")
    parser.add_argument("--mode", choices=("staged", "completed"))
    parser.add_argument(
        "--verification-target-ref",
        help="commit that structured phase evidence must attest (defaults to HEAD)",
    )
    parser.add_argument("--expect", type=Path)
    parser.add_argument(
        "--verify-boundaries",
        action="store_true",
        help="also compare the current tree migration boundary against --expect",
    )
    parser.add_argument(
        "--require-tracked-evidence",
        action="store_true",
        help="phase-final gate: require every non-pending matrix evidence path in Git",
    )
    parser.add_argument(
        "--verify-phase-requirements",
        metavar="PHASE_ID",
        help="require complete implementation/test/evidence bindings for one matrix phase",
    )
    parser.add_argument(
        "--verify-p3-operational-contracts",
        action="store_true",
        help="verify reviewed P3 cross-file contracts without requiring retained phase evidence",
    )
    parser.add_argument(
        "--verify-p5-contracts",
        action="store_true",
        help="verify the current classic/fragment removal and legacy-reader boundary",
    )
    args = parser.parse_args()
    if args.output is not None:
        if args.inventory_output is not None:
            parser.error("--output and --inventory-output are aliases and cannot be combined")
        args.inventory_output = args.output
    if (args.phase is None) != (args.mode is None):
        parser.error("--phase and --mode must be supplied together")
    if args.phase is not None:
        if args.verify_phase_requirements is not None:
            parser.error("--phase cannot be combined with --verify-phase-requirements")
        args.verify_phase_requirements = args.phase
        if args.mode == "completed":
            args.require_tracked_evidence = True
        if args.expect is None:
            args.expect = (
                args.root.resolve()
                / "reports/DOING/fsb_decoupled_diloco_plan_03_unified_ha/artifacts"
                / "20260809-003335_p0-current-boundary-check_review.json"
            )
            args.verify_boundaries = True
    root = args.root.resolve()
    try:
        expected = None
        source_ref = args.source_ref
        if args.expect is not None:
            expected = json.loads(args.expect.resolve().read_text(encoding="utf-8"))
        frozen_source_ref = source_ref or (
            str(expected["source_identity"]["commit"]) if expected is not None else None
        )
        payload = inventory(root, source_ref=frozen_source_ref)
        if expected is not None:
            differences = verify_inventory(payload, expected)
            checks: dict[str, Any] = {
                "frozen_inventory": {
                    "source_ref": frozen_source_ref,
                    "differences": list(differences),
                }
            }
            if args.verify_p5_contracts:
                p5_differences = verify_p5_contracts(root, expected)
                checks["p5_contracts"] = {"differences": p5_differences}
                differences.extend(f"p5_contracts.{item}" for item in p5_differences)
            if args.verify_boundaries:
                current = inventory(root, source_ref=args.source_ref)
                boundary_differences = verify_boundaries(current, expected)
                checks["current_migration_boundaries"] = {
                    "source_ref": args.source_ref or "TRACKED_WORKTREE",
                    "source_commit": current["source_identity"]["commit"],
                    "differences": boundary_differences,
                }
                differences.extend(
                    f"current_migration_boundaries.{difference}"
                    for difference in boundary_differences
                )
                migration_differences = verify_p4_migration_contracts(
                    root, str(expected["source_identity"]["commit"])
                )
                checks["p4_migration_contracts"] = {
                    "frozen_source_ref": str(expected["source_identity"]["commit"]),
                    "differences": migration_differences,
                }
                differences.extend(
                    f"p4_migration_contracts.{difference}" for difference in migration_differences
                )
            if args.require_tracked_evidence:
                matrix_path = root / "plans/DOING/plans" / f"{PLAN_ID}-requirement-matrix.csv"
                evidence_differences = verify_tracked_evidence(root, matrix_path)
                checks["tracked_evidence"] = {"differences": evidence_differences}
                differences.extend(
                    f"tracked_evidence.{difference}" for difference in evidence_differences
                )
            if args.verify_phase_requirements:
                matrix_path = root / "plans/DOING/plans" / f"{PLAN_ID}-requirement-matrix.csv"
                verification_target_commit = _git(
                    root, "rev-parse", args.verification_target_ref or "HEAD"
                )
                excluded_evidence_path = None
                if args.inventory_output is not None:
                    try:
                        excluded_evidence_path = (
                            args.inventory_output.resolve().relative_to(root).as_posix()
                        )
                    except ValueError:
                        excluded_evidence_path = None
                requirement_checks, requirement_differences = verify_phase_requirements(
                    root,
                    matrix_path,
                    args.verify_phase_requirements,
                    expected_source_commit=verification_target_commit,
                    excluded_evidence_path=excluded_evidence_path,
                )
                checks["requirements"] = requirement_checks
                checks["requirements_source_commit"] = verification_target_commit
                differences.extend(requirement_differences)
                if args.verify_phase_requirements == "P3-operational-robustness":
                    operational_differences = verify_p3_operational_contracts(root)
                    checks["p3_operational_contracts"] = {"differences": operational_differences}
                    differences.extend(
                        f"p3_operational_contracts.{difference}"
                        for difference in operational_differences
                    )
            elif args.verify_p3_operational_contracts:
                operational_differences = verify_p3_operational_contracts(root)
                checks["p3_operational_contracts"] = {"differences": operational_differences}
                differences.extend(
                    f"p3_operational_contracts.{difference}"
                    for difference in operational_differences
                )
            payload["status"] = "PASS" if not differences else "BLOCKED"
            payload["differences"] = differences
            payload["checks"] = checks
            if args.phase is not None:
                current = inventory(root)
                payload.update(
                    {
                        "phase_id": args.phase,
                        "mode": args.mode,
                        "source_identity": {
                            "git_commit": current["source_identity"]["commit"],
                            "git_dirty": bool(
                                _git(
                                    root,
                                    "status",
                                    "--short",
                                    "--untracked-files=all",
                                    "--",
                                    *EXECUTABLE_SOURCE_SCOPES,
                                )
                            ),
                        },
                        "environment": {
                            "pbs_job_id": os.environ.get("PBS_JOBID"),
                            "python": sys.version.split()[0],
                        },
                        "schema_identity": {
                            "authority_schema_sha256": _sha256(
                                root / "fs_diloco/storage/schema_v4.sql"
                            ),
                            "dynamic_schema_sha256": _sha256(
                                root / "fs_diloco/storage/schema_v4_dynamic.sql"
                            ),
                        },
                        "metrics": {
                            "requirement_count": len(checks.get("requirements", {})),
                            "difference_count": len(differences),
                        },
                        "errors": list(differences),
                        "evidence_paths": sorted(
                            {
                                path
                                for item in checks.get("requirements", {}).values()
                                for path in item.get("structured_evidence_paths", [])
                            }
                        ),
                    }
                )
    except (OSError, RuntimeError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        payload = {
            "artifact_version": 1,
            "plan_id": PLAN_ID,
            "status": "BLOCKED",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if args.inventory_output is not None:
        _atomic_write_json(args.inventory_output.resolve(), payload)
    if args.expect is not None:
        print(payload["status"])
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if payload["status"] == "BLOCKED":
        sys.exit(1)


if __name__ == "__main__":
    main()
