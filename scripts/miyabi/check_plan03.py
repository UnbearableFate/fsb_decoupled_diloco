#!/usr/bin/env python3
"""Static inventory and staged evidence checker for Plan 03.

The inventory mode is intentionally stdlib-only so it is safe on a Miyabi
login node. Runtime evidence is produced by the individual compute/PBS gates.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
ARCHIVE_TAGS = (
    "archive/classic-full-v1-final",
    "archive/fragment-v0-final",
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tracked(root: Path, prefix: str) -> list[str]:
    output = _git(root, "ls-files", prefix)
    return sorted(line for line in output.splitlines() if line)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bound_mutators(root: Path) -> list[str]:
    path = root / "fs_diloco/storage/fenced_store.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_BOUND_MUTATORS"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if not isinstance(value, set) or not all(isinstance(item, str) for item in value):
                break
            return sorted(value)
    raise RuntimeError("could not statically resolve _BOUND_MUTATORS")


def _fragment_enabled(path: Path) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "fragments:":
            for child in lines[index + 1 :]:
                if child and not child.startswith((" ", "\t")):
                    break
                if child.strip() == "enabled: true":
                    return True
            return False
    return False


def inventory(root: Path) -> dict[str, Any]:
    source = _tracked(root, "fs_diloco")
    tests = [path for path in _tracked(root, "tests") if Path(path).name.startswith("test_")]
    configs = [path for path in _tracked(root, "configs") if path.endswith((".yaml", ".yml"))]
    pbs = [path for path in _tracked(root, "scripts/miyabi") if path.endswith(".pbs")]
    schemas = [path for path in source if path.endswith(".sql")]
    fragments = [path for path in configs if _fragment_enabled(root / path)]
    fragment_pbs = [
        path for path in pbs if "fragment" in Path(path).name and "no_fragment" not in path
    ]
    baseline_configs = [path for path in configs if Path(path).name.startswith("torch_baseline_")]
    baseline_pbs = [path for path in pbs if "torch_" in Path(path).name]
    baseline_tests = [path for path in tests if Path(path).name.startswith("test_torch_baseline_")]
    tag_targets = {tag: _git(root, "rev-parse", f"{tag}^{{commit}}") for tag in ARCHIVE_TAGS}
    files = source + tests + configs + pbs + schemas
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "status": "PASS",
        "source_identity": {
            "branch": _git(root, "branch", "--show-current"),
            "commit": _git(root, "rev-parse", "HEAD"),
            "archive_tag_targets": tag_targets,
        },
        "counts": {
            "source_files": len(source),
            "test_files": len(tests),
            "config_files": len(configs),
            "pbs_files": len(pbs),
            "schema_files": len(schemas),
            "bound_mutators": len(_bound_mutators(root)),
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
            "bound_mutators": _bound_mutators(root),
        },
        "migration_boundaries": {
            "fragment_enabled_configs_delete_in_p5": fragments,
            "fragment_pbs_delete_in_p5": fragment_pbs,
            "historical_full_control_archive_separately": {
                "config": "configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml",
                "pbs": "scripts/miyabi/run_9node_no_fragment_gpt2_wikitext2_50x10.pbs",
            },
            "torch_baseline_retain": {
                "configs": baseline_configs,
                "pbs": baseline_pbs,
                "tests": baseline_tests,
                "package": "fs_diloco/baselines",
            },
            "recursive_config_anchor": "configs/5000/fs_diloco_gpt2_wikitext2_8l_200x25steps.yaml",
        },
        "manifest_sha256": {path: _sha256(root / path) for path in sorted(set(files))},
    }


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
    args = parser.parse_args()
    payload = inventory(args.root.resolve())
    if args.inventory_output is not None:
        _atomic_write_json(args.inventory_output.resolve(), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
