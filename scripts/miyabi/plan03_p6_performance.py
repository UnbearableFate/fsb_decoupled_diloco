#!/usr/bin/env python3
"""Run one formal 20-pair Plan 03 G10 non-inferiority comparison."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from typing import Any

import yaml

from fs_diloco.tools.check_workload_equivalence import compare_workloads


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
PAIRS = 20
CLASSIC_REF = "archive/classic-full-v1-final"
ARM_TIMEOUT_SECONDS = 90.0
TIMER_ANCHOR = (
    "fresh run root absent before any arm-specific initialization through all actor wait() "
    "calls returning cleanly"
)


def _capture_source(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load source identity helper")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.capture(project_root)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _classic_ref_identity(current_root: Path) -> dict[str, str]:
    object_id = _git(current_root, "rev-parse", CLASSIC_REF)
    return {
        "ref": CLASSIC_REF,
        "object_id": object_id,
        "object_type": _git(current_root, "cat-file", "-t", object_id),
        "commit": _git(current_root, "rev-parse", f"{CLASSIC_REF}^{{commit}}"),
    }


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _prepare_configs(
    *, current_root: Path, classic_root: Path | None, scratch: Path, comparison: str
) -> dict[str, Path]:
    static = yaml.safe_load(
        (current_root / "configs/fs_diloco_tiny_ha_static.yaml").read_text(encoding="utf-8")
    )
    static["data"]["synthetic_num_batches"] = 128
    static["sync"].update(
        num_learners=2,
        quorum_min=2,
        quorum_max=2,
        max_staleness_versions=0,
        stop_after_outer_steps=2,
    )
    static["training"].update(inner_steps=2, completion_mode="global_only")
    static.setdefault("learner", {}).update(
        post_publish_latest_wait_seconds=ARM_TIMEOUT_SECONDS,
        post_publish_latest_poll_seconds=0.2,
    )
    static.setdefault("terminal", {})["max_terminal_merges"] = 0
    static.setdefault("wandb", {})["enabled"] = False
    static_path = scratch / "current-static.yaml"
    _write_yaml(static_path, static)
    if comparison == "classic":
        if classic_root is None:
            raise ValueError("classic comparison requires --classic-root")
        classic = yaml.safe_load(
            (classic_root / "configs/fs_diloco_tiny_ha_static.yaml").read_text(encoding="utf-8")
        )
        classic["data"]["synthetic_num_batches"] = 128
        classic["sync"].update(
            num_learners=2,
            quorum_min=2,
            quorum_max=2,
            max_staleness_versions=0,
            stop_after_outer_steps=2,
        )
        classic["coordination"]["syncer_ha"]["enabled"] = False
        classic["training"].update(inner_steps=2, completion_mode="global_only")
        classic.setdefault("learner", {}).update(
            post_publish_latest_wait_seconds=ARM_TIMEOUT_SECONDS,
            post_publish_latest_poll_seconds=0.2,
        )
        classic["failure_sim"].update(
            enabled=False,
            sleep_jitter_seconds=0.0,
            upload_skip_probability=0.0,
            crash_probability=0.0,
        )
        classic_path = scratch / "classic.yaml"
        _write_yaml(classic_path, classic)
        return {"baseline": classic_path, "candidate": static_path}
    dynamic = yaml.safe_load(
        (current_root / "configs/fs_diloco_tiny_ha_dynamic_2node.yaml").read_text(encoding="utf-8")
    )
    dynamic["data"]["synthetic_num_batches"] = 128
    dynamic["sync"].update(
        num_learners=2,
        quorum_min=2,
        quorum_max=2,
        max_staleness_versions=0,
        stop_after_outer_steps=2,
    )
    dynamic["membership"].update(
        stream_pool_size=2,
        bootstrap_instances=2,
    )
    dynamic["scaling"].update(
        enabled=False,
        desired_contributors=2,
        low_contributor_threshold=0,
        max_pending_launch_requests=1,
        max_total_launch_requests=2,
    )
    dynamic["training"].update(inner_steps=2, completion_mode="global_only", precision="fp32")
    dynamic.setdefault("learner", {}).update(
        post_publish_latest_wait_seconds=ARM_TIMEOUT_SECONDS,
        post_publish_latest_poll_seconds=0.2,
    )
    dynamic["terminal"]["max_terminal_merges"] = 0
    dynamic["io"]["tensor_dtype"] = "float32"
    dynamic.setdefault("wandb", {})["enabled"] = False
    dynamic_path = scratch / "current-dynamic.yaml"
    _write_yaml(dynamic_path, dynamic)
    return {"baseline": static_path, "candidate": dynamic_path}


def _current_summary(run_root: Path) -> dict[str, Any]:
    database = run_root / "control/syncer_metadata.sqlite3"
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("current authority integrity check failed")
        terminal = connection.execute("SELECT * FROM terminal_state").fetchone()
        rollup = connection.execute("SELECT * FROM token_rollups WHERE singleton=1").fetchone()
        epochs = connection.execute(
            "SELECT acquired_at, final_at FROM syncer_epochs ORDER BY epoch"
        ).fetchall()
        progress = connection.execute(
            "SELECT stable_contributor_key, data_cursor FROM contributor_progress "
            "ORDER BY stable_contributor_key"
        ).fetchall()
        hot_updates = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM updates WHERE status='applied' ORDER BY update_id"
            ).fetchall()
        ]
    finally:
        connection.close()
    if (
        terminal is None
        or rollup is None
        or not epochs
        or any(row["final_at"] is None for row in epochs)
    ):
        raise RuntimeError("current arm has incomplete terminal/ledger/epoch evidence")
    updates = {
        str(row["update_id"]): row
        for row in [*_audit_table_rows(run_root, "updates"), *hot_updates]
        if row["status"] == "applied"
    }
    applied = list(updates.values())
    cursor_by_contributor: dict[str, int] = {}
    for row in applied:
        key = str(row["stable_contributor_key"])
        cursor_by_contributor[key] = max(
            cursor_by_contributor.get(key, 0), int(row["data_cursor_end"])
        )
    selected_processed = sum(int(row["processed_tokens_this_cycle"]) for row in applied)
    direct = sum(int(row["effective_tokens_this_update"]) for row in applied)
    if direct != int(rollup["direct_applied"]):
        raise RuntimeError("current applied-update projection disagrees with the token rollup")
    terminal_cursor = [int(row["data_cursor"]) for row in progress]
    return {
        "final_version": int(terminal["final_version"]),
        "processed_tokens": int(rollup["adjudicated_processed"]),
        "direct_weight_tokens": direct,
        "selected_count": len(applied),
        "cursor_identity": terminal_cursor,
        "selected_processed_tokens": selected_processed,
        "selected_cursor_identity": [
            cursor_by_contributor[key] for key in sorted(cursor_by_contributor)
        ],
        "active_protocol_seconds": (
            max(float(row["final_at"]) for row in epochs)
            - min(float(row["acquired_at"]) for row in epochs)
        ),
    }


def _classic_summary(run_root: Path) -> dict[str, Any]:
    database = run_root / "control/syncer_metadata.sqlite3"
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("classic authority integrity check failed")
        hot_versions = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM global_versions WHERE status='committed' ORDER BY version"
            ).fetchall()
        ]
        hot_updates = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM updates WHERE status='applied' ORDER BY update_id"
            ).fetchall()
        ]
    finally:
        connection.close()

    versions = sorted(
        (
            row
            for row in _deduplicate_classic_rows(
                [
                    *_read_classic_jsonl(run_root / "metrics/global_version_history.jsonl"),
                    *hot_versions,
                ],
                key="version",
            )
            if row["status"] == "committed"
        ),
        key=lambda row: int(row["version"]),
    )
    updates = sorted(
        (
            row
            for row in _deduplicate_classic_rows(
                [
                    *_read_classic_jsonl(run_root / "metrics/update_history.jsonl"),
                    *hot_updates,
                ],
                key="update_id",
            )
            if row["status"] == "applied"
        ),
        key=lambda row: (str(row["learner_id"]), int(row["applied_version"])),
    )
    if [int(row["version"]) for row in versions] != [0, 1, 2]:
        raise RuntimeError("classic arm did not finish the two-version workload")
    selected_count = sum(int(row["num_updates"]) for row in versions)
    if selected_count != len(updates):
        raise RuntimeError("classic version/update count projection disagrees")
    for version in versions:
        applied = [row for row in updates if int(row["applied_version"]) == int(version["version"])]
        if len(applied) != int(version["num_updates"]) or sum(
            int(row["tokens_this_update"]) for row in applied
        ) != int(version["total_update_tokens"]):
            raise RuntimeError("classic per-version update projection disagrees")
    selected_processed = sum(int(row["tokens_this_update"]) for row in updates)
    direct = selected_processed
    if (
        selected_processed != int(versions[-1]["total_seen_tokens"])
        or sum(int(row["total_update_tokens"]) for row in versions) != selected_processed
    ):
        raise RuntimeError("classic applied-update projection disagrees with global token total")
    cursor_by_contributor: dict[str, int] = {}
    for row in updates:
        key = str(row["learner_id"])
        cursor_by_contributor[key] = max(
            cursor_by_contributor.get(key, 0), int(row["local_step_end"])
        )
    learners: list[dict[str, Any]] = []
    for path in sorted((run_root / "heartbeats").glob("learner_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("learner_id") != path.stem:
            raise RuntimeError(f"classic terminal heartbeat identity is invalid: {path}")
        if payload.get("status") != "stopped":
            raise RuntimeError(f"classic learner did not publish stopped progress: {path}")
        learners.append(payload)
    if [str(row["learner_id"]) for row in learners] != sorted(cursor_by_contributor):
        raise RuntimeError("classic terminal progress does not cover measured contributors")
    tokens_per_step: dict[str, int] = {}
    for row in updates:
        learner_id = str(row["learner_id"])
        step_count = int(row["local_step_end"]) - int(row.get("local_step_start", 0))
        token_count = int(row["tokens_this_update"])
        if step_count <= 0 or token_count <= 0 or token_count % step_count:
            raise RuntimeError("classic update cannot establish an integral token rate")
        rate = token_count // step_count
        if learner_id in tokens_per_step and tokens_per_step[learner_id] != rate:
            raise RuntimeError("classic per-step token rate varied within one contributor")
        tokens_per_step[learner_id] = rate
    terminal_cursor = [int(row["last_local_step"] or 0) for row in learners]
    processed = sum(
        int(row["last_local_step"] or 0) * tokens_per_step[str(row["learner_id"])]
        for row in learners
    )
    return {
        "final_version": int(versions[-1]["version"]),
        "processed_tokens": processed,
        "direct_weight_tokens": direct,
        "selected_count": selected_count,
        "cursor_identity": terminal_cursor,
        "selected_processed_tokens": selected_processed,
        "selected_cursor_identity": [
            cursor_by_contributor[key] for key in sorted(cursor_by_contributor)
        ],
        "active_protocol_seconds": (
            float(versions[-1]["created_at"]) - float(versions[0]["created_at"])
        ),
    }


def _read_classic_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise RuntimeError(f"classic history row is not an object: {path}:{line_number}")
        rows.append(payload)
    return rows


def _deduplicate_classic_rows(rows: list[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    deduplicated: dict[str, dict[str, Any]] = {}
    for payload in rows:
        row = dict(payload)
        row.pop("archived_at", None)
        if key not in row:
            raise RuntimeError(f"classic history row is missing {key}")
        identity = str(row[key])
        if identity in deduplicated and deduplicated[identity] != row:
            raise RuntimeError(f"conflicting classic history row for {key}={identity}")
        deduplicated[identity] = row
    return list(deduplicated.values())


def _audit_table_rows(run_root: Path, table: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for relative in ("audit/batches/authority_history", "audit/partitions/authority_history"):
        root = run_root / relative
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            if path.name.endswith(".manifest.json"):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            for record in payload.get("records", []):
                if record.get("table") != table:
                    continue
                key = str(record["primary_key"])
                row = record["row"]
                if key in rows and rows[key] != row:
                    raise RuntimeError(f"conflicting archived {table} row: {key}")
                rows[key] = row
    return list(rows.values())


def _actor_event_tape(run_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Read actor JSONL before benchmark cleanup so lifecycle evidence survives."""

    tape: dict[str, list[dict[str, Any]]] = {}
    for actor_kind in ("syncer", "learner"):
        root = run_root / "metrics" / actor_kind
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            relative = path.relative_to(run_root).as_posix()
            rows: list[dict[str, Any]] = []
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise RuntimeError(f"actor event is not an object: {relative}:{line_number}")
                rows.append(payload)
            tape[relative] = rows
    return tape


def _wait_processes(
    processes: list[tuple[str, subprocess.Popen[Any], Any]], *, deadline: float
) -> None:
    try:
        for role, process, _handle in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"performance arm timed out before {role} completed")
            process.wait(timeout=remaining)
        failed = [
            (role, process.returncode) for role, process, _ in processes if process.returncode
        ]
        if failed:
            raise RuntimeError(f"performance actor failure: {failed}")
    finally:
        for _role, process, _handle in processes:
            if process.poll() is None:
                process.terminate()
        for _role, process, _handle in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        for _role, _process, handle in processes:
            handle.close()


def _run_arm(
    *,
    arm: str,
    implementation: str,
    project_root: Path,
    python: Path,
    config: Path,
    scratch: Path,
    environment: dict[str, str],
    pair: int,
    warmup: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    run_id = f"plan03-p6-{'warmup' if warmup else f'pair{pair:02d}'}-{arm}"
    run_root = scratch / "runs" / run_id
    log_root = scratch / "logs" / run_id
    if run_root.exists():
        raise FileExistsError(f"performance run root already exists: {run_root}")
    started = time.monotonic()
    deadline = started + timeout_seconds
    log_root.mkdir(parents=True, exist_ok=False)
    resolved_config = config
    if implementation != "classic":
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
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=max(0.1, deadline - time.monotonic()),
        )
        if init.returncode != 0:
            raise RuntimeError(f"current initializer failed: {(init.stdout + init.stderr)[-4000:]}")
        resolved_config = run_root / "control/run_config.resolved.yaml"
    actors_started = time.monotonic()
    syncer_command = [
        str(python),
        "-m",
        "fs_diloco.syncer",
        "--config",
        str(resolved_config),
        "--shared-root",
        str(run_root),
    ]
    if implementation == "classic":
        syncer_command.extend(("--run-id", run_id))
    commands: list[tuple[str, list[str]]] = [("syncer", syncer_command)]
    for index in range(2):
        command = [
            str(python),
            "-m",
            "fs_diloco.learner",
            "--config",
            str(resolved_config),
            "--shared-root",
            str(run_root),
        ]
        if implementation == "classic":
            command.extend(("--run-id", run_id))
        if implementation == "dynamic":
            command.extend(("--bootstrap-slot", str(index)))
        else:
            command.extend(("--learner-id", f"learner_{index:03d}"))
        commands.append((f"learner_{index:03d}", command))
    processes: list[tuple[str, subprocess.Popen[Any], Any]] = []
    for role, command in commands:
        handle = (log_root / f"{role}.log").open("w", encoding="utf-8")
        actor_environment = dict(environment)
        actor_environment["CUDA_VISIBLE_DEVICES"] = (
            "" if role == "syncer" else str(int(role.rsplit("_", 1)[1]))
        )
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=actor_environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        processes.append((role, process, handle))
    _wait_processes(processes, deadline=deadline)
    completed = time.monotonic()
    summary = (
        _classic_summary(run_root) if implementation == "classic" else _current_summary(run_root)
    )
    actor_event_tape = _actor_event_tape(run_root)
    tails = {
        role: (log_root / f"{role}.log")
        .read_text(encoding="utf-8", errors="replace")
        .splitlines()[-20:]
        for role, _command in commands
    }
    result = {
        "arm": arm,
        "implementation": implementation,
        "pair": pair,
        "warmup": warmup,
        "elapsed_seconds": completed - started,
        "timing": {
            "pre_actor_initialization_seconds": actors_started - started,
            "actor_process_seconds": completed - actors_started,
            "active_protocol_seconds": summary["active_protocol_seconds"],
        },
        "workload": summary,
        "process_returncodes": {role: process.returncode for role, process, _ in processes},
        "output_tails": tails,
        "actor_event_tape": actor_event_tape,
    }
    shutil.rmtree(run_root)
    shutil.rmtree(log_root)
    result["run_root_cleaned"] = True
    result["logs_cleaned"] = True
    return result


def _workload_object(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_identity": {"benchmark_contract": "plan03-g10-two-learner-tiny-v1"},
        "config_identity": {
            "learners": 2,
            "quorum": [2, 2],
            "inner_steps": 2,
            "global_versions": 2,
            "precision": "float32",
            "post_publish_latest_wait_seconds": ARM_TIMEOUT_SECONDS,
        },
        "model_identity": {
            "name": "synthetic-tiny",
            "hidden_size": 16,
            "vocab_size": 64,
        },
        "data_identity": {"name": "synthetic", "block_size": 16, "num_batches": 128},
        "seed": 1337,
        "cursor_identity": summary["cursor_identity"],
        "outer_target": summary["final_version"],
        "processed_tokens": summary["processed_tokens"],
        "direct_weight_tokens": summary["direct_weight_tokens"],
        "selected_processed_tokens": summary["selected_processed_tokens"],
        "selected_cursor_identity": summary["selected_cursor_identity"],
        "carried_ancestry_tokens": 0,
        "selected_count": summary["selected_count"],
        "failure_tape": [],
        "timer_anchor": TIMER_ANCHOR,
        "resource_allocation": {
            "nodes": 1,
            "actor_processes": 3,
            "learner_gpus": [0, 1],
            "syncer_device": "cpu",
        },
    }


def run(
    *,
    comparison: str,
    current_root: Path,
    current_python: Path,
    classic_root: Path | None,
    classic_python: Path | None,
    shared_parent: Path,
) -> dict[str, Any]:
    if not os.environ.get("PBS_JOBID"):
        raise RuntimeError("formal G10 must run inside a PBS allocation")
    current_source = _capture_source(current_root)
    if current_source["git_dirty"]:
        raise RuntimeError("formal G10 current source is dirty")
    executable_sources: dict[str, Any] = {
        "current": {
            "git_commit": current_source["git_commit"],
            "source_fingerprint": current_source["source_fingerprint"],
            "python": str(current_python),
        }
    }
    if comparison == "classic":
        if classic_root is None or classic_python is None:
            raise ValueError("classic comparison requires classic root/python")
        if classic_python.absolute() == current_python.absolute():
            raise RuntimeError("classic and current arms must use independent virtualenvs")
        classic_commit = _git(classic_root, "rev-parse", "HEAD")
        frozen_ref = _classic_ref_identity(current_root)
        if _git(classic_root, "status", "--porcelain=v1"):
            raise RuntimeError("formal G10 classic worktree is dirty")
        executable_sources["classic"] = {
            "git_commit": classic_commit,
            "frozen_ref": frozen_ref,
            "python": str(classic_python),
        }
        if classic_commit != frozen_ref["commit"]:
            raise RuntimeError("classic worktree is not the frozen archive tag")
    scratch = Path(tempfile.mkdtemp(prefix=f".plan03-p6-g10-{comparison}-", dir=shared_parent))
    trials: list[dict[str, Any]] = []
    try:
        configs = _prepare_configs(
            current_root=current_root,
            classic_root=classic_root,
            scratch=scratch,
            comparison=comparison,
        )
        current_environment = os.environ.copy()
        current_environment.update(
            FS_DILOCO_GIT_COMMIT=str(current_source["git_commit"]),
            FS_DILOCO_SOURCE_FINGERPRINT=str(current_source["source_fingerprint"]),
            FS_DILOCO_GIT_DIRTY="0",
            FS_DILOCO_REQUIRE_SOURCE_IDENTITY="1",
            CUDA_VISIBLE_DEVICES="",
            WANDB_MODE="disabled",
        )
        classic_environment = os.environ.copy()
        classic_environment.update(CUDA_VISIBLE_DEVICES="", WANDB_MODE="disabled")
        definitions = (
            {
                "baseline": (
                    "classic",
                    classic_root,
                    classic_python,
                    classic_environment,
                ),
                "candidate": (
                    "static",
                    current_root,
                    current_python,
                    current_environment,
                ),
            }
            if comparison == "classic"
            else {
                "baseline": (
                    "static",
                    current_root,
                    current_python,
                    current_environment,
                ),
                "candidate": (
                    "dynamic",
                    current_root,
                    current_python,
                    current_environment,
                ),
            }
        )
        for arm in ("baseline", "candidate"):
            implementation, root, python, environment = definitions[arm]
            assert root is not None and python is not None
            trials.append(
                _run_arm(
                    arm=arm,
                    implementation=implementation,
                    project_root=root,
                    python=python,
                    config=configs[arm],
                    scratch=scratch,
                    environment=environment,
                    pair=-1,
                    warmup=True,
                    timeout_seconds=ARM_TIMEOUT_SECONDS,
                )
            )
        for pair in range(PAIRS):
            for arm in ("baseline", "candidate") if pair % 2 == 0 else ("candidate", "baseline"):
                implementation, root, python, environment = definitions[arm]
                assert root is not None and python is not None
                trials.append(
                    _run_arm(
                        arm=arm,
                        implementation=implementation,
                        project_root=root,
                        python=python,
                        config=configs[arm],
                        scratch=scratch,
                        environment=environment,
                        pair=pair,
                        warmup=False,
                        timeout_seconds=ARM_TIMEOUT_SECONDS,
                    )
                )
        measured = [row for row in trials if not row["warmup"]]
        baseline_rows = [
            next(row for row in measured if row["pair"] == pair and row["arm"] == "baseline")
            for pair in range(PAIRS)
        ]
        candidate_rows = [
            next(row for row in measured if row["pair"] == pair and row["arm"] == "candidate")
            for pair in range(PAIRS)
        ]
        baseline_workloads = {
            _canonical(_workload_object(row["workload"])) for row in baseline_rows
        }
        candidate_workloads = {
            _canonical(_workload_object(row["workload"])) for row in candidate_rows
        }
        if len(baseline_workloads) != 1 or len(candidate_workloads) != 1:
            raise RuntimeError(
                "measured workload identity varied between paired repeats: "
                f"baseline_variants={sorted(baseline_workloads)}; "
                f"candidate_variants={sorted(candidate_workloads)}"
            )
        comparison_input = {
            "baseline": json.loads(next(iter(baseline_workloads))),
            "candidate": json.loads(next(iter(candidate_workloads))),
            "baseline_seconds": [float(row["elapsed_seconds"]) for row in baseline_rows],
            "candidate_seconds": [float(row["elapsed_seconds"]) for row in candidate_rows],
            "margin": 0.10,
            "bootstrap_samples": 10_000,
            "bootstrap_seed": 20_260_808,
        }
        result = compare_workloads(comparison_input)
        errors: list[str] = []
        if result["comparison_status"] != "COMPARABLE":
            errors.append(f"comparison_status={result['comparison_status']}")
        if result.get("noninferiority_pass") is not True:
            errors.append("pre-registered 10% median/upper non-inferiority gate failed")
        if result.get("clipping_applied") is not False:
            errors.append("performance statistic applied clipping")
        return {
            "artifact_version": 1,
            "plan_id": PLAN_ID,
            "phase_id": "P6-acceptance-final-review",
            "gate": f"G10-{'classic-vs-unified' if comparison == 'classic' else 'static-vs-dynamic'}",
            "status": "PASS" if not errors else "BLOCKED",
            "requirements_covered": [
                "P6-PERF-CLASSIC" if comparison == "classic" else "P6-PERF-DYNAMIC"
            ],
            "source_commit": current_source["git_commit"],
            "source_identity": {
                "git_commit": current_source["git_commit"],
                "git_dirty": current_source["git_dirty"],
                "source_fingerprint": current_source["source_fingerprint"],
            },
            "environment": {
                "pbs_job_id": os.environ["PBS_JOBID"],
                "host": os.uname().nodename,
                "executable_sources": executable_sources,
            },
            "method": {
                "pairs": PAIRS,
                "arm_order": "AB on even pair indices, BA on odd pair indices",
                "timer_anchor": TIMER_ANCHOR,
                "warmup": "one unmeasured fresh-root run per arm",
                "margin": 0.10,
                "bootstrap_seed": 20_260_808,
                "bootstrap_samples": 10_000,
                "failure_injection": False,
                "mid_cycle_replacement": False,
            },
            "comparison": result,
            "primary_end_to_end_seconds": {
                "baseline": comparison_input["baseline_seconds"],
                "candidate": comparison_input["candidate_seconds"],
            },
            "secondary_active_protocol_seconds": {
                "baseline": [row["timing"]["active_protocol_seconds"] for row in baseline_rows],
                "candidate": [row["timing"]["active_protocol_seconds"] for row in candidate_rows],
            },
            "workload": {
                "baseline": comparison_input["baseline"],
                "candidate": comparison_input["candidate"],
            },
            "trials": trials,
            "errors": errors,
            "scratch_removed": True,
        }
    finally:
        shutil.rmtree(scratch)


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comparison", choices=("classic", "dynamic"), required=True)
    parser.add_argument("--current-root", type=Path, required=True)
    parser.add_argument("--current-python", type=Path, required=True)
    parser.add_argument("--classic-root", type=Path)
    parser.add_argument("--classic-python", type=Path)
    parser.add_argument("--shared-parent", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current_root = args.current_root.resolve()
    try:
        payload = run(
            comparison=args.comparison,
            current_root=current_root,
            current_python=args.current_python.absolute(),
            classic_root=None if args.classic_root is None else args.classic_root.resolve(),
            classic_python=(
                None if args.classic_python is None else args.classic_python.absolute()
            ),
            shared_parent=args.shared_parent.resolve(),
        )
    except Exception as exc:
        source = _capture_source(current_root)
        payload = {
            "artifact_version": 1,
            "plan_id": PLAN_ID,
            "phase_id": "P6-acceptance-final-review",
            "gate": (
                "G10-classic-vs-unified"
                if args.comparison == "classic"
                else "G10-static-vs-dynamic"
            ),
            "status": "BLOCKED",
            "requirements_covered": [
                "P6-PERF-CLASSIC" if args.comparison == "classic" else "P6-PERF-DYNAMIC"
            ],
            "source_commit": source["git_commit"],
            "source_identity": source,
            "environment": {
                "pbs_job_id": os.environ.get("PBS_JOBID"),
                "host": os.uname().nodename,
            },
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    _write_json(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
