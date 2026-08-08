#!/usr/bin/env python3
"""Run five paired 2-learner tiny classic/static-HA feasibility trials."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable

import yaml

from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.tools.paired_performance import paired_noninferiority


def _capture_source_identity(project_root: Path) -> dict[str, Any]:
    capture_path = project_root / "scripts/miyabi/capture_source_identity.py"
    spec = importlib.util.spec_from_file_location("plan03_capture_source_identity", capture_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source identity helper: {capture_path}")
    capture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture_module)
    return capture_module.capture(project_root)


def _collect_process_results(
    roles: tuple[str, ...],
    processes: list[subprocess.Popen[str]],
    *,
    log_dir: Path,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, Any]], float]:
    results: list[dict[str, Any]] = []
    for role, process in zip(roles, processes, strict=True):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError(f"tiny arm timed out before {role} completed")
        process.wait(timeout=remaining)
    completed = clock()
    for role, process in zip(roles, processes, strict=True):
        output = (log_dir / f"{role}.log").read_text(encoding="utf-8")
        results.append(
            {
                "role": role,
                "returncode": process.returncode,
                "output_tail": output.splitlines()[-20:],
            }
        )
    failed = [result for result in results if result["returncode"] != 0]
    if failed:
        raise RuntimeError(f"tiny arm process failure: {failed}")
    return results, completed


def _run_processes(
    python: Path,
    config: Path,
    run_id: str,
    run_root: Path,
    *,
    environment: dict[str, str],
    deadline: float,
    log_dir: Path,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, Any]], float]:
    commands = [
        [
            str(python),
            "-m",
            "fs_diloco.syncer",
            "--config",
            str(config),
            "--run-id",
            run_id,
            "--shared-root",
            str(run_root),
        ],
        *[
            [
                str(python),
                "-m",
                "fs_diloco.learner",
                "--config",
                str(config),
                "--run-id",
                run_id,
                "--shared-root",
                str(run_root),
                "--learner-id",
                f"learner_{index:03d}",
            ]
            for index in range(2)
        ],
    ]
    roles = ("syncer", "learner_000", "learner_001")
    log_dir.mkdir(parents=True, exist_ok=False)
    processes: list[subprocess.Popen[str]] = []
    log_handles: list[Any] = []
    try:
        for role, command in zip(roles, commands, strict=True):
            handle = (log_dir / f"{role}.log").open("w", encoding="utf-8")
            log_handles.append(handle)
            processes.append(
                subprocess.Popen(
                    command,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=environment,
                )
            )
        for handle in log_handles:
            handle.flush()
        results, completed = _collect_process_results(
            roles,
            processes,
            log_dir=log_dir,
            deadline=deadline,
            clock=clock,
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        for handle in log_handles:
            handle.close()
    return results, completed


def _authority_summary(run_root: Path) -> dict[str, Any]:
    database = run_root / "control" / "syncer_metadata.sqlite3"
    if not database.is_file():
        raise RuntimeError(f"tiny arm authority database is missing: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT version, total_seen_tokens FROM global_versions "
            "WHERE status='committed' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        integrity = [str(value[0]) for value in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()
    if row is None or integrity != ["ok"]:
        raise RuntimeError(f"tiny arm authority invalid: row={row}, integrity={integrity}")
    return {"final_version": int(row[0]), "total_seen_tokens": int(row[1]), "integrity": integrity}


def _run_arm(
    project_root: Path,
    scratch: Path,
    *,
    arm: str,
    config: Path,
    environment: dict[str, str],
    pair: int,
    warmup: bool,
    timeout_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    ha = arm == "static_ha"
    run_id = f"plan03-p0-{'warmup' if warmup else f'pair{pair}'}-{arm}"
    run_root = scratch / run_id
    log_dir = scratch / f".{run_id}-process-logs"
    python = project_root / ".venv/bin/python"
    started = clock()
    deadline = started + timeout_seconds
    if ha:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("HA tiny arm timed out before initialization")
        init = subprocess.run(
            [
                str(python),
                "-m",
                "fs_diloco.tools.init_run",
                "--config",
                str(config),
                "--run-id",
                run_id,
                "--shared-root",
                str(run_root),
                "--project-root",
                str(project_root),
                "--allow-dirty-snapshot",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=remaining,
        )
        if init.returncode != 0:
            raise RuntimeError(
                f"HA tiny init failed: {(init.stdout + init.stderr).splitlines()[-30:]}"
            )
        config = run_root / "control" / "run_config.resolved.yaml"
    actors_started = clock()
    processes, completed = _run_processes(
        python,
        config,
        run_id,
        run_root,
        environment=environment,
        deadline=deadline,
        log_dir=log_dir,
        clock=clock,
    )
    summary = _authority_summary(run_root)
    if summary["final_version"] < 1:
        raise RuntimeError(f"tiny arm did not complete a merge: {summary}")
    result = {
        "arm": arm,
        "pair": pair,
        "warmup": warmup,
        "elapsed_seconds": completed - started,
        "timing": {
            "pre_actor_initialization_seconds": actors_started - started,
            "actor_process_seconds": completed - actors_started,
        },
        "workload": summary,
        "processes": [{"role": row["role"], "returncode": row["returncode"]} for row in processes],
    }
    shutil.rmtree(run_root)
    shutil.rmtree(log_dir)
    result["run_root_cleaned"] = True
    result["process_logs_cleaned"] = True
    return result


def _validate_measured_workloads(measured: list[dict[str, Any]], *, pairs: int) -> tuple[int, int]:
    expected_keys = {(pair, arm) for pair in range(pairs) for arm in ("classic", "static_ha")}
    actual_keys = [(int(row["pair"]), str(row["arm"])) for row in measured]
    if len(actual_keys) != len(expected_keys) or set(actual_keys) != expected_keys:
        raise RuntimeError(f"paired trial coverage mismatch: {actual_keys}")
    if len(actual_keys) != len(set(actual_keys)):
        raise RuntimeError(f"duplicate paired trial key: {actual_keys}")
    signatures = {
        (int(row["workload"]["final_version"]), int(row["workload"]["total_seen_tokens"]))
        for row in measured
    }
    if len(signatures) != 1:
        raise RuntimeError(f"paired workload mismatch: {sorted(signatures)}")
    return next(iter(signatures))


def run(project_root: Path, shared_parent: Path) -> dict[str, Any]:
    if not os.environ.get("PBS_JOBID"):
        raise RuntimeError("P0 paired runtime must execute inside a PBS allocation")
    scratch = Path(tempfile.mkdtemp(prefix=".plan03-p0-performance-", dir=shared_parent))
    trials: list[dict[str, Any]] = []
    try:
        source = _capture_source_identity(project_root)
        environment = os.environ.copy()
        environment.update(
            FS_DILOCO_GIT_COMMIT=str(source["git_commit"]),
            FS_DILOCO_SOURCE_FINGERPRINT=str(source["source_fingerprint"]),
            FS_DILOCO_GIT_DIRTY="1" if source["git_dirty"] else "0",
            FS_DILOCO_REQUIRE_SOURCE_IDENTITY="1",
        )
        ha_config = project_root / "configs/fs_diloco_tiny_ha_static.yaml"
        classic_payload = yaml.safe_load(ha_config.read_text(encoding="utf-8"))
        classic_payload["coordination"]["syncer_ha"]["enabled"] = False
        classic_config = scratch / "matched-classic.yaml"
        classic_config.write_text(
            yaml.safe_dump(classic_payload, sort_keys=False),
            encoding="utf-8",
        )
        configs = {"classic": classic_config, "static_ha": ha_config}
        for arm in ("classic", "static_ha"):
            trials.append(
                _run_arm(
                    project_root,
                    scratch,
                    arm=arm,
                    config=configs[arm],
                    environment=environment,
                    pair=-1,
                    warmup=True,
                    timeout_seconds=90.0,
                )
            )
        for pair in range(5):
            order = ("classic", "static_ha") if pair % 2 == 0 else ("static_ha", "classic")
            for arm in order:
                trials.append(
                    _run_arm(
                        project_root,
                        scratch,
                        arm=arm,
                        config=configs[arm],
                        environment=environment,
                        pair=pair,
                        warmup=False,
                        timeout_seconds=90.0,
                    )
                )
        measured = [trial for trial in trials if not trial["warmup"]]
        workload_signature = _validate_measured_workloads(measured, pairs=5)
        classic = [
            float(
                next(
                    row["elapsed_seconds"]
                    for row in measured
                    if row["pair"] == pair and row["arm"] == "classic"
                )
            )
            for pair in range(5)
        ]
        candidate = [
            float(
                next(
                    row["elapsed_seconds"]
                    for row in measured
                    if row["pair"] == pair and row["arm"] == "static_ha"
                )
            )
            for pair in range(5)
        ]
        statistic = paired_noninferiority(classic, candidate)
        return {
            "artifact_version": 1,
            "experiment_id": "p0-paired-tiny-feasibility",
            "status": "FEASIBLE",
            "host": os.uname().nodename,
            "pbs_job_id": os.environ["PBS_JOBID"],
            "source_identity": {
                "git_commit": source["git_commit"],
                "git_dirty": source["git_dirty"],
                "source_fingerprint": source["source_fingerprint"],
            },
            "method": {
                "pairs": 5,
                "arm_order": "AB/BA alternating, starting with classic on even pair indices",
                "timer_anchor": "before any arm-specific initialization through all three actor clean exits",
                "prewarm": "one unmeasured fresh-root arm per mode",
                "margin": statistic.margin,
                "bootstrap_seed": 20260808,
                "bootstrap_samples": 10000,
            },
            "classic_seconds": classic,
            "static_ha_seconds": candidate,
            "signed_overheads": list(statistic.signed_overheads),
            "median_overhead": statistic.median_overhead,
            "bootstrap_upper_95": statistic.bootstrap_upper_95,
            "noninferiority_pass_is_not_a_p0_gate": statistic.passes,
            "is_formal_gate": False,
            "workload_equivalent": True,
            "workload_signature": {
                "final_version": workload_signature[0],
                "total_seen_tokens": workload_signature[1],
            },
            "trials": trials,
            "scratch_removed": True,
        }
    finally:
        shutil.rmtree(scratch)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--shared-parent", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run(args.project_root.resolve(), args.shared_parent.resolve())
    if args.output is not None:
        atomic_write_json(args.output.resolve(), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
