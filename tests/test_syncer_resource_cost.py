from fs_diloco.tools.analysis import syncer_resource_cost


def test_syncer_resource_cost_uses_merge_and_publish_active_time():
    result = syncer_resource_cost(
        [
            {
                "read_seconds": "1",
                "aggregation_seconds": "2",
                "outer_step_seconds": "1",
                "publish_seconds": "1",
            },
            {
                "read_seconds": "2",
                "aggregation_seconds": "2",
                "outer_step_seconds": "2",
                "publish_seconds": "2",
            },
        ],
        100.0,
    )

    assert result["status"] == "available"
    assert result["merge_compute_total_seconds"] == 10.0
    assert result["publish_total_seconds"] == 3.0
    assert result["active_total_seconds"] == 13.0
    assert result["duty_cycle"] == 0.13
    assert result["merge_compute_p50_seconds"] == 5.0
    assert result["merge_compute_p95_seconds"] == 5.9
    assert result["reserved_syncer_node_hours"] == 100.0 / 3600.0
    assert result["estimated_idle_gpu_node_hours"] == (100.0 / 3600.0) * 0.87


def test_syncer_resource_cost_marks_legacy_or_incomplete_runs_unavailable():
    assert syncer_resource_cost([], 100.0) == {
        "status": "unavailable",
        "merge_count": 0,
    }
    assert syncer_resource_cost([{"read_seconds": "1"}], None) == {
        "status": "unavailable",
        "merge_count": 1,
    }
