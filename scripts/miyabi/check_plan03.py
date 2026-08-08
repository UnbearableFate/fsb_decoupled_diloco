#!/usr/bin/env python3
"""Static inventory and frozen-evidence checker for Plan 03.

The checker performs no Torch/GPU work and is safe on a Miyabi login node when
run through the project environment. Runtime evidence remains a compute/PBS gate.
"""

from __future__ import annotations

import argparse
import ast
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


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tracked(root: Path, prefix: str, *, source_ref: str | None = None) -> list[str]:
    if source_ref is None:
        output = _git(root, "ls-files", prefix)
    else:
        output = _git(root, "ls-tree", "-r", "--name-only", source_ref, "--", prefix)
    return sorted(line for line in output.splitlines() if line)


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
    source = _tracked(root, "fs_diloco", source_ref=source_ref)
    tests = [
        path
        for path in _tracked(root, "tests", source_ref=source_ref)
        if Path(path).name.startswith("test_")
    ]
    configs = [
        path
        for path in _tracked(root, "configs", source_ref=source_ref)
        if path.endswith((".yaml", ".yml"))
    ]
    pbs = [
        path
        for path in _tracked(root, "scripts/miyabi", source_ref=source_ref)
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
    if historical_config not in configs or historical_pbs not in pbs:
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
    paths = {
        *boundaries["fragment_enabled_configs_delete_in_p5"],
        *boundaries["fragment_pbs_delete_in_p5"],
        boundaries["historical_full_control_archive_separately"]["config"],
        boundaries["historical_full_control_archive_separately"]["pbs"],
        *boundaries["torch_baseline_retain"]["configs"],
        *boundaries["torch_baseline_retain"]["pbs"],
        *boundaries["torch_baseline_retain"]["tests"],
        boundaries["recursive_config_anchor"],
        "fs_diloco/storage/fenced_store.py",
    }
    baseline_package = str(boundaries["torch_baseline_retain"]["package"]).rstrip("/") + "/"
    paths.update(
        path for path in payload["inventory"]["source"] if path.startswith(baseline_package)
    )
    return {path: payload["manifest_sha256"][path] for path in sorted(paths)}


def verify_boundaries(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Compare only migration boundaries that must remain frozen through P0-P4."""
    differences: list[str] = []
    comparisons = (
        (
            "source_identity.archive_tag_targets",
            actual["source_identity"]["archive_tag_targets"],
            expected["source_identity"]["archive_tag_targets"],
        ),
        (
            "boundary_counts",
            {key: actual["counts"][key] for key in BOUNDARY_COUNT_KEYS},
            {key: expected["counts"][key] for key in BOUNDARY_COUNT_KEYS},
        ),
        (
            "inventory.bound_mutators",
            actual["inventory"]["bound_mutators"],
            expected["inventory"]["bound_mutators"],
        ),
        (
            "migration_boundaries",
            actual["migration_boundaries"],
            expected["migration_boundaries"],
        ),
        ("boundary_manifest_sha256", _boundary_manifest(actual), _boundary_manifest(expected)),
    )
    for label, actual_value, expected_value in comparisons:
        if actual_value != expected_value:
            differences.append(label)
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
    parser.add_argument("--source-ref")
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
    args = parser.parse_args()
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
            if args.require_tracked_evidence:
                matrix_path = root / "plans/DOING/plans" / f"{PLAN_ID}-requirement-matrix.csv"
                evidence_differences = verify_tracked_evidence(root, matrix_path)
                checks["tracked_evidence"] = {"differences": evidence_differences}
                differences.extend(
                    f"tracked_evidence.{difference}" for difference in evidence_differences
                )
            payload["status"] = "PASS" if not differences else "BLOCKED"
            payload["differences"] = differences
            payload["checks"] = checks
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
