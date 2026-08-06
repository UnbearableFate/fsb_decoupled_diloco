#!/usr/bin/env python3
"""Validate completed Plan 02 Phase 2 evidence from dynamic SQLite authority."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fs_diloco.core.constants import DYNAMIC_SCHEMA_VERSION, PROTOCOL_VERSION
from fs_diloco.core.run_descriptor import load_run_descriptor
from fs_diloco.protocol.membership import read_bootstrap_scheduler_jobs
from fs_diloco.runtime.pbs_scheduler import normalize_job_id
from fs_diloco.storage.atomic_io import safe_read_json, sha256_file
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.schema_bootstrap import open_readonly


ACTIVE_LAUNCH_STATES = {
    "planned",
    "submitting",
    "submitted",
    "started",
    "external_submitted",
    "submission_unknown",
    "retryable",
}
BLOCKING_SYNCER_EVENTS = {
    "error",
    "leader_release_failed",
    "lease_renewer_stop_failed",
    "no_progress_timeout",
    "uncaught_exception",
}


def _history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _dedupe_rows(
    rows: list[dict[str, Any]],
    *,
    key: str,
    name: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Allow crash-replayed archive lines but reject conflicting authority history."""
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row[key])
        prior = unique.get(identity)
        if prior is None:
            unique[identity] = row
            continue
        ignored = {"archived_at", "record_kind"}
        prior_core = {k: v for k, v in prior.items() if k not in ignored}
        row_core = {k: v for k, v in row.items() if k not in ignored}
        if prior_core != row_core:
            errors.append(f"{name} history conflicts for {key}={identity}")
    return list(unique.values())


def _query(connection: Any, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def _load_evidence(path: Path | None, *, name: str, errors: list[str]) -> dict[str, Any] | None:
    if path is None:
        errors.append(f"{name} evidence path is required")
        return None
    payload = safe_read_json(path)
    if not isinstance(payload, dict):
        errors.append(f"{name} evidence is missing or invalid: {path}")
        return None
    if payload.get("status") != "PASS":
        errors.append(f"{name} evidence status is not PASS")
    return payload


def _artifact_identity_errors(
    payload: dict[str, Any] | None,
    *,
    descriptor: dict[str, Any],
    name: str,
) -> list[str]:
    if payload is None:
        return []
    errors: list[str] = []
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        return [f"{name} evidence identity is missing"]
    for field in ("git_commit", "source_fingerprint"):
        if identity.get(field) != descriptor.get(field):
            errors.append(f"{name} evidence {field} mismatch")
    return errors


def check_run(
    run_root: Path,
    *,
    mode: str,
    g7_evidence_path: Path | None = None,
    g8_evidence_path: Path | None = None,
    g9_evidence_path: Path | None = None,
    compatibility_evidence_path: Path | None = None,
    matched_performance_path: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    paths = RunPaths(run_root.resolve())
    try:
        loaded = load_run_descriptor(paths.shared_root)
    except Exception as exc:
        return {
            "checker": "plan02_phase2",
            "mode": mode,
            "status": "BLOCKED",
            "run_root": str(paths.shared_root),
            "errors": [f"run descriptor validation failed: {exc}"],
        }
    descriptor = loaded.descriptor
    config = loaded.config
    if descriptor.get("mode") != "full_ha_dynamic":
        errors.append("run descriptor is not full_ha_dynamic")
    if descriptor.get("schema_version") != DYNAMIC_SCHEMA_VERSION:
        errors.append("descriptor schema version is not dynamic v3")
    if config.membership.mode != "dynamic" or not config.coordination.syncer_ha.enabled:
        errors.append("resolved config is not dynamic full HA")

    g7 = _load_evidence(g7_evidence_path, name="G7", errors=errors)
    g8 = _load_evidence(g8_evidence_path, name="G8", errors=errors)
    g9 = _load_evidence(g9_evidence_path, name="G9", errors=errors)
    compatibility = _load_evidence(
        compatibility_evidence_path,
        name="compatibility",
        errors=errors,
    )
    matched = _load_evidence(
        matched_performance_path,
        name="matched performance",
        errors=errors,
    )
    for name, payload in (
        ("G7", g7),
        ("G8", g8),
        ("G9", g9),
        ("compatibility", compatibility),
        ("matched performance", matched),
    ):
        errors.extend(
            _artifact_identity_errors(payload, descriptor=descriptor, name=name)
        )
    expected_external = {
        "G7": (g7, "plan02_phase2_test_evidence", "g7"),
        "G8": (g8, "plan02_phase2_chaos_evidence", "g8"),
        "G9": (g9, "plan02_phase2_chaos_evidence", "g9"),
        "compatibility": (
            compatibility,
            "plan02_phase2_test_evidence",
            "compatibility",
        ),
        "matched performance": (
            matched,
            "plan02_phase2_matched_performance",
            None,
        ),
    }
    for name, (payload, checker, kind) in expected_external.items():
        if payload is None:
            continue
        if payload.get("checker") != checker:
            errors.append(f"{name} evidence checker mismatch")
        if kind is not None and payload.get("kind") != kind:
            errors.append(f"{name} evidence kind mismatch")
    if g9 is not None:
        g9_identity = g9.get("identity") or {}
        if g9_identity.get("descriptor_sha256") != descriptor.get("descriptor_sha256"):
            errors.append("G9 evidence is not bound to the checked run descriptor")

    marker = safe_read_json(paths.bootstrap_complete_json) or {}
    connection = open_readonly(paths.sqlite_db)
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        schema = dict(
            connection.execute("SELECT * FROM schema_meta WHERE singleton=1").fetchone()
        )
        controller_row = connection.execute(
            "SELECT * FROM controller_state WHERE singleton=1"
        ).fetchone()
        terminal_row = connection.execute(
            "SELECT * FROM terminal_state WHERE singleton=1"
        ).fetchone()
        controller = None if controller_row is None else dict(controller_row)
        terminal = None if terminal_row is None else dict(terminal_row)
        epochs = _query(connection, "SELECT * FROM syncer_epochs ORDER BY epoch")
        versions = _query(connection, "SELECT * FROM global_versions ORDER BY version")
        instances = _query(connection, "SELECT * FROM learner_instances ORDER BY registered_at")
        placements = _query(connection, "SELECT * FROM placements ORDER BY placement_id")
        streams = _query(connection, "SELECT * FROM streams ORDER BY stream_id")
        registrations = _query(
            connection, "SELECT * FROM registration_requests ORDER BY created_at"
        )
        launches = _query(connection, "SELECT * FROM launch_requests ORDER BY created_at")
        observations = _query(
            connection, "SELECT * FROM capacity_observations ORDER BY observation_seq"
        )
        updates = _query(connection, "SELECT * FROM updates ORDER BY committed_at")
        publications = _query(
            connection,
            "SELECT * FROM control_publications ORDER BY published_by_epoch, kind",
        )
        stream_pool_state = connection.execute(
            "SELECT value FROM run_state WHERE key='stream_pool_size'"
        ).fetchone()
        run_state = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute("SELECT key, value FROM run_state")
        }
    finally:
        connection.close()

    if integrity != ["ok"]:
        errors.append(f"SQLite integrity failed: {integrity}")
    if user_version != DYNAMIC_SCHEMA_VERSION:
        errors.append(f"SQLite user_version is {user_version}, expected 3")
    if (
        int(schema.get("schema_version", -1)) != DYNAMIC_SCHEMA_VERSION
        or int(schema.get("protocol_version", -1)) != PROTOCOL_VERSION
        or schema.get("mode") != "full_dynamic"
    ):
        errors.append("schema_meta dynamic identity mismatch")
    if marker.get("schema_version") != DYNAMIC_SCHEMA_VERSION:
        errors.append("bootstrap marker dynamic schema mismatch")
    if journal_mode != "delete" or synchronous < 2:
        errors.append("SQLite journal/synchronous durability settings mismatch")

    configured_pool = int(config.membership.stream_pool_size)
    persisted_pool = (
        None
        if stream_pool_state is None
        else int(json.loads(stream_pool_state["value"]))
    )
    stream_ids = [int(row["stream_id"]) for row in streams]
    if persisted_pool != configured_pool or stream_ids != list(range(configured_pool)):
        errors.append("fixed stream pool does not match config/run_state")

    current_instances = [
        row
        for row in instances
        if str(row["status"]) in {"admitted", "draining", "drained"}
    ]
    placement_currents = [
        str(row["current_instance_id"])
        for row in placements
        if row["current_instance_id"] is not None
    ]
    stream_currents = [
        str(row["current_instance_id"])
        for row in streams
        if row["current_instance_id"] is not None
    ]
    if len(placement_currents) != len(set(placement_currents)):
        errors.append("placement current instance mapping is not unique")
    if len(stream_currents) != len(set(stream_currents)):
        errors.append("stream current instance mapping is not unique")
    for row in current_instances:
        instance_id = str(row["instance_id"])
        placement = next(
            (item for item in placements if item["placement_id"] == row["placement_id"]),
            None,
        )
        stream = next(
            (item for item in streams if item["stream_id"] == row["stream_id"]),
            None,
        )
        if placement is None or stream is None:
            errors.append(f"current instance {instance_id} has missing placement/stream")
            continue
        if (
            str(placement.get("current_instance_id")) != instance_id
            or int(placement["current_placement_epoch"]) != int(row["placement_epoch"])
            or str(stream.get("current_instance_id")) != instance_id
            or int(stream["current_stream_epoch"]) != int(row["stream_epoch"])
        ):
            errors.append(f"current instance {instance_id} is not fenced by placement/stream")

    reserved = [
        row
        for row in launches
        if row["admitted_instance_id"] is None and str(row["state"]) in ACTIVE_LAUNCH_STATES
    ]
    if len(current_instances) + len(reserved) > configured_pool:
        errors.append("current admitted plus reserved capacity exceeds stream pool")

    learner_history = _history(paths.learner_instance_history_jsonl)
    registration_history = _history(paths.registration_history_jsonl)
    launch_history = _history(paths.launch_request_history_jsonl)
    capacity_history = _history(paths.capacity_observation_history_jsonl)
    version_history = _history(paths.global_version_history_jsonl)
    update_history = _history(paths.update_history_jsonl)
    all_launches = _dedupe_rows(
        launches + launch_history,
        key="request_id",
        name="launch request",
        errors=errors,
    )
    all_registrations = _dedupe_rows(
        registrations + registration_history,
        key="instance_id",
        name="registration request",
        errors=errors,
    )
    all_observations = _dedupe_rows(
        observations + capacity_history,
        key="observation_key",
        name="capacity observation",
        errors=errors,
    )
    all_versions = _dedupe_rows(
        versions + version_history,
        key="version",
        name="global version",
        errors=errors,
    )
    all_updates = _dedupe_rows(
        updates + update_history,
        key="update_id",
        name="update",
        errors=errors,
    )
    launch_admissions: dict[str, set[str]] = defaultdict(set)
    for row in all_launches:
        admitted = row.get("admitted_instance_id")
        if admitted:
            launch_admissions[str(row["request_id"])].add(str(admitted))
    duplicate_admissions = {
        request_id: sorted(values)
        for request_id, values in launch_admissions.items()
        if len(values) > 1
    }
    if duplicate_admissions:
        errors.append(f"logical launch requests admitted multiple instances: {duplicate_admissions}")
    bootstrap_slots = {
        int(row["bootstrap_slot"])
        for row in all_launches
        if row.get("reason") == "bootstrap" and row.get("bootstrap_slot") is not None
    }
    if bootstrap_slots != set(range(int(config.membership.bootstrap_instances))):
        errors.append("bootstrap slot history is incomplete")
    try:
        bootstrap_scheduler_jobs = read_bootstrap_scheduler_jobs(
            paths,
            run_id=str(descriptor["run_id"]),
            source_fingerprint=str(descriptor["source_fingerprint"]),
            config_sha256=str(descriptor["resolved_config_sha256"]),
            config_fingerprint=str(descriptor["descriptor_sha256"]),
        )
    except RuntimeError as exc:
        bootstrap_scheduler_jobs = []
        errors.append(f"bootstrap scheduler manifest is invalid: {exc}")
    expected_bootstrap_count = int(config.membership.bootstrap_instances)
    if len(bootstrap_scheduler_jobs) != expected_bootstrap_count:
        errors.append("bootstrap scheduler manifest does not cover every slot")
    manifest_jobs = {
        str(row["request_id"]): str(row["pbs_job_id"])
        for row in bootstrap_scheduler_jobs
    }
    for row in all_launches:
        if row.get("reason") != "bootstrap":
            continue
        manifest_job = manifest_jobs.get(str(row["request_id"]))
        launch_job = row.get("pbs_job_id")
        if (
            manifest_job is None
            or launch_job is None
            or normalize_job_id(manifest_job) != normalize_job_id(str(launch_job))
        ):
            errors.append(
                f"bootstrap request {row['request_id']} lacks its scheduler job identity"
            )
    registration_hashes: dict[str, set[str]] = defaultdict(set)
    for row in all_registrations:
        registration_hashes[str(row["instance_id"])].add(str(row["request_sha256"]))
    if any(len(values) > 1 for values in registration_hashes.values()):
        errors.append("registration instance history contains conflicting accepted checksums")

    lifetime_scale_requests = sum(
        str(row.get("reason")) == "scale_out" for row in all_launches
    )
    if int(run_state.get("scale_launch_request_count", -1)) != lifetime_scale_requests:
        errors.append("persisted lifetime scale launch count is inconsistent")
    maximum_admission_generation = max(
        (int(row.get("admission_generation") or 0) for row in instances),
        default=0,
    )
    if int(run_state.get("admission_generation", -1)) != maximum_admission_generation:
        errors.append("persisted admission generation is inconsistent")

    observation_sequences = [int(row["observation_seq"]) for row in all_observations]
    if len(observation_sequences) != len(set(observation_sequences)):
        errors.append("capacity observation sequences are not unique across active/history")
    if len(observations) > int(config.scaling.capacity_observation_retention_count):
        errors.append("active capacity observation retention bound exceeded")

    updates_by_version: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in all_updates:
        if row.get("learner_instance_id") is None:
            errors.append(f"dynamic update {row.get('update_id')} lacks membership identity")
            continue
        if row.get("status") == "applied":
            updates_by_version[int(row["applied_version"])].append(row)
    for version, rows in updates_by_version.items():
        if len({str(row["placement_id"]) for row in rows}) != len(rows):
            errors.append(f"applied version {version} contains duplicate placement")
        if len({int(row["stream_id"]) for row in rows}) != len(rows):
            errors.append(f"applied version {version} contains duplicate stream")

    if controller is None or str(controller.get("state")) != "terminal":
        errors.append("dynamic controller did not reach terminal")
    if terminal is None:
        errors.append("terminal_state row is missing")
    elif not all_versions or int(terminal["final_version"]) != max(
        int(row["version"]) for row in all_versions
    ):
        errors.append("terminal final version does not match committed head")
    if controller is not None:
        generation = int(controller["generation"])
        if any(
            str(row["status"]) in {"admitted", "draining"}
            for row in current_instances
        ):
            errors.append("terminal controller retains undrained current instances")
        for row in current_instances:
            if str(row["status"]) == "drained" and int(row["drained_generation"] or -1) != generation:
                errors.append(f"instance {row['instance_id']} drain generation mismatch")
    if reserved:
        errors.append("terminal run retains launch capacity that can admit")
    if any(str(row["state"]) == "pending" for row in registrations):
        errors.append("terminal run retains pending registration")

    epoch_owners = {int(row["epoch"]): str(row["owner_id"]) for row in epochs}
    for row in all_versions:
        if epoch_owners.get(int(row["commit_epoch"])) != str(row["commit_owner_id"]):
            errors.append(f"version {row['version']} has invalid epoch writer")
    for row in publications:
        if epoch_owners.get(int(row["published_by_epoch"])) != str(
            row["published_by_owner_id"]
        ):
            errors.append(f"control publication {row['kind']} has invalid epoch writer")
        artifact = paths.shared_root / str(row["relative_path"])
        if not artifact.is_file():
            errors.append(f"control publication is missing: {row['relative_path']}")
        elif sha256_file(artifact) != str(row["sha256"]):
            errors.append(f"control publication checksum mismatch: {row['relative_path']}")

    ordered_versions = sorted(int(row["version"]) for row in all_versions)
    terminal_final_version = (
        None if terminal is None else int(terminal["final_version"])
    )
    if terminal_final_version is None or ordered_versions != list(
        range(0, terminal_final_version + 1)
    ):
        errors.append("global version history is not complete and contiguous")
    if int(config.training.inner_steps) <= 50 or int(
        config.sync.stop_after_outer_steps or 0
    ) <= 10:
        errors.append("G9 workload does not exceed the 50-local-step x 10-global-step baseline")

    discovery = {
        "learner_heartbeats": len(list(paths.iter_learner_heartbeats())),
        "learner_pointers": len(list(paths.iter_instance_pointers())),
        "learner_payloads": len(list(paths.iter_instance_payloads())),
        "registration_request_files": len(list(paths.iter_registration_requests())),
        "syncer_heartbeats": len(list(paths.iter_syncer_heartbeats())),
        "learner_logs": len(list(paths.iter_learner_logs())),
        "syncer_logs": len(list(paths.iter_syncer_logs())),
    }
    for field in (
        "learner_heartbeats",
        "learner_pointers",
        "syncer_heartbeats",
        "learner_logs",
        "syncer_logs",
    ):
        if discovery[field] == 0:
            errors.append(f"dynamic recursive discovery was silently empty: {field}")
    if discovery["registration_request_files"] != 0:
        errors.append("terminal registration discovery surface is not empty")

    syncer_events: list[dict[str, Any]] = []
    for path in paths.iter_syncer_logs():
        syncer_events.extend(_history(path))
    blocking_events = [
        row
        for row in syncer_events
        if str(row.get("event_type", "")).lower() in BLOCKING_SYNCER_EVENTS
    ]
    if blocking_events:
        errors.append(f"syncer failure event count is {len(blocking_events)}")

    if len(instances) > int(config.membership.max_active_instance_records):
        errors.append("active learner instance record bound exceeded")
    if len(registrations) > 2 * int(config.membership.max_active_instance_records):
        errors.append("active registration record bound exceeded")
    if freelist_count > page_count:
        errors.append("SQLite freelist accounting is invalid")

    if g9 is not None:
        chaos = g9.get("chaos")
        if not isinstance(chaos, dict):
            errors.append("G9 chaos evidence is missing")
        else:
            required = {
                "stable_bootstrap_1_plus_8": True,
                "permanent_learner_termination": True,
                "two_unique_low_observations": True,
                "replacement_restored_1_plus_8": True,
                "short_pause_recovered": True,
                "duplicate_physical_job_rejected": True,
                "max_concurrent_compute_nodes_le_9": True,
                "dynamic_close_completed": True,
            }
            for field, expected in required.items():
                if chaos.get(field) is not expected:
                    errors.append(f"G9 chaos predicate failed: {field}")
    if matched is not None:
        overhead = matched.get("dynamic_control_overhead_ratio")
        if not isinstance(overhead, (int, float)) or float(overhead) >= 0.05:
            errors.append("dynamic control critical-path overhead is not below 5%")

    requirement_status = {
        f"MEM-{index:02d}": "PASS" if not errors else "BLOCKED"
        for index in range(1, 21)
    }
    return {
        "checker": "plan02_phase2",
        "mode": mode,
        "status": "PASS" if not errors else "BLOCKED",
        "run_root": str(paths.shared_root),
        "identity": {
            "run_id": descriptor.get("run_id"),
            "git_commit": descriptor.get("git_commit"),
            "source_fingerprint": descriptor.get("source_fingerprint"),
            "descriptor_sha256": descriptor.get("descriptor_sha256"),
            "config_sha256": descriptor.get("resolved_config_sha256"),
            "descriptor_file_sha256": sha256_file(paths.run_descriptor_json),
        },
        "schema": {
            "marker_schema_version": marker.get("schema_version"),
            "schema_meta": schema,
            "user_version": user_version,
            "integrity": integrity,
            "journal_mode": journal_mode,
            "synchronous": synchronous,
        },
        "authority": {
            "epoch_history": epochs,
            "version_to_epoch": {
                str(row["version"]): int(row["commit_epoch"]) for row in all_versions
            },
            "control_publication_count": len(publications),
            "controller": controller,
            "terminal": terminal,
        },
        "membership": {
            "stream_pool_size": configured_pool,
            "streams": streams,
            "active_instances": instances,
            "archived_instance_count": len(learner_history),
            "placements": placements,
            "bootstrap_slots": sorted(bootstrap_slots),
            "bootstrap_scheduler_jobs": bootstrap_scheduler_jobs,
            "logical_request_admission_counts": {
                request_id: len(values) for request_id, values in launch_admissions.items()
            },
            "active_registration_count": len(registrations),
            "archived_registration_count": len(registration_history),
            "active_launch_count": len(launches),
            "archived_launch_count": len(launch_history),
            "reserved_capacity": len(reserved),
        },
        "capacity": {
            "active_observation_count": len(observations),
            "archived_observation_count": len(capacity_history),
            "observation_kind_counts": dict(
                Counter(str(row["kind"]) for row in all_observations)
            ),
            "scale_launch_request_count": run_state.get(
                "scale_launch_request_count"
            ),
            "last_scale_launch_at": run_state.get("last_scale_launch_at"),
            "admission_generation": run_state.get("admission_generation"),
        },
        "discovery": discovery,
        "boundedness": {
            "sqlite_page_count": page_count,
            "sqlite_freelist_count": freelist_count,
            "active_instance_limit": config.membership.max_active_instance_records,
            "active_observation_limit": config.scaling.capacity_observation_retention_count,
        },
        "external_evidence": {
            "g7": None if g7_evidence_path is None else str(g7_evidence_path.resolve()),
            "g8": None if g8_evidence_path is None else str(g8_evidence_path.resolve()),
            "g9": None if g9_evidence_path is None else str(g9_evidence_path.resolve()),
            "compatibility": (
                None
                if compatibility_evidence_path is None
                else str(compatibility_evidence_path.resolve())
            ),
            "matched_performance": (
                None
                if matched_performance_path is None
                else str(matched_performance_path.resolve())
            ),
        },
        "failure_event_scan": {
            "syncer_event_count": len(syncer_events),
            "blocking_count": len(blocking_events),
            "blocking_events": blocking_events,
        },
        "requirement_status": requirement_status,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("phase2-completed",), required=True)
    parser.add_argument("--g7-evidence", type=Path, required=True)
    parser.add_argument("--g8-evidence", type=Path, required=True)
    parser.add_argument("--g9-evidence", type=Path, required=True)
    parser.add_argument("--compatibility-evidence", type=Path, required=True)
    parser.add_argument("--matched-performance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = check_run(
        args.run_root,
        mode=args.mode,
        g7_evidence_path=args.g7_evidence,
        g8_evidence_path=args.g8_evidence,
        g9_evidence_path=args.g9_evidence,
        compatibility_evidence_path=args.compatibility_evidence,
        matched_performance_path=args.matched_performance,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(report["status"])
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
