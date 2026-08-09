#!/usr/bin/env python3
"""Supervise the formal P6 dynamic nine-allocation failure soak."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"{(completed.stdout + completed.stderr)[-8000:]}"
        )
    output = completed.stdout.strip()
    if not output:
        raise RuntimeError(f"command returned no output: {command!r}")
    return output


def _descriptor(init_output: str) -> dict[str, Any]:
    payload = json.loads(init_output)
    descriptor = payload.get("descriptor")
    if not isinstance(descriptor, dict) or descriptor.get("mode") != "full_ha_dynamic":
        raise RuntimeError("initializer did not return a dynamic descriptor")
    if descriptor.get("git_dirty") is not False:
        raise RuntimeError("formal dynamic run requires a clean source target")
    return descriptor


def _qsub_learners(
    *,
    project_root: Path,
    shared_root: Path,
    descriptor: dict[str, Any],
    duplicate_result: Path,
) -> list[str]:
    base = (
        f"FS_DILOCO_SHARED_ROOT={shared_root},"
        f"FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256={descriptor['descriptor_sha256']},"
        f"PROJECT_ROOT={project_root}"
    )
    job_ids: list[str] = []
    for slot in range(8):
        variables = f"{base},BOOTSTRAP_SLOT={slot}"
        if slot == 0:
            variables += ",FS_DILOCO_TEST_TERMINATE_AFTER_ADMISSION_SECONDS=2"
        if slot == 1:
            variables += (
                ",FS_DILOCO_TEST_SPAWN_DUPLICATE_PRETORCH=1,"
                f"FS_DILOCO_TEST_DUPLICATE_RESULT_PATH={duplicate_result}"
            )
        output = _run(
            [
                "qsub",
                "-q",
                "regular-g",
                "-l",
                "walltime=00:20:00",
                "-v",
                variables,
                str(project_root / "scripts/miyabi/run_dynamic_learner.pbs"),
            ]
        )
        job_ids.append(output.splitlines()[-1].strip())
    return job_ids


def _query_rows(database: Path, query: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


def _process_state(pid: int) -> str | None:
    try:
        content = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in content.splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    return "unknown"


class TopologyRecorder:
    def __init__(self, *, candidate_job_id: str) -> None:
        from fs_diloco.runtime.pbs_scheduler import PBSScheduler

        self.scheduler = PBSScheduler()
        self.candidate_job_id = candidate_job_id
        self.maximum = 1
        self.events: list[dict[str, Any]] = []
        self._last_signature: tuple[str, ...] | None = None

    def sample(self, job_ids: set[str]) -> None:
        live: list[str] = []
        states: dict[str, str] = {}
        for job_id in sorted(job_ids):
            observation = self.scheduler.query(job_id)
            if observation.classification == "query_failed":
                raise RuntimeError(f"qstat failed while sampling topology: {job_id}")
            states[job_id] = observation.classification
            if observation.classification not in {"finished", "no_record"}:
                live.append(job_id)
        allocations = 1 + len(live)
        if allocations > 9:
            raise RuntimeError(f"dynamic topology exceeded nine allocations: {allocations}")
        self.maximum = max(self.maximum, allocations)
        signature = tuple(live)
        if signature != self._last_signature:
            self.events.append(
                {
                    "observed_at": time.time(),
                    "candidate_job_id": self.candidate_job_id,
                    "live_learner_job_ids": live,
                    "live_allocations": allocations,
                    "scheduler_states": states,
                }
            )
            self._last_signature = signature

    def payload(self, known_job_ids: set[str]) -> dict[str, Any]:
        return {
            "format_version": 1,
            "candidate_job_id": self.candidate_job_id,
            "known_learner_job_ids": sorted(known_job_ids),
            "maximum_live_allocations": self.maximum,
            "events": self.events,
        }


def supervise(
    *,
    project_root: Path,
    config: Path,
    run_id: str,
    shared_root: Path,
    log_root: Path,
    evidence_output: Path,
    timeout_seconds: float,
) -> None:
    python = project_root / ".venv/bin/python"
    log_root.mkdir(parents=True, exist_ok=False)
    init_output = _run(
        [
            str(python),
            "-m",
            "fs_diloco.tools.init_run",
            "--config",
            str(config),
            "--run-id",
            run_id,
            "--shared-root",
            str(shared_root),
            "--project-root",
            str(project_root),
        ]
    )
    (log_root / "init_run.json").write_text(init_output + "\n", encoding="utf-8")
    descriptor = _descriptor(init_output)
    resolved_config = Path(str(descriptor["resolved_config_path"]))
    pause_marker = shared_root / "metrics/p6_candidate_pause.json"
    duplicate_result = shared_root / "metrics/p6_duplicate_result.json"
    timeline_path = shared_root / "metrics/p6_topology_timeline.json"
    initial_job_ids = _qsub_learners(
        project_root=project_root,
        shared_root=shared_root,
        descriptor=descriptor,
        duplicate_result=duplicate_result,
    )
    (log_root / "bootstrap_jobs.json").write_text(
        json.dumps({"job_ids": initial_job_ids}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    base_environment = os.environ.copy()
    base_environment.update(
        FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256=str(descriptor["descriptor_sha256"]),
        FS_DILOCO_EXPECTED_GIT_COMMIT=str(descriptor["git_commit"]),
        FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT=str(descriptor["source_fingerprint"]),
        FS_DILOCO_GIT_COMMIT=str(descriptor["git_commit"]),
        FS_DILOCO_SOURCE_FINGERPRINT=str(descriptor["source_fingerprint"]),
        FS_DILOCO_GIT_DIRTY="0",
        FS_DILOCO_REQUIRE_SOURCE_IDENTITY="1",
        CUDA_VISIBLE_DEVICES="",
    )
    old_environment = dict(base_environment)
    old_environment.update(
        FS_DILOCO_TEST_PAUSE_AFTER_COMMITTED_VERSION="5",
        FS_DILOCO_TEST_PAUSE_MARKER_PATH=str(pause_marker),
    )
    old_log = (log_root / "syncer_old.log").open("w", encoding="utf-8")
    successor_log = (log_root / "syncer_successor.log").open("w", encoding="utf-8")
    command = [
        str(python),
        "-m",
        "fs_diloco.syncer",
        "--config",
        str(resolved_config),
        "--shared-root",
        str(shared_root),
    ]
    old = subprocess.Popen(command, env=old_environment, stdout=old_log, stderr=subprocess.STDOUT)
    successor: subprocess.Popen[Any] | None = None
    known_job_ids = set(initial_job_ids)
    recorder = TopologyRecorder(candidate_job_id=os.environ["PBS_JOBID"])
    deadline = time.monotonic() + timeout_seconds
    database = shared_root / "control/syncer_metadata.sqlite3"
    try:
        while not pause_marker.is_file():
            if old.poll() is not None:
                raise RuntimeError(f"old candidate exited before pause: {old.returncode}")
            if time.monotonic() >= deadline:
                raise TimeoutError("old candidate did not reach the outside-transaction pause")
            recorder.sample(known_job_ids)
            time.sleep(0.5)
        while _process_state(old.pid) != "T":
            if time.monotonic() >= deadline:
                raise TimeoutError("old candidate marker appeared but process was not stopped")
            time.sleep(0.05)
        successor = subprocess.Popen(
            command,
            env=base_environment,
            stdout=successor_log,
            stderr=subprocess.STDOUT,
        )
        successor_epoch: int | None = None
        while successor_epoch is None:
            if successor.poll() is not None:
                raise RuntimeError(f"successor exited before takeover: {successor.returncode}")
            if time.monotonic() >= deadline:
                raise TimeoutError("successor did not acquire and commit after the pause")
            launch_rows = _query_rows(
                database,
                "SELECT pbs_job_id FROM launch_requests WHERE role='replacement' "
                "AND pbs_job_id IS NOT NULL",
            )
            known_job_ids.update(str(row["pbs_job_id"]) for row in launch_rows)
            recorder.sample(known_job_ids)
            epoch_rows = _query_rows(
                database,
                "SELECT epoch FROM syncer_epochs ORDER BY epoch",
            )
            current_rows = _query_rows(
                database,
                "SELECT version, committed_by_epoch FROM global_versions "
                "ORDER BY version DESC LIMIT 1",
            )
            if len(epoch_rows) >= 2 and current_rows:
                expected_epoch = int(epoch_rows[-1]["epoch"])
                current = current_rows[0]
                if (
                    int(current["committed_by_epoch"]) == expected_epoch
                    and int(current["version"]) >= 6
                ):
                    successor_epoch = expected_epoch
                    break
            time.sleep(0.5)
        os.kill(old.pid, signal.SIGCONT)
        try:
            old_status = old.wait(timeout=60.0)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("resumed stale candidate did not terminate") from exc
        if old_status == 0:
            raise RuntimeError("resumed stale candidate exited successfully instead of fencing")
        while successor.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("dynamic successor did not finalize the 120-version run")
            launch_rows = _query_rows(
                database,
                "SELECT pbs_job_id FROM launch_requests WHERE pbs_job_id IS NOT NULL",
            )
            known_job_ids.update(str(row["pbs_job_id"]) for row in launch_rows)
            recorder.sample(known_job_ids)
            time.sleep(1.0)
        if successor.returncode != 0:
            raise RuntimeError(f"dynamic successor failed: {successor.returncode}")
        for _ in range(120):
            recorder.sample(known_job_ids)
            live = recorder.events[-1]["live_learner_job_ids"] if recorder.events else []
            if not live:
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("learner jobs did not reach terminal scheduler state")
        timeline = recorder.payload(known_job_ids)
        _atomic_json(timeline_path, timeline)
        _run(
            [
                str(python),
                str(project_root / "scripts/miyabi/plan03_p6_validate_run.py"),
                "--project-root",
                str(project_root),
                "--run-root",
                str(shared_root),
                "--mode",
                "dynamic",
                "--minimum-versions",
                "120",
                "--minimum-inner-steps",
                "60",
                "--expected-contributors",
                "8",
                "--pause-marker",
                str(pause_marker),
                "--duplicate-result",
                str(duplicate_result),
                "--topology-timeline",
                str(timeline_path),
                "--output",
                str(evidence_output),
            ]
        )
    finally:
        if old.poll() is None:
            os.kill(old.pid, signal.SIGCONT)
            old.terminate()
            try:
                old.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                old.kill()
                old.wait(timeout=10.0)
        if successor is not None and successor.poll() is None:
            successor.terminate()
            try:
                successor.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                successor.kill()
                successor.wait(timeout=10.0)
        old_log.close()
        successor_log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1500.0)
    args = parser.parse_args()
    supervise(
        project_root=args.project_root.resolve(),
        config=args.config.resolve(),
        run_id=args.run_id,
        shared_root=args.shared_root.resolve(),
        log_root=args.log_root.resolve(),
        evidence_output=args.evidence_output.resolve(),
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    main()
