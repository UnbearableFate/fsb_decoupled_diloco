from __future__ import annotations

import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts/miyabi/plan03_p6_acceptance.py"


def _load_aggregate_module():
    specification = importlib.util.spec_from_file_location("plan03_p6_acceptance", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_phase_requirement_inventory_is_exact_and_includes_auth_11() -> None:
    module = _load_aggregate_module()
    expected = sorted(module.EXPECTED_PHASE_REQUIREMENTS)

    assert "AUTH-11" in expected
    assert module.phase_requirement_error(expected) is None
    assert "missing=['AUTH-11']" in module.phase_requirement_error(
        [item for item in expected if item != "AUTH-11"]
    )
    assert "extra=['P6-UNKNOWN']" in module.phase_requirement_error([*expected, "P6-UNKNOWN"])
    assert "duplicates=1" in module.phase_requirement_error([*expected, expected[0]])
