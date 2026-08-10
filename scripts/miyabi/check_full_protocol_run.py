#!/usr/bin/env python3
"""Build strict evidence from one terminal Full Protocol run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query).fetchall()]


def _source_identity(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("capture_source_identity", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load source identity helper: {helper}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    captured = module.capture(project_root)
    return {
        "git_commit": captured["git_commit"],
        "git_dirty": captured["git_dirty"],
        "source_fingerprint": captured["source_fingerprint"],
    }


def _audit_rows(run_root: Path, table: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for root in (
        run_root / "audit/batches/authority_history",
        run_root / "audit/partitions/authority_history",
    ):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            if path.name.endswith(".manifest.json"):
                continue
            payload = _read_json(path)
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
    if not root.is_dir():
        return []
    return [_read_json(path) for path in sorted(root.rglob("*.json"))]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            path = run_root / relative
            item = {"version": version, "kind": kind, "relative_path": relative}
            if not path.is_file():
                errors.append(f"publication object is missing: {relative}")
                item["status"] = "missing"
            else:
                size = path.stat().st_size
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


def validate_run(
    *,
    project_root: Path,
    run_root: Path,
    expected_global_steps: int,
    expected_inner_steps: int,
    expected_contributors: int,
    expected_hosts: int,
    expected_min_syncer_epochs: int,
    expected_replaced_learner: str | None,
) -> dict[str, Any]:
    descriptor = _read_json(run_root / "control/run_descriptor.json")
    config = yaml.safe_load(
        (run_root / "control/run_config.resolved.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(config, dict):
        raise RuntimeError("resolved config is not a mapping")
    summary = _read_json(run_root / "control/summary.json")
    stop = _read_json(run_root / "control/stop.json")
    connection = sqlite3.connect(
        f"file:{(run_root / 'control/syncer_metadata.sqlite3').resolve()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        terminal = _rows(connection, "SELECT * FROM terminal_state")
        controller = _rows(connection, "SELECT * FROM controller_state WHERE singleton=1")
        epochs = _rows(connection, "SELECT * FROM syncer_epochs ORDER BY epoch")
        hot_versions = _rows(connection, "SELECT * FROM global_versions ORDER BY version")
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

    archived_versions = _audit_rows(run_root, "global_versions")
    by_version = {int(row["version"]): row for row in [*archived_versions, *hot_versions]}
    versions = [by_version[number] for number in sorted(by_version)]
    errors: list[str] = []
    final_version = int(terminal[0]["final_version"]) if len(terminal) == 1 else -1
    expected_versions = list(range(expected_global_steps + 1))

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
    if summary.get("all_learners_stopped") is not True:
        errors.append("terminal summary does not confirm all learners stopped")
    if pending or prepared:
        errors.append("authority retained pending updates or prepared publications")
    if len(selected_credit) != expected_contributors or any(
        int(row["committed_credit"]) != expected_global_steps for row in selected_credit
    ):
        errors.append("selection credit is not exact for every contributor")
    if len(terminal_fences) != expected_contributors or {
        row["state"] for row in terminal_fences
    } != {"acked"}:
        errors.append("terminal contributor acknowledgements are incomplete")
    if len(bindings) != expected_contributors or {row["status"] for row in bindings} != {
        "terminal"
    }:
        errors.append("static contributor bindings are not all terminal")
    if len(epochs) < expected_min_syncer_epochs:
        errors.append("syncer epoch history is shorter than the registered fault scenario")
    if expected_min_syncer_epochs > 1 and len(epochs) >= 2:
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
    direct_by_versions = sum(int(row["direct_weight_tokens_applied"]) for row in versions)
    rollup = rollups[0] if len(rollups) == 1 else None
    balance = _token_balance(rollup)
    if direct_by_versions != expected_direct:
        errors.append(
            f"publication direct tokens are {direct_by_versions}, expected {expected_direct}"
        )
    if rollup is None or int(rollup["direct_applied"]) != expected_direct:
        errors.append("token rollup direct_applied is not exact")
    if balance != 0 or (rollup is not None and int(rollup["direct_outstanding"]) != 0):
        errors.append(f"token ledger is unbalanced: {balance}")

    attestations = _attestations(run_root)
    learner_attestations = [row for row in attestations if row.get("actor_kind") == "learner"]
    syncer_attestations = [row for row in attestations if row.get("actor_kind") == "syncer"]
    contributor_ids = {row.get("actor_id") for row in learner_attestations}
    hosts = {str(row.get("hostname")) for row in attestations}
    if not {f"learner_{index:03d}" for index in range(expected_contributors)} <= contributor_ids:
        errors.append("learner attestations do not cover every contributor")
    if len(syncer_attestations) < expected_min_syncer_epochs:
        errors.append("syncer attestations do not cover every expected candidate")
    if len(hosts) != expected_hosts:
        errors.append(f"attested topology used {len(hosts)} hosts, expected {expected_hosts}")

    source = _source_identity(project_root)
    if descriptor.get("git_commit") != source["git_commit"] or descriptor.get(
        "source_fingerprint"
    ) != source["source_fingerprint"]:
        errors.append("descriptor source identity differs from the validation target")
    if bool(descriptor.get("git_dirty")) or source["git_dirty"]:
        errors.append("validation source is dirty")

    objects = _verify_publication_objects(run_root, versions, errors)
    return {
        "artifact_version": 1,
        "status": "PASS" if not errors else "BLOCKED",
        "requirements_covered": ["Full Protocol terminal acceptance"],
        "run_root": str(run_root),
        "source_identity": source,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--expected-global-steps", type=int, required=True)
    parser.add_argument("--expected-inner-steps", type=int, required=True)
    parser.add_argument("--expected-contributors", type=int, required=True)
    parser.add_argument("--expected-hosts", type=int, required=True)
    parser.add_argument("--expected-min-syncer-epochs", type=int, default=1)
    parser.add_argument("--expected-replaced-learner")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        payload = validate_run(
            project_root=args.project_root.resolve(),
            run_root=args.run_root.resolve(),
            expected_global_steps=args.expected_global_steps,
            expected_inner_steps=args.expected_inner_steps,
            expected_contributors=args.expected_contributors,
            expected_hosts=args.expected_hosts,
            expected_min_syncer_epochs=args.expected_min_syncer_epochs,
            expected_replaced_learner=args.expected_replaced_learner,
        )
    except Exception as exc:
        payload = {
            "artifact_version": 1,
            "status": "BLOCKED",
            "requirements_covered": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    _atomic_write(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
