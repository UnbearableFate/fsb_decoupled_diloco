from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/module_coverage.json"


def _retained_surfaces() -> set[str]:
    listed = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "fs_diloco",
            "configs",
            "scripts/miyabi",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    suffixes = {".py", ".sql", ".yaml", ".sh", ".pbs"}
    return {path for path in listed if Path(path).suffix in suffixes}


def _assert_test_selector(selector: str) -> None:
    components = selector.split("::")
    assert len(components) == 2, selector
    relative, function_name = components
    assert relative.startswith("tests/") and function_name.startswith("test_"), selector
    path = ROOT / relative
    assert path.is_file(), selector
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert function_name in functions, selector


def test_every_retained_surface_has_one_current_boundary_test_mapping() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "surfaces"}
    assert manifest["schema_version"] == 2
    entries = manifest["surfaces"]
    assert entries == sorted(entries, key=lambda entry: entry["surface"])
    mapped: list[str] = []
    for entry in entries:
        assert set(entry) == {"surface", "boundary", "tests"}
        assert isinstance(entry["surface"], str) and entry["surface"]
        assert isinstance(entry["boundary"], str) and entry["boundary"].strip()
        assert isinstance(entry["tests"], list) and entry["tests"]
        assert len(entry["tests"]) == len(set(entry["tests"])), entry["surface"]
        for selector in entry["tests"]:
            _assert_test_selector(selector)
        mapped.append(entry["surface"])

    assert len(mapped) == len(set(mapped)), "a retained surface has multiple owners"
    assert set(mapped) == _retained_surfaces()
