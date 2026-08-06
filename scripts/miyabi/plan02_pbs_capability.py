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
SAFE_QSTAT_FIELDS = {
    "Job_Name",
    "job_state",
    "substate",
    "Exit_status",
    "run_count",
    "Rerunable",
    "array_indices_submitted",
    "array_state_count",
}
REQUEST_VARIABLE_KEYS = {"PLAN02_REQUEST_FINGERPRINT"}


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
        return "query_failed"
    if not fields:
        return "no_record"
    state = fields.get("job_state", "").upper()
    substate_text = fields.get("substate", "")
    try:
        substate = int(substate_text)
    except ValueError:
        substate = None
    if state in TERMINAL_STATES:
        return "finished"
    if state in {"Q", "H", "W"}:
        return "queued"
    if state == "S":
        return "suspended"
    if state == "R" and substate is not None and substate < 42:
        return "prologue"
    if state in {"R", "E", "B"}:
        return "running"
    return "unknown"


def _request_variables(value: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for item in value.split(","):
        key, separator, variable_value = item.partition("=")
        if separator and key in REQUEST_VARIABLE_KEYS:
            variables[key] = variable_value
    return variables


def _safe_qstat_fields(fields: dict[str, str] | None) -> dict[str, Any] | None:
    if fields is None:
        return None
    safe: dict[str, Any] = {
        key: value for key, value in fields.items() if key in SAFE_QSTAT_FIELDS
    }
    request_variables = _request_variables(fields.get("Variable_List", ""))
    if request_variables:
        safe["request_variables"] = request_variables
    return safe


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
    parsed_fields = parse_qstat_full(completed.stdout) if completed.returncode == 0 else None
    fields = _safe_qstat_fields(parsed_fields)
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
    start_delay_seconds: float = 0.0,
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
    start_at: str | None = None
    if start_delay_seconds > 0:
        start_at = time.strftime(
            "%Y%m%d%H%M.%S", time.localtime(time.time() + start_delay_seconds)
        )
        command.extend(("-a", start_at))
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
        "start_delay_seconds": start_delay_seconds,
        "start_at": start_at,
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
    initial_observations: list[dict[str, Any]] | None = None,
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
    observations: list[dict[str, Any]] = list(initial_observations or [])
    artifact_paths: list[Path] = []
    historical: dict[str, Any] | None = None
    terminal_query: dict[str, Any] | None = None
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
        if query["classification"] == "finished":
            terminal_query = query
        if len(artifact_paths) >= expected_artifacts:
            historical = _query_job(job_id, historical=True)
            if historical["classification"] == "finished":
                terminal_query = historical
        if terminal_query is not None:
            break
        time.sleep(0.1)
    artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in artifact_paths]
    artifact_terminal_queries = {
        str(artifact["pbs_job_id"]): _query_job(
            str(artifact["pbs_job_id"]), historical=True
        )
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("pbs_job_id")
    }
    if historical is None:
        historical = _query_job(job_id, historical=True)
    terminal_fields = terminal_query.get("fields") if terminal_query else None
    exit_status_text = terminal_fields.get("Exit_status") if terminal_fields else None
    try:
        exit_status = int(exit_status_text)
    except (TypeError, ValueError):
        exit_status = None
    workload_artifacts_complete = len(artifacts) == expected_artifacts
    terminal_state_observed = terminal_query is not None
    terminal_success = terminal_state_observed and exit_status == 0
    return {
        "completed": workload_artifacts_complete and terminal_success,
        "workload_artifacts_complete": workload_artifacts_complete,
        "terminal_state_observed": terminal_state_observed,
        "terminal_success": terminal_success,
        "exit_status": exit_status,
        "terminal_query": terminal_query,
        "artifact_terminal_queries": artifact_terminal_queries,
        "state_observations": observations,
        "historical_query": historical,
        "artifacts": artifacts,
        "artifact_paths": [str(path) for path in artifact_paths],
    }


def _observe_queued(
    submission: dict[str, Any], *, timeout_seconds: float
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    job_id = submission.get("job_id_raw")
    if not isinstance(job_id, str):
        return observations
    deadline = time.monotonic() + timeout_seconds
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
        if query["classification"] == "queued":
            break
        time.sleep(0.1)
    return observations


def _scheduler_fingerprint_observed(
    completion: dict[str, Any], *, request: str, job_name: str
) -> bool:
    queries = [
        *completion.get("state_observations", []),
        completion.get("terminal_query"),
    ]
    for query in queries:
        if not isinstance(query, dict):
            continue
        fields = query.get("fields")
        if not isinstance(fields, dict):
            continue
        variables = fields.get("request_variables", {})
        if (
            fields.get("Job_Name") == job_name
            and isinstance(variables, dict)
            and variables.get("PLAN02_REQUEST_FINGERPRINT") == request
        ):
            return True
    return False


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
        "suspended": classify_scheduler_state({"job_state": "S", "substate": "45"}),
        "no_record": classify_scheduler_state({}),
        "query_failed": classify_scheduler_state(None),
    }
    expected_classifications = {
        "queued": "queued",
        "prologue": "prologue",
        "running": "running",
        "finished": "finished",
        "unknown": "unknown",
        "suspended": "suspended",
        "no_record": "no_record",
        "query_failed": "query_failed",
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
        "scheduler_query_supported": bool(
            qstat_path and current_job and current_job["returncode"] == 0
        ),
        "manual_independent_job_supported": bool(
            qstat_path and current_job and current_job["returncode"] == 0
        ),
        "manual_independent_evidence": {
            "operator_submitted_parent_job": True,
            "parent_job_query_returncode": (
                current_job["returncode"] if current_job is not None else None
            ),
            "restart_execution_deferred_to_phase_1": True,
        },
        "automatic_submission_supported": False,
        "job_array_supported": False,
        "terminal_state_query_supported": False,
        "observed_scheduler_states": [],
        "initial_learner_orchestration": "independent_manifest",
        "submissions": {},
    }
    if not classifier_validated or not result["scheduler_query_supported"]:
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
        start_delay_seconds=5.0,
    )
    queued_observations = _observe_queued(
        scalar, timeout_seconds=min(30.0, args.timeout_seconds)
    )
    scalar["completion"] = _wait_for_child(
        scalar,
        probe_root=args.probe_root,
        expected_artifacts=1,
        timeout_seconds=args.timeout_seconds,
        initial_observations=queued_observations,
    )
    result["submissions"]["scalar"] = scalar
    scalar_artifacts = scalar["completion"]["artifacts"]
    scalar_supported = bool(
        scalar["returncode"] == 0
        and scalar["completion"]["completed"]
        and len(scalar_artifacts) == 1
        and scalar_artifacts[0].get("request_fingerprint") == scalar_request
        and scalar_artifacts[0].get("job_name") == scalar["job_name"]
        and _scheduler_fingerprint_observed(
            scalar["completion"], request=scalar_request, job_name=scalar["job_name"]
        )
    )
    result["automatic_submission_supported"] = scalar_supported
    if not scalar_supported:
        if scalar["returncode"] == 0:
            result["status"] = "BLOCKED"
            result["automatic_submission_reason"] = (
                "submitted scalar child did not complete successfully and audibly"
            )
        else:
            result["automatic_submission_reason"] = "compute-node qsub was rejected"
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
        and _scheduler_fingerprint_observed(
            array["completion"], request=array_request, job_name=array["job_name"]
        )
    )
    if result["job_array_supported"]:
        result["initial_learner_orchestration"] = "pbs_job_array"
    elif array["returncode"] == 0:
        result["status"] = "BLOCKED"
        result["job_array_reason"] = (
            "submitted array child did not complete successfully and audibly"
        )
    completions = [scalar["completion"], array["completion"]]
    result["observed_scheduler_states"] = sorted(
        {
            str(observation["classification"])
            for completion in completions
            for observation in [
                *completion.get("state_observations", []),
                completion.get("terminal_query"),
            ]
            if isinstance(observation, dict)
            and observation.get("classification")
            not in {"query_failed", "no_record"}
        }
    )
    scalar_terminal_supported = (
        scalar["completion"].get("terminal_state_observed") is True
        and scalar["completion"].get("terminal_success") is True
    )
    array_terminal_supported = (
        array["completion"].get("terminal_state_observed") is True
        and array["completion"].get("terminal_success") is True
    )
    result["terminal_state_query_supported"] = scalar_terminal_supported and (
        not result["job_array_supported"] or array_terminal_supported
    )
    rerunable_job_evidence: dict[str, dict[str, Any]] = {}
    for name, completion in (
        ("scalar", scalar["completion"]),
        ("array", array["completion"]),
    ):
        terminal_query = completion.get("terminal_query")
        fields = terminal_query.get("fields") if isinstance(terminal_query, dict) else None
        rerunable_job_evidence[name] = {
            key: fields.get(key) if isinstance(fields, dict) else None
            for key in ("Rerunable", "run_count")
        }
        child_queries = completion.get("artifact_terminal_queries", {})
        rerunable_job_evidence[name]["physical_incarnations"] = {
            job_id: {
                key: query.get("fields", {}).get(key)
                if isinstance(query.get("fields"), dict)
                else None
                for key in ("Rerunable", "run_count", "Exit_status")
            }
            for job_id, query in child_queries.items()
            if isinstance(query, dict)
        }
    result["rerunable_job_evidence"] = rerunable_job_evidence
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
