import json

import pytest

from fs_diloco.runtime.syncer import merge_staleness_evidence
from fs_diloco.tools.analysis import staleness_observational_summary


def test_full_and_fragment_staleness_evidence_is_weighted_and_counted():
    selected = [
        {"update_id": "u0", "base_global_version": 5},
        {"update_id": "u1", "base_global_version": 4},
        {"update_id": "u2", "base_global_version": 3},
    ]
    evidence = merge_staleness_evidence(
        selected,
        {"u0": 0.5, "u1": 0.3, "u2": 0.2},
        current_version=5,
        base_version_field="base_global_version",
    )
    assert evidence["effective_staleness_mean"] == pytest.approx(0.7)
    assert evidence["fresh_effective_weight"] == pytest.approx(0.5)
    assert json.loads(evidence["staleness_counts_json"]) == {"0": 1, "1": 1, "2": 1}


def test_observational_link_uses_first_subsequent_learner_loss():
    summary = staleness_observational_summary(
        [
            {
                "timestamp": "10",
                "version": "1",
                "effective_staleness_mean": "0.25",
                "fresh_effective_weight": "0.75",
                "staleness_counts_json": '{"0":3,"1":1}',
            },
            {
                "timestamp": "20",
                "version": "2",
                "effective_staleness_mean": "0.5",
                "fresh_effective_weight": "0.5",
                "staleness_counts_json": '{"0":2,"1":2}',
            },
        ],
        [
            {"timestamp": "9", "learner_id": "old", "train_loss": "9"},
            {
                "timestamp": "11",
                "learner_id": "learner_001",
                "local_step": "100",
                "train_loss": "3.0",
            },
            {
                "timestamp": "21",
                "learner_id": "learner_002",
                "local_step": "200",
                "train_loss": "2.9",
            },
        ],
    )

    assert summary["status"] == "available_observational_only"
    assert summary["aggregate_staleness_counts"] == {"0": 5, "1": 3}
    assert summary["links"][0]["next_learner_id"] == "learner_001"
    assert summary["links"][0]["next_train_loss"] == 3.0
    assert summary["links"][0]["delay_seconds"] == 1.0
