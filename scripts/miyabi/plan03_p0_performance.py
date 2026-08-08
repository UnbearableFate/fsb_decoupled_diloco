#!/usr/bin/env python3
"""Run five paired 2-learner tiny classic/static-HA feasibility trials."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from fs_diloco.tools.paired_performance import paired_noninferiority


def _run_processes(
    python: Path,
    config: Path,
    run_id: str,
    run_root: Path,
    *,
    environment: dict[str, str],
    timeout_seconds: float,
) -> tuple[float, list[dict[str, Any]]]:
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
    started = time.monotonic()
    processes = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        for command in commands
    ]
    results: list[dict[str, Any]] = []
    deadline = started + timeout_seconds
    try:
        for role, process in zip(("syncer", "learner_000", "learner_001"), processes, strict=True):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"tiny arm timed out before {role} completed")
            output, _ = process.communicate(timeout=remaining)
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
    return time.monotonic() - started, results


def _authority_summary(run_root: Path) -> dict[str, Any]:
    database = run_root / "control" / "syncer_metadata.sqlite3"
    if not database.is_file():
        database = run_root / "run_state.sqlite"
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
) -> dict[str, Any]:
    ha = arm == "static_ha"
    run_id = f"plan03-p0-{'warmup' if warmup else f'pair{pair}'}-{arm}"
    run_root = scratch / run_id
    python = project_root / ".venv/bin/python"
    if ha:
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
            timeout=timeout_seconds,
        )
        if init.returncode != 0:
            raise RuntimeError(
                f"HA tiny init failed: {(init.stdout + init.stderr).splitlines()[-30:]}"
            )
        config = run_root / "control" / "run_config.resolved.yaml"
    elapsed, processes = _run_processes(
        python,
        config,
        run_id,
        run_root,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    summary = _authority_summary(run_root)
    if summary["final_version"] < 1:
        raise RuntimeError(f"tiny arm did not complete a merge: {summary}")
    result = {
        "arm": arm,
        "pair": pair,
        "warmup": warmup,
        "elapsed_seconds": elapsed,
        "workload": summary,
        "processes": processes,
    }
    shutil.rmtree(run_root)
    result["run_root_cleaned"] = True
    return result


def run(project_root: Path, shared_parent: Path) -> dict[str, Any]:
    if not os.environ.get("PBS_JOBID"):
        raise RuntimeError("P0 paired runtime must execute inside a PBS allocation")
    scratch = Path(tempfile.mkdtemp(prefix=".plan03-p0-performance-", dir=shared_parent))
    trials: list[dict[str, Any]] = []
    try:
        sys.path.insert(0, str(project_root / "scripts/miyabi"))
        from capture_source_identity import capture

        source = capture(project_root)
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
                "arm_order": "AB/BA alternating",
                "timer_anchor": "immediately before spawning syncer+two learner processes through all three clean exits",
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
            "workload_equivalent": len(
                {
                    (row["workload"]["final_version"], row["workload"]["total_seen_tokens"])
                    for row in measured
                }
            )
            == 1,
            "trials": trials,
            "scratch_removed": True,
        }
    finally:
        shutil.rmtree(scratch)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--shared-parent", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.project_root.resolve(), args.shared_parent.resolve()), indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
