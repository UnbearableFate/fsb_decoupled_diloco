from __future__ import annotations

import pytest

from fs_diloco.storage.audit_archive import (
    build_audit_batch,
    build_audit_partition,
    deduplicate_audit_records,
    publish_audit_partition,
    validate_audit_partition_manifest,
    delete_claimed_audit_batch_object,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.paths import RunPaths


def _record(value: int) -> dict:
    return {"table": "events", "primary_key": "1", "row": {"value": value}}


def test_audit_identity_components_cannot_escape_the_audit_root() -> None:
    with pytest.raises(ValueError, match="safe path component"):
        build_audit_batch(
            batch_id="..",
            record_kind="authority_history",
            cutoff_version=1,
            records=[],
        )


def test_audit_consumer_dedup_key_includes_record_kind() -> None:
    first = build_audit_batch(
        batch_id="first",
        record_kind="authority_history",
        cutoff_version=1,
        records=[_record(1)],
    )
    other_kind = build_audit_batch(
        batch_id="second",
        record_kind="scheduler_history",
        cutoff_version=1,
        records=[_record(2)],
    )

    deduplicated = deduplicate_audit_records((first, other_kind))
    assert {(item["record_kind"], item["row"]["value"]) for item in deduplicated} == {
        ("authority_history", 1),
        ("scheduler_history", 2),
    }


def test_partition_is_published_before_hashed_manifest_and_validates(tmp_path) -> None:
    first = build_audit_batch(
        batch_id="first",
        record_kind="authority_history",
        cutoff_version=1,
        records=[_record(1)],
    )
    replay = build_audit_batch(
        batch_id="second",
        record_kind="authority_history",
        cutoff_version=2,
        records=[_record(1)],
    )
    partition = build_audit_partition(
        partition_id="partition",
        record_kind="authority_history",
        batches=(first, replay),
    )
    paths = RunPaths(tmp_path)
    partition_path, _partition_sha, manifest_path, _manifest_sha = publish_audit_partition(
        paths, partition
    )

    assert len(partition["records"]) == 1
    validate_audit_partition_manifest(
        paths=paths,
        partition=read_json(partition_path),
        manifest=read_json(manifest_path),
    )
    assert partition_path.stat().st_mode & 0o222 == 0
    assert manifest_path.stat().st_mode & 0o222 == 0


def test_audit_gc_refuses_leaf_and_parent_symlinks_without_deleting_targets(tmp_path) -> None:
    paths = RunPaths(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    leaf = paths.audit_batches / "history" / "batch.json"
    leaf.parent.mkdir(parents=True)
    leaf.symlink_to(outside)

    with pytest.raises(RuntimeError, match="immutable regular batch"):
        delete_claimed_audit_batch_object(
            paths,
            relative_path="audit/batches/history/batch.json",
            expected_sha256="0" * 64,
        )
    assert outside.read_text(encoding="utf-8") == "outside"

    leaf.unlink()
    leaf.parent.rmdir()
    leaf.parent.symlink_to(tmp_path)
    with pytest.raises(RuntimeError, match="parent.*non-symlink"):
        delete_claimed_audit_batch_object(
            paths,
            relative_path="audit/batches/history/outside.json",
            expected_sha256="0" * 64,
        )
    assert outside.read_text(encoding="utf-8") == "outside"
