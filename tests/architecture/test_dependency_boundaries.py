from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            result.add("." * node.level + module)
    return result


def test_protocol_v4_is_dependency_free_of_runtime_storage_and_pathlib() -> None:
    files = [
        ROOT / "fs_diloco/protocol/proposal.py",
        ROOT / "fs_diloco/protocol/cycle_receipt.py",
        ROOT / "fs_diloco/protocol/contributor.py",
        ROOT / "fs_diloco/protocol/authority.py",
    ]
    for path in files:
        imported = imports(path)
        assert not any("runtime" in item or "storage" in item for item in imported), path
        assert "pathlib" not in imported, path


def test_torch_baseline_does_not_import_runtime_learner() -> None:
    imported = imports(ROOT / "fs_diloco/baselines/train.py")

    assert not any("runtime.learner" in item for item in imported)
    assert any("modeling.training" in item for item in imported)


def test_runtime_does_not_import_query_only_legacy_package() -> None:
    for path in (ROOT / "fs_diloco/runtime").glob("*.py"):
        assert not any("legacy" in item for item in imports(path)), path
