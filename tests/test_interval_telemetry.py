import pytest

from fs_diloco.runtime import syncer


def test_interval_breakdown_uses_nonoverlapping_components_and_explicit_residual():
    breakdown = syncer.interval_breakdown(
        total_seconds=10.0,
        discovery_seconds=1.5,
        idle_seconds=1.0,
        grace_seconds=2.0,
        read_seconds=1.0,
        merge_seconds=1.5,
        publish_seconds=2.0,
        maintenance_seconds=0.5,
        quorum_trigger="quorum_max",
    )

    assert breakdown == {
        "discovery_seconds": 1.5,
        "idle_seconds": 1.0,
        "grace_seconds": 2.0,
        "merge_seconds": 1.5,
        "interval_residual_seconds": 0.5,
        "quorum_trigger": "quorum_max",
    }
    accounted = sum(
        breakdown[key]
        for key in (
            "discovery_seconds",
            "idle_seconds",
            "grace_seconds",
            "merge_seconds",
            "interval_residual_seconds",
        )
    ) + 1.0 + 2.0 + 0.5
    assert accounted == pytest.approx(10.0)


def test_interval_breakdown_rejects_overlapping_or_negative_durations():
    with pytest.raises(ValueError, match="exceed total"):
        syncer.interval_breakdown(
            total_seconds=1.0,
            discovery_seconds=0.5,
            idle_seconds=0.5,
            grace_seconds=0.5,
            read_seconds=0.0,
            merge_seconds=0.0,
            publish_seconds=0.0,
            maintenance_seconds=0.0,
            quorum_trigger="deadline",
        )
