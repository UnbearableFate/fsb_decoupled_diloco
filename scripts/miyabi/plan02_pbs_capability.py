#!/usr/bin/env python3
"""Probe PBS query/submission/array behavior from a Miyabi compute job."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"F", "X"}


def normalize_job_id(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty PBS job ID")
    return stripped.split(".", 1)[0]


def parse_qstat_full(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in output.splitlines():
        if raw_line.startswith(("\t", " ")) and current_key is not None and " = " not in raw_line:
            fields[current_key] += raw_line.strip()
            continue
        match = re.match(r"^\s*([^=]+?)\s*=\s*(.*)$", raw_line)
        if match:
            current_key = match.group(1).strip()
            fields[current_key] = match.group(2).strip()
    return fields


def classify_scheduler_state(fields: dict[str, str] | None) -> str:
    if fields is None:
        return "unknown"
    state = fields.get("job_state", "").upper()
    substate_text = fields.get("substate", "")
    try:
        substate = int(substate_text)
    except ValueError:
        substate = None
    if state in TERMINAL_STATES:
        return "finished"
    if state in {"Q", "H", "W", "S"}:
        return "queued"
    if state == "R" and substate is not None and substate < 42:
        return "prologue"
    if state in {"R", "E", "B"}:
        return "running"
    return "unknown"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _query_job(job_id: str, *, historical: bool = False) -> dict[str, Any]:
    command = ["qstat", "-f"]
    if historical:
        command = ["qstat", "-H", "-f"]
    command.append(job_id)
    completed = _run(command)
    fields = parse_qstat_full(completed.stdout) if completed.returncode == 0 else None
    return {
        "command": command,
        "returncode": completed.returncode,
        "classification": classify_scheduler_state(fields),
        "fields": fields,
        "stderr": completed.stderr.strip(),
    }


def _submit_child(
    *,
    child_script: Path,
    probe_root: Path,
    request_fingerprint: str,
    project_root: Path,
    array: bool,
) -> dict[str, Any]:
    name = f"p02_{request_fingerprint[-10:]}_{'a' if array else 's'}"
    variables = ",".join(
        (
            f"PLAN02_PROBE_ROOT={probe_root}",
            f"PLAN02_REQUEST_FINGERPRINT={request_fingerprint}",
            f"PROJECT_ROOT={project_root}",
        )
    )
    command = ["qsub", "-N", name, "-v", variables]
    if array:
        command.extend(("-r", "y", "-J", "0-1"))
    command.append(str(child_script))
    completed = _run(command)
    result: dict[str, Any] = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "job_name": name,
        "request_fingerprint": request_fingerprint,
        "array_requested": array,
    }
    if completed.returncode == 0 and completed.stdout.strip():
        result["job_id_raw"] = completed.stdout.strip().splitlines()[-1]
        result["job_id_normalized"] = normalize_job_id(result["job_id_raw"])
    return result


def _wait_for_child(
    submission: dict[str, Any],
    *,
    probe_root: Path,
    expected_artifacts: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if "job_id_raw" not in submission:
        return {
            "completed": False,
            "state_observations": [],
            "artifacts": [],
            "reason": "submission_failed",
        }
    job_id = str(submission["job_id_raw"])
    request = str(submission["request_fingerprint"])
    deadline = time.monotonic() + timeout_seconds
    observations: list[dict[str, Any]] = []
    artifact_paths: list[Path] = []
    while time.monotonic() < deadline:
        query = _query_job(job_id)
        observations.append(
            {
                "observed_at": time.time(),
                "classification": query["classification"],
                "returncode": query["returncode"],
                "fields": query["fields"],
            }
        )
        artifact_paths = sorted(probe_root.glob(f"child_{request}_*.json"))
        if len(artifact_paths) >= expected_artifacts:
            break
        time.sleep(1.0)
    artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in artifact_paths]
    historical = _query_job(job_id, historical=True)
    return {
        "completed": len(artifacts) == expected_artifacts,
        "state_observations": observations,
        "historical_query": historical,
        "artifacts": artifacts,
        "artifact_paths": [str(path) for path in artifact_paths],
    }


def probe(args: argparse.Namespace) -> dict[str, Any]:
    qsub_path = shutil.which("qsub")
    qstat_path = shutil.which("qstat")
    current_job = _query_job(args.parent_job_id) if qstat_path else None
    classifications = {
        "queued": classify_scheduler_state({"job_state": "Q", "substate": "10"}),
        "prologue": classify_scheduler_state({"job_state": "R", "substate": "41"}),
        "running": classify_scheduler_state({"job_state": "R", "substate": "42"}),
        "finished": classify_scheduler_state({"job_state": "F", "substate": "92"}),
        "unknown": classify_scheduler_state({"job_state": "Z", "substate": ""}),
    }
    expected_classifications = {
        "queued": "queued",
        "prologue": "prologue",
        "running": "running",
        "finished": "finished",
        "unknown": "unknown",
    }
    classifier_validated = classifications == expected_classifications

    result: dict[str, Any] = {
        "status": "PASS",
        "hostname": os.uname().nodename,
        "parent_job_id": args.parent_job_id,
        "parent_job_id_normalized": normalize_job_id(args.parent_job_id),
        "commands": {"qsub": qsub_path, "qstat": qstat_path},
        "current_job_query": current_job,
        "state_classifier": classifications,
        "state_classifier_validated": classifier_validated,
        "manual_independent_restart_supported": bool(
            qstat_path and current_job and current_job["returncode"] == 0
        ),
        "automatic_submission_supported": False,
        "job_array_supported": False,
        "initial_learner_orchestration": "independent_manifest",
        "submissions": {},
    }
    if not classifier_validated or not result["manual_independent_restart_supported"]:
        result["status"] = "BLOCKED"
        return result
    if not qsub_path:
        result["automatic_submission_reason"] = "qsub unavailable on compute node"
        return result

    scalar_request = f"plan02-scalar-{normalize_job_id(args.parent_job_id)}"
    scalar = _submit_child(
        child_script=args.child_script,
        probe_root=args.probe_root,
        request_fingerprint=scalar_request,
        project_root=args.project_root,
        array=False,
    )
    scalar["completion"] = _wait_for_child(
        scalar,
        probe_root=args.probe_root,
        expected_artifacts=1,
        timeout_seconds=args.timeout_seconds,
    )
    result["submissions"]["scalar"] = scalar
    scalar_artifacts = scalar["completion"]["artifacts"]
    scalar_supported = bool(
        scalar["returncode"] == 0
        and scalar["completion"]["completed"]
        and len(scalar_artifacts) == 1
        and scalar_artifacts[0].get("request_fingerprint") == scalar_request
        and scalar_artifacts[0].get("job_name") == scalar["job_name"]
    )
    result["automatic_submission_supported"] = scalar_supported
    if not scalar_supported:
        result["automatic_submission_reason"] = "scalar child submission was not auditable"
        return result

    array_request = f"plan02-array-{normalize_job_id(args.parent_job_id)}"
    array = _submit_child(
        child_script=args.child_script,
        probe_root=args.probe_root,
        request_fingerprint=array_request,
        project_root=args.project_root,
        array=True,
    )
    array["completion"] = _wait_for_child(
        array,
        probe_root=args.probe_root,
        expected_artifacts=2,
        timeout_seconds=args.timeout_seconds,
    )
    result["submissions"]["array"] = array
    array_indices = {
        str(artifact.get("array_index")) for artifact in array["completion"]["artifacts"]
    }
    result["job_array_supported"] = bool(
        array["returncode"] == 0
        and array["completion"]["completed"]
        and array_indices == {"0", "1"}
    )
    if result["job_array_supported"]:
        result["initial_learner_orchestration"] = "pbs_job_array"
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--probe-root", type=Path, required=True)
    parser.add_argument("--parent-job-id", required=True)
    parser.add_argument("--child-script", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = probe(args)
    _atomic_write_json(args.output_json, payload)
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(0 if payload["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
