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


def test_protocol_is_dependency_free_of_runtime_storage_and_pathlib() -> None:
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


def test_storage_does_not_import_runtime() -> None:
    for path in (ROOT / "fs_diloco/storage").glob("*.py"):
        assert not any("runtime" in item for item in imports(path)), path
