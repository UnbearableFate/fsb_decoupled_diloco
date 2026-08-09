from __future__ import annotations

from scripts.miyabi.plan03_p6_two_node_sqlite import REQUIREMENTS_COVERED


def test_g7_primary_evidence_covers_acceptance_and_auth_11() -> None:
    assert set(REQUIREMENTS_COVERED) == {"AUTH-11", "P6-ACCEPTANCE"}
