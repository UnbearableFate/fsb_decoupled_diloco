#!/usr/bin/env python3
"""Build strict P6 static/dynamic acceptance evidence from one terminal v4 run."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _source_identity(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
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


def _rows(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _scalar(connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> Any:
    row = connection.execute(query, parameters).fetchone()
    return None if row is None else row[0]


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


def _attestations(run_root: Path) -> list[dict[str, Any]]:
    root = run_root / "metrics/attestations"
    if not root.is_dir():
        return []
    return [_read_json(path) for path in sorted(root.rglob("*.json"))]


def _audit_rows(run_root: Path, table: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    roots = (
        run_root / "audit/batches/authority_history",
        run_root / "audit/partitions/authority_history",
    )
    for root in roots:
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
                prior = records.get(key)
                if prior is not None and prior != row:
                    raise RuntimeError(f"conflicting {table} audit record: {key}")
                records[key] = row
    return list(records.values())


def _validate_config(config: dict[str, Any], *, mode: str, minimum_inner_steps: int) -> list[str]:
    errors: list[str] = []
    membership = config.get("membership", {}).get("mode", "static")
    if membership != mode:
        errors.append(f"membership mode is {membership!r}, expected {mode!r}")
    if int(config.get("training", {}).get("inner_steps", -1)) < minimum_inner_steps:
        errors.append("training.inner_steps is below the formal minimum")
    syncer = config.get("syncer", {})
    if syncer.get("compute_dtype") != "float32":
        errors.append("syncer.compute_dtype is not float32")
    if mode == "static":
        expected = {
            "training.precision": config.get("training", {}).get("precision"),
            "io.tensor_dtype": config.get("io", {}).get("tensor_dtype"),
            "syncer.publish_dtype": syncer.get("publish_dtype"),
        }
        if set(expected.values()) != {"fp32", "float32"}:
            errors.append(f"static dtype contract failed: {expected}")
        if syncer.get("device") != "cuda":
            errors.append("static formal syncer is not on CUDA")
    else:
        expected = {
            "training.precision": config.get("training", {}).get("precision"),
            "io.tensor_dtype": config.get("io", {}).get("tensor_dtype"),
            "syncer.publish_dtype": syncer.get("publish_dtype"),
        }
        if expected != {
            "training.precision": "bf16",
            "io.tensor_dtype": "bfloat16",
            "syncer.publish_dtype": "bfloat16",
        }:
            errors.append(f"dynamic dtype contract failed: {expected}")
        if syncer.get("device") != "cpu":
            errors.append("dynamic formal syncer is not on CPU")
    return errors


def validate_run(
    *,
    project_root: Path,
    run_root: Path,
    mode: str,
    minimum_versions: int,
    minimum_inner_steps: int,
    expected_contributors: int,
    pause_marker: Path | None = None,
    duplicate_result: Path | None = None,
    bootstrap_release: Path | None = None,
    loss_result: Path | None = None,
    topology_timeline: Path | None = None,
) -> dict[str, Any]:
    descriptor = _read_json(run_root / "control/run_descriptor.json")
    config = yaml.safe_load(
        (run_root / "control/run_config.resolved.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(config, dict):
        raise RuntimeError("resolved config is not a mapping")
    summary = _read_json(run_root / "control/summary.json")
    stop = _read_json(run_root / "control/stop.json")
    database = run_root / "control/syncer_metadata.sqlite3"
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        terminal = _rows(connection, "SELECT * FROM terminal_state")
        controller = _rows(connection, "SELECT * FROM controller_state WHERE singleton=1")
        epochs = _rows(connection, "SELECT * FROM syncer_epochs ORDER BY epoch")
        hot_versions = _rows(connection, "SELECT * FROM global_versions ORDER BY version")
        final_version = int(terminal[0]["final_version"]) if len(terminal) == 1 else -1
        pending_selected = int(
            _scalar(
                connection, "SELECT COUNT(*) FROM updates WHERE status IN ('pending','selected')"
            )
            or 0
        )
        prepared = int(
            _scalar(connection, "SELECT COUNT(*) FROM publication_intents WHERE state='prepared'")
            or 0
        )
        selected_credit = _rows(
            connection,
            "SELECT stable_contributor_key, committed_credit, last_committed_version "
            "FROM selection_state ORDER BY stable_contributor_key",
        )
        rollup_rows = _rows(connection, "SELECT * FROM token_rollups WHERE singleton=1")
        terminal_fences = _rows(
            connection,
            "SELECT * FROM terminal_contributor_fences ORDER BY stable_contributor_key",
        )
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        dynamic_instances = (
            _rows(connection, "SELECT * FROM learner_instances ORDER BY registered_at")
            if "learner_instances" in tables
            else []
        )
        launch_requests = (
            _rows(connection, "SELECT * FROM launch_requests ORDER BY created_at")
            if "launch_requests" in tables
            else []
        )
        static_bindings = (
            _rows(connection, "SELECT * FROM static_contributor_bindings ORDER BY learner_id")
            if "static_contributor_bindings" in tables
            else []
        )
        hot_update_dtypes = _rows(
            connection,
            "SELECT tensor_dtype, COUNT(*) AS count FROM updates GROUP BY tensor_dtype",
        )
    finally:
        connection.close()

    archived_versions = _audit_rows(run_root, "global_versions")
    versions_by_number = {int(row["version"]): row for row in [*archived_versions, *hot_versions]}
    versions = [versions_by_number[key] for key in sorted(versions_by_number)]
    archived_updates = _audit_rows(run_root, "updates")
    dtype_counts: dict[str, int] = {}
    for row in archived_updates:
        dtype = str(row["tensor_dtype"])
        dtype_counts[dtype] = dtype_counts.get(dtype, 0) + 1
    for row in hot_update_dtypes:
        dtype = str(row["tensor_dtype"])
        dtype_counts[dtype] = dtype_counts.get(dtype, 0) + int(row["count"])
    update_dtypes = [
        {"tensor_dtype": dtype, "count": count} for dtype, count in sorted(dtype_counts.items())
    ]
    errors = _validate_config(config, mode=mode, minimum_inner_steps=minimum_inner_steps)
    if integrity != ["ok"]:
        errors.append(f"SQLite integrity failed: {integrity}")
    if len(terminal) != 1 or terminal[0].get("state") != "finalized":
        errors.append("terminal_state is not one finalized row")
    if not controller or controller[0].get("state") != "finalized":
        errors.append("controller is not finalized")
    if final_version < minimum_versions:
        errors.append(f"final version {final_version} is below {minimum_versions}")
    if [int(row["version"]) for row in versions] != list(range(final_version + 1)):
        errors.append("archived+hot global versions are not one contiguous history")
    if len(hot_versions) != 1 or int(hot_versions[0]["version"]) != final_version:
        errors.append("recovery-hot authority does not retain only the current global version")
    if (
        int(summary.get("final_version", -1)) != final_version
        or int(stop.get("final_version", -2)) != final_version
    ):
        errors.append("terminal filesystem controls disagree with authority")
    if summary.get("all_learners_stopped") is not True:
        errors.append("terminal summary does not confirm all learners stopped")
    if pending_selected or prepared:
        errors.append(f"outstanding authority work: updates={pending_selected}, intents={prepared}")
    if len(selected_credit) != expected_contributors:
        errors.append(
            f"selection credit has {len(selected_credit)} contributors, expected {expected_contributors}"
        )
    if selected_credit and min(int(row["committed_credit"]) for row in selected_credit) < 1:
        errors.append("at least one contributor starved")
    if len(terminal_fences) != expected_contributors or {
        row["state"] for row in terminal_fences
    } != {"acked"}:
        errors.append("terminal contributor fences are incomplete")
    if not update_dtypes:
        errors.append("run has no archived or recovery-hot update dtype evidence")
    rollup = rollup_rows[0] if len(rollup_rows) == 1 else None
    balance = _token_balance(rollup)
    if balance != 0 or (rollup is not None and int(rollup["direct_outstanding"]) != 0):
        errors.append(f"token ledger is unbalanced: {balance}")
    if mode == "static":
        if len(static_bindings) != expected_contributors or {
            row["status"] for row in static_bindings
        } != {"terminal"}:
            errors.append("static bindings are not all terminal")
        if update_dtypes and {row["tensor_dtype"] for row in update_dtypes} != {"float32"}:
            errors.append(f"static update dtypes are not FP32: {update_dtypes}")
    else:
        admitted_by_stream: dict[int, int] = {}
        for row in dynamic_instances:
            if row["admitted_at"] is not None:
                stream = int(row["stream_id"])
                admitted_by_stream[stream] = admitted_by_stream.get(stream, 0) + 1
        if len(admitted_by_stream) != expected_contributors:
            errors.append("dynamic run did not admit every stream")
        if not any(count >= 2 for count in admitted_by_stream.values()):
            errors.append("dynamic run has no replacement stream")
        if not any(
            row["role"] == "replacement" and row["state"] == "admitted" for row in launch_requests
        ):
            errors.append("dynamic replacement launch was not admitted")
        if update_dtypes and {row["tensor_dtype"] for row in update_dtypes} != {"bfloat16"}:
            errors.append(f"dynamic update dtypes are not BF16: {update_dtypes}")

    attestation_rows = _attestations(run_root)
    learner_attestations = [row for row in attestation_rows if row.get("actor_kind") == "learner"]
    syncer_attestations = [row for row in attestation_rows if row.get("actor_kind") == "syncer"]
    if len({row.get("actor_id") for row in learner_attestations}) < expected_contributors:
        errors.append("learner attestations do not cover every contributor")
    if not syncer_attestations:
        errors.append("syncer attestation is missing")
    hosts = {str(row.get("hostname")) for row in attestation_rows}
    scheduler_jobs = {
        str(row.get("scheduler_job_id"))
        for row in learner_attestations
        if row.get("scheduler_job_id")
    }
    if mode == "static" and len(hosts) != expected_contributors + 1:
        errors.append(
            f"static topology used {len(hosts)} hosts, expected {expected_contributors + 1}"
        )
    if mode == "dynamic" and len(scheduler_jobs) < expected_contributors + 1:
        errors.append("dynamic topology lacks an independent replacement learner job")

    pause = _read_json(pause_marker) if pause_marker is not None else None
    duplicate = _read_json(duplicate_result) if duplicate_result is not None else None
    bootstrap = _read_json(bootstrap_release) if bootstrap_release is not None else None
    loss = _read_json(loss_result) if loss_result is not None else None
    timeline = _read_json(topology_timeline) if topology_timeline is not None else None
    if mode == "dynamic":
        if (
            pause is None
            or pause.get("sqlite_transaction_active") is not False
            or pause.get("lease_renewer_quiesced") is not True
        ):
            errors.append("candidate pause lacks an outside-transaction proof")
        if duplicate is None or duplicate.get("rejected_before_torch") is not True:
            errors.append("duplicate learner was not rejected before Torch")
        if (
            bootstrap is None
            or len(bootstrap.get("bootstrap_job_ids", [])) != expected_contributors
            or int(bootstrap.get("maximum_live_allocations", -1)) != 9
        ):
            errors.append("bootstrap evidence does not prove eight simultaneous learners")
        if (
            loss is None
            or loss.get("returncode") != 0
            or loss.get("bootstrap_slot") != 0
            or loss.get("injected_after_candidate_pause") is not True
            or pause is None
            or loss.get("candidate_pause_epoch") != pause.get("epoch")
            or bootstrap is None
            or str(loss.get("job_id", "")).split(".", 1)[0]
            not in {
                str(job_id).split(".", 1)[0] for job_id in bootstrap.get("bootstrap_job_ids", [])
            }
        ):
            errors.append("permanent learner loss lacks an exact post-pause qdel proof")
        if len(epochs) < 2:
            errors.append("dynamic candidate successor never acquired a new epoch")
        else:
            successor_epoch = int(epochs[-1]["epoch"])
            successor_versions = [
                int(row["version"])
                for row in versions
                if int(row["committed_by_epoch"]) == successor_epoch
            ]
            if not successor_versions:
                errors.append("successor committed no version")
            else:
                first_successor = min(successor_versions)
                stale_after = [
                    row
                    for row in versions
                    if int(row["version"]) >= first_successor
                    and int(row["committed_by_epoch"]) < successor_epoch
                ]
                if stale_after:
                    errors.append(f"stale writer committed after takeover: {stale_after}")
        if timeline is None or int(timeline.get("maximum_live_allocations", -1)) != 9:
            errors.append("topology timeline does not prove the exact nine-allocation maximum")

    source = _source_identity(project_root)
    if (
        descriptor.get("git_commit") != source["git_commit"]
        or descriptor.get("source_fingerprint") != source["source_fingerprint"]
    ):
        errors.append("run descriptor source identity differs from the validation target")
    if bool(descriptor.get("git_dirty")) or source["git_dirty"]:
        errors.append("formal acceptance source is dirty")

    requirement = "P6-STATIC-9N" if mode == "static" else "P6-DYNAMIC-9N"
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase": "P6-acceptance-final-review",
        "experiment_id": f"p6-g{'8-static' if mode == 'static' else '9-dynamic'}-9node",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source["git_commit"],
        "source_identity": source,
        "requirements_covered": [requirement],
        "run_root": str(run_root),
        "identity": {
            "run_id": descriptor.get("run_id"),
            "descriptor_sha256": descriptor.get("descriptor_sha256"),
            "source_fingerprint": descriptor.get("source_fingerprint"),
            "schema_version": descriptor.get("schema_version"),
        },
        "topology": {
            "mode": mode,
            "expected_contributors": expected_contributors,
            "attested_hosts": sorted(hosts),
            "learner_scheduler_jobs": sorted(scheduler_jobs),
            "timeline": timeline,
        },
        "config_contract": {
            "inner_steps": config.get("training", {}).get("inner_steps"),
            "training_precision": config.get("training", {}).get("precision"),
            "update_dtype": config.get("io", {}).get("tensor_dtype"),
            "syncer": config.get("syncer"),
        },
        "authority": {
            "integrity": integrity,
            "final_version": final_version,
            "hot_versions": hot_versions,
            "all_versions": versions,
            "epochs": epochs,
            "pending_selected_updates": pending_selected,
            "prepared_intents": prepared,
            "selection_credit": selected_credit,
            "token_rollup": rollup,
            "token_balance": balance,
            "terminal_fences": terminal_fences,
            "dynamic_instances": dynamic_instances,
            "launch_requests": launch_requests,
            "static_bindings": static_bindings,
            "update_dtypes": update_dtypes,
        },
        "fault_evidence": {
            "pause": pause,
            "duplicate": duplicate,
            "bootstrap": bootstrap,
            "permanent_loss": loss,
        },
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("static", "dynamic"), required=True)
    parser.add_argument("--minimum-versions", type=int, required=True)
    parser.add_argument("--minimum-inner-steps", type=int, default=60)
    parser.add_argument("--expected-contributors", type=int, default=8)
    parser.add_argument("--pause-marker", type=Path)
    parser.add_argument("--duplicate-result", type=Path)
    parser.add_argument("--bootstrap-release", type=Path)
    parser.add_argument("--loss-result", type=Path)
    parser.add_argument("--topology-timeline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = validate_run(
            project_root=args.project_root.resolve(),
            run_root=args.run_root.resolve(),
            mode=args.mode,
            minimum_versions=args.minimum_versions,
            minimum_inner_steps=args.minimum_inner_steps,
            expected_contributors=args.expected_contributors,
            pause_marker=None if args.pause_marker is None else args.pause_marker.resolve(),
            duplicate_result=(
                None if args.duplicate_result is None else args.duplicate_result.resolve()
            ),
            bootstrap_release=(
                None if args.bootstrap_release is None else args.bootstrap_release.resolve()
            ),
            loss_result=(None if args.loss_result is None else args.loss_result.resolve()),
            topology_timeline=(
                None if args.topology_timeline is None else args.topology_timeline.resolve()
            ),
        )
    except Exception as exc:
        payload = {
            "artifact_version": 1,
            "plan_id": PLAN_ID,
            "phase": "P6-acceptance-final-review",
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
