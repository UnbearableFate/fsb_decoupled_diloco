from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts/miyabi/plan03_p6_quality_manifest.py"


def _load_manifest_module():
    specification = importlib.util.spec_from_file_location("plan03_p6_quality_manifest", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_quality_manifest_uses_retained_post_p5_owners_and_frozen_oracle_evidence() -> None:
    module = _load_manifest_module()

    assert "tests/reference/test_plan03_classic_static_oracle.py" not in (
        module.QUALITY_TEST_OWNERS
    )
    assert "tests/gates/test_plan03_p6_performance_harness.py" in module.QUALITY_TEST_OWNERS
    assert module.quality_evidence_errors(PROJECT_ROOT) == []


def test_quality_manifest_write_removes_temporary_file_after_serialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_manifest_module()
    output = tmp_path / "quality.json"

    def fail_serialization(*_args, **_kwargs):
        raise RuntimeError("injected serialization failure")

    monkeypatch.setattr(module.json, "dumps", fail_serialization)
    with pytest.raises(RuntimeError, match="injected serialization failure"):
        module._write(output, {"status": "PASS"})

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []
