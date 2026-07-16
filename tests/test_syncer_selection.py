from fs_diloco.protocol.merge import select_one_per_learner


def test_syncer_selection_respects_quorum_max():
    updates = [
        {"update_id": "u0", "learner_id": "learner_000", "local_step_end": 1, "committed_at": 1.0},
        {"update_id": "u1", "learner_id": "learner_001", "local_step_end": 1, "committed_at": 2.0},
        {"update_id": "u2", "learner_id": "learner_002", "local_step_end": 1, "committed_at": 3.0},
    ]
    selected = select_one_per_learner(updates, quorum_max=2)
    assert len(selected) == 2
    assert {row["learner_id"] for row in selected} == {"learner_000", "learner_001"}
