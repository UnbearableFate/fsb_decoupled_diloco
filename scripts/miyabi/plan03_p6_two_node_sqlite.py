#!/usr/bin/env python3
"""Exercise Plan 03 outside/inside-transaction SIGSTOP semantics on two hosts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority_v4
from fs_diloco.storage.leader_lease import LeaseUnavailableError, StaleLeaderTokenError

if __package__:
    from .capture_source_identity import capture as capture_source_identity
else:
    from capture_source_identity import capture as capture_source_identity


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
REQUIREMENTS_COVERED = ("AUTH-11", "P6-ACCEPTANCE")
LEASE_SECONDS = 2.0
CLOCK_SKEW_SECONDS = 0.1


def _identity(name: str) -> AuthorityIdentity:
    return AuthorityIdentity(name, "p6-two-node-source", hashlib.sha256(name.encode()).hexdigest())


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _wait(path: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.05)
    return json.loads(path.read_text(encoding="utf-8"))


def _authority(database: Path, name: str, *, busy_timeout_ms: int = 30_000) -> LeaderAuthority:
    return LeaderAuthority(
        database,
        _identity(name),
        StaticMembershipScope(("learner-0",)),
        lease_duration_seconds=LEASE_SECONDS,
        max_clock_skew_seconds=CLOCK_SKEW_SECONDS,
        busy_timeout_ms=busy_timeout_ms,
    )


def outside_old(root: Path) -> None:
    database = root / "authority.sqlite3"
    with _authority(database, "outside") as authority:
        token = authority.acquire_leader(owner_id="outside-old", hostname=os.uname().nodename)
        leader = authority.open_leader(token)
        authority.assert_outside_transaction()
        _atomic_json(
            root / "old-paused.json",
            {
                "pid": os.getpid(),
                "hostname": os.uname().nodename,
                "epoch": token.epoch,
                "sqlite_transaction_active": False,
            },
        )
        os.kill(os.getpid(), signal.SIGSTOP)
        stale_commits = 0
        stale_error = None
        try:
            leader.bind_or_replace_static_attempt(
                command_id="outside-old-after-resume",
                learner_id="learner-0",
                logical_launch_id="outside-old-launch",
                attempt_id="outside-old-attempt",
            )
            stale_commits += 1
        except StaleLeaderTokenError as exc:
            stale_error = type(exc).__name__
        _atomic_json(
            root / "old-resumed.json",
            {"stale_successful_commits": stale_commits, "error": stale_error},
        )


def outside_successor(root: Path) -> None:
    _wait(root / "old-paused.json", timeout=30.0)
    started = time.monotonic()
    with _authority(root / "authority.sqlite3", "outside") as authority:
        deadline = time.monotonic() + 30.0
        while True:
            try:
                token = authority.acquire_leader(
                    owner_id="outside-successor", hostname=os.uname().nodename
                )
                break
            except LeaseUnavailableError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="outside-successor-bind",
            learner_id="learner-0",
            logical_launch_id="outside-successor-launch",
            attempt_id="outside-successor-attempt",
        )
        _atomic_json(
            root / "successor.json",
            {
                "pid": os.getpid(),
                "hostname": os.uname().nodename,
                "epoch": token.epoch,
                "wait_seconds": time.monotonic() - started,
                "binding_generation": binding.binding_generation,
            },
        )


def inside_old(root: Path) -> None:
    connection = sqlite3.connect(root / "authority.sqlite3", timeout=30.0)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE controller_state SET reason='uncommitted-inside-stop' WHERE singleton=1"
    )
    _atomic_json(
        root / "old-paused.json",
        {
            "pid": os.getpid(),
            "hostname": os.uname().nodename,
            "sqlite_transaction_active": connection.in_transaction,
        },
    )
    os.kill(os.getpid(), signal.SIGSTOP)
    raise RuntimeError("inside-transaction old writer unexpectedly resumed")


def inside_successor(root: Path) -> None:
    _wait(root / "old-paused.json", timeout=30.0)
    _atomic_json(root / "successor-attempting.json", {"started_at": time.time()})
    started = time.monotonic()
    with _authority(root / "authority.sqlite3", "inside", busy_timeout_ms=30_000) as authority:
        token = authority.acquire_leader(owner_id="inside-successor", hostname=os.uname().nodename)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="inside-successor-bind",
            learner_id="learner-0",
            logical_launch_id="inside-successor-launch",
            attempt_id="inside-successor-attempt",
        )
        _atomic_json(
            root / "successor.json",
            {
                "pid": os.getpid(),
                "hostname": os.uname().nodename,
                "epoch": token.epoch,
                "wait_seconds": time.monotonic() - started,
                "binding_generation": binding.binding_generation,
            },
        )


def _spawn_role(
    *,
    project_root: Path,
    host: str,
    role: str,
    root: Path,
    log_path: Path,
) -> tuple[subprocess.Popen[Any], Any]:
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            "mpirun",
            "--bind-to",
            "none",
            "-np",
            "1",
            "--host",
            host,
            "/usr/bin/env",
            f"PYTHONPATH={project_root}",
            str(project_root / ".venv/bin/python"),
            str(project_root / "scripts/miyabi/plan03_p6_two_node_sqlite.py"),
            "--role",
            role,
            "--scenario-root",
            str(root),
        ],
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return process, handle


def _remote_signal(host: str, pid: int, value: signal.Signals) -> None:
    completed = subprocess.run(
        [
            "mpirun",
            "--bind-to",
            "none",
            "-np",
            "1",
            "--host",
            host,
            "/bin/kill",
            f"-{value.name.removeprefix('SIG')}",
            str(pid),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"remote signal failed: {completed.stdout}{completed.stderr}")


def _initialize(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    initialize_authority_v4(
        root / "authority.sqlite3",
        _identity(name),
        StaticMembershipScope(("learner-0",)),
    )


def _integrity(root: Path) -> dict[str, Any]:
    connection = sqlite3.connect(root / "authority.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        bindings = [
            dict(row) for row in connection.execute("SELECT * FROM static_contributor_bindings")
        ]
        epochs = [
            dict(row) for row in connection.execute("SELECT * FROM syncer_epochs ORDER BY epoch")
        ]
        reason = connection.execute(
            "SELECT reason FROM controller_state WHERE singleton=1"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "integrity": integrity,
        "bindings": bindings,
        "epochs": epochs,
        "controller_reason": reason,
    }


def orchestrate(
    project_root: Path, shared_root: Path, output: Path, hosts: tuple[str, str]
) -> None:
    results: dict[str, Any] = {}
    shared_root.mkdir(parents=True, exist_ok=False)
    supplemental_nodeids = (
        "tests/runtime/test_p4_mandatory_runtime.py::test_epoch_control_ignores_polluted_fixed_cache_and_repairs_it",
        "tests/runtime/test_p4_mandatory_runtime.py::test_stale_leader_cannot_use_exact_binding_shortcut_to_remove_hot_request",
        "tests/runtime/test_pbs_scheduler.py",
        "tests/storage/test_visibility_v4.py",
    )
    supplemental_log = shared_root / "supplemental-g7.log"
    with supplemental_log.open("w", encoding="utf-8") as handle:
        supplemental = subprocess.run(
            [
                str(project_root / ".venv/bin/python"),
                "-m",
                "pytest",
                "-q",
                *supplemental_nodeids,
            ],
            cwd=project_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if supplemental.returncode != 0:
        raise RuntimeError(f"G7 supplemental contracts failed: {supplemental.returncode}")
    results["supplemental_contracts"] = {
        "nodeids": list(supplemental_nodeids),
        "returncode": supplemental.returncode,
        "log": str(supplemental_log),
    }
    outside = shared_root / "outside-transaction"
    _initialize(outside, "outside")
    old, old_log = _spawn_role(
        project_root=project_root,
        host=hosts[0],
        role="outside-old",
        root=outside,
        log_path=outside / "old.log",
    )
    successor, successor_log = _spawn_role(
        project_root=project_root,
        host=hosts[1],
        role="outside-successor",
        root=outside,
        log_path=outside / "successor.log",
    )
    try:
        paused = _wait(outside / "old-paused.json", timeout=30.0)
        successor_result = _wait(outside / "successor.json", timeout=30.0)
        _remote_signal(str(paused["hostname"]), int(paused["pid"]), signal.SIGCONT)
        resumed = _wait(outside / "old-resumed.json", timeout=20.0)
        if successor.wait(timeout=10.0) != 0 or old.wait(timeout=10.0) != 0:
            raise RuntimeError("outside-transaction role failed")
    finally:
        old_log.close()
        successor_log.close()
    outside_integrity = _integrity(outside)
    if resumed["stale_successful_commits"] != 0 or outside_integrity["integrity"] != ["ok"]:
        raise RuntimeError("outside-transaction takeover violated fencing or integrity")
    results["outside_transaction"] = {
        "paused": paused,
        "successor": successor_result,
        "old_resumed": resumed,
        "authority": outside_integrity,
    }

    inside = shared_root / "inside-transaction"
    _initialize(inside, "inside")
    old, old_log = _spawn_role(
        project_root=project_root,
        host=hosts[0],
        role="inside-old",
        root=inside,
        log_path=inside / "old.log",
    )
    successor, successor_log = _spawn_role(
        project_root=project_root,
        host=hosts[1],
        role="inside-successor",
        root=inside,
        log_path=inside / "successor.log",
    )
    explicit_termination_after = LEASE_SECONDS + CLOCK_SKEW_SECONDS + 1.0
    try:
        paused = _wait(inside / "old-paused.json", timeout=30.0)
        _wait(inside / "successor-attempting.json", timeout=30.0)
        time.sleep(explicit_termination_after)
        if (inside / "successor.json").exists():
            raise RuntimeError("inside-transaction successor bypassed the held SQLite writer lock")
        _remote_signal(str(paused["hostname"]), int(paused["pid"]), signal.SIGKILL)
        old_status = old.wait(timeout=20.0)
        if old_status == 0:
            raise RuntimeError("explicitly terminated inside writer exited successfully")
        successor_result = _wait(inside / "successor.json", timeout=30.0)
        if successor.wait(timeout=10.0) != 0:
            raise RuntimeError("inside-transaction successor failed after lock release")
    finally:
        old_log.close()
        successor_log.close()
    inside_integrity = _integrity(inside)
    if inside_integrity["integrity"] != ["ok"] or inside_integrity["controller_reason"] is not None:
        raise RuntimeError("inside-transaction rollback/integrity check failed")
    results["inside_transaction"] = {
        "paused": paused,
        "successor": successor_result,
        "explicit_termination_after_seconds": explicit_termination_after,
        "old_mpirun_returncode": old_status,
        "authority": inside_integrity,
    }
    source_identity = capture_source_identity(project_root)
    source_commit = str(source_identity["git_commit"])
    dirty = bool(source_identity["git_dirty"])
    errors: list[str] = []
    if dirty:
        errors.append("formal source target is dirty")
    payload = {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase": "P6-acceptance-final-review",
        "experiment_id": "p6-g7-two-node-sqlite-stop",
        "status": "PASS" if not errors else "BLOCKED",
        "source_commit": source_commit,
        "source_identity": source_identity,
        "requirements_covered": list(REQUIREMENTS_COVERED),
        "pbs_job_id": os.environ.get("PBS_JOBID"),
        "hosts": list(hosts),
        "results": results,
        "errors": errors,
    }
    _atomic_json(output, payload)
    if errors:
        raise RuntimeError(str(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        choices=(
            "orchestrate",
            "outside-old",
            "outside-successor",
            "inside-old",
            "inside-successor",
        ),
        default="orchestrate",
    )
    parser.add_argument("--scenario-root", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--shared-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hosts", nargs=2)
    args = parser.parse_args()
    if args.role == "orchestrate":
        if (
            args.project_root is None
            or args.shared_root is None
            or args.output is None
            or args.hosts is None
        ):
            parser.error("orchestrate requires --project-root, --shared-root, --output and --hosts")
        orchestrate(
            args.project_root.resolve(),
            args.shared_root.resolve(),
            args.output.resolve(),
            (args.hosts[0], args.hosts[1]),
        )
        return
    if args.scenario_root is None:
        parser.error("a role process requires --scenario-root")
    roles = {
        "outside-old": outside_old,
        "outside-successor": outside_successor,
        "inside-old": inside_old,
        "inside-successor": inside_successor,
    }
    roles[args.role](args.scenario_root.resolve())


if __name__ == "__main__":
    main()
