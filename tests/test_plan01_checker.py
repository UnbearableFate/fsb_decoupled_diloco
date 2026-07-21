from __future__ import annotations

import pytest

from scripts.miyabi.check_plan01_invariants import validate_resume_progress


def resume_generation() -> dict[str, object]:
    return {
        "resume_id": "resume-1",
        "heartbeat_fences": {"learner_000": "digest"},
    }


def test_validate_resume_progress_requires_new_liveness_generation_before_commit():
    details = validate_resume_progress(
        [
            {
                "event_type": "run_resumed",
                "resume_id": "resume-1",
                "version": 3,
                "heartbeat_fence_count": 1,
            },
            {"event_type": "learner_liveness_updated", "active": 1},
            {"event_type": "outer_step_applied", "version": 4},
            {"event_type": "stop_published", "version": 4},
        ],
        resume_generation=resume_generation(),
        expected_learners=1,
        final_version=4,
    )

    assert details["resume_version"] == 3
    assert details["progress_version"] == 4
    assert details["active_liveness_event_index"] == 1


@pytest.mark.parametrize(
    ("events", "match"),
    [
        (
            [
                {
                    "event_type": "run_resumed",
                    "resume_id": "resume-1",
                    "version": 3,
                    "heartbeat_fence_count": 1,
                },
                {"event_type": "input_exhausted", "version": 3},
                {"event_type": "outer_step_applied", "version": 4},
            ],
            "before post-resume progress",
        ),
        (
            [
                {
                    "event_type": "run_resumed",
                    "resume_id": "resume-1",
                    "version": 3,
                    "heartbeat_fence_count": 1,
                },
                {"event_type": "outer_step_applied", "version": 4},
            ],
            "no active current-generation learner",
        ),
    ],
)
def test_validate_resume_progress_rejects_false_progress(events, match):
    with pytest.raises(RuntimeError, match=match):
        validate_resume_progress(
            events,
            resume_generation=resume_generation(),
            expected_learners=1,
            final_version=4,
        )


def test_validate_resume_progress_rejects_incomplete_fence_set():
    with pytest.raises(RuntimeError, match="fence set is incomplete"):
        validate_resume_progress(
            [
                {
                    "event_type": "run_resumed",
                    "resume_id": "resume-1",
                    "version": 3,
                    "heartbeat_fence_count": 1,
                },
                {"event_type": "learner_liveness_updated", "active": 1},
                {"event_type": "global_published", "version": 4},
            ],
            resume_generation={"resume_id": "resume-1", "heartbeat_fences": {}},
            expected_learners=1,
            final_version=4,
        )
