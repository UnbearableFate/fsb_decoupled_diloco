#!/usr/bin/env python3
"""Execute and validate all six formal P6 real tiny-pipeline scenarios."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import uuid
from typing import Any

import yaml


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"


def _source(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load source identity helper")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.capture(project_root)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
            payload = json.loads(path.read_text(encoding="utf-8"))
            for record in payload.get("records", []):
                if not isinstance(record, dict) or record.get("table") != table:
                    continue
                primary_key = str(record.get("primary_key", ""))
                row = record.get("row")
                if not primary_key or not isinstance(row, dict):
                    raise RuntimeError(f"invalid {table} audit record: {path}")
                prior = records.get(primary_key)
                if prior is not None and prior != row:
                    raise RuntimeError(f"conflicting {table} audit record: {primary_key}")
                records[primary_key] = row
    return list(records.values())


def _config(
    project_root: Path,
    output: Path,
    *,
    mode: str,
    learners: int,
    stop_after: int,
) -> None:
    source = project_root / (
        "configs/fs_diloco_tiny_ha_static.yaml"
        if mode == "static"
        else "configs/fs_diloco_tiny_ha_dynamic_2node.yaml"
    )
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    payload["data"]["synthetic_num_batches"] = 2048
    payload["sync"].update(
        num_learners=learners,
        quorum_min=learners,
        quorum_max=learners,
        max_staleness_versions=0,
        stop_after_outer_steps=stop_after,
    )
    payload["training"].update(inner_steps=2, completion_mode="global_only", precision="fp32")
    payload["io"]["tensor_dtype"] = "float32"
    payload.setdefault("wandb", {})["enabled"] = False
    if mode == "dynamic":
        payload["membership"].update(
            stream_pool_size=learners,
            bootstrap_instances=learners,
            initial_membership_deadline_seconds=60.0,
            heartbeat_stale_after_seconds=2.0,
            heartbeat_dead_after_seconds=4.0,
        )
        payload["scaling"].update(
            desired_contributors=learners,
            low_contributor_threshold=learners - 1,
            consecutive_low_windows=1,
            startup_grace_seconds=0.5,
            cooldown_seconds=0.5,
            max_total_launch_requests=4,
            learner_queue="debug-g",
            learner_walltime="00:10:00",
            scheduler_reconcile_interval_seconds=0.5,
            starvation_observation_seconds=0.5,
        )
        payload["liveness"].update(
            heartbeat_interval_seconds=0.2,
            stale_after_seconds=2.0,
            dead_after_seconds=4.0,
            no_progress_timeout_seconds=120.0,
        )
        payload["terminal"].update(
            drain_ack_timeout_seconds=15.0,
            registration_visibility_grace_seconds=0.2,
            proposal_visibility_grace_seconds=0.2,
        )
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _run_checked(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"{(completed.stdout + completed.stderr)[-6000:]}"
        )
    return completed.stdout


def _initialize(
    *,
    project_root: Path,
    python: Path,
    config: Path,
    run_id: str,
    run_root: Path,
    environment: dict[str, str],
) -> tuple[Path, dict[str, Any]]:
    output = _run_checked(
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
        environment=environment,
    )
    descriptor = json.loads(output)["descriptor"]
    return Path(str(descriptor["resolved_config_path"])), descriptor


def _spawn(
    *,
    role: str,
    command: list[str],
    project_root: Path,
    environment: dict[str, str],
    log_root: Path,
    cuda: str,
) -> tuple[subprocess.Popen[Any], Any]:
    actor_environment = dict(environment)
    actor_environment["CUDA_VISIBLE_DEVICES"] = cuda
    handle = (log_root / f"{role}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=project_root,
        env=actor_environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return process, handle


def _wait_file(path: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.05)
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_version(run_root: Path, version: int, *, timeout: float) -> None:
    database = run_root / "control/syncer_metadata.sqlite3"
    deadline = time.monotonic() + timeout
    while True:
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        try:
            current = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
        finally:
            connection.close()
        if current is not None and int(current) >= version:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"run did not reach version {version}")
        time.sleep(0.1)


def _qsub_dynamic_bootstrap(
    *,
    project_root: Path,
    run_root: Path,
    bootstrap_slot: int,
    environment: dict[str, str],
) -> str:
    variables = ",".join(
        (
            f"PROJECT_ROOT={project_root}",
            f"FS_DILOCO_SHARED_ROOT={run_root}",
            f"BOOTSTRAP_SLOT={bootstrap_slot}",
        )
    )
    output = _run_checked(
        [
            "qsub",
            "-q",
            "debug-g",
            "-l",
            "walltime=00:10:00",
            "-v",
            variables,
            str(project_root / "scripts/miyabi/run_dynamic_learner.pbs"),
        ],
        cwd=project_root,
        environment=environment,
    )
    job_id = output.splitlines()[-1].strip()
    if not job_id:
        raise RuntimeError("dynamic bootstrap qsub returned no job ID")
    return job_id


def _wait_dynamic_admission(database: Path, job_id: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM learner_instances WHERE pbs_job_id=? AND status='admitted'",
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            return dict(row)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"dynamic child {job_id} was not admitted")
        time.sleep(0.2)


def _wait_scheduler_finished(job_id: str, *, timeout: float) -> dict[str, str]:
    from fs_diloco.runtime.pbs_scheduler import PBSScheduler

    scheduler = PBSScheduler()
    deadline = time.monotonic() + timeout
    while True:
        live = scheduler.query(job_id)
        if live.classification == "query_failed":
            raise RuntimeError(f"live qstat failed for dynamic child {job_id}")
        historical = None
        if live.classification == "no_record":
            historical = scheduler.query(job_id, historical=True)
            if historical.classification == "query_failed":
                raise RuntimeError(f"historical qstat failed for dynamic child {job_id}")
        if live.classification == "finished" or (
            live.classification == "no_record"
            and historical is not None
            and historical.classification == "finished"
        ):
            return {
                "live": live.classification,
                "historical": ("not_queried" if historical is None else historical.classification),
            }
        if time.monotonic() >= deadline:
            raise TimeoutError(f"dynamic child {job_id} did not reach scheduler FINISH")
        time.sleep(0.5)


def _wait_replacement_launch(
    database: Path, *, replace_instance_id: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM launch_requests WHERE role='replacement' "
                "AND replace_instance_id=? AND pbs_job_id IS NOT NULL "
                "ORDER BY created_at LIMIT 1",
                (replace_instance_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            return dict(row)
        if time.monotonic() >= deadline:
            raise TimeoutError("production scheduler did not submit a replacement learner")
        time.sleep(0.5)


def _terminate_scenario_job(
    *, job_id: str, project_root: Path, environment: dict[str, str]
) -> dict[str, str]:
    _run_checked(["qdel", job_id], cwd=project_root, environment=environment)
    return _wait_scheduler_finished(job_id, timeout=60.0)


def _wait_process(
    role: str,
    process: subprocess.Popen[Any],
    *,
    deadline: float,
    expected_zero: bool | None,
) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"scenario timed out before {role} exited")
    process.wait(timeout=remaining)
    status = int(process.returncode)
    if expected_zero is not None and expected_zero != (status == 0):
        raise RuntimeError(f"{role} exit status {status} violated expected_zero={expected_zero}")
    return status


def _validate(
    run_root: Path, *, expected_epochs: int, expected_contributors: int
) -> dict[str, Any]:
    database = run_root / "control/syncer_metadata.sqlite3"
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        terminal = connection.execute("SELECT * FROM terminal_state").fetchone()
        controller = connection.execute(
            "SELECT * FROM controller_state WHERE singleton=1"
        ).fetchone()
        epochs = [dict(row) for row in connection.execute("SELECT * FROM syncer_epochs")]
        outstanding = int(
            connection.execute(
                "SELECT COUNT(*) FROM updates WHERE status IN ('pending','selected')"
            ).fetchone()[0]
        )
        prepared = int(
            connection.execute(
                "SELECT COUNT(*) FROM publication_intents WHERE state='prepared'"
            ).fetchone()[0]
        )
        fences = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM terminal_contributor_fences ORDER BY stable_contributor_key"
            )
        ]
        rollup = connection.execute("SELECT * FROM token_rollups WHERE singleton=1").fetchone()
        latest = connection.execute("SELECT * FROM global_versions").fetchall()
    finally:
        connection.close()
    summary = json.loads((run_root / "control/summary.json").read_text(encoding="utf-8"))
    if integrity != ["ok"] or terminal is None or controller is None:
        raise RuntimeError("tiny authority integrity/terminal rows are incomplete")
    if terminal["state"] != "finalized" or controller["state"] != "finalized":
        raise RuntimeError("tiny controller did not finalize")
    if len(epochs) < expected_epochs or epochs[-1]["final_state"] != "released":
        raise RuntimeError("tiny candidate epoch history is incomplete")
    if outstanding or prepared or len(latest) != 1:
        raise RuntimeError("tiny recovery-hot authority is not quiescent/current-only")
    if len(fences) != expected_contributors or {row["state"] for row in fences} != {"acked"}:
        raise RuntimeError("tiny terminal contributor fences are incomplete")
    if rollup is None:
        raise RuntimeError("tiny token rollup is missing")
    balance = int(rollup["adjudicated_processed"]) - sum(
        int(rollup[name])
        for name in (
            "local_discarded",
            "direct_applied",
            "direct_dropped",
            "direct_quarantined_or_conflicted",
            "direct_reported_unpublished",
            "direct_outstanding",
        )
    )
    if balance != 0 or int(rollup["direct_outstanding"]) != 0:
        raise RuntimeError("tiny token ledger is unbalanced")
    if summary.get("all_learners_stopped") is not True:
        raise RuntimeError("tiny summary does not prove all learners stopped")
    temporary_files = [
        str(path.relative_to(run_root))
        for path in run_root.rglob("*")
        if path.is_file() and (".tmp" in path.name or ".staging" in path.name)
    ]
    if temporary_files:
        raise RuntimeError(f"tiny run retained temporary files: {temporary_files[:10]}")
    return {
        "run_root": str(run_root),
        "final_version": int(terminal["final_version"]),
        "epochs": epochs,
        "token_balance": balance,
        "terminal_fences": fences,
        "summary": summary,
    }


def _commands(
    python: Path, config: Path, run_root: Path, *, mode: str, learners: int
) -> tuple[list[str], list[list[str]]]:
    syncer = [
        str(python),
        "-m",
        "fs_diloco.syncer",
        "--config",
        str(config),
        "--shared-root",
        str(run_root),
    ]
    learner_commands: list[list[str]] = []
    for index in range(learners):
        command = [
            str(python),
            "-m",
            "fs_diloco.learner",
            "--config",
            str(config),
            "--shared-root",
            str(run_root),
        ]
        if mode == "static":
            command.extend(("--learner-id", f"learner_{index:03d}"))
        else:
            command.extend(("--bootstrap-slot", str(index)))
        learner_commands.append(command)
    return syncer, learner_commands


def _simple_scenario(
    *,
    name: str,
    project_root: Path,
    python: Path,
    config: Path,
    run_root: Path,
    log_root: Path,
    environment: dict[str, str],
    mode: str,
    learners: int,
) -> dict[str, Any]:
    resolved, _descriptor = _initialize(
        project_root=project_root,
        python=python,
        config=config,
        run_id=run_root.name,
        run_root=run_root,
        environment=environment,
    )
    syncer_command, learner_commands = _commands(
        python, resolved, run_root, mode=mode, learners=learners
    )
    actors = [
        (
            "syncer",
            *_spawn(
                role="syncer",
                command=syncer_command,
                project_root=project_root,
                environment=environment,
                log_root=log_root,
                cuda="",
            ),
        )
    ]
    for index, command in enumerate(learner_commands):
        actors.append(
            (
                f"learner_{index:03d}",
                *_spawn(
                    role=f"learner_{index:03d}",
                    command=command,
                    project_root=project_root,
                    environment=environment,
                    log_root=log_root,
                    cuda=str(index),
                ),
            )
        )
    deadline = time.monotonic() + 240.0
    try:
        statuses = {
            role: _wait_process(role, process, deadline=deadline, expected_zero=True)
            for role, process, _handle in actors
        }
    finally:
        for _role, _process, handle in actors:
            handle.close()
    return {
        "name": name,
        "process_statuses": statuses,
        **_validate(run_root, expected_epochs=1, expected_contributors=learners),
    }


def _static_replacement(
    *,
    project_root: Path,
    python: Path,
    config: Path,
    run_root: Path,
    log_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    resolved, descriptor = _initialize(
        project_root=project_root,
        python=python,
        config=config,
        run_id=run_root.name,
        run_root=run_root,
        environment=environment,
    )
    syncer_command, learner_commands = _commands(
        python, resolved, run_root, mode="static", learners=2
    )
    logical = "p6-tiny-static-rerun"
    old_attempt = "attempt-p6-tiny-old"
    new_attempt = "attempt-p6-tiny-new"
    old_signal = log_root / "old-admitted.json"
    new_signal = log_root / "new-admitted.json"
    learner_commands[0].extend(("--logical-launch-id", logical, "--attempt-id", old_attempt))
    old_environment = {**environment, "FS_DILOCO_TEST_ADMISSION_SIGNAL_PATH": str(old_signal)}
    syncer, syncer_log = _spawn(
        role="syncer",
        command=syncer_command,
        project_root=project_root,
        environment=environment,
        log_root=log_root,
        cuda="",
    )
    old, old_log = _spawn(
        role="learner_old",
        command=learner_commands[0],
        project_root=project_root,
        environment=old_environment,
        log_root=log_root,
        cuda="0",
    )
    other, other_log = _spawn(
        role="learner_001",
        command=learner_commands[1],
        project_root=project_root,
        environment=environment,
        log_root=log_root,
        cuda="1",
    )
    new: subprocess.Popen[Any] | None = None
    new_log = None
    try:
        _wait_file(old_signal, timeout=60.0)
        os.kill(old.pid, signal.SIGSTOP)
        _run_checked(
            [
                str(python),
                "-m",
                "fs_diloco.tools.authorize_static_replacement",
                "--shared-root",
                str(run_root),
                "--run-id",
                run_root.name,
                "--descriptor-sha256",
                str(descriptor["descriptor_sha256"]),
                "--learner-id",
                "learner_000",
                "--old-logical-launch-id",
                logical,
                "--old-attempt-id",
                old_attempt,
                "--old-binding-generation",
                "1",
                "--new-logical-launch-id",
                logical,
                "--new-attempt-id",
                new_attempt,
                "--reason",
                "P6 tiny injected old-attempt loss",
            ],
            cwd=project_root,
            environment=environment,
        )
        new_command = list(learner_commands[0])
        new_command[new_command.index(old_attempt)] = new_attempt
        new_environment = {
            **environment,
            "FS_DILOCO_TEST_ADMISSION_SIGNAL_PATH": str(new_signal),
        }
        new, new_log = _spawn(
            role="learner_new",
            command=new_command,
            project_root=project_root,
            environment=new_environment,
            log_root=log_root,
            cuda="0",
        )
        _wait_file(new_signal, timeout=60.0)
        os.kill(old.pid, signal.SIGCONT)
        deadline = time.monotonic() + 240.0
        statuses = {
            "old": _wait_process("old", old, deadline=deadline, expected_zero=None),
            "new": _wait_process("new", new, deadline=deadline, expected_zero=True),
            "other": _wait_process("other", other, deadline=deadline, expected_zero=True),
            "syncer": _wait_process("syncer", syncer, deadline=deadline, expected_zero=True),
        }
        database = run_root / "control/syncer_metadata.sqlite3"
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            old_history = connection.execute(
                "SELECT * FROM static_binding_history WHERE learner_id='learner_000' "
                "AND logical_launch_id=? AND attempt_id=?",
                (logical, old_attempt),
            ).fetchone()
            current_binding = connection.execute(
                "SELECT * FROM static_contributor_bindings WHERE learner_id='learner_000'"
            ).fetchone()
            hot_updates = [dict(row) for row in connection.execute("SELECT * FROM updates")]
        finally:
            connection.close()
        old_updates = [
            row
            for row in [*hot_updates, *_audit_rows(run_root, "updates")]
            if row.get("stable_contributor_key") == "learner_000"
            and json.loads(str(row["fence_json"])).get("attempt_id") == old_attempt
        ]
        if old_history is None or old_history["final_status"] != "replaced":
            raise RuntimeError("old static binding lacks durable replacement history")
        if (
            current_binding is None
            or current_binding["logical_launch_id"] != logical
            or current_binding["attempt_id"] != new_attempt
            or int(current_binding["binding_generation"]) != 2
        ):
            raise RuntimeError("new static attempt is not the exact current binding")
        if old_updates:
            raise RuntimeError("resumed old static generation produced an authority update")
        replacement_evidence = {
            "old_final_status": str(old_history["final_status"]),
            "current_attempt_id": str(current_binding["attempt_id"]),
            "current_binding_generation": int(current_binding["binding_generation"]),
            "old_generation_authority_updates": 0,
        }
    finally:
        if old.poll() is None:
            os.kill(old.pid, signal.SIGCONT)
            old.terminate()
        for process in (syncer, old, other, new):
            if process is not None and process.poll() is None:
                process.terminate()
        for handle in (syncer_log, old_log, other_log, new_log):
            if handle is not None:
                handle.close()
    return {
        "name": "static-2-learners-loss-rerun-old-resume",
        "process_statuses": statuses,
        "replacement_evidence": replacement_evidence,
        **_validate(run_root, expected_epochs=1, expected_contributors=2),
    }


def _candidate_and_optional_dynamic_failure(
    *,
    name: str,
    project_root: Path,
    python: Path,
    config: Path,
    run_root: Path,
    log_root: Path,
    environment: dict[str, str],
    mode: str,
    kill_learner: bool,
) -> dict[str, Any]:
    if mode == "dynamic" and kill_learner:
        return _dynamic_candidate_and_learner_failure(
            name=name,
            project_root=project_root,
            python=python,
            config=config,
            run_root=run_root,
            log_root=log_root,
            environment=environment,
        )
    resolved, _descriptor = _initialize(
        project_root=project_root,
        python=python,
        config=config,
        run_id=run_root.name,
        run_root=run_root,
        environment=environment,
    )
    syncer_command, learner_commands = _commands(python, resolved, run_root, mode=mode, learners=2)
    first_environment = dict(environment)
    first_environment["FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION"] = "2"
    signal_path = log_root / "dynamic-learner-0-admitted.json"
    learner_zero_environment = dict(environment)
    if kill_learner:
        learner_zero_environment["FS_DILOCO_TEST_ADMISSION_SIGNAL_PATH"] = str(signal_path)
    first, first_log = _spawn(
        role="syncer_first",
        command=syncer_command,
        project_root=project_root,
        environment=first_environment,
        log_root=log_root,
        cuda="",
    )
    successor: subprocess.Popen[Any] | None = None
    successor_log = None
    learners = [
        _spawn(
            role=f"learner_{index:03d}",
            command=command,
            project_root=project_root,
            environment=(learner_zero_environment if index == 0 else environment),
            log_root=log_root,
            cuda=str(index),
        )
        for index, command in enumerate(learner_commands)
    ]
    try:
        _wait_version(run_root, 0, timeout=60.0)
        successor, successor_log = _spawn(
            role="syncer_successor",
            command=syncer_command,
            project_root=project_root,
            environment=environment,
            log_root=log_root,
            cuda="",
        )
        if kill_learner:
            _wait_file(signal_path, timeout=60.0)
            if name.endswith("syncer-and-learner-failure"):
                _wait_version(run_root, 2, timeout=120.0)
            learners[0][0].terminate()
        assert successor is not None
        deadline = time.monotonic() + 300.0
        statuses = {
            "first_candidate": _wait_process(
                "first_candidate", first, deadline=deadline, expected_zero=False
            ),
            "successor": _wait_process(
                "successor", successor, deadline=deadline, expected_zero=True
            ),
            "learner_0": _wait_process(
                "learner_0", learners[0][0], deadline=deadline, expected_zero=not kill_learner
            ),
            "learner_1": _wait_process(
                "learner_1", learners[1][0], deadline=deadline, expected_zero=True
            ),
        }
    finally:
        for process in (first, successor, learners[0][0], learners[1][0]):
            if process is not None and process.poll() is None:
                process.terminate()
        for handle in (first_log, successor_log, learners[0][1], learners[1][1]):
            if handle is not None:
                handle.close()
    return {
        "name": name,
        "process_statuses": statuses,
        **_validate(run_root, expected_epochs=2, expected_contributors=2),
    }


def _dynamic_replacement(
    *,
    project_root: Path,
    python: Path,
    config: Path,
    run_root: Path,
    log_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    resolved, _descriptor = _initialize(
        project_root=project_root,
        python=python,
        config=config,
        run_id=run_root.name,
        run_root=run_root,
        environment=environment,
    )
    syncer_command, learner_commands = _commands(
        python, resolved, run_root, mode="dynamic", learners=2
    )
    syncer, syncer_log = _spawn(
        role="syncer",
        command=syncer_command,
        project_root=project_root,
        environment=environment,
        log_root=log_root,
        cuda="",
    )
    survivor, survivor_log = _spawn(
        role="learner_001",
        command=learner_commands[1],
        project_root=project_root,
        environment=environment,
        log_root=log_root,
        cuda="1",
    )
    initial_job_id: str | None = None
    replacement_job_id: str | None = None
    try:
        initial_job_id = _qsub_dynamic_bootstrap(
            project_root=project_root,
            run_root=run_root,
            bootstrap_slot=0,
            environment=environment,
        )
        database = run_root / "control/syncer_metadata.sqlite3"
        lost_instance = _wait_dynamic_admission(database, initial_job_id, timeout=120.0)
        initial_scheduler = _terminate_scenario_job(
            job_id=initial_job_id,
            project_root=project_root,
            environment=environment,
        )
        replacement_launch = _wait_replacement_launch(
            database,
            replace_instance_id=str(lost_instance["instance_id"]),
            timeout=120.0,
        )
        replacement_job_id = str(replacement_launch["pbs_job_id"])
        if replacement_job_id == initial_job_id:
            raise RuntimeError("replacement reused the terminal learner job ID")
        replacement_instance = _wait_dynamic_admission(database, replacement_job_id, timeout=120.0)
        if int(replacement_instance["stream_id"]) != int(lost_instance["stream_id"]) or int(
            replacement_instance["stream_epoch"]
        ) <= int(lost_instance["stream_epoch"]):
            raise RuntimeError("replacement did not advance the lost stream epoch")
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        try:
            replacement_boundary = int(
                connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            )
        finally:
            connection.close()
        deadline = time.monotonic() + 300.0
        statuses = {
            "survivor": _wait_process("survivor", survivor, deadline=deadline, expected_zero=True),
            "syncer": _wait_process("syncer", syncer, deadline=deadline, expected_zero=True),
        }
        replacement_scheduler = _wait_scheduler_finished(replacement_job_id, timeout=60.0)
    finally:
        for process in (syncer, survivor):
            if process.poll() is None:
                process.terminate()
        for job_id in (initial_job_id, replacement_job_id):
            if job_id is None:
                continue
            try:
                _wait_scheduler_finished(job_id, timeout=0.1)
            except (RuntimeError, TimeoutError):
                try:
                    _terminate_scenario_job(
                        job_id=job_id,
                        project_root=project_root,
                        environment=environment,
                    )
                except (RuntimeError, TimeoutError):
                    pass
        for handle in (syncer_log, survivor_log):
            handle.close()
    result = _validate(run_root, expected_epochs=1, expected_contributors=2)
    connection = sqlite3.connect(run_root / "control/syncer_metadata.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        replacements = int(
            connection.execute(
                "SELECT COUNT(*) FROM launch_requests WHERE role='replacement' AND state='admitted'"
            ).fetchone()[0]
        )
        lost_final = connection.execute(
            "SELECT * FROM learner_instances WHERE instance_id=?",
            (str(lost_instance["instance_id"]),),
        ).fetchone()
        replacement_final = connection.execute(
            "SELECT * FROM learner_instances WHERE instance_id=?",
            (str(replacement_instance["instance_id"]),),
        ).fetchone()
        hot_updates = [dict(row) for row in connection.execute("SELECT * FROM updates")]
        nonbootstrap_launches = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM launch_requests WHERE role!='bootstrap' ORDER BY created_at"
            )
        ]
    finally:
        connection.close()
    if replacements != 1:
        raise RuntimeError("dynamic tiny scenario did not admit a replacement")
    if len(nonbootstrap_launches) != 1 or nonbootstrap_launches[0]["role"] != "replacement":
        raise RuntimeError("dynamic replacement scenario submitted an unrelated scale-out")
    if lost_final is None or lost_final["status"] != "revoked":
        raise RuntimeError("lost dynamic instance was not durably revoked")
    if replacement_final is None or replacement_final["status"] != "stopped":
        raise RuntimeError("replacement dynamic instance did not stop terminally")
    old_applied_versions = [
        int(row["applied_version"])
        for row in [*hot_updates, *_audit_rows(run_root, "updates")]
        if json.loads(str(row["fence_json"])).get("instance_id")
        == str(lost_instance["instance_id"])
        and row.get("applied_version") is not None
    ]
    if old_applied_versions and max(old_applied_versions) > replacement_boundary:
        raise RuntimeError("lost dynamic instance committed after its replacement boundary")
    replacement_updates = [
        row
        for row in [*hot_updates, *_audit_rows(run_root, "updates")]
        if json.loads(str(row["fence_json"])).get("instance_id")
        == str(replacement_instance["instance_id"])
    ]
    if not any(row.get("applied_version") is not None for row in replacement_updates):
        raise RuntimeError("replacement never contributed to a committed version")
    return {
        "name": "dynamic-2-learners-replacement",
        "process_statuses": statuses,
        "replacement_launches": replacements,
        "scheduler_jobs": {
            "lost": {"job_id": initial_job_id, **initial_scheduler},
            "replacement": {"job_id": replacement_job_id, **replacement_scheduler},
            "maximum_live_allocations": 2,
        },
        "replacement_evidence": {
            "stream_id": int(lost_instance["stream_id"]),
            "old_stream_epoch": int(lost_instance["stream_epoch"]),
            "new_stream_epoch": int(replacement_instance["stream_epoch"]),
            "replacement_boundary_version": replacement_boundary,
            "old_max_applied_version": (
                None if not old_applied_versions else max(old_applied_versions)
            ),
            "old_final_status": str(lost_final["status"]),
            "replacement_final_status": str(replacement_final["status"]),
        },
        **result,
    }


def _dynamic_candidate_and_learner_failure(
    *,
    name: str,
    project_root: Path,
    python: Path,
    config: Path,
    run_root: Path,
    log_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    resolved, _descriptor = _initialize(
        project_root=project_root,
        python=python,
        config=config,
        run_id=run_root.name,
        run_root=run_root,
        environment=environment,
    )
    syncer_command, learner_commands = _commands(
        python, resolved, run_root, mode="dynamic", learners=2
    )
    first_environment = {
        **environment,
        "FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION": "2",
    }
    first, first_log = _spawn(
        role="syncer_first",
        command=syncer_command,
        project_root=project_root,
        environment=first_environment,
        log_root=log_root,
        cuda="",
    )
    survivor, survivor_log = _spawn(
        role="learner_001",
        command=learner_commands[1],
        project_root=project_root,
        environment=environment,
        log_root=log_root,
        cuda="1",
    )
    successor: subprocess.Popen[Any] | None = None
    successor_log = None
    initial_job_id: str | None = None
    replacement_job_id: str | None = None
    try:
        initial_job_id = _qsub_dynamic_bootstrap(
            project_root=project_root,
            run_root=run_root,
            bootstrap_slot=0,
            environment=environment,
        )
        database = run_root / "control/syncer_metadata.sqlite3"
        lost_instance = _wait_dynamic_admission(database, initial_job_id, timeout=120.0)
        _wait_version(run_root, 0, timeout=60.0)
        successor, successor_log = _spawn(
            role="syncer_successor",
            command=syncer_command,
            project_root=project_root,
            environment=environment,
            log_root=log_root,
            cuda="",
        )
        _wait_version(run_root, 2, timeout=120.0)
        initial_scheduler = _terminate_scenario_job(
            job_id=initial_job_id,
            project_root=project_root,
            environment=environment,
        )
        replacement_launch = _wait_replacement_launch(
            database,
            replace_instance_id=str(lost_instance["instance_id"]),
            timeout=120.0,
        )
        replacement_job_id = str(replacement_launch["pbs_job_id"])
        if replacement_job_id == initial_job_id:
            raise RuntimeError("replacement reused the terminal learner job ID")
        replacement_instance = _wait_dynamic_admission(database, replacement_job_id, timeout=120.0)
        if int(replacement_instance["stream_id"]) != int(lost_instance["stream_id"]) or int(
            replacement_instance["stream_epoch"]
        ) <= int(lost_instance["stream_epoch"]):
            raise RuntimeError("replacement did not advance the lost stream epoch")
        connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        try:
            replacement_boundary = int(
                connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
            )
        finally:
            connection.close()
        assert successor is not None
        deadline = time.monotonic() + 300.0
        statuses = {
            "first_candidate": _wait_process(
                "first_candidate", first, deadline=deadline, expected_zero=False
            ),
            "successor": _wait_process(
                "successor", successor, deadline=deadline, expected_zero=True
            ),
            "survivor": _wait_process("survivor", survivor, deadline=deadline, expected_zero=True),
        }
        replacement_scheduler = _wait_scheduler_finished(replacement_job_id, timeout=60.0)
    finally:
        for process in (first, successor, survivor):
            if process is not None and process.poll() is None:
                process.terminate()
        for job_id in (initial_job_id, replacement_job_id):
            if job_id is None:
                continue
            try:
                _wait_scheduler_finished(job_id, timeout=0.1)
            except (RuntimeError, TimeoutError):
                try:
                    _terminate_scenario_job(
                        job_id=job_id,
                        project_root=project_root,
                        environment=environment,
                    )
                except (RuntimeError, TimeoutError):
                    pass
        for handle in (first_log, successor_log, survivor_log):
            if handle is not None:
                handle.close()
    result = _validate(run_root, expected_epochs=2, expected_contributors=2)
    connection = sqlite3.connect(run_root / "control/syncer_metadata.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        replacements = int(
            connection.execute(
                "SELECT COUNT(*) FROM launch_requests WHERE role='replacement' AND state='admitted'"
            ).fetchone()[0]
        )
        lost_final = connection.execute(
            "SELECT * FROM learner_instances WHERE instance_id=?",
            (str(lost_instance["instance_id"]),),
        ).fetchone()
        replacement_final = connection.execute(
            "SELECT * FROM learner_instances WHERE instance_id=?",
            (str(replacement_instance["instance_id"]),),
        ).fetchone()
        hot_updates = [dict(row) for row in connection.execute("SELECT * FROM updates")]
        nonbootstrap_launches = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM launch_requests WHERE role!='bootstrap' ORDER BY created_at"
            )
        ]
    finally:
        connection.close()
    if replacements != 1:
        raise RuntimeError("combined failure scenario did not admit exactly one replacement")
    if len(nonbootstrap_launches) != 1 or nonbootstrap_launches[0]["role"] != "replacement":
        raise RuntimeError("combined failure scenario submitted an unrelated scale-out")
    if lost_final is None or lost_final["status"] != "revoked":
        raise RuntimeError("combined failure lost instance was not revoked")
    if replacement_final is None or replacement_final["status"] != "stopped":
        raise RuntimeError("combined failure replacement did not stop terminally")
    old_applied_versions = [
        int(row["applied_version"])
        for row in [*hot_updates, *_audit_rows(run_root, "updates")]
        if json.loads(str(row["fence_json"])).get("instance_id")
        == str(lost_instance["instance_id"])
        and row.get("applied_version") is not None
    ]
    if old_applied_versions and max(old_applied_versions) > replacement_boundary:
        raise RuntimeError("lost instance committed after combined-failure replacement")
    replacement_updates = [
        row
        for row in [*hot_updates, *_audit_rows(run_root, "updates")]
        if json.loads(str(row["fence_json"])).get("instance_id")
        == str(replacement_instance["instance_id"])
    ]
    if not any(row.get("applied_version") is not None for row in replacement_updates):
        raise RuntimeError("combined-failure replacement never committed a contribution")
    return {
        "name": name,
        "process_statuses": statuses,
        "scheduler_jobs": {
            "lost": {"job_id": initial_job_id, **initial_scheduler},
            "replacement": {"job_id": replacement_job_id, **replacement_scheduler},
            "maximum_live_allocations": 2,
        },
        "replacement_evidence": {
            "stream_id": int(lost_instance["stream_id"]),
            "old_stream_epoch": int(lost_instance["stream_epoch"]),
            "new_stream_epoch": int(replacement_instance["stream_epoch"]),
            "replacement_boundary_version": replacement_boundary,
            "old_max_applied_version": (
                None if not old_applied_versions else max(old_applied_versions)
            ),
            "old_final_status": str(lost_final["status"]),
            "replacement_final_status": str(replacement_final["status"]),
        },
        **result,
    }


def run(project_root: Path, output: Path, runs_root: Path, temp_root: Path) -> dict[str, Any]:
    source = _source(project_root)
    if source["git_dirty"]:
        raise RuntimeError("formal tiny scenarios require a clean source tree")
    python = project_root / ".venv/bin/python"
    environment = os.environ.copy()
    environment.update(
        FS_DILOCO_GIT_COMMIT=str(source["git_commit"]),
        FS_DILOCO_SOURCE_FINGERPRINT=str(source["source_fingerprint"]),
        FS_DILOCO_GIT_DIRTY="0",
        FS_DILOCO_REQUIRE_SOURCE_IDENTITY="1",
        WANDB_MODE="disabled",
    )
    definitions = [
        ("static-1-learner-1-candidate-none", "static", 1, 1),
        ("static-2-learners-1-candidate-none", "static", 2, 2),
        ("static-2-learners-loss-rerun-old-resume", "static", 2, 10),
        ("static-2-learners-2-candidates-active-crash", "static", 2, 10),
        ("dynamic-2-learners-replacement", "dynamic", 2, 10),
        ("dynamic-2-learners-2-candidates-syncer-and-learner-failure", "dynamic", 2, 10),
    ]
    configs: dict[str, Path] = {}
    for name, mode, learners, stop_after in definitions:
        path = temp_root / f"{name}.yaml"
        _config(project_root, path, mode=mode, learners=learners, stop_after=stop_after)
        configs[name] = path
    results: list[dict[str, Any]] = []
    for name, mode, learners, _stop_after in definitions:
        run_root = runs_root / f"plan03_p6_g5_{name}_{os.environ['PBS_JOBID'].split('.')[0]}"
        log_root = project_root / "logs" / f"qsub_{run_root.name}"
        log_root.mkdir(parents=True, exist_ok=False)
        if name == "static-2-learners-loss-rerun-old-resume":
            result = _static_replacement(
                project_root=project_root,
                python=python,
                config=configs[name],
                run_root=run_root,
                log_root=log_root,
                environment=environment,
            )
        elif name == "static-2-learners-2-candidates-active-crash":
            result = _candidate_and_optional_dynamic_failure(
                name=name,
                project_root=project_root,
                python=python,
                config=configs[name],
                run_root=run_root,
                log_root=log_root,
                environment=environment,
                mode=mode,
                kill_learner=False,
            )
        elif name == "dynamic-2-learners-replacement":
            result = _dynamic_replacement(
                project_root=project_root,
                python=python,
                config=configs[name],
                run_root=run_root,
                log_root=log_root,
                environment=environment,
            )
        elif name.endswith("syncer-and-learner-failure"):
            result = _candidate_and_optional_dynamic_failure(
                name=name,
                project_root=project_root,
                python=python,
                config=configs[name],
                run_root=run_root,
                log_root=log_root,
                environment=environment,
                mode=mode,
                kill_learner=True,
            )
        else:
            result = _simple_scenario(
                name=name,
                project_root=project_root,
                python=python,
                config=configs[name],
                run_root=run_root,
                log_root=log_root,
                environment=environment,
                mode=mode,
                learners=learners,
            )
        results.append(result)
        _atomic_json(
            output.with_name(f".{output.name}.{len(results)}-of-6.partial.json"),
            {"completed": len(results), "scenarios": results},
        )
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase_id": "P6-acceptance-final-review",
        "gate": "G5-six-real-tiny-pipelines",
        "status": "PASS",
        "source_commit": source["git_commit"],
        "source_identity": {
            "git_commit": source["git_commit"],
            "git_dirty": source["git_dirty"],
            "source_fingerprint": source["source_fingerprint"],
        },
        "requirements_covered": ["P6-ACCEPTANCE"],
        "environment": {"pbs_job_id": os.environ.get("PBS_JOBID")},
        "scenario_count": len(results),
        "scenarios": results,
        "errors": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = run(
        args.project_root.resolve(),
        args.output.resolve(),
        args.runs_root.resolve(),
        args.temp_root.resolve(),
    )
    _atomic_json(args.output.resolve(), payload)
    print(payload["status"])


if __name__ == "__main__":
    main()
