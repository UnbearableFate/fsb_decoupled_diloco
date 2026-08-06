"""Build G8/G9 evidence from scheduler history and dynamic SQLite authority."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.run_descriptor import load_run_descriptor
from ..runtime.pbs_scheduler import parse_qstat_full
from ..storage.atomic_io import atomic_write_json, safe_read_json
from ..storage.paths import RunPaths
from ..storage.schema_bootstrap import open_readonly


def _history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _scheduler_row(job_id: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["qstat", "-H", "-f", job_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    fields = parse_qstat_full(completed.stdout) if completed.returncode == 0 else {}
    return {
        "job_id": job_id,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
        "fields": fields,
    }


def _interval(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    fields = row["fields"]
    started = fields.get("stime")
    walltime = fields.get("resources_used.walltime")
    if not started or not walltime:
        return None
    try:
        start = datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
        hours, minutes, seconds = (int(value) for value in walltime.split(":"))
    except (TypeError, ValueError):
        return None
    return start, start + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _max_concurrency(rows: list[dict[str, Any]]) -> int:
    events: list[tuple[datetime, int]] = []
    for row in rows:
        interval = _interval(row)
        if interval is None:
            continue
        start, end = interval
        events.extend(((start, 1), (end, -1)))
    current = 0
    maximum = 0
    for _timestamp, change in sorted(events, key=lambda item: (item[0], item[1])):
        current += change
        maximum = max(maximum, current)
    return maximum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("g8", "g9"), required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = RunPaths(args.run_root.resolve())
    loaded = load_run_descriptor(paths.shared_root)
    receipts = safe_read_json(args.receipts) or {}
    errors: list[str] = []
    if receipts.get("status") != "PASS":
        errors.append("acceptance launcher receipts are not PASS")

    connection = open_readonly(paths.sqlite_db)
    try:

        def query(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in connection.execute(sql).fetchall()]

        epochs = query("SELECT * FROM syncer_epochs ORDER BY epoch")
        controller = query("SELECT * FROM controller_state")
        terminal = query("SELECT * FROM terminal_state")
        instances = query("SELECT * FROM learner_instances ORDER BY registered_at")
        streams = query("SELECT * FROM streams ORDER BY stream_id")
        launches = query("SELECT * FROM launch_requests ORDER BY created_at")
        registrations = query("SELECT * FROM registration_requests ORDER BY created_at")
        observations = query("SELECT * FROM capacity_observations ORDER BY observation_seq")
        versions = query("SELECT * FROM global_versions ORDER BY version")
    finally:
        connection.close()
    instances += _history(paths.learner_instance_history_jsonl)
    launches += _history(paths.launch_request_history_jsonl)
    registrations += _history(paths.registration_history_jsonl)
    observations += _history(paths.capacity_observation_history_jsonl)

    role_job_ids: dict[str, str] = {}
    scheduler_rows: list[dict[str, Any]] = []
    for receipt in receipts.get("submission_receipts", []):
        if receipt.get("status") != "submitted" or not receipt.get("job_id"):
            continue
        role = str(receipt["role"])
        job_id = str(receipt["job_id"])
        role_job_ids[role] = job_id
        if role != "evidence_checker":
            scheduler_rows.append({"role": role, **_scheduler_row(job_id)})
    known_ids = {row["job_id"] for row in scheduler_rows}
    for launch in launches:
        job_id = launch.get("pbs_job_id")
        if job_id and str(job_id) not in known_ids:
            scheduler_rows.append(
                {"role": f"launch:{launch.get('request_id')}", **_scheduler_row(str(job_id))}
            )
            known_ids.add(str(job_id))

    expected_zero = {
        role
        for role in role_job_ids
        if role.startswith("bootstrap_")
        or role.startswith("paused_bootstrap")
        or role == "successor_syncer"
    }
    expected_nonzero = {"crash_syncer", "victim_bootstrap_0"}
    if args.kind == "g9":
        expected_nonzero.add("duplicate_bootstrap_0")
    for row in scheduler_rows:
        role = str(row["role"])
        exit_status = row["fields"].get("Exit_status")
        if role in expected_zero and exit_status != "0":
            errors.append(f"scheduler role {role} did not exit zero: {exit_status}")
        if role in expected_nonzero and exit_status in {None, "0"}:
            errors.append(f"injected role {role} did not terminate nonzero: {exit_status}")

    current_instances = [
        row for row in instances if str(row.get("status")) in {"admitted", "draining", "drained"}
    ]
    scale_launches = [row for row in launches if row.get("reason") == "scale_out"]
    admitted_scale = [row for row in scale_launches if row.get("admitted_instance_id")]
    low_observations = [row for row in observations if int(row.get("low_capacity", 0)) == 1]
    bootstrap_admissions: dict[int, set[str]] = defaultdict(set)
    for row in launches:
        if row.get("reason") == "bootstrap" and row.get("admitted_instance_id"):
            bootstrap_admissions[int(row["bootstrap_slot"])].add(str(row["admitted_instance_id"]))
    duplicate_rejections = [
        row
        for row in registrations
        if str(row.get("state")) == "rejected"
        and "already admitted" in str(row.get("rejection_reason", ""))
    ]
    victim_job = role_job_ids.get("victim_bootstrap_0")
    victim_instances = [row for row in instances if row.get("pbs_job_id") == victim_job]
    paused_job = role_job_ids.get("paused_bootstrap_1")
    paused_instances = [row for row in instances if row.get("pbs_job_id") == paused_job]
    controller_row = None if not controller else controller[0]
    terminal_row = None if not terminal else terminal[0]
    terminal_complete = (
        controller_row is not None
        and controller_row.get("state") == "terminal"
        and terminal_row is not None
        and versions
        and int(terminal_row["final_version"]) == int(versions[-1]["version"])
    )
    max_concurrency = _max_concurrency(
        [row for row in scheduler_rows if row["role"] not in {"crash_syncer"}]
    )

    if len(epochs) < 2:
        errors.append("acceptance did not exercise syncer takeover")
    if not admitted_scale:
        errors.append("acceptance did not admit a scale-out replacement")
    if not victim_instances or not any(
        str(row.get("status")) in {"revoked", "stopped", "expired"} for row in victim_instances
    ):
        errors.append("victim learner has no terminal membership transition")
    if max(int(row["current_stream_epoch"]) for row in streams) < 1:
        errors.append("replacement did not advance stream epoch")
    if not terminal_complete:
        errors.append("dynamic close/terminal authority is incomplete")
    if not all(str(row.get("status")) == "drained" for row in current_instances):
        errors.append("healthy terminal members did not all drain-ack")

    chaos: dict[str, bool] = {}
    if args.kind == "g9":
        chaos = {
            "stable_bootstrap_1_plus_8": (
                set(bootstrap_admissions) == set(range(8))
                and all(len(values) == 1 for values in bootstrap_admissions.values())
            ),
            "permanent_learner_termination": bool(victim_instances),
            "two_unique_low_observations": len(
                {str(row["observation_key"]) for row in low_observations}
            )
            >= 2,
            "replacement_restored_1_plus_8": len(current_instances) == 8,
            "short_pause_recovered": bool(paused_instances)
            and any(str(row.get("status")) == "drained" for row in paused_instances),
            "duplicate_physical_job_rejected": bool(duplicate_rejections),
            "max_concurrent_compute_nodes_le_9": max_concurrency <= 9,
            "dynamic_close_completed": terminal_complete,
        }
        errors.extend(
            f"G9 chaos predicate failed: {name}" for name, passed in chaos.items() if not passed
        )
    else:
        chaos = {
            "two_node_independent_jobs": max_concurrency <= 2,
            "syncer_takeover": len(epochs) >= 2,
            "replacement_admitted": bool(admitted_scale),
            "stream_reused": max(int(row["current_stream_epoch"]) for row in streams) >= 1,
            "drain_ack": terminal_complete
            and all(str(row.get("status")) == "drained" for row in current_instances),
        }
        errors.extend(
            f"G8 predicate failed: {name}" for name, passed in chaos.items() if not passed
        )

    payload = {
        "checker": "plan02_phase2_chaos_evidence",
        "kind": args.kind,
        "status": "PASS" if not errors else "BLOCKED",
        "identity": {
            "run_id": loaded.descriptor["run_id"],
            "git_commit": loaded.descriptor["git_commit"],
            "source_fingerprint": loaded.descriptor["source_fingerprint"],
            "descriptor_sha256": loaded.descriptor["descriptor_sha256"],
        },
        "run_root": str(paths.shared_root),
        "chaos": chaos,
        "scheduler": {
            "jobs": scheduler_rows,
            "max_concurrent_compute_nodes": max_concurrency,
        },
        "authority": {
            "epoch_count": len(epochs),
            "final_version": None if terminal_row is None else terminal_row["final_version"],
            "controller": controller_row,
            "current_instance_count": len(current_instances),
            "max_stream_epoch": max(int(row["current_stream_epoch"]) for row in streams),
            "bootstrap_admission_counts": {
                str(slot): len(values) for slot, values in bootstrap_admissions.items()
            },
            "scale_launch_count": len(scale_launches),
            "admitted_scale_launch_count": len(admitted_scale),
            "low_observation_keys": sorted(
                {str(row["observation_key"]) for row in low_observations}
            ),
            "duplicate_rejection_count": len(duplicate_rejections),
        },
        "errors": errors,
    }
    atomic_write_json(args.output, payload)
    print(payload["status"])
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
