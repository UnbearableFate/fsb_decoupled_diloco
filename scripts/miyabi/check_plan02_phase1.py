#!/usr/bin/env python3
"""Validate Plan 02 Phase 1 HA evidence without trusting fixed control caches."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from safetensors import safe_open

from fs_diloco.core.run_descriptor import load_run_descriptor
from fs_diloco.observability.phase1_performance import (
    BUSINESS_TRANSACTION_MAX_P99_RATIO,
    BUSINESS_TRANSACTION_MIN_SAMPLES,
    BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
    CHECKPOINT_PUBLISH_MAX_P99_RATIO,
    CHECKPOINT_PUBLISH_MIN_SAMPLES,
    CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
    MATCHED_PERFORMANCE_FORMAT_VERSION,
    matched_p99_limit,
)
from fs_diloco.protocol.control_epoch import EpochControlReader
from fs_diloco.storage.atomic_io import safe_read_json, sha256_file
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.schema_bootstrap import open_readonly


_BLOCKING_EVENT_TYPES = {
    "canonical_latest_wait_failed",
    "error",
    "final_fragment_adoption_failed",
    "leader_release_failed",
    "lease_renewer_stop_failed",
    "no_progress_timeout",
    "syncer_recovery_exhausted",
    "syncer_unresponsive",
    "uncaught_exception",
}


def _history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _relative_artifact(paths: RunPaths, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else paths.shared_root / path


def _check(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _floating(value: Any, default: float = float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _jsonl_events(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not root.is_dir():
        return events
    for path in sorted(root.rglob("*.jsonl")):
        events.extend(_history(path))
    return events


def _percentile(samples: list[float], quantile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(float(value) for value in samples)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _blocking_failure_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if str(event.get("event_type", "")).lower() in _BLOCKING_EVENT_TYPES
    ]


def _stale_business_commit_violations(
    *,
    versions: list[dict[str, Any]],
    epochs: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    controller: dict[str, Any] | None,
    terminal: dict[str, Any] | None,
    publications: list[dict[str, Any]],
) -> list[str]:
    """Validate every persisted business writer against immutable epoch ownership."""

    epoch_owners = {int(row["epoch"]): str(row["owner_id"]) for row in epochs}
    version_epochs: dict[int, int] = {}
    violations: list[str] = []
    highest_version_epoch = 0
    for row in sorted(versions, key=lambda value: int(value["version"])):
        version = int(row["version"])
        epoch = int(row.get("commit_epoch") or -1)
        owner = str(row.get("commit_owner_id") or "")
        version_epochs[version] = epoch
        if epoch_owners.get(epoch) != owner:
            violations.append(f"version {version} writer {epoch}/{owner} is not an epoch owner")
        if epoch < highest_version_epoch:
            violations.append(
                f"version {version} moved writer epoch backwards: {epoch} < {highest_version_epoch}"
            )
        highest_version_epoch = max(highest_version_epoch, epoch)

    for row in updates:
        update_id = str(row.get("update_id", "<unknown>"))
        for field in ("selected_by_epoch", "applied_by_epoch", "dropped_by_epoch"):
            value = row.get(field)
            if value is not None and int(value) not in epoch_owners:
                violations.append(f"update {update_id} has unknown {field}={value}")
        if row.get("status") == "applied":
            applied_version = int(row.get("applied_version") or -1)
            expected_epoch = version_epochs.get(applied_version)
            observed_epoch = row.get("applied_by_epoch")
            if (
                expected_epoch is None
                or observed_epoch is None
                or int(observed_epoch) != expected_epoch
            ):
                violations.append(
                    f"update {update_id} applied writer epoch {observed_epoch} "
                    f"does not match version {applied_version} epoch {expected_epoch}"
                )

    if controller is not None:
        epoch = int(controller.get("updated_by_epoch") or -1)
        owner = str(controller.get("updated_by_owner_id") or "")
        if epoch_owners.get(epoch) != owner:
            violations.append(f"controller writer {epoch}/{owner} is not an epoch owner")
    if terminal is not None:
        epoch = int(terminal.get("finalized_by_epoch") or -1)
        owner = str(terminal.get("finalized_by_owner_id") or "")
        if epoch_owners.get(epoch) != owner:
            violations.append(f"terminal writer {epoch}/{owner} is not an epoch owner")
    for row in publications:
        epoch = int(row.get("published_by_epoch") or -1)
        owner = str(row.get("published_by_owner_id") or "")
        if epoch_owners.get(epoch) != owner:
            violations.append(
                f"control publication {row.get('kind')}/{row.get('logical_generation')} "
                f"writer {epoch}/{owner} is not an epoch owner"
            )
    return violations


def _canonical_adoption_violations(
    *,
    learner_events: list[dict[str, Any]],
    terminal: dict[str, Any] | None,
    expected_learner_ids: set[str],
) -> list[str]:
    violations: list[str] = []
    learner_errors = _blocking_failure_events(learner_events)
    violations.extend(
        f"learner {event.get('actor')} emitted {event.get('event_type')}"
        for event in learner_errors
    )
    exits = [event for event in learner_events if event.get("event_type") == "process_exit"]
    exits_by_actor: dict[str, list[dict[str, Any]]] = {}
    for event in exits:
        exits_by_actor.setdefault(str(event.get("actor") or ""), []).append(event)
    observed_ids = set(exits_by_actor)
    if observed_ids != expected_learner_ids:
        violations.append(
            f"learner exit identities mismatch: {sorted(observed_ids)} != "
            f"{sorted(expected_learner_ids)}"
        )
    final_version = None if terminal is None else int(terminal["final_version"])
    for learner_id, actor_exits in exits_by_actor.items():
        if len(actor_exits) != 1:
            violations.append(f"learner {learner_id} has {len(actor_exits)} process exits")
            continue
        event = actor_exits[0]
        if event.get("status_reason") not in (None, ""):
            violations.append(
                f"learner {learner_id} exited with status_reason={event.get('status_reason')}"
            )
        if final_version is None or int(event.get("global_version", -1)) != final_version:
            violations.append(
                f"learner {learner_id} exit version {event.get('global_version')} "
                f"does not match terminal {final_version}"
            )
    return violations


def _matched_performance_errors(
    payload: dict[str, Any] | None,
    *,
    expected_identity: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if payload is None:
        return ["completed gate requires a matched performance artifact"]
    _check(
        payload.get("checker") == "plan02_phase1_matched_performance",
        "matched performance checker identity mismatch",
        errors,
    )
    _check(
        payload.get("format_version") == MATCHED_PERFORMANCE_FORMAT_VERSION,
        "matched performance format version mismatch",
        errors,
    )
    _check(payload.get("status") == "PASS", "matched performance status is not PASS", errors)
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        errors.append("matched performance identity is missing")
    else:
        for field, expected in expected_identity.items():
            _check(
                identity.get(field) == expected,
                f"matched performance identity {field} mismatch",
                errors,
            )

    business = payload.get("business_candidate_observer")
    if not isinstance(business, dict):
        errors.append("matched candidate-observer evidence is missing")
    else:
        baseline_count = _integer(business.get("baseline_sample_count"))
        observer_count = _integer(business.get("observer_sample_count"))
        baseline_p99 = _floating(business.get("baseline_p99_seconds"))
        observer_p99 = _floating(business.get("observer_p99_seconds"))
        expected_limit = matched_p99_limit(
            baseline_p99,
            max_ratio=BUSINESS_TRANSACTION_MAX_P99_RATIO,
            jitter_seconds=BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
        )
        _check(
            baseline_count >= BUSINESS_TRANSACTION_MIN_SAMPLES,
            "matched business baseline sample count is too small",
            errors,
        )
        _check(
            observer_count >= BUSINESS_TRANSACTION_MIN_SAMPLES,
            "matched candidate-observer sample count is too small",
            errors,
        )
        _check(
            _integer(business.get("candidate_observation_count")) > 0,
            "matched candidate observer made no observations",
            errors,
        )
        _check(
            _integer(business.get("candidate_writer_transaction_attempt_count"), -1) == 0,
            "healthy candidate observer attempted a writer transaction",
            errors,
        )
        _check(
            business.get("max_p99_ratio") == BUSINESS_TRANSACTION_MAX_P99_RATIO
            and business.get("jitter_seconds") == BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
            "matched business threshold definition changed",
            errors,
        )
        _check(
            math.isclose(
                _floating(business.get("allowed_observer_p99_seconds")),
                expected_limit,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ),
            "matched business allowed p99 was not derived from the frozen threshold",
            errors,
        )
        _check(
            math.isfinite(observer_p99) and observer_p99 <= expected_limit,
            f"candidate observer p99 regression exceeded threshold: "
            f"{observer_p99} > {expected_limit}",
            errors,
        )

    checkpoint = payload.get("checkpoint_publish")
    if not isinstance(checkpoint, dict):
        errors.append("matched checkpoint publish evidence is missing")
    else:
        baseline_count = _integer(checkpoint.get("baseline_sample_count"))
        ha_count = _integer(checkpoint.get("ha_sample_count"))
        baseline_p99 = _floating(checkpoint.get("baseline_p99_seconds"))
        ha_p99 = _floating(checkpoint.get("ha_p99_seconds"))
        expected_limit = matched_p99_limit(
            baseline_p99,
            max_ratio=CHECKPOINT_PUBLISH_MAX_P99_RATIO,
            jitter_seconds=CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
        )
        _check(
            checkpoint.get("baseline_contract") == "Plan 01 legacy SQLiteStore publication",
            "checkpoint baseline is not the frozen Plan 01 contract",
            errors,
        )
        _check(
            checkpoint.get("matched_fields") == "source/config/model/seed/tensor/dtype/filesystem",
            "checkpoint matched-field contract changed",
            errors,
        )
        _check(
            _integer(checkpoint.get("tensor_numel")) > 0,
            "matched checkpoint tensor is empty",
            errors,
        )
        _check(
            bool(checkpoint.get("publish_dtype")),
            "matched checkpoint publish dtype is missing",
            errors,
        )
        _check(checkpoint.get("digest_mode") == "off", "matched digest mode is not off", errors)
        _check(
            baseline_count >= CHECKPOINT_PUBLISH_MIN_SAMPLES,
            "matched checkpoint baseline sample count is too small",
            errors,
        )
        _check(
            ha_count >= CHECKPOINT_PUBLISH_MIN_SAMPLES,
            "matched HA checkpoint sample count is too small",
            errors,
        )
        _check(
            checkpoint.get("max_p99_ratio") == CHECKPOINT_PUBLISH_MAX_P99_RATIO
            and checkpoint.get("jitter_seconds") == CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
            "matched checkpoint threshold definition changed",
            errors,
        )
        _check(
            math.isclose(
                _floating(checkpoint.get("allowed_ha_p99_seconds")),
                expected_limit,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ),
            "matched checkpoint allowed p99 was not derived from the frozen threshold",
            errors,
        )
        _check(
            math.isfinite(ha_p99) and ha_p99 <= expected_limit,
            f"HA checkpoint p99 regression exceeded threshold: {ha_p99} > {expected_limit}",
            errors,
        )
    return errors


def check_run(
    run_root: Path,
    *,
    mode: str,
    matched_performance_path: Path | None = None,
) -> dict[str, Any]:
    paths = RunPaths(run_root.resolve())
    loaded = load_run_descriptor(paths.shared_root)
    config = loaded.config
    errors: list[str] = []
    warnings: list[str] = []
    matched_performance: dict[str, Any] | None = None
    matched_performance_sha256: str | None = None
    if matched_performance_path is not None:
        try:
            matched_performance = safe_read_json(matched_performance_path.resolve())
            matched_performance_sha256 = sha256_file(matched_performance_path.resolve())
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"matched performance artifact is unreadable: {exc!r}")
    inventory_path = (
        Path(__file__).resolve().parents[2] / "plans/artifacts/plan02_phase1_mutator_inventory.json"
    )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory_count = len(inventory["full_common"]) + len(inventory["fragment_legacy"])
    _check(
        inventory_count == inventory["frozen_count"] == 31,
        f"mutator inventory count mismatch: {inventory_count}",
        errors,
    )
    conn = open_readonly(paths.sqlite_db)
    try:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        schema = dict(conn.execute("SELECT * FROM schema_meta WHERE singleton=1").fetchone())
        leader_row = conn.execute("SELECT * FROM syncer_leader WHERE singleton=1").fetchone()
        leader = None if leader_row is None else dict(leader_row)
        epochs = [dict(row) for row in conn.execute("SELECT * FROM syncer_epochs ORDER BY epoch")]
        active_versions = [
            dict(row) for row in conn.execute("SELECT * FROM global_versions ORDER BY version")
        ]
        terminal_row = conn.execute("SELECT * FROM terminal_state WHERE singleton=1").fetchone()
        terminal = None if terminal_row is None else dict(terminal_row)
        controller_row = conn.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
        controller = None if controller_row is None else dict(controller_row)
        active_updates = [dict(row) for row in conn.execute("SELECT * FROM updates")]
        publications = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM control_publications ORDER BY published_by_epoch, kind, logical_generation"
            )
        ]
        gc_rows = [dict(row) for row in conn.execute("SELECT * FROM gc_candidates")]
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        pragmas = {
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
            "query_only": int(conn.execute("PRAGMA query_only").fetchone()[0]),
        }
    finally:
        conn.close()

    _check(integrity == ["ok"], f"integrity_check={integrity}", errors)
    _check(schema["schema_version"] == 2, "schema version is not 2", errors)
    _check(schema["protocol_version"] == 3, "protocol version is not 3", errors)
    _check(schema["mode"] == "full", "schema mode is not full", errors)
    _check(pragmas["user_version"] == 2, "PRAGMA user_version is not 2", errors)
    _check(pragmas["journal_mode"].lower() == "delete", "journal_mode is not DELETE", errors)
    _check(pragmas["synchronous"] == 2, "synchronous is not FULL", errors)
    _check(pragmas["query_only"] == 1, "checker connection is not query-only", errors)
    archived_ha_records = _history(paths.syncer_epoch_history_jsonl)
    archived_epochs = [
        row for row in archived_ha_records if row.get("record_kind") == "syncer_epoch"
    ]
    all_epochs_by_id = {int(row["epoch"]): row for row in [*archived_epochs, *epochs]}
    all_epochs = [all_epochs_by_id[key] for key in sorted(all_epochs_by_id)]
    observed_epochs = [int(row["epoch"]) for row in all_epochs]
    _check(
        observed_epochs == list(range(1, len(observed_epochs) + 1)),
        f"epoch sequence is not contiguous: {observed_epochs}",
        errors,
    )
    _check(bool(all_epochs), "no leader epoch was recorded", errors)
    for row in all_epochs:
        _check(
            row["source_fingerprint"] == loaded.identity.source_fingerprint,
            f"epoch {row['epoch']} source mismatch",
            errors,
        )
        _check(
            row["config_sha256"] == loaded.identity.config_sha256,
            f"epoch {row['epoch']} config mismatch",
            errors,
        )

    archived_versions = _history(paths.global_version_history_jsonl)
    all_versions = sorted(
        [*archived_versions, *active_versions], key=lambda row: int(row["version"])
    )
    version_numbers = [int(row["version"]) for row in all_versions]
    active_version_numbers = {int(row["version"]) for row in active_versions}
    _check(
        version_numbers == list(range(0, max(version_numbers, default=-1) + 1)),
        f"global version sequence is not contiguous: {version_numbers}",
        errors,
    )
    checkpoint_evidence: list[dict[str, Any]] = []
    for row in all_versions:
        epoch = row.get("commit_epoch")
        owner = row.get("commit_owner_id")
        publication_id = row.get("publication_id")
        _check(epoch is not None and owner, f"version {row['version']} lacks leader", errors)
        _check(bool(publication_id), f"version {row['version']} lacks publication_id", errors)
        for path_field, size_field, digest_field in (
            ("weight_path", "weight_size_bytes", "weight_sha256"),
            ("optim_path", "optim_size_bytes", "optim_sha256"),
        ):
            artifact = _relative_artifact(paths, str(row[path_field]))
            is_active = int(row["version"]) in active_version_numbers
            if is_active:
                _check(artifact.is_file(), f"missing active checkpoint: {artifact}", errors)
            if artifact.is_file():
                _check(
                    artifact.stat().st_size == int(row[size_field]),
                    f"checkpoint size mismatch: {artifact}",
                    errors,
                )
                if config.io.checkpoint_digest_mode == "always":
                    _check(
                        sha256_file(artifact) == row[digest_field],
                        f"checkpoint digest mismatch: {artifact}",
                        errors,
                    )
                try:
                    with safe_open(artifact, framework="pt", device="cpu") as handle:
                        _check(bool(handle.keys()), f"empty safetensors: {artifact}", errors)
                except Exception as exc:
                    errors.append(f"invalid safetensors {artifact}: {exc!r}")
            observed_digest = (
                sha256_file(artifact)
                if artifact.is_file() and config.io.checkpoint_digest_mode in {"checker", "always"}
                else None
            )
            checkpoint_evidence.append(
                {
                    "version": int(row["version"]),
                    "artifact_kind": path_field.removesuffix("_path"),
                    "path": str(artifact),
                    "active": is_active,
                    "exists": artifact.is_file(),
                    "size_bytes": artifact.stat().st_size if artifact.is_file() else None,
                    "observed_sha256": observed_digest,
                }
            )
            if config.io.checkpoint_digest_mode == "off":
                _check(row.get(digest_field) is None, f"off mode stored {digest_field}", errors)

    control_evidence: list[dict[str, Any]] = []
    for publication in publications:
        artifact = paths.shared_root / str(publication["relative_path"])
        exists = artifact.is_file()
        digest = sha256_file(artifact) if exists else None
        valid = exists and digest == publication["sha256"]
        _check(valid, f"invalid control publication: {artifact}", errors)
        control_evidence.append({**publication, "exists": exists, "observed_sha256": digest})

    epoch_weights = list(paths.iter_epoch_weights())
    epoch_optim = list(paths.iter_epoch_optim())
    heartbeats = list(paths.iter_syncer_heartbeats())
    expected_versions = len(active_versions)
    _check(
        len(epoch_weights) >= expected_versions, "epoch weight discovery was empty/short", errors
    )
    _check(len(epoch_optim) >= expected_versions, "epoch optim discovery was empty/short", errors)
    _check(bool(heartbeats), "syncer heartbeat discovery was empty", errors)

    epoch_dirs = sorted(paths.syncer_epochs.glob("e*_*"))
    _check(
        len(epoch_dirs) <= config.coordination.syncer_ha.max_retained_epoch_dirs,
        "active epoch control directories exceed configured retention",
        errors,
    )
    active_claim_dirs = list(paths.syncer_launch_claims.glob("*/attempt_*.lock"))
    claim_evidence: list[dict[str, Any]] = []
    for directory in active_claim_dirs:
        claim_evidence.append(
            {
                "path": str(directory),
                "claim": safe_read_json(directory / "claim.json"),
                "submission": safe_read_json(directory / "submission.json"),
            }
        )
    canonical_reader = EpochControlReader(paths, run_id=loaded.identity.run_id)
    try:
        try:
            canonical_latest = canonical_reader.read_current_latest()
            canonical_terminal = canonical_reader.read_current_terminal()
        except Exception as exc:
            canonical_latest = None
            canonical_terminal = None
            errors.append(f"canonical control validation failed: {exc!r}")
    finally:
        canonical_reader.close()
    fixed_latest = safe_read_json(paths.latest_json)
    fixed_stop = safe_read_json(paths.stop_json)

    def cache_matches(cache: dict[str, Any] | None, canonical: dict[str, Any] | None) -> bool:
        if cache is None or canonical is None:
            return cache is canonical
        identity_fields = ("run_id", "epoch", "owner_id")
        return all(cache.get(key) == canonical.get(key) for key in identity_fields)

    runtime_events = _jsonl_events(paths.logs)
    learner_events = [event for path in paths.iter_learner_logs() for event in _history(path)]
    syncer_events = [event for path in paths.iter_syncer_logs() for event in _history(path)]
    syncer_exits = [event for event in syncer_events if event.get("event_type") == "process_exit"]
    learner_exits = [
        event
        for event in runtime_events
        if event.get("event_type") == "process_exit" and "control_scan_count" in event
    ]
    renew_samples = [
        float(sample) for event in syncer_exits for sample in event.get("lease_renew_seconds", [])
    ]
    business_samples = [
        float(sample)
        for event in syncer_exits
        for sample in event.get("business_transaction_seconds", [])
    ]
    checkpoint_publish_samples = [
        float(event["publish_checkpoint_seconds"])
        for event in syncer_events
        if event.get("event_type") == "global_published"
        and event.get("publish_checkpoint_seconds") is not None
    ]
    takeover_samples: list[float] = []
    for predecessor, successor in zip(all_epochs, all_epochs[1:]):
        if predecessor.get("final_state") != "expired":
            continue
        expiry_boundary = float(predecessor["last_renewed_at"]) + float(
            config.coordination.syncer_ha.lease_duration_seconds
        )
        takeover_samples.append(max(0.0, float(successor["acquired_at"]) - expiry_boundary))
    candidate_events = _jsonl_events(paths.logs / "candidates")
    renew_failure_count = sum(
        int(event.get("lease_renew_failure_count", 0)) for event in syncer_exits
    )
    business_failure_count = sum(
        int(event.get("business_transaction_failure_count", 0)) for event in syncer_exits
    )
    control_scan_count = sum(int(event["control_scan_count"]) for event in learner_exits)
    control_scan_cache_hit_count = sum(
        int(event.get("control_scan_cache_hit_count", 0)) for event in learner_exits
    )
    control_scan_wall_seconds = sum(
        float(event.get("control_scan_wall_seconds", 0.0)) for event in learner_exits
    )
    control_scan_cpu_seconds = sum(
        float(event.get("control_scan_cpu_seconds", 0.0)) for event in learner_exits
    )
    renew_p99 = _percentile(renew_samples, 0.99)
    business_p99 = _percentile(business_samples, 0.99)
    performance_missing: list[str] = []
    if not renew_samples:
        performance_missing.append("lease_renew_seconds")
    if not business_samples:
        performance_missing.append("business_transaction_seconds")
    if not takeover_samples:
        performance_missing.append("takeover_protocol_seconds")
    if not checkpoint_publish_samples:
        performance_missing.append("publish_checkpoint_seconds")
    if not learner_exits or control_scan_count <= 0:
        performance_missing.append("learner_control_scan_metrics")
    failure_events = _blocking_failure_events(runtime_events)
    archived_updates = _history(paths.update_history_jsonl)
    updates_by_id = {
        str(row["update_id"]): row
        for row in [*archived_updates, *active_updates]
        if row.get("update_id")
    }
    stale_commit_violations = _stale_business_commit_violations(
        versions=all_versions,
        epochs=all_epochs,
        updates=list(updates_by_id.values()),
        controller=controller,
        terminal=terminal,
        publications=publications,
    )
    canonical_adoption_violations = _canonical_adoption_violations(
        learner_events=learner_events,
        terminal=terminal,
        expected_learner_ids={f"learner_{index:03d}" for index in range(config.sync.num_learners)},
    )
    _check(not failure_events, "run recorded blocking runtime failure events", errors)
    latest_version = max(version_numbers, default=-1)
    completed_ready = (
        terminal is not None
        and terminal.get("stop_reason") not in (None, "", "error")
        and int(terminal["final_version"]) == latest_version
        and latest_version >= 10
        and leader is not None
        and leader.get("state") == "released"
    )
    if mode == "phase1-completed":
        matched_errors = _matched_performance_errors(
            matched_performance,
            expected_identity={
                "run_id": loaded.identity.run_id,
                "descriptor_sha256": loaded.descriptor["descriptor_sha256"],
                "git_commit": loaded.descriptor["git_commit"],
                "git_dirty": loaded.descriptor["git_dirty"],
                "source_fingerprint": loaded.identity.source_fingerprint,
                "config_sha256": loaded.identity.config_sha256,
            },
        )
        errors.extend(matched_errors)
        _check(completed_ready, "completed gate requires terminal release and >=10 merges", errors)
        _check(
            len(active_versions) == 1,
            f"completed gate requires one active global version, found {len(active_versions)}",
            errors,
        )
        _check(canonical_latest is not None, "completed gate lacks canonical latest", errors)
        _check(canonical_terminal is not None, "completed gate lacks canonical terminal", errors)
        _check(
            not performance_missing,
            f"completed gate lacks core performance metrics: {performance_missing}",
            errors,
        )
        _check(
            len(renew_samples) >= 100,
            f"completed gate requires >=100 real renew samples, found {len(renew_samples)}",
            errors,
        )
        _check(renew_failure_count == 0, "normal run recorded lease renew failures", errors)
        _check(
            not stale_commit_violations,
            f"stale/invalid business commit evidence: {stale_commit_violations}",
            errors,
        )
        _check(
            not canonical_adoption_violations,
            f"canonical learner adoption evidence failed: {canonical_adoption_violations}",
            errors,
        )
        if renew_p99 is not None:
            _check(
                renew_p99 < config.coordination.syncer_ha.lease_duration_seconds / 4.0,
                f"lease renew p99 exceeded threshold: {renew_p99}",
                errors,
            )
        _check(
            business_failure_count == 0,
            "normal run recorded failed business transactions",
            errors,
        )
        if business_p99 is not None:
            _check(
                business_p99 < config.coordination.syncer_ha.renew_interval_seconds / 2.0,
                f"business transaction p99 exceeded threshold: {business_p99}",
                errors,
            )
        if takeover_samples:
            takeover_threshold = 2.0 * config.coordination.syncer_ha.renew_interval_seconds + 10.0
            _check(
                max(takeover_samples) <= takeover_threshold,
                "takeover protocol latency exceeded threshold: "
                f"{max(takeover_samples)} > {takeover_threshold}",
                errors,
            )
        if canonical_latest is not None and active_versions and leader is not None:
            active_latest = max(active_versions, key=lambda row: int(row["version"]))
            expected_latest = {
                "epoch": int(leader["epoch"]),
                "owner_id": str(leader["owner_id"]),
                "version": int(active_latest["version"]),
                "source_commit_epoch": int(active_latest["commit_epoch"]),
                "source_commit_owner_id": str(active_latest["commit_owner_id"]),
                "publication_id": str(active_latest["publication_id"]),
                "weight_path": str(_relative_artifact(paths, str(active_latest["weight_path"]))),
                "optim_path": str(_relative_artifact(paths, str(active_latest["optim_path"]))),
            }
            for field, expected in expected_latest.items():
                _check(
                    canonical_latest.get(field) == expected,
                    f"canonical latest {field} mismatch: "
                    f"{canonical_latest.get(field)!r} != {expected!r}",
                    errors,
                )
        if canonical_terminal is not None and terminal is not None:
            expected_terminal = {
                "epoch": int(terminal["finalized_by_epoch"]),
                "owner_id": str(terminal["finalized_by_owner_id"]),
                "generation": int(terminal["generation"]),
                "stop_reason": str(terminal["stop_reason"]),
                "final_version": int(terminal["final_version"]),
                "total_seen_tokens": int(terminal["total_seen_tokens"]),
            }
            for field, expected in expected_terminal.items():
                _check(
                    canonical_terminal.get(field) == expected,
                    f"canonical terminal {field} mismatch: "
                    f"{canonical_terminal.get(field)!r} != {expected!r}",
                    errors,
                )
            current_publications = {
                (str(row["kind"]), int(row["logical_generation"]))
                for row in publications
                if int(row["published_by_epoch"]) == int(terminal["finalized_by_epoch"])
                and str(row["published_by_owner_id"]) == str(terminal["finalized_by_owner_id"])
            }
            generation = int(terminal["generation"])
            for kind in ("stop", "summary"):
                _check(
                    (kind, generation) in current_publications,
                    f"completed gate lacks {kind} publication for generation {generation}",
                    errors,
                )
        status = "PASS" if not errors else "BLOCKED"
    else:
        status = (
            "PASS"
            if completed_ready and not errors
            else ("PASS_WITH_FOLLOWUPS" if not errors else "BLOCKED")
        )
        if status == "PASS_WITH_FOLLOWUPS":
            warnings.append(
                "run is internally consistent but has not met the completed workload gate"
            )
    return {
        "checker": "plan02_phase1",
        "mode": mode,
        "status": status,
        "checked_at": time.time(),
        "run_root": str(paths.shared_root),
        "identity": loaded.identity.as_dict(),
        "mutator_inventory": {
            "path": str(inventory_path),
            "frozen_count": inventory["frozen_count"],
            "observed_count": inventory_count,
        },
        "schema": {**schema, "integrity_check": integrity, "pragmas": pragmas},
        "leader": leader,
        "epochs": all_epochs,
        "version_to_epoch": [
            {
                "version": int(row["version"]),
                "epoch": row.get("commit_epoch"),
                "owner_id": row.get("commit_owner_id"),
                "publication_id": row.get("publication_id"),
            }
            for row in all_versions
        ],
        "checkpoint_evidence": checkpoint_evidence,
        "terminal": terminal,
        "control_publications": control_evidence,
        "fixed_cache": {
            "latest_present": fixed_latest is not None,
            "latest_matches_canonical_identity": cache_matches(fixed_latest, canonical_latest),
            "stop_present": fixed_stop is not None,
            "stop_matches_canonical_identity": cache_matches(fixed_stop, canonical_terminal),
            "authoritative_reader": "epoch_control_filesystem",
        },
        "discovery": {
            "epoch_weights": {"expected_min": expected_versions, "observed": len(epoch_weights)},
            "epoch_optim": {"expected_min": expected_versions, "observed": len(epoch_optim)},
            "syncer_heartbeats": {"expected_min": 1, "observed": len(heartbeats)},
            "instance_pointers": {
                "expected_min": 0,
                "observed": len(list(paths.iter_instance_pointers())),
            },
            "instance_payloads": {
                "expected_min": 0,
                "observed": len(list(paths.iter_instance_payloads())),
            },
        },
        "watchdog_recovery": {
            "heartbeat_stale_after_seconds": config.coordination.syncer_ha.heartbeat_stale_after_seconds,
            "candidate_wait_seconds": config.coordination.syncer_ha.candidate_wait_seconds,
            "learner_recovery_wait_seconds": config.coordination.syncer_ha.learner_recovery_wait_seconds,
            "canonical_repair_wait_seconds": config.coordination.syncer_ha.canonical_repair_wait_seconds,
            "recovery_submission_enabled": config.coordination.recovery_submission.enabled,
            "claims": claim_evidence,
        },
        "performance_reliability": {
            "aggregation": "nearest-rank across all retained process-exit samples",
            "warm_up_samples": 0,
            "missing_core_fields": performance_missing,
            "lease_renew": {
                "sample_count": len(renew_samples),
                "failure_count": renew_failure_count,
                "busy_retry_count": sum(
                    int(event.get("lease_renew_busy_retry_count", 0)) for event in syncer_exits
                ),
                "p95_seconds": _percentile(renew_samples, 0.95),
                "p99_seconds": renew_p99,
                "max_seconds": max(renew_samples, default=None),
                "threshold_p99_seconds": (
                    config.coordination.syncer_ha.lease_duration_seconds / 4.0
                ),
                "cpu_seconds": sum(
                    float(event.get("lease_renew_cpu_seconds", 0.0)) for event in syncer_exits
                ),
                "wall_seconds": sum(
                    float(event.get("lease_renew_wall_seconds", 0.0)) for event in syncer_exits
                ),
            },
            "heartbeat_publish": {
                "sample_count": sum(
                    int(event.get("heartbeat_publish_count", 0)) for event in syncer_exits
                ),
                "cpu_seconds": sum(
                    float(event.get("heartbeat_publish_cpu_seconds", 0.0)) for event in syncer_exits
                ),
                "wall_seconds": sum(
                    float(event.get("heartbeat_publish_wall_seconds", 0.0))
                    for event in syncer_exits
                ),
            },
            "business_transaction": {
                "sample_count": len(business_samples),
                "failure_count": business_failure_count,
                "p95_seconds": _percentile(business_samples, 0.95),
                "p99_seconds": business_p99,
                "max_seconds": max(business_samples, default=None),
                "threshold_p99_seconds": (
                    config.coordination.syncer_ha.renew_interval_seconds / 2.0
                ),
            },
            "takeover_protocol": {
                "sample_count": len(takeover_samples),
                "samples_seconds": takeover_samples,
                "p95_seconds": _percentile(takeover_samples, 0.95),
                "p99_seconds": _percentile(takeover_samples, 0.99),
                "max_seconds": max(takeover_samples, default=None),
                "threshold_max_seconds": (
                    2.0 * config.coordination.syncer_ha.renew_interval_seconds + 10.0
                ),
                "writer_lock_pause_included": False,
            },
            "learner_control_scan": {
                "process_count": len(learner_exits),
                "scan_count": control_scan_count,
                "cache_hit_count": control_scan_cache_hit_count,
                "wall_seconds": control_scan_wall_seconds,
                "cpu_seconds": control_scan_cpu_seconds,
            },
            "checkpoint_publish": {
                "digest_mode": config.io.checkpoint_digest_mode,
                "sample_count": len(checkpoint_publish_samples),
                "p95_seconds": _percentile(checkpoint_publish_samples, 0.95),
                "p99_seconds": _percentile(checkpoint_publish_samples, 0.99),
                "max_seconds": max(checkpoint_publish_samples, default=None),
            },
            "candidate_observation": {
                "event_count": len(candidate_events),
                "writer_transaction_attempt_count": sum(
                    event.get("event_type") == "candidate_writer_transaction_attempt"
                    for event in candidate_events
                ),
                "writer_lock_blocked_count": sum(
                    event.get("event_type") == "writer_lock_blocked" for event in candidate_events
                ),
            },
            "canonical_adoption_error_count": len(canonical_adoption_violations),
            "canonical_adoption_errors": canonical_adoption_violations,
            "stale_epoch_business_commit_count": len(stale_commit_violations),
            "stale_epoch_business_commit_violations": stale_commit_violations,
            "matched_performance": {
                "path": (
                    None
                    if matched_performance_path is None
                    else str(matched_performance_path.resolve())
                ),
                "sha256": matched_performance_sha256,
                "evidence": matched_performance,
            },
        },
        "failure_event_scan": {
            "event_count": len(runtime_events),
            "failure_count": len(failure_events),
            "failure_events": failure_events,
        },
        "boundedness": {
            "epoch_dirs": len(epoch_dirs),
            "active_claim_dirs": len(active_claim_dirs),
            "gc_candidate_rows": len(gc_rows),
            "sqlite_page_count": page_count,
            "sqlite_freelist_count": freelist_count,
        },
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("phase1-staged", "phase1-completed"), required=True)
    parser.add_argument("--matched-performance", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = check_run(
        args.run_root,
        mode=args.mode,
        matched_performance_path=args.matched_performance,
    )
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(report["status"])
    raise SystemExit(0 if report["status"] in {"PASS", "PASS_WITH_FOLLOWUPS"} else 1)


if __name__ == "__main__":
    main()
