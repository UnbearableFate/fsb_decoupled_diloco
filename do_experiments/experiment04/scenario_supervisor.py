#!/usr/bin/env python3
"""Supervise one current Full Protocol scheduler-backed experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fs_diloco.core.config import load_config
from fs_diloco.core.source_identity import SOURCE_SCOPES
from fs_diloco.runtime.pbs_scheduler import PBSScheduler
from fs_diloco.storage.audit_archive import read_logical_authority_rows
from fs_diloco.storage.paths import RunPaths
from fs_diloco.tools.launch_independent_run import launch


@dataclass(frozen=True)
class Scenario:
    """Describe one fixed submission and fault timeline."""

    experiment_id: str  # Plan05 experiment number.
    scaling_enabled: bool  # Whether the production capacity service may launch replacement.
    inject_learner_failure: bool  # Whether the supervisor deletes one admitted learner job.
    fault_delay: float | None = None  # Delay before deleting the registered learner.


SCENARIOS: dict[str, Scenario] = {
    "no_failure": Scenario("1", False, False),
    "failure_no_replacement": Scenario("2", False, True, fault_delay=60.0),
    "failure_authorized_replacement": Scenario("3", True, True, fault_delay=60.0),
}


def _atomic_json(path: Path, payload: dict[str, Any], *, create_only: bool = False) -> None:
    """Publish one durable JSON object atomically, optionally refusing replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _normalize_job_id(value: str) -> str:
    """Return the scheduler's stable numeric identity from a full PBS job ID."""

    result = value.strip().split(".", 1)[0]
    if not result:
        raise ValueError("PBS job ID must not be empty")
    return result


def _run(command: list[str], *, require_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run one control-plane command and raise with bounded diagnostics on failure."""

    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        diagnostic = (completed.stdout + completed.stderr)[-8000:]
        raise RuntimeError(f"command failed ({completed.returncode}): {command!r}\n{diagnostic}")
    if require_output and not completed.stdout.strip():
        raise RuntimeError(f"command returned no output: {command!r}")
    return completed


def _replace_output_path(command: list[str], path: Path) -> list[str]:
    """Return a qsub command with one exact actor output path."""

    result = list(command)
    try:
        index = result.index("-o")
    except ValueError as exc:
        raise ValueError("actor qsub command has no output path") from exc
    if index + 1 >= len(result):
        raise ValueError("actor qsub output option has no value")
    result[index + 1] = str(path)
    return result


def _qsub(command: list[str], *, role: str, slot: int | None) -> dict[str, Any]:
    """Submit one actor and return its complete scheduler receipt."""

    submitted_at = time.time()
    completed = _run(command, require_output=True)
    job_id = completed.stdout.strip().splitlines()[-1]
    return {
        "role": role,
        "slot": slot,
        "submitted_at": submitted_at,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "job_id": job_id,
        "job_id_normalized": _normalize_job_id(job_id),
    }


def _qdel(job_id: str, *, reason: str) -> dict[str, Any]:
    """Delete one exact scheduler job and retain the operator receipt."""

    requested_at = time.time()
    completed = _run(["qdel", job_id])
    return {
        "job_id": job_id,
        "job_id_normalized": _normalize_job_id(job_id),
        "reason": reason,
        "requested_at": requested_at,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _sleep_until(origin: float, delay_seconds: float) -> None:
    """Wait until one monotonic scenario deadline without accumulating prior drift."""

    while True:
        remaining = origin + delay_seconds - time.monotonic()
        if remaining <= 0.0:
            return
        time.sleep(min(remaining, 1.0))


def _read_rows(
    database: Path, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    """Execute one bounded read-only authority query during orchestration."""

    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    finally:
        connection.close()


def _wait_bootstrap_admissions(
    database: Path,
    initial_job_ids: list[str],
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Wait until all eight submitted bootstrap jobs own admitted instances."""

    expected = {_normalize_job_id(job_id) for job_id in initial_job_ids}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = _read_rows(
            database,
            "SELECT instance_id, stream_id, stream_epoch, status, pbs_job_id, admitted_at "
            "FROM learner_instances WHERE launch_request_id IS NULL AND admitted_at IS NOT NULL",
        )
        matched = [
            row
            for row in rows
            if isinstance(row.get("pbs_job_id"), str)
            and _normalize_job_id(str(row["pbs_job_id"])) in expected
        ]
        if len(matched) == len(expected) and {row["status"] for row in matched} == {"admitted"}:
            return matched
        time.sleep(1.0)
    raise TimeoutError("eight bootstrap learner jobs did not become admitted")


def _choose_learner_victim(
    run_id: str,
    admissions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Choose one reproducible random admitted bootstrap learner as the fault target."""

    seed = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16], 16)
    ordered = sorted(admissions, key=lambda row: int(row["stream_id"]))
    victim = dict(random.Random(seed).choice(ordered))
    victim["selection_seed"] = seed
    return victim


def _wait_replacement(
    database: Path,
    *,
    victim_instance_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for production capacity recovery to admit the exact replacement."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = _read_rows(
            database,
            "SELECT request_id, role, reason, stream_id, replace_instance_id, state, "
            "pbs_job_id, admitted_instance_id, created_at, updated_at "
            "FROM launch_requests WHERE role<>'bootstrap' ORDER BY created_at",
        )
        exact = [row for row in rows if row["replace_instance_id"] == victim_instance_id]
        if len(exact) > 1:
            raise RuntimeError("capacity service created multiple requests for one lost instance")
        if exact and exact[0]["state"] == "admitted" and exact[0]["admitted_instance_id"]:
            return exact[0]
        time.sleep(1.0)
    raise TimeoutError("production capacity service did not admit the replacement learner")


def _wait_terminal(summary_path: Path, *, timeout_seconds: float) -> dict[str, Any]:
    """Wait for create-once terminal control evidence and validate its completion bit."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if summary_path.is_file():
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("all_learners_stopped") is not True:
                raise RuntimeError("terminal summary does not prove all learners stopped")
            return payload
        time.sleep(2.0)
    raise TimeoutError("Full Protocol run did not publish terminal summary")


def _scheduler_history(job_id: str, scheduler: PBSScheduler) -> dict[str, Any]:
    """Capture current and historical scheduler observations for one owned job."""

    current = scheduler.query(job_id)
    historical = scheduler.query(job_id, historical=True)
    return {
        "job_id": job_id,
        "current": {
            "classification": current.classification,
            "fields": current.fields,
            "returncode": current.returncode,
            "stderr": current.stderr,
        },
        "historical": {
            "classification": historical.classification,
            "fields": historical.fields,
            "returncode": historical.returncode,
            "stderr": historical.stderr,
        },
    }


def _wait_owned_jobs_finished(
    job_ids: set[str],
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Wait until every exact owned actor has scheduler terminal evidence."""

    scheduler = PBSScheduler()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        histories = [_scheduler_history(job_id, scheduler) for job_id in sorted(job_ids)]
        unfinished = [
            item
            for item in histories
            if item["current"]["classification"] not in {"finished", "no_record"}
            or item["historical"]["classification"] != "finished"
        ]
        if not unfinished:
            return histories
        time.sleep(2.0)
    raise TimeoutError("owned actor jobs did not all reach scheduler FINISH")


def _best_effort_cancel(job_ids: set[str]) -> list[dict[str, Any]]:
    """Request cleanup only for the exact actor IDs owned by this supervisor."""

    results: list[dict[str, Any]] = []
    scheduler = PBSScheduler()
    for job_id in sorted(job_ids):
        observation = scheduler.query(job_id)
        if observation.classification in {"finished", "no_record"}:
            continue
        try:
            results.append(_qdel(job_id, reason="supervisor_failure_cleanup"))
        except Exception as exc:  # Cleanup diagnostics must not hide the primary failure.
            results.append({"job_id": job_id, "error": repr(exc)})
    return results


def _attestation_topology(
    run_root: Path, initial_job_ids: list[str], syncer_job_id: str
) -> dict[str, Any]:
    """Prove that the initial eight learners and first syncer used distinct allocations."""

    expected = {_normalize_job_id(item) for item in [*initial_job_ids, syncer_job_id]}
    matched: dict[str, dict[str, Any]] = {}
    for path in sorted((run_root / "metrics" / "attestations").glob("*/*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_job_id = payload.get("scheduler_job_id")
        if not isinstance(payload, dict) or not isinstance(raw_job_id, str):
            continue
        normalized = _normalize_job_id(raw_job_id)
        if normalized in expected:
            matched[normalized] = {
                "actor_kind": payload.get("actor_kind"),
                "actor_id": payload.get("actor_id"),
                "attempt_id": payload.get("attempt_id"),
                "hostname": payload.get("hostname"),
                "scheduler_job_id": raw_job_id,
                "path": str(path),
            }
    if set(matched) != expected:
        raise RuntimeError("initial actor attestations do not cover all submitted jobs")
    hosts = {str(item["hostname"]) for item in matched.values()}
    if len(hosts) != 9:
        raise RuntimeError(f"initial independent topology used {len(hosts)} hosts, expected 9")
    return {
        "expected_job_ids": sorted(expected),
        "distinct_hosts": sorted(hosts),
        "actors": matched,
    }


def _final_authority_evidence(
    database: Path,
    *,
    run_root: Path,
    scenario: Scenario,
    first_syncer_job_id: str,
    victim: dict[str, Any] | None,
    replacement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate terminal, merge, membership, replacement, and lease invariants."""

    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [tuple(row) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != [("ok",)]:
            raise RuntimeError(f"authority integrity check failed: {integrity}")
        controller = dict(
            connection.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
        )
        terminal = dict(
            connection.execute("SELECT * FROM terminal_state WHERE singleton=1").fetchone()
        )
        generation = int(controller["generation"])
        fences = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM terminal_contributor_fences WHERE generation=? "
                "ORDER BY stable_contributor_key",
                (generation,),
            )
        ]
        updates = read_logical_authority_rows(
            connection,
            RunPaths(run_root),
            table="updates",
            primary_key="update_id",
        )
        launches = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM launch_requests ORDER BY created_at, request_id"
            )
        ]
        instances = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM learner_instances ORDER BY registered_at, instance_id"
            )
        ]
        epochs = [
            dict(row) for row in connection.execute("SELECT * FROM syncer_epochs ORDER BY epoch")
        ]
        versions = sorted(
            read_logical_authority_rows(
                connection,
                RunPaths(run_root),
                table="global_versions",
                primary_key="version",
            ),
            key=lambda row: int(row["version"]),
        )
    finally:
        connection.close()

    merge_totals: dict[int, dict[str, int]] = {}
    for row in updates:
        if row["status"] != "applied":
            continue
        version = int(row["applied_version"])
        inner_steps = int(row["inner_steps"])
        aggregate = merge_totals.setdefault(
            version,
            {
                "applied_version": version,
                "contributor_count": 0,
                "min_inner_steps": inner_steps,
                "max_inner_steps": inner_steps,
            },
        )
        aggregate["contributor_count"] += 1
        aggregate["min_inner_steps"] = min(aggregate["min_inner_steps"], inner_steps)
        aggregate["max_inner_steps"] = max(aggregate["max_inner_steps"], inner_steps)
    merge_counts = [merge_totals[version] for version in sorted(merge_totals)]

    if controller["state"] != "finalized" or int(terminal["final_version"]) != 10:
        raise RuntimeError("terminal authority did not finalize global version 10")
    if len(fences) != 8:
        raise RuntimeError("terminal authority does not cover the eight-stream pool")
    hard_crashes = [row for row in fences if row["state"] == "hard_crash"]
    if scenario.inject_learner_failure and not scenario.scaling_enabled:
        if len(hard_crashes) != 1 or sum(row["state"] == "acked" for row in fences) != 7:
            raise RuntimeError("fixed-capacity learner failure lacks one bounded hard-crash fence")
        if int(hard_crashes[0]["hard_crash_gap_tokens_upper_bound"]) <= 0:
            raise RuntimeError("fixed-capacity hard crash lacks a positive one-cycle token bound")
    elif {row["state"] for row in fences} != {"acked"} or any(
        int(row["hard_crash_gap_tokens_upper_bound"]) != 0 for row in fences
    ):
        raise RuntimeError("healthy terminal streams are not all acknowledged without gaps")
    expected_merges = [
        {
            "applied_version": version,
            "contributor_count": 4,
            "min_inner_steps": 200,
            "max_inner_steps": 200,
        }
        for version in range(1, 11)
    ]
    if merge_counts != expected_merges:
        raise RuntimeError("global versions are not ten exact 4-contributor/200-step merges")
    by_instance_id = {str(row["instance_id"]): row for row in instances}
    bootstrap = [row for row in launches if row["role"] == "bootstrap"]
    if len(bootstrap) != 8 or any(
        row["request_id"] != f"bootstrap-{row['stream_id']}"
        or row["bootstrap_slot"] != row["stream_id"]
        or row["reason"] != "initial_bootstrap"
        or row["state"] != "admitted"
        or str(row["admitted_instance_id"]) not in by_instance_id
        or int(by_instance_id[str(row["admitted_instance_id"])]["stream_id"])
        != int(row["stream_id"])
        for row in bootstrap
    ):
        raise RuntimeError("authority does not retain one exact bootstrap admission per stream")
    nonbootstrap = [row for row in launches if row["role"] != "bootstrap"]

    replacement_boundary: dict[str, Any] | None = None
    if scenario.scaling_enabled:
        if victim is None or replacement is None or len(nonbootstrap) != 1:
            raise RuntimeError("authorized learner fault did not produce exactly one replacement")
        launch_row = nonbootstrap[0]
        if (
            launch_row["role"] != "replacement"
            or launch_row["reason"] != "confirmed_scheduler_terminal_after_progress_stall"
            or launch_row["replace_instance_id"] != victim["instance_id"]
            or launch_row["state"] != "admitted"
            or launch_row["admitted_instance_id"] != replacement["admitted_instance_id"]
        ):
            raise RuntimeError("replacement launch identity or durable state is incorrect")
        old = by_instance_id[str(victim["instance_id"])]
        new = by_instance_id[str(replacement["admitted_instance_id"])]
        if (
            old["status"] != "expired"
            or new["status"] != "stopped"
            or int(old["stream_id"]) != int(new["stream_id"])
            or int(new["stream_epoch"]) <= int(old["stream_epoch"])
        ):
            raise RuntimeError("replacement did not advance the exact expired stream fence")
        replacement_version = max(
            int(row["version"])
            for row in versions
            if float(row["committed_at"]) <= float(new["admitted_at"])
        )
        old_fence = json.dumps(
            {
                "instance_id": old["instance_id"],
                "placement_id": old["placement_id"],
                "placement_epoch": old["placement_epoch"],
                "stream_id": old["stream_id"],
                "stream_epoch": old["stream_epoch"],
                "admission_generation": old["admission_generation"],
                "admission_token_sha256": old["admission_token_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        old_versions = [
            int(row["applied_version"])
            for row in updates
            if row["fence_json"] == old_fence and row["applied_version"] is not None
        ]
        old_max = max(old_versions, default=None)
        if old_max is not None and int(old_max) > replacement_version:
            raise RuntimeError("expired learner produced an authority effect after replacement")
        replacement_boundary = {
            "version_at_admission": replacement_version,
            "old_max_applied_version": old_max,
            "old_instance": old,
            "new_instance": new,
            "launch": launch_row,
        }
    elif scenario.inject_learner_failure:
        if victim is None or replacement is not None or nonbootstrap:
            raise RuntimeError("fixed-capacity learner failure produced a launch or replacement")
        old = by_instance_id[str(victim["instance_id"])]
        if old["status"] != "expired":
            raise RuntimeError("terminal close did not retire the failed fixed-capacity instance")
        old_fence = json.dumps(
            {
                "instance_id": old["instance_id"],
                "placement_id": old["placement_id"],
                "placement_epoch": old["placement_epoch"],
                "stream_id": old["stream_id"],
                "stream_epoch": old["stream_epoch"],
                "admission_generation": old["admission_generation"],
                "admission_token_sha256": old["admission_token_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        late_created = [
            str(row["update_id"])
            for row in updates
            if row["fence_json"] == old_fence
            and float(row["created_at"]) > float(victim["fault_requested_at"])
        ]
        if late_created:
            raise RuntimeError("deleted fixed-capacity learner created proposals after qdel")
        replacement_boundary = {
            "fault_requested_at": victim["fault_requested_at"],
            "old_instance": old,
            "late_created_update_ids": late_created,
        }
    elif nonbootstrap:
        raise RuntimeError("non-fault scenario produced an unexpected scale-out or replacement")

    if len(epochs) != 1:
        raise RuntimeError("learner scenarios must retain one clean syncer epoch")
    if _normalize_job_id(str(epochs[0]["pbs_job_id"])) != _normalize_job_id(first_syncer_job_id):
        raise RuntimeError("first syncer epoch does not belong to the submitted first candidate")
    if epochs[0]["final_state"] != "released":
        raise RuntimeError("syncer epoch did not release cleanly")

    return {
        "integrity_check": ["ok"],
        "controller": controller,
        "terminal": terminal,
        "terminal_contributor_fences": fences,
        "merge_counts": merge_counts,
        "bootstrap_launches": bootstrap,
        "launch_requests": nonbootstrap,
        "learner_instances": instances,
        "syncer_epochs": epochs,
        "global_versions": versions,
        "replacement_boundary": replacement_boundary,
    }


def _summary_row(project_root: Path, run_root: Path, log_root: Path) -> Path:
    """Run the canonical summary tool for this exact completed run."""

    output = log_root / "run_summary.csv"
    _run(
        [
            str(project_root / ".venv/bin/python"),
            str(project_root / "tools/summarize_runs.py"),
            str(run_root),
            "--output",
            str(output),
        ]
    )
    return output


def supervise(
    *,
    project_root: Path,
    config: Path,
    scenario_name: str,
    run_id: str,
    run_root: Path,
    log_root: Path,
    evidence_output: Path,
    actor_queue: str,
    actor_walltime: str,
    timeout_seconds: float,
) -> None:
    """Initialize, schedule, fault, verify, and cleanly close one experiment."""

    scenario = SCENARIOS[scenario_name]
    resolved_experiment_config = load_config(config)
    if resolved_experiment_config.scaling.enabled is not scenario.scaling_enabled:
        raise RuntimeError("scenario scaling policy differs from its config")
    if evidence_output.exists() or run_root.exists() or log_root.exists():
        raise FileExistsError("run, log, and evidence outputs must all be fresh")
    known_job_ids: set[str] = set()
    submissions: list[dict[str, Any]] = []
    faults: list[dict[str, Any]] = []
    victim: dict[str, Any] | None = None
    replacement: dict[str, Any] | None = None
    started_at = time.time()
    try:
        prepared = launch(
            config_path=config,
            run_id=run_id,
            shared_root=str(run_root),
            project_root=project_root,
            submit=False,
            allow_dirty_snapshot=False,
            syncer_walltime=actor_walltime,
            learner_walltime=actor_walltime,
            log_root=log_root,
            actor_queue=actor_queue,
        )
        descriptor = prepared["descriptor"]
        if descriptor.get("git_dirty") is not False:
            raise RuntimeError("experiment requires a clean run descriptor")
        log_root.mkdir(parents=True, exist_ok=False)
        _atomic_json(log_root / "init_run.json", prepared)
        syncer_command = _replace_output_path(
            list(prepared["syncer_qsub"]), log_root / "syncer_000.log"
        )
        first_syncer = _qsub(syncer_command, role="syncer", slot=0)
        submissions.append(first_syncer)
        first_syncer_job_id = str(first_syncer["job_id"])
        known_job_ids.add(first_syncer_job_id)
        learner_commands = [list(command) for command in prepared["learner_qsubs"]]
        origin = time.monotonic()
        initial_job_ids: list[str] = []
        for slot, learner_command in enumerate(learner_commands):
            command = _replace_output_path(learner_command, log_root / f"bootstrap_{slot:03d}.log")
            receipt = _qsub(command, role="bootstrap_learner", slot=slot)
            submissions.append(receipt)
            job_id = str(receipt["job_id"])
            initial_job_ids.append(job_id)
            known_job_ids.add(job_id)
        _atomic_json(
            log_root / "scenario_state.json", {"submissions": submissions, "faults": faults}
        )
        if len(initial_job_ids) != 8:
            raise RuntimeError("scenario did not submit exactly eight bootstrap learners")

        database = run_root / "control" / "syncer_metadata.sqlite3"
        if scenario.inject_learner_failure:
            assert scenario.fault_delay is not None
            _sleep_until(origin, scenario.fault_delay)
            admissions = _wait_bootstrap_admissions(
                database, initial_job_ids, timeout_seconds=180.0
            )
            victim = _choose_learner_victim(run_id, admissions)
            victim_job_id = str(victim["pbs_job_id"])
            deletion = _qdel(victim_job_id, reason="inject_learner_loss")
            victim["fault_requested_at"] = deletion["requested_at"]
            faults.append({"kind": "learner_loss", "victim": victim, "qdel": deletion})
            if scenario.scaling_enabled:
                replacement = _wait_replacement(
                    database,
                    victim_instance_id=str(victim["instance_id"]),
                    timeout_seconds=300.0,
                )
                replacement_job_id = str(replacement["pbs_job_id"])
                known_job_ids.add(replacement_job_id)
                faults[-1]["replacement"] = replacement
        _atomic_json(
            log_root / "scenario_state.json", {"submissions": submissions, "faults": faults}
        )

        terminal_summary = _wait_terminal(
            run_root / "control" / "summary.json",
            timeout_seconds=timeout_seconds,
        )
        replacement_rows = _read_rows(
            database,
            "SELECT pbs_job_id FROM launch_requests WHERE role<>'bootstrap' "
            "AND pbs_job_id IS NOT NULL",
        )
        known_job_ids.update(str(row["pbs_job_id"]) for row in replacement_rows)
        scheduler_history = _wait_owned_jobs_finished(known_job_ids, timeout_seconds=180.0)
        authority = _final_authority_evidence(
            database,
            run_root=run_root,
            scenario=scenario,
            first_syncer_job_id=first_syncer_job_id,
            victim=victim,
            replacement=replacement,
        )
        topology = _attestation_topology(run_root, initial_job_ids, first_syncer_job_id)
        summary_csv = _summary_row(project_root, run_root, log_root)
        evidence = {
            "status": "PASS",
            "gate": f"plan05-full-protocol-{scenario_name}",
            "experiment_id": scenario.experiment_id,
            "requirements": ["P05-R06", "P05-R10"],
            "source": {
                "commit": descriptor["git_commit"],
                "dirty": descriptor["git_dirty"],
                "scopes": list(SOURCE_SCOPES),
                "fingerprint": descriptor["source_fingerprint"],
            },
            "environment": {
                "supervisor_pbs_job_id": os.environ.get("PBS_JOBID"),
                "actor_queue": actor_queue,
                "actor_walltime": actor_walltime,
                "topology": topology,
            },
            "config": {
                "path": str(config),
                "resolved_path": descriptor["resolved_config_path"],
                "descriptor_sha256": descriptor["descriptor_sha256"],
                "workload": {
                    "learners": 8,
                    "inner_steps": 200,
                    "global_steps": 10,
                    "merge_contributors": 4,
                },
                "scenario": scenario_name,
                "timeline": {
                    "bootstrap_slots": list(range(8)),
                    "fault_delay": scenario.fault_delay,
                },
            },
            "metrics": {
                "started_at": started_at,
                "completed_at": time.time(),
                "terminal_summary": terminal_summary,
                "authority": authority,
            },
            "errors": [],
            "evidence_paths": {
                "run_root": str(run_root),
                "log_root": str(log_root),
                "summary_csv": str(summary_csv),
                "scenario_state": str(log_root / "scenario_state.json"),
            },
            "scheduler": {
                "submissions": submissions,
                "faults": faults,
                "history": scheduler_history,
            },
            "cleanup": {
                "owner": "scenario_supervisor",
                "owned_actor_job_ids": sorted(known_job_ids),
                "active_or_queued_after_check": [],
            },
        }
        _atomic_json(evidence_output, evidence, create_only=True)
    except BaseException as exc:
        cleanup = _best_effort_cancel(known_job_ids)
        failure = {
            "status": "FAIL",
            "gate": f"plan05-full-protocol-{scenario_name}",
            "experiment_id": scenario.experiment_id,
            "requirements": ["P05-R06", "P05-R10"],
            "source": {"commit": None, "dirty": None, "scopes": None, "fingerprint": None},
            "environment": {
                "supervisor_pbs_job_id": os.environ.get("PBS_JOBID"),
                "actor_queue": actor_queue,
                "actor_walltime": actor_walltime,
            },
            "config": {"path": str(config), "scenario": scenario_name},
            "metrics": {"started_at": started_at, "failed_at": time.time()},
            "errors": [
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            ],
            "evidence_paths": {"run_root": str(run_root), "log_root": str(log_root)},
            "scheduler": {"submissions": submissions, "faults": faults},
            "cleanup": {"owner": "scenario_supervisor", "qdel_receipts": cleanup},
        }
        if not evidence_output.exists():
            _atomic_json(evidence_output, failure, create_only=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface for one scenario supervisor allocation."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--actor-queue", default="debug-g")
    parser.add_argument("--actor-walltime", default="00:25:00")
    parser.add_argument("--timeout-seconds", type=float, default=1500.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Resolve arguments and supervise one registered plan05 scenario."""

    args = build_parser().parse_args(argv)
    supervise(
        project_root=args.project_root.resolve(),
        config=args.config.resolve(),
        scenario_name=args.scenario,
        run_id=args.run_id,
        run_root=args.run_root.resolve(),
        log_root=args.log_root.resolve(),
        evidence_output=args.evidence_output.resolve(),
        actor_queue=args.actor_queue,
        actor_walltime=args.actor_walltime,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    main()
