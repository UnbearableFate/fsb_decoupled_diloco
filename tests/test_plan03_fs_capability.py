from __future__ import annotations

from scripts.miyabi.plan03_fs_capability import (
    FALLBACK_STEPS,
    _probe_directory_publish_fallback,
)


def test_initializer_fallback_covers_every_pre_visibility_crash_prefix(tmp_path) -> None:
    result = _probe_directory_publish_fallback(tmp_path)

    assert result["status"] == "PASS"
    assert result["same_identity_retry"] is True
    assert result["different_identity_collision"] == "fail_closed"
    assert result["completed_root_overwrite"] == "fail_closed"
    prefixes = result["crash_prefixes"]
    marker_step_index = FALLBACK_STEPS.index("link_complete_marker")
    assert len(prefixes) == marker_step_index + 1
    assert [row["next_step"] for row in prefixes] == list(FALLBACK_STEPS[: marker_step_index + 1])
    assert all(row["visible_before_retry"] is False for row in prefixes)
    assert all(row["visible_after_retry"] is True for row in prefixes)
    post_visibility = result["post_visibility_prefixes"]
    assert [row["completed_steps"] for row in post_visibility] == list(
        range(marker_step_index + 1, len(FALLBACK_STEPS))
    )
    assert all(row["visible_before_retry"] is True for row in post_visibility)
    assert all(row["visible_after_retry"] is True for row in post_visibility)
