from __future__ import annotations

import json
from pathlib import Path

import pytest

from fs_diloco.observability.logging_utils import ActorTelemetryWriter
from fs_diloco.tools.check_workload_equivalence import MATCHED_FIELDS, compare_workloads


PLAN03_REQUIREMENTS = frozenset({"AUDIT-05"})


def test_actor_telemetry_claim_prevents_shared_attempt_append(tmp_path: Path) -> None:
    path = tmp_path / "metrics/learner/learner-0/attempt-1.jsonl"
    writer = ActorTelemetryWriter(
        path,
        actor_kind="learner",
        actor_id="learner-0",
        attempt_id="attempt-1",
    )
    writer.event("step", tokens=8)
    with pytest.raises(FileExistsError):
        ActorTelemetryWriter(
            path,
            actor_kind="learner",
            actor_id="learner-0",
            attempt_id="attempt-1",
        )
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["actor_id"] == "learner-0"
    assert row["attempt_id"] == "attempt-1"


def test_actor_telemetry_payload_cannot_override_frozen_actor_identity(tmp_path: Path) -> None:
    writer = ActorTelemetryWriter(
        tmp_path / "metrics/learner/learner-0/attempt-1.jsonl",
        actor_kind="learner",
        actor_id="learner-0",
        attempt_id="attempt-1",
    )

    with pytest.raises(ValueError, match="reserved identity"):
        writer.event("step", actor_id="different")


def test_comparison_is_blocked_before_identity_and_never_clips_signed_delta() -> None:
    assert compare_workloads({})["comparison_status"] == "BLOCKED"
    identity = {field: f"identity-{field}" for field in MATCHED_FIELDS}
    result = compare_workloads(
        {
            "baseline": identity,
            "candidate": dict(identity),
            "baseline_seconds": [10.0] * 20,
            "candidate_seconds": [9.0] * 20,
            "bootstrap_samples": 100,
        }
    )
    assert result["comparison_status"] == "COMPARABLE"
    assert result["signed_delta_ratio"] == pytest.approx(-0.1)
    assert result["clipping_applied"] is False


def test_comparison_rejects_workload_drift_and_large_absolute_delta() -> None:
    identity = {field: f"identity-{field}" for field in MATCHED_FIELDS}
    drifted = dict(identity)
    drifted["cursor_identity"] = "different"
    mismatch = compare_workloads(
        {
            "baseline": identity,
            "candidate": drifted,
            "baseline_seconds": [10.0],
            "candidate_seconds": [10.0],
        }
    )
    assert mismatch["comparison_status"] == "INCOMPARABLE"
    large = compare_workloads(
        {
            "baseline": identity,
            "candidate": dict(identity),
            "baseline_seconds": [10.0] * 5,
            "candidate_seconds": [13.0] * 5,
            "bootstrap_samples": 100,
        }
    )
    assert large["comparison_status"] == "INCOMPARABLE"


def test_comparison_audits_absolute_signed_median_not_median_absolute_noise() -> None:
    identity = {field: f"identity-{field}" for field in MATCHED_FIELDS}
    result = compare_workloads(
        {
            "baseline": identity,
            "candidate": dict(identity),
            "baseline_seconds": [10.0, 10.0, 10.0],
            "candidate_seconds": [7.0, 10.0, 13.0],
            "bootstrap_samples": 100,
        }
    )

    assert result["comparison_status"] == "COMPARABLE"
    assert result["signed_delta_ratio"] == 0.0
    assert result["absolute_signed_delta_ratio"] == 0.0
