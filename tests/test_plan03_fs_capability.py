from __future__ import annotations

from datetime import datetime
import json

import pytest

from scripts.miyabi import plan03_fs_capability as capability
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
    assert result["preexisting_final_retry"] == "repeatable_fail_closed_without_reservation_leak"
    assert result["different_staging_concurrency"] == "fail_closed_until_final_identity_exists"
    assert result["same_staging_concurrency"] == "converges_when_peer_links_reserved_identity"
    assert result["foreign_entry_visibility"] == "fail_closed"
    assert result["reservation_repair"] == "PASS"
    assert result["reservation_loss_before_repair_visible"] is False
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


def test_probe_records_source_identity_and_timestamp(tmp_path) -> None:
    result = capability.probe(tmp_path)

    assert datetime.fromisoformat(result["recorded_at"]).utcoffset() is not None
    assert len(result["source_identity"]["git_commit"]) == 40
    assert isinstance(result["source_identity"]["git_dirty"], bool)
    assert result["source_identity"]["source_fingerprint"].startswith("sha256:")
    assert result["temporary_path_removed"] is True


def test_failed_probe_artifact_is_named_fail(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_at = "2026-08-09T01:02:03+00:00"
    payload = {
        "artifact_version": 1,
        "experiment_id": "p0-shared-fs-capability",
        "status": "FAIL",
        "recorded_at": recorded_at,
    }
    output_dir = tmp_path / "artifacts"
    monkeypatch.setattr(capability, "probe", lambda *_args, **_kwargs: payload)

    with pytest.raises(SystemExit, match="1"):
        capability.main(
            [
                "--shared-parent",
                str(tmp_path),
                "--output-dir",
                str(output_dir),
            ]
        )

    artifacts = list(output_dir.iterdir())
    assert [path.name for path in artifacts] == [
        "20260809-010203_p0-shared-fs-capability_fail.json"
    ]
    assert json.loads(artifacts[0].read_text(encoding="utf-8"))["status"] == "FAIL"
