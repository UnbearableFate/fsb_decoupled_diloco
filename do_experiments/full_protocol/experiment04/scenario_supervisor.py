#!/usr/bin/env python3
"""Submit, perturb, and verify one plan04 Full Protocol experiment."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib.util
import json
import os
import random
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from fs_diloco.core.config import Config, load_config
from fs_diloco.core.source_identity import capture_source_identity
from fs_diloco.protocol.contributor import ContributorFence
from fs_diloco.runtime.pbs_scheduler import PBSScheduler, normalize_job_id
from fs_diloco.storage.audit_archive import read_logical_authority_rows
from fs_diloco.storage.paths import RunPaths
from fs_diloco.tools.launch_independent_run import launch


ACTOR_QUEUE = "regular-g"
ACTOR_WALLTIME = "00:30:00"
FAULT_DELAY_SECONDS = 60.0
BATCH_DELAY_SECONDS = 30.0
SYNCER_RESTART_DELAY_SECONDS = 20.0
DUAL_SYNCER_OBSERVATION_SECONDS = 60.0


@dataclass(frozen=True)
class Scenario:
    """Describe one exact learner launch schedule and fault timeline."""

    experiment_id: int  # Number used by plan04 and run identities.
    learner_batches: tuple[int, ...]  # Ordered scalar-job batch sizes.
    learner_fault: bool  # Whether one admitted bootstrap learner is deleted.
    syncer_fault: str  # One of none, restart, or dual.
    expected_inner_steps: int  # Optimizer steps represented by one proposal.
    expected_merge_width: int  # Exact proposals committed per global version.


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(0, (8,), False, "none", 50, 8),
    "normal": Scenario(1, (8,), False, "none", 100, 4),
    "stagger_4_4": Scenario(2, (4, 4), False, "none", 100, 4),
    "stagger_3_3_2": Scenario(3, (3, 3, 2), False, "none", 100, 4),
    "learner_failure_simultaneous": Scenario(4, (8,), True, "none", 100, 4),
    "learner_failure_staggered": Scenario(5, (4, 4), True, "none", 100, 4),
    "syncer_failure": Scenario(6, (8,), False, "restart", 100, 4),
    "dual_syncer": Scenario(7, (8,), False, "dual", 100, 4),
}


def _atomic_json(path: Path, payload: dict[str, Any], *, create_only: bool = False) -> None:
    """Publish one fsync-backed JSON object, optionally refusing replacement."""

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


def _run(command: list[str], *, require_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run one control command and raise with bounded diagnostics on failure."""

    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        diagnostic = (completed.stdout + completed.stderr)[-8000:]
        raise RuntimeError(f"command failed ({completed.returncode}): {command!r}\n{diagnostic}")
    if require_output and not completed.stdout.strip():
        raise RuntimeError(f"command returned no output: {command!r}")
    return completed


def _replace_output_path(command: list[str], path: Path) -> list[str]:
    """Copy a qsub command while replacing its single PBS output path."""

    result = list(command)
    try:
        option = result.index("-o")
    except ValueError as exc:
        raise ValueError("actor qsub command has no output path") from exc
    if option + 1 >= len(result):
        raise ValueError("actor qsub output option has no value")
    result[option + 1] = str(path)
    return result


def _qsub(command: list[str], *, role: str, slot: int | None = None) -> dict[str, Any]:
    """Submit one scalar actor and return the complete scheduler receipt."""

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
        "job_id_normalized": normalize_job_id(job_id),
    }


def _qdel(job_id: str, *, reason: str) -> dict[str, Any]:
    """Delete one exact scheduler job and retain the operator receipt."""

    requested_at = time.time()
    completed = _run(["qdel", job_id])
    return {
        "job_id": job_id,
        "job_id_normalized": normalize_job_id(job_id),
        "reason": reason,
        "requested_at": requested_at,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _sleep_until(origin: float, delay_seconds: float) -> None:
    """Wait for one monotonic timeline boundary without accumulating drift."""

    while True:
        remaining = origin + delay_seconds - time.monotonic()
        if remaining <= 0.0:
            return
        time.sleep(min(remaining, 1.0))


def _read_rows(
    database: Path,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Execute one bounded read-only authority query during orchestration."""

    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    finally:
        connection.close()


def _wait_initial_admissions(
    database: Path,
    learner_job_ids: Iterable[str],
    *,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Wait until all eight submitted bootstrap jobs own admitted instances."""

    expected = {normalize_job_id(job_id) for job_id in learner_job_ids}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not database.is_file():
            time.sleep(1.0)
            continue
        rows = _read_rows(
            database,
            "SELECT instance_id, stream_id, stream_epoch, placement_epoch, status, "
            "pbs_job_id, admitted_at FROM learner_instances WHERE admitted_at IS NOT NULL",
        )
        matched = [
            row
            for row in rows
            if isinstance(row.get("pbs_job_id"), str)
            and normalize_job_id(str(row["pbs_job_id"])) in expected
        ]
        if len(matched) == len(expected) and {str(row["status"]) for row in matched} == {
            "admitted"
        }:
            return matched
        time.sleep(1.0)
    raise TimeoutError("eight bootstrap learner jobs did not become admitted")


def _choose_victim(run_id: str, admissions: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose a reproducible random admitted learner for destructive injection."""

    seed = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16], 16)
    ordered = sorted(admissions, key=lambda row: int(row["stream_id"]))
    victim = dict(random.Random(seed).choice(ordered))
    victim["selection_seed"] = seed
    return victim


def _require_outstanding(job_id: str) -> dict[str, Any]:
    """Require one owned job to remain cancellable at its registered fault boundary."""

    observation = PBSScheduler().query(job_id)
    if not observation.outstanding:
        raise RuntimeError(
            f"fault target {job_id} is not outstanding: {observation.classification}"
        )
    return {
        "job_id": job_id,
        "classification": observation.classification,
        "fields": observation.fields,
    }


def _wait_replacement(
    database: Path,
    *,
    victim_instance_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for production capacity recovery to admit the exact successor instance."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = _read_rows(
            database,
            "SELECT request_id, role, reason, stream_id, replace_instance_id, state, "
            "pbs_job_id, admitted_instance_id, created_at, updated_at FROM launch_requests "
            "WHERE role<>'bootstrap' ORDER BY created_at",
        )
        exact = [row for row in rows if row["replace_instance_id"] == victim_instance_id]
        if len(exact) > 1:
            raise RuntimeError("capacity service created multiple replacements for one victim")
        if (
            exact
            and exact[0]["state"] == "admitted"
            and exact[0]["admitted_instance_id"]
            and exact[0]["pbs_job_id"]
        ):
            return exact[0]
        time.sleep(1.0)
    raise TimeoutError("production capacity service did not admit the replacement learner")


def _wait_terminal(summary_path: Path, *, timeout_seconds: float) -> dict[str, Any]:
    """Wait for create-once terminal control evidence from the protocol authority."""

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
    """Capture current and historical scheduler observations for one owned actor."""

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
    """Wait until every exact actor job has scheduler terminal evidence."""

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


def _discover_authority_jobs(database: Path) -> set[str]:
    """Return actor scheduler IDs already committed by the run authority."""

    if not database.is_file():
        return set()
    rows = _read_rows(
        database,
        "SELECT pbs_job_id FROM learner_instances WHERE pbs_job_id IS NOT NULL "
        "UNION SELECT pbs_job_id FROM syncer_epochs WHERE pbs_job_id IS NOT NULL",
    )
    return {str(row["pbs_job_id"]) for row in rows}


def _best_effort_cancel(job_ids: set[str]) -> list[dict[str, Any]]:
    """Cancel only exact owned jobs while preserving cleanup diagnostics."""

    results: list[dict[str, Any]] = []
    scheduler = PBSScheduler()
    for job_id in sorted(job_ids):
        observation = scheduler.query(job_id)
        if not observation.outstanding:
            continue
        try:
            results.append(_qdel(job_id, reason="supervisor_failure_cleanup"))
        except Exception as exc:  # Cleanup must not hide the primary failure.
            results.append({"job_id": job_id, "error": repr(exc)})
    return results


def _load_summary_tool(project_root: Path) -> ModuleType:
    """Load the repository's canonical standalone run summarizer."""

    path = project_root / "tools/summarize_runs.py"
    specification = importlib.util.spec_from_file_location("plan04_summarize_runs", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load tools/summarize_runs.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _read_summary_rows(path: Path) -> list[dict[str, str]]:
    """Read the unified CSV after the canonical tool completes its update."""

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _append_summary(
    *,
    project_root: Path,
    run_root: Path,
    scenario: Scenario,
    source_fingerprint: str,
    comparison_output: Path,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Append one run with the canonical tool and compare normal against baseline."""

    tool = _load_summary_tool(project_root)
    summary_csv = project_root / "runs/summary.csv"
    lock_path = project_root / "runs/.summary.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        tool.update_summary_csv([run_root], summary_csv)
        rows = _read_summary_rows(summary_csv)
    matches = [row for row in rows if row.get("run_id") == run_root.name]
    if len(matches) != 1:
        raise RuntimeError("canonical summary did not contain the completed run exactly once")
    current = matches[0]
    if scenario.experiment_id != 1:
        return current, None

    baselines = [
        row
        for row in rows
        if "_e0_baseline_" in row.get("run_id", "")
        and row.get("source_fingerprint") == source_fingerprint
    ]
    if len(baselines) != 1:
        raise RuntimeError("normal comparison requires one current-target plan04 baseline row")
    baseline = baselines[0]
    identity_fields = (
        "model_name_or_path",
        "model_revision",
        "model_dtype",
        "dataset_name",
        "dataset_config_name",
        "dataset_revision",
        "train_split",
        "block_size",
        "micro_batch_size",
        "gradient_accumulation_steps",
        "learning_rate",
        "optimizer_beta1",
        "optimizer_beta2",
        "optimizer_epsilon",
        "weight_decay",
    )
    mismatches = [field for field in identity_fields if baseline[field] != current[field]]
    baseline_work = (
        int(baseline["merge_contributors"])
        * int(baseline["synchronization_interval"])
        * int(baseline["synchronization_count"])
    )
    current_work = (
        int(current["merge_contributors"])
        * int(current["synchronization_interval"])
        * int(current["synchronization_count"])
    )
    metrics: dict[str, dict[str, Any]] = {}
    exceeded = False
    for field in ("final_mean_loss", "training_time_seconds"):
        baseline_value = float(baseline[field])
        current_value = float(current[field])
        if baseline_value == 0.0:
            raise RuntimeError(f"baseline {field} is zero")
        difference = (current_value - baseline_value) / abs(baseline_value)
        over_threshold = abs(difference) > 0.20
        exceeded = exceeded or over_threshold
        metrics[field] = {
            "baseline": baseline_value,
            "normal": current_value,
            "relative_difference": difference,
            "absolute_difference_exceeds_threshold": over_threshold,
        }
    comparison = {
        "format_version": 1,
        "baseline_run_id": baseline["run_id"],
        "normal_run_id": current["run_id"],
        "source_fingerprint": source_fingerprint,
        "identity_mismatches": mismatches,
        "baseline_applied_local_steps": baseline_work,
        "normal_applied_local_steps": current_work,
        "equal_applied_local_steps": baseline_work == current_work,
        "threshold": 0.20,
        "metrics": metrics,
        "investigation_required": bool(mismatches) or baseline_work != current_work or exceeded,
    }
    _atomic_json(comparison_output, comparison, create_only=True)
    return current, comparison


def _attestations(run_root: Path) -> list[dict[str, Any]]:
    """Read immutable actor attestations and validate path/content identity."""

    root = run_root / "metrics/attestations"
    payloads: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/*/*.json")):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
            raise RuntimeError(f"actor attestation is not immutable: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"actor attestation is not an object: {path}")
        claimed_sha256 = payload.get("attestation_sha256")
        canonical = json.dumps(
            {key: value for key, value in payload.items() if key != "attestation_sha256"},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if claimed_sha256 != hashlib.sha256(canonical).hexdigest():
            raise RuntimeError(f"actor attestation checksum is invalid: {path}")
        expected = (
            root
            / str(payload.get("actor_kind"))
            / str(payload.get("actor_id"))
            / (f"{payload.get('attempt_id')}.json")
        )
        if path != expected:
            raise RuntimeError(f"actor attestation path does not match its identity: {path}")
        payloads.append(payload)
    return payloads


def _authority_evidence(
    *,
    run_root: Path,
    config: Config,
    scenario: Scenario,
    initial_learner_job_ids: list[str],
    primary_syncer_job_id: str,
    successor_syncer_job_id: str | None,
    victim: dict[str, Any] | None,
    replacement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate terminal workload, membership recovery, topology, and lease history."""

    database = RunPaths(run_root).sqlite_db
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [tuple(row) for row in connection.execute("PRAGMA integrity_check")]
        controller = dict(
            connection.execute("SELECT * FROM controller_state WHERE singleton=1").fetchone()
        )
        terminal = dict(
            connection.execute("SELECT * FROM terminal_state WHERE singleton=1").fetchone()
        )
        epochs = [
            dict(row) for row in connection.execute("SELECT * FROM syncer_epochs ORDER BY epoch")
        ]
        instances = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM learner_instances ORDER BY registered_at, instance_id"
            )
        ]
        launches = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM launch_requests ORDER BY created_at, request_id"
            )
        ]
        fences = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM terminal_contributor_fences WHERE generation=? "
                "ORDER BY stable_contributor_key",
                (int(controller["generation"]),),
            )
        ]
        versions = [
            dict(row)
            for row in connection.execute("SELECT * FROM global_versions ORDER BY version")
        ]
        progress = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM contributor_progress ORDER BY stable_contributor_key"
            )
        ]
        capacity = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM capacity_observations ORDER BY observation_seq"
            )
        ]
        updates = read_logical_authority_rows(
            connection,
            RunPaths(run_root),
            table="updates",
            primary_key="update_id",
        )
    finally:
        connection.close()

    if integrity != [("ok",)]:
        raise RuntimeError(f"authority integrity check failed: {integrity}")
    if controller["state"] != "finalized" or terminal["state"] != "finalized":
        raise RuntimeError("terminal authority is not finalized")
    if int(terminal["final_version"]) != 10:
        raise RuntimeError("terminal authority did not stop at global version 10")
    if [int(row["version"]) for row in versions] != list(range(11)):
        raise RuntimeError("global versions are not complete and contiguous")

    applied_counts: dict[int, int] = {}
    for update in updates:
        if int(update["inner_steps"]) != scenario.expected_inner_steps:
            raise RuntimeError("an update does not contain the registered local-step workload")
        if update["status"] == "applied":
            version = int(update["applied_version"])
            applied_counts[version] = applied_counts.get(version, 0) + 1
    expected_counts = {version: scenario.expected_merge_width for version in range(1, 11)}
    if applied_counts != expected_counts:
        raise RuntimeError("applied updates do not form ten exact merge thresholds")
    if config.training.inner_steps != scenario.expected_inner_steps:
        raise RuntimeError("resolved config has the wrong local-step workload")
    if (
        config.sync.stop_after_outer_steps != 10
        or config.sync.quorum_min != scenario.expected_merge_width
        or config.sync.quorum_max != scenario.expected_merge_width
        or config.membership.stream_pool_size != 8
        or config.membership.bootstrap_instances != 8
    ):
        raise RuntimeError("resolved config differs from the registered topology or merge workload")

    expected_streams = {str(index) for index in range(8)}
    if {str(row["stable_contributor_key"]) for row in fences} != expected_streams or {
        str(row["state"]) for row in fences
    } != {"acked"}:
        raise RuntimeError("terminal fences do not acknowledge all eight streams")
    for row in fences:
        ContributorFence.from_dict(json.loads(str(row["fence_json"])))
    if {str(row["stable_contributor_key"]) for row in progress} != expected_streams:
        raise RuntimeError("contributor progress does not cover all eight streams")

    initial_jobs = {normalize_job_id(job_id) for job_id in initial_learner_job_ids}
    initial_instances = [
        row
        for row in instances
        if isinstance(row.get("pbs_job_id"), str)
        and normalize_job_id(str(row["pbs_job_id"])) in initial_jobs
    ]
    if (
        len(initial_instances) != 8
        or {normalize_job_id(str(row["pbs_job_id"])) for row in initial_instances} != initial_jobs
    ):
        raise RuntimeError("authority does not bind exactly eight bootstrap scheduler jobs")
    bootstrap_launches = [row for row in launches if row["role"] == "bootstrap"]
    if len(bootstrap_launches) != 8 or {
        int(row["bootstrap_slot"]) for row in bootstrap_launches
    } != set(range(8)):
        raise RuntimeError("authority does not retain exactly eight bootstrap authorizations")

    replacement_evidence: dict[str, Any] | None = None
    nonbootstrap = [row for row in launches if row["role"] != "bootstrap"]
    if scenario.learner_fault:
        if victim is None or replacement is None or len(nonbootstrap) != 1:
            raise RuntimeError("learner fault lacks one exact replacement authorization")
        request = nonbootstrap[0]
        old = [row for row in instances if row["instance_id"] == victim["instance_id"]]
        new = [
            row for row in instances if row["instance_id"] == replacement["admitted_instance_id"]
        ]
        if (
            request["request_id"] != replacement["request_id"]
            or request["replace_instance_id"] != victim["instance_id"]
            or request["reason"] != "confirmed_scheduler_terminal_after_progress_stall"
            or request["state"] != "admitted"
            or len(old) != 1
            or len(new) != 1
            or old[0]["status"] != "expired"
            or int(new[0]["stream_id"]) != int(old[0]["stream_id"])
            or int(new[0]["stream_epoch"]) <= int(old[0]["stream_epoch"])
            or int(new[0]["placement_epoch"]) <= int(old[0]["placement_epoch"])
        ):
            raise RuntimeError("replacement does not prove exact expired-stream succession")
        replacement_evidence = {"request": request, "victim": old[0], "successor": new[0]}
    elif nonbootstrap:
        raise RuntimeError("fault-free scenario created an unexpected capacity launch")

    expected_epoch_states = (
        ("released",) if scenario.syncer_fault == "none" else ("expired", "released")
    )
    if tuple(str(row["final_state"]) for row in epochs) != expected_epoch_states:
        raise RuntimeError("syncer epoch lifecycle differs from the registered fault scenario")
    if normalize_job_id(str(epochs[0]["pbs_job_id"])) != normalize_job_id(primary_syncer_job_id):
        raise RuntimeError("primary syncer epoch does not match its scheduler job")
    if successor_syncer_job_id is not None:
        if (
            len(epochs) != 2
            or normalize_job_id(str(epochs[1]["pbs_job_id"]))
            != normalize_job_id(successor_syncer_job_id)
            or int(epochs[0]["superseded_by_epoch"] or -1) != int(epochs[1]["epoch"])
        ):
            raise RuntimeError("successor syncer epoch is not linked to the expired primary")
        successor_epoch = int(epochs[1]["epoch"])
        successor_versions = [
            int(row["version"])
            for row in versions
            if int(row["committed_by_epoch"]) == successor_epoch
        ]
        if not successor_versions:
            raise RuntimeError("successor syncer committed no global publication")
    elif len(epochs) != 1:
        raise RuntimeError("fault-free scenario has multiple syncer authority epochs")

    attestations = _attestations(run_root)
    descriptor = json.loads((run_root / "control/run_descriptor.json").read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict):
        raise RuntimeError("run descriptor is not a JSON object")
    if any(
        row.get("run_id") != descriptor.get("run_id")
        or row.get("descriptor_sha256") != descriptor.get("descriptor_sha256")
        or row.get("source_fingerprint") != descriptor.get("source_fingerprint")
        for row in attestations
    ):
        raise RuntimeError("actor attestation identity differs from the run descriptor")
    attested_jobs = [
        normalize_job_id(str(row["scheduler_job_id"]))
        for row in attestations
        if isinstance(row.get("scheduler_job_id"), str) and row["scheduler_job_id"]
    ]
    authority_jobs = {
        normalize_job_id(str(row["pbs_job_id"]))
        for row in [*instances, *epochs]
        if isinstance(row.get("pbs_job_id"), str) and row["pbs_job_id"]
    }
    if len(attested_jobs) != len(set(attested_jobs)) or set(attested_jobs) != authority_jobs:
        raise RuntimeError("actor attestations are not one-to-one with authority scheduler jobs")
    return {
        "integrity_check": [row[0] for row in integrity],
        "controller": controller,
        "terminal": terminal,
        "merge_counts": applied_counts,
        "versions": versions,
        "syncer_epochs": epochs,
        "terminal_fences": fences,
        "contributor_progress": progress,
        "bootstrap_launches": bootstrap_launches,
        "replacement": replacement_evidence,
        "capacity_observations": capacity,
        "actor_attestations": attestations,
        "distinct_actor_hosts": sorted({str(row["hostname"]) for row in attestations}),
    }


def _validate_submission_timeline(
    *,
    scenario: Scenario,
    origin_wall: float,
    primary_syncer: dict[str, Any],
    learners: list[dict[str, Any]],
    successor_syncer: dict[str, Any] | None,
    syncer_qdel: dict[str, Any] | None,
    conflict_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate exact batch and syncer-fault timing from scheduler receipts."""

    if len(learners) != 8 or [int(row["slot"]) for row in learners] != list(range(8)):
        raise RuntimeError("submission timeline does not contain eight ordered learner slots")
    boundaries: list[dict[str, Any]] = []
    offset = 0
    for batch_index, batch_size in enumerate(scenario.learner_batches):
        batch = learners[offset : offset + batch_size]
        start = min(float(row["submitted_at"]) for row in batch)
        expected = origin_wall + batch_index * BATCH_DELAY_SECONDS
        if start < expected - 1.0:
            raise RuntimeError("a learner batch was submitted before its registered boundary")
        boundaries.append(
            {
                "batch_index": batch_index,
                "size": batch_size,
                "first_submitted_at": start,
                "offset_seconds": start - origin_wall,
            }
        )
        offset += batch_size
    if len(scenario.learner_batches) == 1:
        simultaneous_span = max(float(row["submitted_at"]) for row in learners) - min(
            float(row["submitted_at"]) for row in learners
        )
        if simultaneous_span > 10.0:
            raise RuntimeError("simultaneous learner submissions span more than ten seconds")
    if abs(float(primary_syncer["submitted_at"]) - origin_wall) > 10.0:
        raise RuntimeError("primary syncer was not submitted at the initial boundary")

    if scenario.syncer_fault == "restart":
        if successor_syncer is None or syncer_qdel is None:
            raise RuntimeError("syncer restart lacks successor or qdel evidence")
        qdel_offset = float(syncer_qdel["requested_at"]) - origin_wall
        successor_gap = float(successor_syncer["submitted_at"]) - float(syncer_qdel["requested_at"])
        if qdel_offset < FAULT_DELAY_SECONDS - 1.0 or successor_gap < (
            SYNCER_RESTART_DELAY_SECONDS - 1.0
        ):
            raise RuntimeError("syncer restart did not preserve its 60+20 second boundaries")
    elif scenario.syncer_fault == "dual":
        if successor_syncer is None or syncer_qdel is None or conflict_snapshot is None:
            raise RuntimeError("dual-syncer scenario lacks conflict/takeover evidence")
        candidate_offset = float(successor_syncer["submitted_at"]) - origin_wall
        overlap = float(syncer_qdel["requested_at"]) - float(successor_syncer["submitted_at"])
        if candidate_offset < FAULT_DELAY_SECONDS - 1.0 or overlap < (
            DUAL_SYNCER_OBSERVATION_SECONDS - 1.0
        ):
            raise RuntimeError("dual-syncer scenario did not preserve both 60-second boundaries")
        epochs = conflict_snapshot.get("syncer_epochs")
        if (
            not isinstance(epochs, list)
            or len(epochs) != 1
            or epochs[0].get("final_state") is not None
            or conflict_snapshot.get("candidate_scheduler_state")
            not in {
                "queued",
                "prologue",
                "running",
                "suspended",
            }
        ):
            raise RuntimeError("second syncer was not fenced behind the live primary lease")
    return {"batch_boundaries": boundaries}


def _evidence_paths(
    *,
    project_root: Path,
    run_root: Path,
    log_root: Path,
    comparison_output: Path,
) -> list[str]:
    """Collect regular files supporting one gate without including the gate itself."""

    candidates = [
        project_root / "runs/summary.csv",
        run_root / "control/run_descriptor.json",
        run_root / "control/run_config.resolved.yaml",
        run_root / "control/run_source_manifest.json",
        run_root / "control/syncer_metadata.sqlite3",
        run_root / "control/summary.json",
        run_root / "control/stop.json",
        log_root / "submission_receipt.json",
        log_root / "scenario_state.json",
        comparison_output,
    ]
    if log_root.is_dir():
        candidates.extend(path for path in log_root.rglob("*") if path.is_file())
    if (run_root / "metrics/attestations").is_dir():
        candidates.extend((run_root / "metrics/attestations").rglob("*.json"))
    return sorted(
        {str(path.resolve()) for path in candidates if path.is_file() and not path.is_symlink()}
    )


def _scenario_requirement(scenario: Scenario) -> str:
    """Map one plan experiment to its primary requirement owner."""

    return {
        0: "METRICS-01",
        1: "NORMAL-01",
        2: "STAGGER-01",
        3: "STAGGER-01",
        4: "LEARNER-FAULT-01",
        5: "LEARNER-FAULT-01",
        6: "SYNCER-FAULT-01",
        7: "DUAL-SYNCER-01",
    }[scenario.experiment_id]


def supervise(
    *,
    project_root: Path,
    config_path: Path,
    scenario_name: str,
    run_id: str,
    run_root: Path,
    log_root: Path,
    evidence_output: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Own one scenario from immutable initialization through terminal evidence."""

    scenario = SCENARIOS[scenario_name]
    config = load_config(config_path)
    owned_jobs: set[str] = set()
    timeline: dict[str, Any] = {
        "format_version": 1,
        "scenario": scenario_name,
        "run_id": run_id,
        "events": [],
    }
    database = run_root / "control/syncer_metadata.sqlite3"
    comparison_output = evidence_output.with_name(f"{evidence_output.stem}_comparison.json")
    log_root.mkdir(parents=True, exist_ok=False)
    try:
        prepared = launch(
            config_path=config_path,
            run_id=run_id,
            shared_root=str(run_root),
            project_root=project_root,
            submit=False,
            allow_dirty_snapshot=False,
            syncer_walltime=ACTOR_WALLTIME,
            learner_walltime=ACTOR_WALLTIME,
            log_root=log_root,
            actor_queue=ACTOR_QUEUE,
        )
        source = capture_source_identity(project_root)
        if source["git_dirty"]:
            raise RuntimeError("formal source scopes became dirty after initialization")
        origin_monotonic = time.monotonic()
        origin_wall = time.time()
        primary_syncer = _qsub(prepared["syncer_qsub"], role="syncer_primary")
        owned_jobs.add(str(primary_syncer["job_id"]))
        timeline["events"].append(primary_syncer)

        learner_receipts: list[dict[str, Any]] = []
        offset = 0
        for batch_index, batch_size in enumerate(scenario.learner_batches):
            _sleep_until(origin_monotonic, batch_index * BATCH_DELAY_SECONDS)
            for command in prepared["learner_qsubs"][offset : offset + batch_size]:
                receipt = _qsub(command, role="learner", slot=offset)
                owned_jobs.add(str(receipt["job_id"]))
                learner_receipts.append(receipt)
                timeline["events"].append(receipt)
                offset += 1
            _atomic_json(log_root / "scenario_state.json", timeline)
        if offset != 8:
            raise RuntimeError("registered learner batches do not submit exactly eight jobs")

        submission_receipt = {
            "format_version": 1,
            "submission_status": "submitted",
            "scenario": scenario_name,
            "actor_queue": ACTOR_QUEUE,
            "actor_walltime": ACTOR_WALLTIME,
            "syncer_job_id": primary_syncer["job_id"],
            "learner_job_ids": [row["job_id"] for row in learner_receipts],
            "syncer_submission": primary_syncer,
            "learner_submissions": learner_receipts,
        }
        _atomic_json(log_root / "submission_receipt.json", submission_receipt, create_only=True)
        admissions = _wait_initial_admissions(
            database,
            [str(row["job_id"]) for row in learner_receipts],
            timeout_seconds=min(180.0, timeout_seconds),
        )

        victim: dict[str, Any] | None = None
        replacement: dict[str, Any] | None = None
        successor_syncer: dict[str, Any] | None = None
        syncer_qdel: dict[str, Any] | None = None
        conflict_snapshot: dict[str, Any] | None = None
        if scenario.learner_fault:
            _sleep_until(origin_monotonic, FAULT_DELAY_SECONDS)
            victim = _choose_victim(run_id, admissions)
            target_job = str(victim["pbs_job_id"])
            victim["pre_qdel_scheduler"] = _require_outstanding(target_job)
            victim["qdel"] = _qdel(target_job, reason="plan04_learner_fault")
            timeline["events"].append({"role": "learner_fault", **victim})
            _atomic_json(log_root / "scenario_state.json", timeline)
            replacement = _wait_replacement(
                database,
                victim_instance_id=str(victim["instance_id"]),
                timeout_seconds=min(300.0, timeout_seconds),
            )
            replacement_job_id = str(replacement["pbs_job_id"])
            owned_jobs.add(replacement_job_id)
            timeline["events"].append({"role": "learner_replacement", **replacement})
            _atomic_json(log_root / "scenario_state.json", timeline)
        elif scenario.syncer_fault == "restart":
            _sleep_until(origin_monotonic, FAULT_DELAY_SECONDS)
            _require_outstanding(str(primary_syncer["job_id"]))
            syncer_qdel = _qdel(str(primary_syncer["job_id"]), reason="plan04_syncer_restart_fault")
            timeline["events"].append({"role": "syncer_fault", **syncer_qdel})
            _atomic_json(log_root / "scenario_state.json", timeline)
            _sleep_until(origin_monotonic, FAULT_DELAY_SECONDS + SYNCER_RESTART_DELAY_SECONDS)
            successor_command = _replace_output_path(
                prepared["syncer_qsub"], log_root / "syncer_successor.log"
            )
            successor_syncer = _qsub(successor_command, role="syncer_successor")
            owned_jobs.add(str(successor_syncer["job_id"]))
            timeline["events"].append(successor_syncer)
            _atomic_json(log_root / "scenario_state.json", timeline)
        elif scenario.syncer_fault == "dual":
            _sleep_until(origin_monotonic, FAULT_DELAY_SECONDS)
            successor_command = _replace_output_path(
                prepared["syncer_qsub"], log_root / "syncer_candidate.log"
            )
            successor_syncer = _qsub(successor_command, role="syncer_candidate")
            owned_jobs.add(str(successor_syncer["job_id"]))
            timeline["events"].append(successor_syncer)
            _atomic_json(log_root / "scenario_state.json", timeline)
            _sleep_until(
                origin_monotonic,
                FAULT_DELAY_SECONDS + DUAL_SYNCER_OBSERVATION_SECONDS,
            )
            candidate = _require_outstanding(str(successor_syncer["job_id"]))
            conflict_snapshot = {
                "observed_at": time.time(),
                "candidate_scheduler_state": candidate["classification"],
                "syncer_epochs": _read_rows(database, "SELECT * FROM syncer_epochs ORDER BY epoch"),
            }
            _require_outstanding(str(primary_syncer["job_id"]))
            syncer_qdel = _qdel(
                str(primary_syncer["job_id"]), reason="plan04_dual_syncer_primary_fault"
            )
            timeline["events"].append(
                {
                    "role": "dual_syncer_observation",
                    "conflict_snapshot": conflict_snapshot,
                    "primary_qdel": syncer_qdel,
                }
            )
            _atomic_json(log_root / "scenario_state.json", timeline)

        terminal_summary = _wait_terminal(
            run_root / "control/summary.json",
            timeout_seconds=timeout_seconds,
        )
        owned_jobs.update(_discover_authority_jobs(database))
        scheduler_history = _wait_owned_jobs_finished(
            owned_jobs,
            timeout_seconds=min(180.0, timeout_seconds),
        )
        timeline_evidence = _validate_submission_timeline(
            scenario=scenario,
            origin_wall=origin_wall,
            primary_syncer=primary_syncer,
            learners=learner_receipts,
            successor_syncer=successor_syncer,
            syncer_qdel=syncer_qdel,
            conflict_snapshot=conflict_snapshot,
        )
        authority = _authority_evidence(
            run_root=run_root,
            config=config,
            scenario=scenario,
            initial_learner_job_ids=[str(row["job_id"]) for row in learner_receipts],
            primary_syncer_job_id=str(primary_syncer["job_id"]),
            successor_syncer_job_id=(
                None if successor_syncer is None else str(successor_syncer["job_id"])
            ),
            victim=victim,
            replacement=replacement,
        )
        summary_row, comparison = _append_summary(
            project_root=project_root,
            run_root=run_root,
            scenario=scenario,
            source_fingerprint=str(source["source_fingerprint"]),
            comparison_output=comparison_output,
        )
        errors: list[str] = []
        if comparison is not None and comparison["investigation_required"]:
            errors.append("normal baseline comparison exceeds the registered 20% acceptance rule")
        timeline["terminal_summary"] = terminal_summary
        timeline["scheduler_history"] = scheduler_history
        _atomic_json(log_root / "scenario_state.json", timeline)
        evidence_paths = _evidence_paths(
            project_root=project_root,
            run_root=run_root,
            log_root=log_root,
            comparison_output=comparison_output,
        )
        return {
            "artifact_version": 1,
            "status": "PASS" if not errors else "FAIL",
            "gate": f"plan04_experiment_{scenario.experiment_id}",
            "experiment_id": str(scenario.experiment_id),
            "scenario": scenario_name,
            "requirements_covered": [
                "PKG-01",
                "TOPO-01",
                "WORK-01",
                _scenario_requirement(scenario),
            ],
            "source": {
                "commit": source["git_commit"],
                "dirty": source["git_dirty"],
                "scopes": source["source_scopes"],
                "fingerprint": source["source_fingerprint"],
            },
            "environment": {
                "python": sys.version,
                "pbs_job_id": os.environ.get("PBS_JOBID"),
                "hostname": os.uname().nodename,
                "modules": os.environ.get("LOADEDMODULES", "").split(":")
                if os.environ.get("LOADEDMODULES")
                else [],
                "actor_queue": ACTOR_QUEUE,
                "actor_walltime": ACTOR_WALLTIME,
            },
            "config": {
                "path": str(config_path),
                "inner_steps": config.training.inner_steps,
                "global_steps": config.sync.stop_after_outer_steps,
                "merge_threshold": config.sync.quorum_min,
                "stream_pool_size": config.membership.stream_pool_size,
            },
            "workload": {
                "initial_learner_jobs": 8,
                "initial_syncer_jobs": 1,
                "successor_syncer_jobs": 0 if successor_syncer is None else 1,
                "replacement_learner_jobs": 0 if replacement is None else 1,
                "applied_local_steps": (
                    scenario.expected_inner_steps * scenario.expected_merge_width * 10
                ),
            },
            "timeline": {**timeline_evidence, "events": timeline["events"]},
            "authority": authority,
            "scheduler_history": scheduler_history,
            "metrics": {"summary_row": summary_row, "comparison": comparison},
            "errors": errors,
            "evidence_paths": evidence_paths,
            "cleanup": {
                "owner": f"plan04:{run_id}",
                "eligible": not errors,
                "active_or_queued_jobs": [],
                "owned_job_ids": sorted(owned_jobs),
            },
        }
    except Exception as exc:
        owned_jobs.update(_discover_authority_jobs(database))
        cleanup = _best_effort_cancel(owned_jobs)
        timeline["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        timeline["cleanup"] = cleanup
        _atomic_json(log_root / "scenario_state.json", timeline)
        try:
            source = capture_source_identity(project_root)
            source_projection: dict[str, Any] | None = {
                "commit": source["git_commit"],
                "dirty": source["git_dirty"],
                "scopes": source["source_scopes"],
                "fingerprint": source["source_fingerprint"],
            }
        except Exception:
            source_projection = None
        return {
            "artifact_version": 1,
            "status": "FAIL",
            "gate": f"plan04_experiment_{scenario.experiment_id}",
            "experiment_id": str(scenario.experiment_id),
            "scenario": scenario_name,
            "requirements_covered": [_scenario_requirement(scenario)],
            "source": source_projection,
            "environment": {
                "python": sys.version,
                "pbs_job_id": os.environ.get("PBS_JOBID"),
                "hostname": os.uname().nodename,
            },
            "config": {"path": str(config_path)},
            "workload": None,
            "metrics": {},
            "errors": [f"{type(exc).__name__}: {exc}"],
            "evidence_paths": _evidence_paths(
                project_root=project_root,
                run_root=run_root,
                log_root=log_root,
                comparison_output=comparison_output,
            ),
            "cleanup": {
                "owner": f"plan04:{run_id}",
                "eligible": False,
                "owned_job_ids": sorted(owned_jobs),
                "actions": cleanup,
            },
        }


def build_parser() -> argparse.ArgumentParser:
    """Build the exact scenario supervisor command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1500.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run one scenario and publish a create-only final artifact."""

    args = build_parser().parse_args(argv)
    if args.timeout_seconds <= 0.0:
        raise SystemExit("--timeout-seconds must be positive")
    payload = supervise(
        project_root=args.project_root.resolve(),
        config_path=args.config.resolve(),
        scenario_name=args.scenario,
        run_id=args.run_id,
        run_root=args.run_root.resolve(),
        log_root=args.log_root.resolve(),
        evidence_output=args.evidence_output.resolve(),
        timeout_seconds=args.timeout_seconds,
    )
    _atomic_json(args.evidence_output.resolve(), payload, create_only=True)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
