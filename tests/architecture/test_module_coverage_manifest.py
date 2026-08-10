from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/module_coverage.json"


def _retained_surfaces() -> set[str]:
    surfaces: set[str] = set()
    for root, suffixes in (
        (ROOT / "fs_diloco", {".py", ".sql"}),
        (ROOT / "configs", {".yaml"}),
        (ROOT / "scripts/miyabi", {".py", ".sh", ".pbs"}),
    ):
        surfaces.update(
            path.relative_to(ROOT).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.suffix in suffixes
        )
    return surfaces


def test_every_retained_surface_has_one_current_boundary_test_mapping() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "groups"}
    assert manifest["schema_version"] == 1
    mapped: list[str] = []
    for group in manifest["groups"]:
        assert set(group) == {"boundary", "surfaces", "tests"}
        assert isinstance(group["boundary"], str) and group["boundary"].strip()
        assert group["surfaces"]
        assert group["tests"]
        for test in group["tests"]:
            assert test.startswith("tests/")
            assert (ROOT / test).is_file(), test
        mapped.extend(group["surfaces"])

    assert len(mapped) == len(set(mapped)), "a retained surface has multiple owners"
    assert set(mapped) == _retained_surfaces()
