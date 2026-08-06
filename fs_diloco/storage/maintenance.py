"""Crash-safe archival, pruning, and reference-driven artifact collection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from .atomic_io import ensure_dir, safe_read_json
from .paths import RunPaths
from .sqlite_store import SQLiteStore


def _append_jsonl_fsync(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    records = list(rows)
    if not records:
        return 0
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(records)


def archive_and_prune(store: SQLiteStore, paths: RunPaths) -> dict[str, Any]:
    """Fsync terminal/history records before deleting their active DB rows."""
    update_rows = store.terminal_update_rows()
    version_rows = store.historical_version_rows()
    archived_at = time.time()
    archived_updates = [dict(row, archived_at=archived_at) for row in update_rows]
    archived_versions = [dict(row, archived_at=archived_at) for row in version_rows]
    _append_jsonl_fsync(paths.update_history_jsonl, archived_updates)
    _append_jsonl_fsync(paths.global_version_history_jsonl, archived_versions)
    store.delete_archived_rows(update_rows=update_rows, version_rows=version_rows)
    return {
        "updates": len(update_rows),
        "versions": len(version_rows),
        "terminal_paths": {
            Path(str(row["file_path"])).resolve(strict=False)
            for row in update_rows
            if row.get("file_path")
        },
    }


def archive_ha_history(store: SQLiteStore, paths: RunPaths) -> int:
    if not hasattr(store, "archivable_ha_history") or not hasattr(store, "token"):
        return 0
    retain = int(getattr(store.fenced_store, "max_retained_epoch_dirs"))
    before_epoch = max(1, int(store.token.epoch) - retain + 1)
    history = store.archivable_ha_history(before_epoch=before_epoch)
    epochs = list(history["epochs"])
    publications = list(history["control_publications"])
    records = [{"record_kind": "syncer_epoch", **row} for row in epochs] + [
        {"record_kind": "control_publication", **row} for row in publications
    ]
    _append_jsonl_fsync(paths.syncer_epoch_history_jsonl, records)
    store.delete_archived_ha_history(before_epoch=before_epoch)
    archived_epoch_ids = {int(row["epoch"]) for row in epochs}
    for row in publications:
        artifact = paths.shared_root / str(row["relative_path"])
        try:
            artifact.unlink()
        except FileNotFoundError:
            pass
    old_directories = []
    if paths.syncer_epochs.is_dir():
        for directory in paths.syncer_epochs.glob("e*_*"):
            try:
                epoch = int(directory.name.split("_", 1)[0].removeprefix("e"))
            except ValueError:
                continue
            if epoch < before_epoch:
                old_directories.append(directory)
    for directory in old_directories:
        for child in sorted(directory.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
        try:
            directory.rmdir()
        except OSError:
            pass
    return len(archived_epoch_ids)


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _resolved_paths(values: Iterable[str | Path], *, shared_root: Path | None = None) -> set[Path]:
    resolved: set[Path] = set()
    for value in values:
        path = Path(value)
        if not path.is_absolute() and shared_root is not None:
            path = shared_root / path
        resolved.add(path.resolve(strict=False))
    return resolved


def _pointer_state(paths: RunPaths) -> dict[tuple[str, int | None], tuple[str, Path]]:
    pointers: dict[tuple[str, int | None], tuple[str, Path]] = {}
    for pointer in paths.iter_instance_pointers():
        row = safe_read_json(pointer)
        if not row:
            continue
        file_path = row.get("file_path")
        update_id = row.get("update_id")
        learner_id = row.get("learner_id")
        if file_path and learner_id and update_id:
            fragment_id = row.get("fragment_id")
            key = (
                str(learner_id),
                None if fragment_id is None else int(fragment_id),
            )
            pointers[key] = (
                str(update_id),
                Path(str(file_path)).resolve(strict=False),
            )
    return pointers


def collect_runtime_artifacts(
    store: SQLiteStore,
    paths: RunPaths,
    *,
    orphan_grace_seconds: float,
    extra_terminal_paths: Iterable[Path] = (),
    now: float | None = None,
    stats: dict[str, int | float] | None = None,
) -> int:
    """Collect everything outside the DB/reference live set.

    Global and fragment checkpoints have no grace because SQLite is authoritative.
    Unpublished proposal payloads and temporary files receive the configured grace.
    """
    now = time.time() if now is None else now
    keep_checkpoints: set[Path] = set()
    current = store.latest_global_version()
    if current is not None:
        keep_checkpoints.update(
            _resolved_paths(
                (current["weight_path"], current["optim_path"]),
                shared_root=paths.shared_root,
            )
        )
    keep_checkpoints.update(
        _resolved_paths(
            [
                path
                for row in store.current_fragment_versions()
                for path in (row["weight_path"], row["optim_path"])
            ],
            shared_root=paths.shared_root,
        )
    )
    latest = safe_read_json(paths.latest_json) or {}
    materialized = latest.get("materialized_weight_path")
    if materialized:
        keep_checkpoints.add(Path(str(materialized)).resolve(strict=False))

    deleted = 0
    if hasattr(store, "claim_ready_gc_candidates"):
        candidates = store.claim_ready_gc_candidates(now=now)
        allowed_roots = (
            paths.weight_epochs.resolve(strict=False),
            paths.optim_epochs.resolve(strict=False),
        )
        for candidate in candidates:
            relative_path = str(candidate["relative_path"])
            target = (paths.shared_root / relative_path).resolve(strict=False)
            if not any(target.is_relative_to(root) for root in allowed_roots):
                raise RuntimeError(f"HA GC candidate escaped epoch roots: {target}")
            if _unlink(target):
                deleted += 1
            store.complete_gc_candidate(relative_path, deleted_at=now)
        if stats is not None:
            stats["ha_gc_candidates"] = len(candidates)
        current_epoch = int(store.token.epoch)
        ledger_paths = {
            (paths.shared_root / relative).resolve(strict=False)
            for relative in store.ha_gc_candidate_paths()
        }
        old_epoch_orphans = 0
        for artifact in (*paths.iter_epoch_weights(), *paths.iter_epoch_optim()):
            resolved = artifact.resolve(strict=False)
            if resolved in keep_checkpoints or resolved in ledger_paths:
                continue
            try:
                relative_parts = artifact.relative_to(paths.shared_root).parts
                epoch_component = next(
                    part
                    for part in relative_parts
                    if len(part) > 1 and part[0] == "e" and part[1:].isdigit()
                )
                artifact_epoch = int(epoch_component.removeprefix("e"))
                age = now - artifact.stat().st_mtime
            except (FileNotFoundError, StopIteration, ValueError):
                continue
            if artifact_epoch >= current_epoch or age < orphan_grace_seconds:
                continue
            if _unlink(artifact):
                deleted += 1
                old_epoch_orphans += 1
        if stats is not None:
            stats["ha_old_epoch_orphans"] = old_epoch_orphans
    checkpoint_patterns = (
        (paths.weights, "global_v*.safetensors"),
        (paths.optim, "outer_v*.safetensors"),
        (paths.fragment_weights, "**/v*.safetensors"),
        (paths.fragment_optim, "**/v*.safetensors"),
    )
    for directory, pattern in checkpoint_patterns:
        for path in directory.glob(pattern):
            if path.resolve(strict=False) not in keep_checkpoints and _unlink(path):
                deleted += 1

    live_payloads = {path.resolve(strict=False) for path in store.active_payload_paths()}
    terminal_payloads = store.gc_pending_paths()
    if stats is not None:
        stats["maintenance_scanned_rows"] = len(terminal_payloads)
    terminal_payloads.update(path.resolve(strict=False) for path in extra_terminal_paths)
    pointer_state = _pointer_state(paths)
    frontiers = store.proposal_frontiers()
    fragment_frontiers = store.fragment_proposal_frontiers()
    consumed_pointer_payloads = {
        payload
        for (learner_id, fragment_id), (update_id, payload) in pointer_state.items()
        if (
            frontiers.get(learner_id) == update_id
            if fragment_id is None
            else fragment_frontiers.get((learner_id, fragment_id)) == update_id
        )
    }

    for payload in paths.iter_instance_payloads():
        resolved = payload.resolve(strict=False)
        if resolved in live_payloads:
            continue
        terminal = resolved in terminal_payloads or resolved in consumed_pointer_payloads
        age = now - payload.stat().st_mtime
        if not terminal and age < orphan_grace_seconds:
            continue
        if _unlink(payload):
            deleted += 1

    # HA authority writers remain active during final maintenance: in
    # particular, the lease thread can atomically replace heartbeat.json while
    # the main thread collects artifacts.  Never treat a freshly discovered
    # authority temp as abandoned.  The lease/clock-skew GC grace is the point
    # after which no current writer may still own it.
    authority_temp_grace_seconds = max(
        float(orphan_grace_seconds),
        float(getattr(store, "gc_grace_seconds", 0.0)),
    )
    for root in (paths.control, paths.weights, paths.optim, paths.fragments):
        for tmp in root.glob("**/.*.tmp"):
            try:
                mtime = tmp.stat().st_mtime
            except FileNotFoundError:
                continue
            if now - mtime >= authority_temp_grace_seconds and _unlink(tmp):
                deleted += 1
    for root in (paths.updates_payloads,):
        for tmp in root.glob("**/.*.tmp"):
            try:
                mtime = tmp.stat().st_mtime
            except FileNotFoundError:
                # An atomic writer may rename the temp between glob and stat.
                continue
            if now - mtime >= orphan_grace_seconds and _unlink(tmp):
                deleted += 1
    store.clear_gc_pending_paths(path for path in terminal_payloads if not path.exists())
    if stats is not None:
        stats["gc_pending_rows"] = store.gc_pending_count()
    for root in (paths.weight_epochs, paths.optim_epochs):
        if not root.is_dir():
            continue
        for directory in sorted(
            (path for path in root.glob("e*/*") if path.is_dir()), reverse=True
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        for directory in sorted((path for path in root.glob("e*") if path.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    return deleted


def run_maintenance(
    store: SQLiteStore,
    paths: RunPaths,
    *,
    heartbeat_interval_seconds: float,
    scan_interval_seconds: float,
    input_closed: bool = False,
) -> dict[str, int | float]:
    archived = archive_and_prune(store, paths)
    archived_epochs = archive_ha_history(store, paths)
    grace = (
        0.0 if input_closed else max(2.0 * heartbeat_interval_seconds, 2.0 * scan_interval_seconds)
    )
    scan_stats: dict[str, int | float] = {}
    scan_started = time.monotonic()
    deleted = collect_runtime_artifacts(
        store,
        paths,
        orphan_grace_seconds=grace,
        extra_terminal_paths=archived["terminal_paths"],
        stats=scan_stats,
    )
    maintenance_scan_seconds = time.monotonic() - scan_started
    return {
        "archived_updates": int(archived["updates"]),
        "archived_versions": int(archived["versions"]),
        "archived_epochs": archived_epochs,
        "deleted_artifacts": deleted,
        "gc_pending_rows": int(scan_stats.get("gc_pending_rows", 0)),
        "maintenance_scanned_rows": int(scan_stats.get("maintenance_scanned_rows", 0)),
        "maintenance_scan_seconds": maintenance_scan_seconds,
    }
