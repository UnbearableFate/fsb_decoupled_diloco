#!/usr/bin/env python3
"""Build strict evidence from one terminal Full Protocol run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from fs_diloco.core.config import config_to_dict, load_config
from fs_diloco.core.source_identity import capture_source_identity
from fs_diloco.core.versions import (
    ACTOR_ATTESTATION_FORMAT_VERSION,
    AUTHORITY_SCHEMA_VERSION,
    PROTOCOL_VERSION,
)
from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    AuthorityReader,
    ddl_bundle_sha256,
)
from fs_diloco.storage.audit_archive import (
    validate_audit_batch,
    validate_audit_partition,
    validate_audit_partition_manifest,
)
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.run_initializer import validate_completed_run_for_actor


class GatePrerequisiteUnavailable(RuntimeError):
    """The registered run prerequisite does not exist, so validation cannot start."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")


def _require_immutable_file(path: Path, *, label: str) -> None:
    _require_regular_file(path, label=label)
    if path.lstat().st_mode & 0o222:
        raise RuntimeError(f"{label} is writable: {path}")


def _protocol_file(run_root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or "\\" in relative_path
        or "\0" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError(f"protocol path is not canonical: {relative_path}")
    current = run_root
    for component in relative.parts[:-1]:
        current = current / component
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"protocol path parent is not a directory: {relative_path}")
    return current / relative.name


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query).fetchall()]


def _source_identity(project_root: Path) -> dict[str, Any]:
    captured = capture_source_identity(project_root)
    return {
        "commit": captured["git_commit"],
        "dirty": captured["git_dirty"],
        "scopes": captured["source_scopes"],
        "fingerprint": captured["source_fingerprint"],
    }


def _package_provenance() -> dict[str, str]:
    packages: dict[str, str] = {}
    for distribution in ("torch", "transformers", "datasets", "safetensors", "PyYAML"):
        try:
            packages[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = "not-installed"
    return packages


def _pbs_nodes() -> list[str]:
    nodefile = os.environ.get("PBS_NODEFILE")
    if not nodefile:
        return []
    path = Path(nodefile)
    if not path.is_file():
        return []
    return sorted({line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line})


def _merge_history_rows(
    hot: list[dict[str, Any]],
    archived: list[dict[str, Any]],
    *,
    key: str,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*archived, *hot]:
        identity = str(row[key])
        previous = merged.get(identity)
        if previous is not None and previous != row:
            raise RuntimeError(f"conflicting hot and archived {key}: {identity}")
        merged[identity] = row
    return list(merged.values())


def _audit_rows(run_root: Path, table: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    roots = (
        ("batch", run_root / "audit/batches/authority_history"),
        ("partition", run_root / "audit/partitions/authority_history"),
    )
    for artifact_kind, root in roots:
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RuntimeError(f"audit {artifact_kind} root is not a directory: {root}")
        for path in sorted(root.glob("*.json")):
            if path.name.endswith(".manifest.json"):
                continue
            _require_immutable_file(path, label=f"audit {artifact_kind}")
            payload = _read_json(path)
            if artifact_kind == "batch":
                validate_audit_batch(payload)
            else:
                validate_audit_partition(payload)
                manifest_path = path.with_suffix(".manifest.json")
                _require_immutable_file(manifest_path, label="audit partition manifest")
                validate_audit_partition_manifest(
                    paths=RunPaths(run_root),
                    partition=payload,
                    manifest=_read_json(manifest_path),
                )
            for record in payload.get("records", []):
                if not isinstance(record, dict) or record.get("table") != table:
                    continue
                key = str(record.get("primary_key"))
                row = record.get("row")
                if not key or not isinstance(row, dict):
                    raise RuntimeError(f"invalid {table} audit record: {path}")
                previous = records.get(key)
                if previous is not None and previous != row:
                    raise RuntimeError(f"conflicting {table} audit record: {key}")
                records[key] = row
    return list(records.values())


def _attestations(run_root: Path) -> list[dict[str, Any]]:
    root = run_root / "metrics/attestations"
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("actor attestation root is not a directory")
    attestations: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    required_fields = {
        "format_version",
        "run_id",
        "descriptor_sha256",
        "source_fingerprint",
        "source_lock_sha256",
        "model_identity",
        "tokenizer_identity",
        "dataset_identity",
        "actor_kind",
        "actor_id",
        "attempt_id",
        "hostname",
        "pid",
        "python_version",
        "runtime_evidence",
        "scheduler_job_id",
        "accelerator_identity",
        "observed_at",
        "attestation_sha256",
    }
    for path in sorted(root.rglob("*.json")):
        _protocol_file(run_root, path.relative_to(run_root).as_posix())
        _require_immutable_file(path, label="actor attestation")
        payload = _read_json(path)
        if set(payload) != required_fields or payload.get(
            "format_version"
        ) != ACTOR_ATTESTATION_FORMAT_VERSION:
            raise RuntimeError(f"actor attestation schema is invalid: {path}")
        identity = (
            str(payload["actor_kind"]),
            str(payload["actor_id"]),
            str(payload["attempt_id"]),
        )
        expected_path = root / identity[0] / identity[1] / f"{identity[2]}.json"
        if identity in identities or path != expected_path:
            raise RuntimeError(f"actor attestation identity/path is invalid: {path}")
        if identity[0] not in {"learner", "syncer"}:
            raise RuntimeError(f"actor attestation kind is invalid: {path}")
        runtime_evidence = payload["runtime_evidence"]
        if not isinstance(runtime_evidence, dict) or set(runtime_evidence) != {
            "torch_version",
            "cuda_runtime_version",
            "gpu_driver_version",
            "module_environment",
            "resource_allocation",
        }:
            raise RuntimeError(f"actor runtime evidence is invalid: {path}")
        identities.add(identity)
        attestations.append(payload)
    return attestations


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256_without(payload: dict[str, Any], field: str) -> str:
    content = {key: value for key, value in payload.items() if key != field}
    raw = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verify_publication_objects(
    run_root: Path, versions: list[dict[str, Any]], errors: list[str]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for row in versions:
        version = int(row["version"])
        for kind, path_field, size_field, sha_field in (
            ("weight", "weight_relative_path", "weight_size", "weight_sha256"),
            ("outer_state", "optim_relative_path", "optim_size", "optim_sha256"),
        ):
            relative = str(row[path_field])
            try:
                path = _protocol_file(run_root, relative)
            except (FileNotFoundError, RuntimeError):
                errors.append(f"publication object path is not canonical: {relative}")
                evidence.append(
                    {
                        "version": version,
                        "kind": kind,
                        "relative_path": relative,
                        "status": "invalid_path",
                    }
                )
                continue
            item = {"version": version, "kind": kind, "relative_path": relative}
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                errors.append(f"publication object is missing: {relative}")
                item["status"] = "missing"
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    errors.append(f"publication object is not a regular file: {relative}")
                    item["status"] = "invalid_type"
                    evidence.append(item)
                    continue
                size = metadata.st_size
                sha256 = _sha256(path)
                item.update({"size": size, "sha256": sha256})
                if size != int(row[size_field]) or sha256 != str(row[sha_field]):
                    errors.append(f"publication object identity mismatch: {relative}")
                    item["status"] = "mismatch"
                else:
                    item["status"] = "ok"
            evidence.append(item)
    return evidence


def _token_balance(row: dict[str, Any] | None) -> int:
    if row is None:
        return 0
    return int(row["adjudicated_processed"]) - sum(
        int(row[name])
        for name in (
            "local_discarded",
            "direct_applied",
            "direct_dropped",
            "direct_quarantined_or_conflicted",
            "direct_reported_unpublished",
            "direct_outstanding",
        )
    )


def _registered_epoch_states(expected_syncer_epochs: int) -> tuple[str, ...]:
    if expected_syncer_epochs == 1:
        return ("released",)
    if expected_syncer_epochs == 2:
        return ("expired", "released")
    raise ValueError("registered Full Protocol scenarios require one or two syncer epochs")


def _evidence_paths(
    *,
    run_root: Path,
    log_root: Path,
    attestations: list[dict[str, Any]],
) -> list[str]:
    candidates = [
        run_root / "control/run_descriptor.json",
        run_root / "control/run_config.resolved.yaml",
        run_root / "control/run_source_manifest.json",
        run_root / "control/syncer_metadata.sqlite3",
        run_root / "control/summary.json",
        run_root / "control/stop.json",
        log_root / "source_identity.json",
        log_root / "init_run.json",
        log_root / "summary.json",
        log_root / "learner_replacement.json",
        log_root / "syncer_takeover.json",
        log_root / "syncer_primary_fault_boundary.json",
    ]
    attestation_root = run_root / "metrics/attestations"
    if attestations and attestation_root.is_dir():
        candidates.extend(sorted(attestation_root.rglob("*.json")))
    candidates.extend(
        sorted(
            (run_root / "control/syncer_epochs").glob("e*_*/terminal/stop_*.json")
        )
    )
    return sorted(
        {
            str(path.resolve())
            for path in candidates
            if path.is_file() and not path.is_symlink()
        }
    )


def _partial_evidence_paths(run_root: Path, log_root: Path, output: Path) -> list[str]:
    files: list[Path] = []
    for root in (run_root / "control", log_root):
        if root.is_dir():
            files.extend(
                path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
            )
    output = output.resolve()
    return sorted({str(path.resolve()) for path in files if path.resolve() != output})


def validate_run(
    *,
    gate: str,
    experiment_id: str,
    requirement_id: str,
    project_root: Path,
    run_root: Path,
    log_root: Path,
    expected_global_steps: int,
    expected_inner_steps: int,
    expected_contributors: int,
    expected_hosts: int,
    expected_syncer_epochs: int,
    expected_replaced_learner: str | None,
) -> dict[str, Any]:
    validate_completed_run_for_actor(run_root)
    descriptor_path = run_root / "control/run_descriptor.json"
    resolved_config_path = run_root / "control/run_config.resolved.yaml"
    source_manifest_path = run_root / "control/run_source_manifest.json"
    summary_path = run_root / "control/summary.json"
    stop_path = run_root / "control/stop.json"
    database_path = run_root / "control/syncer_metadata.sqlite3"
    for path, label in (
        (descriptor_path, "run descriptor"),
        (resolved_config_path, "resolved config"),
        (source_manifest_path, "source manifest"),
        (summary_path, "terminal summary"),
        (stop_path, "terminal stop"),
        (database_path, "authority database"),
    ):
        _require_regular_file(path, label=label)
    descriptor = _read_json(descriptor_path)
    resolved_config = load_config(resolved_config_path)
    config = config_to_dict(resolved_config)
    summary = _read_json(summary_path)
    stop = _read_json(stop_path)
    database = database_path.resolve()
    reader = AuthorityReader(
        database,
        AuthorityIdentity(
            run_id=str(descriptor.get("run_id", "")),
            source_fingerprint=str(descriptor.get("source_fingerprint", "")),
            config_sha256=str(descriptor.get("resolved_config_sha256", "")),
        ),
        StaticMembershipScope(
            tuple(f"learner_{index:03d}" for index in range(expected_contributors))
        ),
        busy_timeout_ms=resolved_config.leader.business_busy_timeout_ms,
    )
    reader.close()
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        schema_meta = _rows(connection, "SELECT * FROM schema_meta WHERE singleton=1")
        run_identity = _rows(connection, "SELECT * FROM run_identity WHERE singleton=1")
        terminal = _rows(connection, "SELECT * FROM terminal_state")
        controller = _rows(connection, "SELECT * FROM controller_state WHERE singleton=1")
        epochs = _rows(connection, "SELECT * FROM syncer_epochs ORDER BY epoch")
        hot_versions = _rows(connection, "SELECT * FROM global_versions ORDER BY version")
        hot_receipts = _rows(
            connection,
            "SELECT * FROM cycle_receipts ORDER BY stable_contributor_key, cycle_seq",
        )
        hot_updates = _rows(
            connection,
            "SELECT * FROM updates ORDER BY stable_contributor_key, cycle_seq",
        )
        progress = _rows(
            connection,
            "SELECT * FROM contributor_progress ORDER BY stable_contributor_key",
        )
        selected_credit = _rows(
            connection,
            "SELECT stable_contributor_key, committed_credit, last_committed_version "
            "FROM selection_state ORDER BY stable_contributor_key",
        )
        rollups = _rows(connection, "SELECT * FROM token_rollups WHERE singleton=1")
        terminal_fences = _rows(
            connection,
            "SELECT * FROM terminal_contributor_fences ORDER BY stable_contributor_key",
        )
        bindings = _rows(
            connection,
            "SELECT * FROM static_contributor_bindings ORDER BY learner_id",
        )
        binding_history = _rows(
            connection,
            "SELECT * FROM static_binding_history ORDER BY learner_id, binding_generation",
        )
        pending = _rows(
            connection,
            "SELECT update_id, status FROM updates WHERE status IN ('pending', 'selected')",
        )
        prepared = _rows(
            connection,
            "SELECT publication_id FROM publication_intents WHERE state='prepared'",
        )
    finally:
        connection.close()

    versions = sorted(
        _merge_history_rows(
            hot_versions,
            _audit_rows(run_root, "global_versions"),
            key="version",
        ),
        key=lambda row: int(row["version"]),
    )
    receipts = sorted(
        _merge_history_rows(
            hot_receipts,
            _audit_rows(run_root, "cycle_receipts"),
            key="receipt_id",
        ),
        key=lambda row: (str(row["stable_contributor_key"]), int(row["cycle_seq"])),
    )
    updates = sorted(
        _merge_history_rows(
            hot_updates,
            _audit_rows(run_root, "updates"),
            key="update_id",
        ),
        key=lambda row: (str(row["stable_contributor_key"]), int(row["cycle_seq"])),
    )
    errors: list[str] = []
    final_version = int(terminal[0]["final_version"]) if len(terminal) == 1 else -1
    expected_versions = list(range(expected_global_steps + 1))
    expected_contributor_ids = {
        f"learner_{index:03d}" for index in range(expected_contributors)
    }
    expected_epoch_states = _registered_epoch_states(expected_syncer_epochs)

    if descriptor.get("descriptor_sha256") != _json_sha256_without(
        descriptor, "descriptor_sha256"
    ):
        errors.append("run descriptor self-checksum is invalid")
    if descriptor.get("resolved_config_sha256") != _sha256(resolved_config_path):
        errors.append("resolved config checksum differs from the run descriptor")
    source_manifest = _read_json(source_manifest_path)
    if (
        descriptor.get("source_manifest_sha256") != _sha256(source_manifest_path)
        or source_manifest.get("manifest_sha256")
        != _json_sha256_without(source_manifest, "manifest_sha256")
    ):
        errors.append("source manifest checksum is invalid")
    if any(
        source_manifest.get(field) != descriptor.get(field)
        for field in ("git_commit", "git_dirty", "source_fingerprint")
    ):
        errors.append("source manifest identity differs from the run descriptor")

    if descriptor.get("mode") != "static":
        errors.append("descriptor mode is not static")
    if config.get("membership", {}).get("mode") != "static":
        errors.append("resolved membership mode is not static")
    if int(config.get("training", {}).get("inner_steps", -1)) != expected_inner_steps:
        errors.append("resolved inner_steps does not equal the registered workload")
    if int(config.get("sync", {}).get("stop_after_outer_steps", -1)) != expected_global_steps:
        errors.append("resolved global stop does not equal the registered workload")
    if int(config.get("sync", {}).get("num_learners", -1)) != expected_contributors:
        errors.append("resolved learner count does not equal the registered topology")
    if int(config.get("sync", {}).get("quorum_min", -1)) != expected_contributors or int(
        config.get("sync", {}).get("quorum_max", -1)
    ) != expected_contributors:
        errors.append("formal selection quorum is not the full contributor set")
    if integrity != ["ok"]:
        errors.append(f"SQLite integrity failed: {integrity}")
    if len(schema_meta) != 1:
        errors.append("authority schema identity is missing")
    elif (
        int(schema_meta[0]["schema_version"]) != AUTHORITY_SCHEMA_VERSION
        or int(descriptor.get("schema_version", -1)) != AUTHORITY_SCHEMA_VERSION
        or int(schema_meta[0]["protocol_version"]) != PROTOCOL_VERSION
        or int(descriptor.get("protocol_version", -1)) != PROTOCOL_VERSION
        or schema_meta[0]["mode"] != descriptor.get("mode")
        or schema_meta[0]["ddl_sha256"] != ddl_bundle_sha256("static")
    ):
        errors.append("authority schema identity differs from the run descriptor")
    if len(run_identity) != 1 or any(
        run_identity[0].get(name) != descriptor.get(descriptor_name)
        for name, descriptor_name in (
            ("run_id", "run_id"),
            ("source_fingerprint", "source_fingerprint"),
            ("config_sha256", "resolved_config_sha256"),
        )
    ):
        errors.append("SQLite run identity differs from the run descriptor")
    if len(terminal) != 1 or terminal[0].get("state") != "finalized":
        errors.append("terminal authority is not finalized")
    if len(controller) != 1 or controller[0].get("state") != "finalized":
        errors.append("controller authority is not finalized")
    if final_version != expected_global_steps:
        errors.append(f"final version is {final_version}, expected {expected_global_steps}")
    if [int(row["version"]) for row in versions] != expected_versions:
        errors.append("global publication history is not exact and contiguous")
    if int(summary.get("final_version", -1)) != final_version or int(
        stop.get("final_version", -2)
    ) != final_version:
        errors.append("filesystem terminal controls disagree with authority")
    if len(terminal) == 1:
        terminal_row = terminal[0]
        owner_id = str(terminal_row["finalized_by_owner_id"])
        owner_short = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:12]
        immutable_stop = (
            run_root
            / "control/syncer_epochs"
            / f"e{int(terminal_row['finalized_by_epoch']):06d}_{owner_short}"
            / "terminal"
            / f"stop_g{int(terminal_row['generation']):06d}.json"
        )
        try:
            _require_regular_file(immutable_stop, label="immutable terminal control")
            if _read_json(immutable_stop) != stop:
                errors.append("fixed terminal control differs from its immutable publication")
        except RuntimeError as exc:
            errors.append(str(exc))
    if summary.get("all_learners_stopped") is not True:
        errors.append("terminal summary does not confirm all learners stopped")
    if pending or prepared:
        errors.append("authority retained pending updates or prepared publications")
    if {str(row["stable_contributor_key"]) for row in selected_credit} != (
        expected_contributor_ids
    ) or any(
        int(row["committed_credit"]) != expected_global_steps
        or int(row["last_committed_version"]) != expected_global_steps
        for row in selected_credit
    ):
        errors.append("selection credit is not exact for every contributor")
    if {str(row["stable_contributor_key"]) for row in progress} != expected_contributor_ids:
        errors.append("contributor progress does not cover the registered topology")
    if {
        str(row["stable_contributor_key"]) for row in terminal_fences
    } != expected_contributor_ids or {
        row["state"] for row in terminal_fences
    } != {"acked"}:
        errors.append("terminal contributor acknowledgements are incomplete")
    if {str(row["learner_id"]) for row in bindings} != expected_contributor_ids or {
        row["status"] for row in bindings
    } != {"terminal"}:
        errors.append("static contributor bindings are not all terminal")
    if len(epochs) != expected_syncer_epochs:
        errors.append("syncer epoch history does not match the registered scenario")
    elif tuple(str(row["final_state"]) for row in epochs) != expected_epoch_states:
        errors.append("syncer epoch lifecycle does not match the registered scenario")
    elif any(row["final_at"] is None for row in epochs):
        errors.append("syncer epoch lifecycle is not durably terminal")
    elif expected_syncer_epochs == 1 and epochs[0]["superseded_by_epoch"] is not None:
        errors.append("normal syncer epoch was unexpectedly superseded")
    elif expected_syncer_epochs == 2 and (
        int(epochs[0]["superseded_by_epoch"] or -1) != int(epochs[1]["epoch"])
        or epochs[1]["superseded_by_epoch"] is not None
    ):
        errors.append("syncer takeover epoch linkage is invalid")
    if expected_syncer_epochs > 1 and len(epochs) >= 2:
        successor_epoch = int(epochs[-1]["epoch"])
        successor_versions = [
            int(row["version"])
            for row in versions
            if int(row["committed_by_epoch"]) == successor_epoch
        ]
        if not successor_versions:
            errors.append("successor syncer committed no publication")
        elif any(
            int(row["version"]) >= min(successor_versions)
            and int(row["committed_by_epoch"]) < successor_epoch
            for row in versions
        ):
            errors.append("a stale syncer committed after successor takeover")
    if expected_replaced_learner is not None:
        replacement = next(
            (row for row in bindings if row["learner_id"] == expected_replaced_learner), None
        )
        if replacement is None or int(replacement["binding_generation"]) < 2:
            errors.append("registered learner replacement did not advance its durable fence")

    per_update_tokens = (
        expected_inner_steps
        * int(config["training"]["gradient_accumulation_steps"])
        * int(config["training"]["micro_batch_size"])
        * int(config["data"]["block_size"])
    )
    expected_direct = expected_global_steps * expected_contributors * per_update_tokens
    if any(int(row["processed_tokens_this_cycle"]) != per_update_tokens for row in receipts):
        errors.append("at least one durable receipt has the wrong cycle workload")
    expected_cursor_advance = expected_inner_steps * int(
        config["training"]["gradient_accumulation_steps"]
    )
    if any(
        int(row["data_cursor_end"]) - int(row["data_cursor_start"])
        != expected_cursor_advance
        for row in receipts
    ):
        errors.append("at least one durable receipt has the wrong cursor advance")
    if any(
        int(row["inner_steps"]) != expected_inner_steps
        or int(row["processed_tokens_this_cycle"]) != per_update_tokens
        for row in updates
    ):
        errors.append("at least one durable proposal has the wrong local-step workload")
    applied_updates = [row for row in updates if row["status"] == "applied"]
    applied_by_contributor = {
        learner_id: sum(
            row["stable_contributor_key"] == learner_id for row in applied_updates
        )
        for learner_id in expected_contributor_ids
    }
    if len(applied_updates) != expected_global_steps * expected_contributors or any(
        count != expected_global_steps for count in applied_by_contributor.values()
    ):
        errors.append("applied proposal count is not exact for every contributor")
    direct_by_versions = sum(int(row["direct_weight_tokens_applied"]) for row in versions)
    rollup = rollups[0] if len(rollups) == 1 else None
    balance = _token_balance(rollup)
    receipt_processed = sum(int(row["processed_tokens_this_cycle"]) for row in receipts)
    registered_fault = expected_replaced_learner is not None or expected_syncer_epochs > 1
    if not registered_fault and (
        len(receipts) != expected_global_steps * expected_contributors
        or len(updates) != expected_global_steps * expected_contributors
        or receipt_processed != expected_direct
    ):
        errors.append("fault-free run did not execute exactly one cycle per committed contribution")
    if direct_by_versions != expected_direct:
        errors.append(
            f"publication direct tokens are {direct_by_versions}, expected {expected_direct}"
        )
    if rollup is None or int(rollup["direct_applied"]) != expected_direct:
        errors.append("token rollup direct_applied is not exact")
    if rollup is None or int(rollup["adjudicated_processed"]) != receipt_processed:
        errors.append("token rollup processed total differs from durable receipts")
    if balance != 0 or (rollup is not None and int(rollup["direct_outstanding"]) != 0):
        errors.append(f"token ledger is unbalanced: {balance}")

    attestations = _attestations(run_root)
    learner_attestations = [row for row in attestations if row.get("actor_kind") == "learner"]
    syncer_attestations = [row for row in attestations if row.get("actor_kind") == "syncer"]
    contributor_ids = {row.get("actor_id") for row in learner_attestations}
    hosts = {str(row.get("hostname")) for row in attestations}
    pbs_job_id = os.environ.get("PBS_JOBID")
    nodes = _pbs_nodes()
    if any(
        row.get("attestation_sha256")
        != _json_sha256_without(row, "attestation_sha256")
        or row.get("run_id") != descriptor.get("run_id")
        or row.get("descriptor_sha256") != descriptor.get("descriptor_sha256")
        or row.get("source_fingerprint") != descriptor.get("source_fingerprint")
        for row in attestations
    ):
        errors.append("an actor attestation has an invalid checksum or run identity")
    if expected_contributor_ids != contributor_ids:
        errors.append("learner attestations do not cover every contributor")
    if len(syncer_attestations) != expected_syncer_epochs:
        errors.append("syncer attestations do not cover every expected candidate")
    if len(hosts) != expected_hosts:
        errors.append(f"attested topology used {len(hosts)} hosts, expected {expected_hosts}")
    if len(nodes) != expected_hosts or set(nodes) != hosts:
        errors.append("PBS nodefile and actor attestations describe different topologies")
    if not pbs_job_id or any(
        row.get("scheduler_job_id") != pbs_job_id for row in attestations
    ):
        errors.append("actor attestations are not bound to the current full PBS job ID")

    replacement_evidence: dict[str, Any] | None = None
    if expected_replaced_learner is not None:
        replacement_path = log_root / "learner_replacement.json"
        if not replacement_path.is_file():
            errors.append("registered learner replacement evidence is missing")
        else:
            replacement_evidence = _read_json(replacement_path)
            current = next(
                (row for row in bindings if row["learner_id"] == expected_replaced_learner),
                None,
            )
            old_history = next(
                (
                    row
                    for row in binding_history
                    if row["learner_id"] == expected_replaced_learner
                    and int(row["binding_generation"])
                    == int(replacement_evidence.get("old_binding_generation", -1))
                ),
                None,
            )
            if (
                replacement_evidence.get("learner_id") != expected_replaced_learner
                or int(replacement_evidence.get("injected_exit_status", 0)) == 0
                or current is None
                or old_history is None
                or old_history["final_status"] != "replaced"
                or old_history["attempt_id"] != replacement_evidence.get("old_attempt_id")
                or current["attempt_id"] != replacement_evidence.get("new_attempt_id")
                or int(current["binding_generation"])
                != int(replacement_evidence.get("old_binding_generation", -1)) + 1
            ):
                errors.append("learner replacement evidence does not match durable binding history")

    takeover_evidence: dict[str, Any] | None = None
    if expected_syncer_epochs > 1:
        takeover_path = log_root / "syncer_takeover.json"
        if not takeover_path.is_file():
            errors.append("registered syncer takeover evidence is missing")
        else:
            takeover_evidence = _read_json(takeover_path)
            boundary = takeover_evidence.get("fault_boundary")
            if (
                int(takeover_evidence.get("primary_exit_status", 0)) == 0
                or not isinstance(boundary, dict)
                or boundary.get("sqlite_transaction_active") is not False
                or boundary.get("lease_renewer_quiesced") is not True
                or int(boundary.get("committed_version", -1)) != 2
                or int(boundary.get("pid", -1))
                != int(takeover_evidence.get("primary_pid", -2))
                or not epochs
                or int(boundary.get("epoch", -1)) >= int(epochs[-1]["epoch"])
            ):
                errors.append("syncer takeover evidence does not prove the registered fault layer")

    source = _source_identity(project_root)
    if descriptor.get("git_commit") != source["commit"] or descriptor.get(
        "source_fingerprint"
    ) != source["fingerprint"]:
        errors.append("descriptor source identity differs from the validation target")
    if bool(descriptor.get("git_dirty")) or source["dirty"]:
        errors.append("validation source is dirty")
    captured_source_path = log_root / "source_identity.json"
    if not captured_source_path.is_file():
        errors.append("allocation source identity evidence is missing")
    else:
        captured_source = _read_json(captured_source_path)
        if (
            captured_source.get("git_commit") != source["commit"]
            or captured_source.get("git_dirty") != source["dirty"]
            or captured_source.get("source_fingerprint") != source["fingerprint"]
            or captured_source.get("source_scopes") != source["scopes"]
        ):
            errors.append("allocation and validation source identities differ")

    objects = _verify_publication_objects(run_root, versions, errors)
    cursor_terminal = {
        str(row["stable_contributor_key"]): int(row["data_cursor"]) for row in progress
    }
    receipt_cursor_terminal = {
        learner_id: max(
            (
                int(row["data_cursor_end"])
                for row in receipts
                if row["stable_contributor_key"] == learner_id
            ),
            default=0,
        )
        for learner_id in cursor_terminal
    }
    if cursor_terminal != receipt_cursor_terminal:
        errors.append("terminal contributor cursors differ from durable receipt history")
    topology = {
        f"{row['actor_kind']}:{row['actor_id']}:{row['attempt_id']}": row["hostname"]
        for row in attestations
    }
    evidence_paths = _evidence_paths(
        run_root=run_root,
        log_root=log_root,
        attestations=attestations,
    )
    schema = schema_meta[0] if len(schema_meta) == 1 else None
    return {
        "artifact_version": 1,
        "status": "PASS" if not errors else "FAIL",
        "gate": gate,
        "experiment_id": experiment_id,
        "requirements_covered": [requirement_id],
        "run_root": str(run_root),
        "source_identity": source,
        "config_schema_identity": {
            "version": config.get("config_schema_version"),
            "sha256": descriptor.get("resolved_config_sha256"),
        },
        "protocol_schema_identity": (
            None
            if schema is None
            else {
                "version": int(schema["schema_version"]),
                "ddl_sha256": schema["ddl_sha256"],
                "mode": schema["mode"],
            }
        ),
        "environment": {
            "interpreter": {"executable": sys.executable, "version": sys.version},
            "packages": _package_provenance(),
            "pbs_job_id": pbs_job_id,
            "nodes": nodes,
            "topology": topology,
        },
        "workload_identity": {
            "configured_local_steps": expected_inner_steps,
            "committed_global_steps": final_version,
            "processed_tokens": (
                None if rollup is None else int(rollup["adjudicated_processed"])
            ),
            "direct_weight_tokens_applied": (
                None if rollup is None else int(rollup["direct_applied"])
            ),
            "cursor_terminal": cursor_terminal,
        },
        "metrics": {
            "contributors": expected_contributors,
            "tokens_per_update": per_update_tokens,
            "expected_direct_tokens": expected_direct,
            "processed_receipt_tokens": receipt_processed,
            "receipt_count": len(receipts),
            "proposal_count": len(updates),
            "applied_proposal_count": len(applied_updates),
            "applied_by_contributor": applied_by_contributor,
            "token_balance": balance,
            "publication_object_count": len(objects),
            "learner_attestation_count": len(learner_attestations),
            "syncer_attestation_count": len(syncer_attestations),
        },
        "evidence_paths": evidence_paths,
        "cleanup": {
            "owner": "full_protocol_harness",
            "eligible": not errors,
            "targets": [str(run_root)],
        },
        "identity": {
            "run_id": descriptor.get("run_id"),
            "descriptor_sha256": descriptor.get("descriptor_sha256"),
            "source_fingerprint": descriptor.get("source_fingerprint"),
            "schema_version": descriptor.get("schema_version"),
        },
        "workload": {
            "contributors": expected_contributors,
            "inner_steps": expected_inner_steps,
            "global_steps": expected_global_steps,
            "tokens_per_update": per_update_tokens,
            "expected_direct_tokens": expected_direct,
        },
        "topology": {
            "expected_hosts": expected_hosts,
            "attested_hosts": sorted(hosts),
            "learner_attestation_count": len(learner_attestations),
            "syncer_attestation_count": len(syncer_attestations),
        },
        "authority": {
            "integrity": integrity,
            "final_version": final_version,
            "versions": versions,
            "epochs": epochs,
            "selection_credit": selected_credit,
            "token_rollup": rollup,
            "token_balance": balance,
            "terminal_fences": terminal_fences,
            "static_bindings": bindings,
            "static_binding_history": binding_history,
            "contributor_progress": progress,
        },
        "fault_evidence": {
            "learner_replacement": replacement_evidence,
            "syncer_takeover": takeover_evidence,
        },
        "publication_objects": objects,
        "terminal_summary": summary,
        "errors": errors,
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _safe_identifier(value: str) -> str:
    if not value or any(not (character.isalnum() or character in "._-") for character in value):
        raise argparse.ArgumentTypeError("must contain only letters, digits, '.', '_', or '-'")
    return value


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _syncer_epoch_count(value: str) -> int:
    parsed = _positive_integer(value)
    _registered_epoch_states(parsed)
    return parsed


def _diagnostic_artifact(
    args: argparse.Namespace,
    exc: Exception,
    *,
    status: str,
) -> dict[str, Any]:
    try:
        source: dict[str, Any] | None = _source_identity(args.project_root.resolve())
    except Exception:
        source = None
    evidence_paths = _partial_evidence_paths(
        args.run_root.resolve(),
        args.log_root.resolve(),
        args.output.resolve(),
    )
    source_helper = args.project_root.resolve() / "scripts/miyabi/capture_source_identity.py"
    if not evidence_paths and source_helper.is_file():
        evidence_paths = [str(source_helper)]
    return {
        "artifact_version": 1,
        "status": status,
        "gate": args.gate,
        "experiment_id": args.experiment_id,
        "requirements_covered": [args.requirement_id],
        "run_root": str(args.run_root.resolve()),
        "source_identity": source,
        "config_schema_identity": None,
        "protocol_schema_identity": None,
        "environment": {
            "interpreter": {"executable": sys.executable, "version": sys.version},
            "packages": _package_provenance(),
            "pbs_job_id": os.environ.get("PBS_JOBID"),
            "nodes": _pbs_nodes(),
            "topology": {},
        },
        "workload_identity": None,
        "metrics": {},
        "errors": [f"{type(exc).__name__}: {exc}"],
        "evidence_paths": evidence_paths,
        "cleanup": {
            "owner": "full_protocol_harness",
            "eligible": False,
            "targets": [],
        },
        "identity": {},
        "authority": {"final_version": None},
    }


_REQUIRED_ARTIFACT_FIELDS = {
    "artifact_version",
    "status",
    "gate",
    "experiment_id",
    "requirements_covered",
    "source_identity",
    "config_schema_identity",
    "protocol_schema_identity",
    "environment",
    "workload_identity",
    "metrics",
    "errors",
    "evidence_paths",
    "cleanup",
}


def validate_gate_artifact(payload: dict[str, Any], *, output: Path) -> None:
    missing = sorted(_REQUIRED_ARTIFACT_FIELDS - set(payload))
    if missing:
        raise RuntimeError(f"gate artifact is missing fields: {missing}")
    if payload["artifact_version"] != 1:
        raise RuntimeError("gate artifact version must be 1")
    if payload["status"] not in {"PASS", "FAIL", "BLOCKED", "REVIEW"}:
        raise RuntimeError("gate artifact status is invalid")
    requirements = payload["requirements_covered"]
    if not isinstance(requirements, list) or not requirements or any(
        not isinstance(item, str) or not item for item in requirements
    ):
        raise RuntimeError("gate artifact requirements_covered is invalid")
    errors = payload["errors"]
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise RuntimeError("gate artifact errors is invalid")
    evidence = payload["evidence_paths"]
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("gate artifact evidence_paths must not be empty")
    resolved_output = output.resolve()
    for item in evidence:
        if not isinstance(item, str):
            raise RuntimeError("gate artifact evidence path is not a string")
        path = Path(item)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.resolve() == resolved_output
        ):
            raise RuntimeError(f"gate artifact evidence path is invalid: {item}")
    cleanup = payload["cleanup"]
    if not isinstance(cleanup, dict) or set(cleanup) != {"owner", "eligible", "targets"}:
        raise RuntimeError("gate artifact cleanup projection is invalid")
    if not isinstance(cleanup["eligible"], bool) or not isinstance(cleanup["targets"], list):
        raise RuntimeError("gate artifact cleanup types are invalid")
    if payload["status"] == "PASS":
        source = payload["source_identity"]
        if (
            errors
            or not isinstance(source, dict)
            or source.get("dirty") is not False
            or not source.get("commit")
            or not source.get("scopes")
            or not source.get("fingerprint")
            or not isinstance(payload["config_schema_identity"], dict)
            or not isinstance(payload["protocol_schema_identity"], dict)
            or not isinstance(payload["workload_identity"], dict)
            or cleanup["eligible"] is not True
        ):
            raise RuntimeError("PASS gate artifact has incomplete acceptance identity")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=_safe_identifier, required=True)
    parser.add_argument("--experiment-id", type=_safe_identifier, required=True)
    parser.add_argument("--requirement-id", type=_safe_identifier, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--expected-global-steps", type=_positive_integer, required=True)
    parser.add_argument("--expected-inner-steps", type=_positive_integer, required=True)
    parser.add_argument("--expected-contributors", type=_positive_integer, required=True)
    parser.add_argument("--expected-hosts", type=_positive_integer, required=True)
    parser.add_argument("--expected-syncer-epochs", type=_syncer_epoch_count, default=1)
    parser.add_argument("--expected-replaced-learner", type=_safe_identifier)
    parser.add_argument("--blocked-reason")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.blocked_reason is not None:
        if not args.blocked_reason.strip():
            raise SystemExit("--blocked-reason must not be empty")
        payload = _diagnostic_artifact(
            args,
            GatePrerequisiteUnavailable(args.blocked_reason),
            status="BLOCKED",
        )
    else:
        try:
            if not args.run_root.resolve().is_dir():
                raise GatePrerequisiteUnavailable(
                    f"registered run root is unavailable: {args.run_root.resolve()}"
                )
            if not (args.run_root.resolve() / ".complete").is_file():
                raise GatePrerequisiteUnavailable(
                    f"registered run completion marker is unavailable: "
                    f"{args.run_root.resolve() / '.complete'}"
                )
            payload = validate_run(
                gate=args.gate,
                experiment_id=args.experiment_id,
                requirement_id=args.requirement_id,
                project_root=args.project_root.resolve(),
                run_root=args.run_root.resolve(),
                log_root=args.log_root.resolve(),
                expected_global_steps=args.expected_global_steps,
                expected_inner_steps=args.expected_inner_steps,
                expected_contributors=args.expected_contributors,
                expected_hosts=args.expected_hosts,
                expected_syncer_epochs=args.expected_syncer_epochs,
                expected_replaced_learner=args.expected_replaced_learner,
            )
        except GatePrerequisiteUnavailable as exc:
            payload = _diagnostic_artifact(args, exc, status="BLOCKED")
        except Exception as exc:
            payload = _diagnostic_artifact(args, exc, status="FAIL")
    output = args.output.resolve()
    validate_gate_artifact(payload, output=output)
    _atomic_write(output, payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
