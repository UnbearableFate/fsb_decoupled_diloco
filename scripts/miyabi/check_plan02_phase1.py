#!/usr/bin/env python3
"""Validate Plan 02 Phase 1 HA evidence without trusting fixed control caches."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from safetensors import safe_open

from fs_diloco.core.run_descriptor import load_run_descriptor
from fs_diloco.protocol.control_epoch import EpochControlReader
from fs_diloco.storage.atomic_io import safe_read_json, sha256_file
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.schema_bootstrap import open_readonly


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


def _jsonl_events(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not root.is_dir():
        return events
    for path in sorted(root.rglob("*.jsonl")):
        events.extend(_history(path))
    return events


def check_run(run_root: Path, *, mode: str) -> dict[str, Any]:
    paths = RunPaths(run_root.resolve())
    loaded = load_run_descriptor(paths.shared_root)
    config = loaded.config
    errors: list[str] = []
    warnings: list[str] = []
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
    failure_events = [
        event
        for event in runtime_events
        if str(event.get("event_type", "")).lower() in {"error", "uncaught_exception"}
    ]
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
        _check(completed_ready, "completed gate requires terminal release and >=10 merges", errors)
        _check(
            len(active_versions) == 1,
            f"completed gate requires one active global version, found {len(active_versions)}",
            errors,
        )
        _check(canonical_latest is not None, "completed gate lacks canonical latest", errors)
        _check(canonical_terminal is not None, "completed gate lacks canonical terminal", errors)
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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = check_run(args.run_root, mode=args.mode)
    output = args.output
    if output is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = args.run_root / "reports" / "phase1" / f"{stamp}_{args.mode}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(report["status"])
    raise SystemExit(0 if report["status"] in {"PASS", "PASS_WITH_FOLLOWUPS"} else 1)


if __name__ == "__main__":
    main()
