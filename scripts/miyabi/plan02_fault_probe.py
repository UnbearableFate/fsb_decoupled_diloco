#!/usr/bin/env python3
"""Run Plan 02 writer-lock, old-cache-writer, and source-pinning probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


FIXED_ARTIFACTS = ("latest.json", "stop.json", "summary.json")
CANONICAL_PATTERNS = {
    "latest": "e*_*/latest/head.json",
    "stop": "e*_*/terminal/stop_g*.json",
    "summary": "e*_*/terminal/summary_g*.json",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(path, _canonical_json_bytes(payload))


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _wait_for(predicate: Any, *, timeout_seconds: float, description: str) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {description}")
        time.sleep(0.01)


def _wait_for_object(path: Path, *, timeout_seconds: float, description: str) -> dict[str, Any]:
    _wait_for(path.is_file, timeout_seconds=timeout_seconds, description=description)
    return _read_object(path)


def _process_state(pid: int) -> str | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in status.splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    return None


def _initialize_lock_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """
            CREATE TABLE writer_lock_probe (
                event_id TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()
        if journal_mode.lower() != "delete":
            raise RuntimeError(f"unexpected journal mode: {journal_mode}")
    finally:
        conn.close()


def _hold_writer_lock(db_path: Path, ready_path: Path) -> None:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO writer_lock_probe(event_id, value) VALUES ('tentative', 'uncommitted')"
    )
    _atomic_write_json(
        ready_path,
        {"pid": os.getpid(), "transaction": "BEGIN IMMEDIATE", "tentative_row": True},
    )
    while True:
        signal.pause()


def writer_lock_probe(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "writer_lock.sqlite3"
    ready_path = work_dir / "holder_ready.json"
    _initialize_lock_db(db_path)
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_hold-writer-lock",
            "--db",
            str(db_path),
            "--ready",
            str(ready_path),
        ]
    )
    try:
        _wait_for(ready_path.is_file, timeout_seconds=10.0, description="lock holder readiness")
        os.kill(child.pid, signal.SIGSTOP)
        _wait_for(
            lambda: _process_state(child.pid) == "T",
            timeout_seconds=5.0,
            description="lock holder SIGSTOP state",
        )
        contender_started = time.monotonic()
        contender_error: str | None = None
        conn = sqlite3.connect(db_path, timeout=0.25)
        conn.execute("PRAGMA busy_timeout=250")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            contender_error = str(exc)
            conn.rollback()
        else:
            conn.rollback()
        finally:
            conn.close()
        contender_elapsed = time.monotonic() - contender_started
        contender_blocked = contender_error is not None and any(
            token in contender_error.lower() for token in ("locked", "busy")
        )
        if not contender_blocked:
            raise RuntimeError(
                f"contender unexpectedly acquired writer lock: error={contender_error!r}"
            )

        reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            visible_before_kill = int(
                reader.execute("SELECT COUNT(*) FROM writer_lock_probe").fetchone()[0]
            )
        finally:
            reader.close()
        if visible_before_kill != 0:
            raise RuntimeError("uncommitted writer row became visible")

        os.kill(child.pid, signal.SIGKILL)
        child_status = child.wait(timeout=10.0)
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO writer_lock_probe(event_id, value) VALUES ('successor', 'committed')"
            )
            conn.commit()
            rows = [
                list(row)
                for row in conn.execute(
                    "SELECT event_id, value FROM writer_lock_probe ORDER BY event_id"
                )
            ]
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            pragmas = {
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
            }
        finally:
            conn.close()
        if rows != [["successor", "committed"]] or integrity != ["ok"]:
            raise RuntimeError(f"post-kill verification failed: rows={rows}, integrity={integrity}")
        successor_acquired = rows == [["successor", "committed"]]
        uncommitted_rolled_back = visible_before_kill == 0 and successor_acquired
        holder_killed = child_status == -signal.SIGKILL
        return {
            "status": "PASS",
            "holder_pid": child.pid,
            "holder_stopped_state": "T",
            "contender_error": contender_error,
            "contender_elapsed_seconds": contender_elapsed,
            "visible_rows_before_kill": visible_before_kill,
            "holder_exit_status": child_status,
            "post_kill_rows": rows,
            "integrity_check": integrity,
            "pragmas": pragmas,
            "availability_boundary": {
                "paused_writer_transaction_blocks_takeover": contender_blocked,
                "killing_holder_releases_lock": holder_killed and successor_acquired,
                "uncommitted_state_rolled_back": uncommitted_rolled_back,
            },
        }
    finally:
        if child.poll() is None:
            if _process_state(child.pid) == "T":
                os.kill(child.pid, signal.SIGCONT)
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10.0)


def cross_node_writer_lock_role(work_dir: Path, role: str) -> dict[str, Any]:
    if role not in {"holder", "contender"}:
        raise ValueError(f"unknown cross-node writer-lock role: {role}")
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "writer_lock.sqlite3"
    child_ready_path = work_dir / "holder_child_ready.json"
    holder_ready_path = work_dir / "holder_stopped.json"
    kill_request_path = work_dir / "kill_holder.json"
    holder_killed_path = work_dir / "holder_killed.json"
    hostname = os.uname().nodename

    if role == "holder":
        _initialize_lock_db(db_path)
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_hold-writer-lock",
                "--db",
                str(db_path),
                "--ready",
                str(child_ready_path),
            ]
        )
        try:
            _wait_for_object(
                child_ready_path,
                timeout_seconds=15.0,
                description="cross-node lock holder readiness",
            )
            os.kill(child.pid, signal.SIGSTOP)
            _wait_for(
                lambda: _process_state(child.pid) == "T",
                timeout_seconds=5.0,
                description="cross-node lock holder SIGSTOP state",
            )
            _atomic_write_json(
                holder_ready_path,
                {
                    "hostname": hostname,
                    "holder_pid": child.pid,
                    "holder_stopped_state": "T",
                },
            )
            _wait_for_object(
                kill_request_path,
                timeout_seconds=60.0,
                description="cross-node holder kill request",
            )
            os.kill(child.pid, signal.SIGKILL)
            child_status = child.wait(timeout=10.0)
            killed = {
                "hostname": hostname,
                "holder_pid": child.pid,
                "holder_stopped_state": "T",
                "holder_exit_status": child_status,
            }
            _atomic_write_json(holder_killed_path, killed)
            if child_status != -signal.SIGKILL:
                raise RuntimeError(f"cross-node holder did not exit via SIGKILL: {child_status}")
            return {"status": "PASS", "role": role, **killed}
        finally:
            if child.poll() is None:
                if _process_state(child.pid) == "T":
                    os.kill(child.pid, signal.SIGCONT)
                os.kill(child.pid, signal.SIGKILL)
                child.wait(timeout=10.0)

    holder = _wait_for_object(
        holder_ready_path,
        timeout_seconds=30.0,
        description="remote stopped writer",
    )
    if holder.get("hostname") == hostname:
        raise RuntimeError("cross-node contender was placed on the holder host")
    contender_started = time.monotonic()
    contender_error: str | None = None
    conn = sqlite3.connect(db_path, timeout=0.25)
    conn.execute("PRAGMA busy_timeout=250")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        contender_error = str(exc)
        conn.rollback()
    else:
        conn.rollback()
    finally:
        conn.close()
    contender_elapsed = time.monotonic() - contender_started
    contender_blocked = contender_error is not None and any(
        token in contender_error.lower() for token in ("locked", "busy")
    )
    if not contender_blocked:
        raise RuntimeError(
            f"remote contender unexpectedly acquired writer lock: {contender_error!r}"
        )

    reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        visible_before_kill = int(
            reader.execute("SELECT COUNT(*) FROM writer_lock_probe").fetchone()[0]
        )
    finally:
        reader.close()
    if visible_before_kill != 0:
        raise RuntimeError("remote contender observed uncommitted writer state")

    _atomic_write_json(
        kill_request_path,
        {"requested_by_hostname": hostname, "contender_blocked": contender_blocked},
    )
    killed = _wait_for_object(
        holder_killed_path,
        timeout_seconds=30.0,
        description="remote writer termination",
    )
    if int(killed.get("holder_exit_status", 0)) != -signal.SIGKILL:
        raise RuntimeError(f"remote holder termination was not SIGKILL: {killed}")

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO writer_lock_probe(event_id, value) VALUES ('successor', 'committed')"
        )
        conn.commit()
        rows = [
            list(row)
            for row in conn.execute(
                "SELECT event_id, value FROM writer_lock_probe ORDER BY event_id"
            )
        ]
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        pragmas = {
            "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
        }
    finally:
        conn.close()
    if rows != [["successor", "committed"]] or integrity != ["ok"]:
        raise RuntimeError(
            f"cross-node post-kill verification failed: rows={rows}, integrity={integrity}"
        )
    return {
        "status": "PASS",
        "role": role,
        "holder_hostname": holder["hostname"],
        "contender_hostname": hostname,
        "successor_hostname": hostname,
        "contender_error": contender_error,
        "contender_elapsed_seconds": contender_elapsed,
        "visible_rows_before_kill": visible_before_kill,
        "post_kill_rows": rows,
        "integrity_check": integrity,
        "pragmas": pragmas,
    }


def combine_writer_lock_evidence(
    single_node_path: Path, holder_path: Path, contender_path: Path
) -> dict[str, Any]:
    single_node = _read_object(single_node_path)
    holder = _read_object(holder_path)
    contender = _read_object(contender_path)
    hostnames = sorted(
        {
            str(holder.get("hostname", "")),
            str(contender.get("contender_hostname", "")),
        }
        - {""}
    )
    contender_error = str(contender.get("contender_error", ""))
    contender_blocked = any(token in contender_error.lower() for token in ("locked", "busy"))
    successor_acquired = contender.get("post_kill_rows") == [["successor", "committed"]]
    holder_killed = int(holder.get("holder_exit_status", 0)) == -signal.SIGKILL
    cross_node = {
        **contender,
        "holder_pid": holder.get("holder_pid"),
        "holder_stopped_state": holder.get("holder_stopped_state"),
        "holder_exit_status": holder.get("holder_exit_status"),
        "hostnames": hostnames,
        "host_count": len(hostnames),
        "availability_boundary": {
            "paused_writer_transaction_blocks_takeover": contender_blocked,
            "killing_holder_releases_lock": holder_killed and successor_acquired,
            "uncommitted_state_rolled_back": (
                contender.get("visible_rows_before_kill") == 0 and successor_acquired
            ),
        },
    }
    passed = (
        single_node.get("status") == "PASS"
        and holder.get("status") == "PASS"
        and contender.get("status") == "PASS"
        and contender.get("holder_hostname") == holder.get("hostname")
        and holder.get("holder_stopped_state") == "T"
        and len(hostnames) == 2
        and all(cross_node["availability_boundary"].values())
        and cross_node.get("integrity_check") == ["ok"]
    )
    if not passed:
        raise RuntimeError(
            "writer-lock evidence did not prove both single-node and cross-node boundaries"
        )
    return {
        **cross_node,
        "status": "PASS",
        "single_node": single_node,
        "cross_node": cross_node,
    }


def _artifact_payload(kind: str, epoch: int, owner_id: str) -> dict[str, Any]:
    generation = 2 if kind == "latest" else 1
    return {
        "artifact_kind": kind,
        "logical_generation": generation,
        "published_by_epoch": epoch,
        "published_by_owner_id": owner_id,
        "version": epoch,
    }


def _epoch_artifact_paths(control: Path, epoch: int, owner_short: str) -> dict[str, Path]:
    epoch_root = control / "syncer_epochs" / f"e{epoch:06d}_{owner_short}"
    return {
        "latest": epoch_root / "latest" / "head.json",
        "stop": epoch_root / "terminal" / "stop_g000001.json",
        "summary": epoch_root / "terminal" / "summary_g000001.json",
    }


def _publish_epoch(control: Path, epoch: int, owner_id: str) -> dict[str, dict[str, Any]]:
    owner_short = owner_id.split("-")[0]
    paths = _epoch_artifact_paths(control, epoch, owner_short)
    payloads: dict[str, dict[str, Any]] = {}
    for kind, path in paths.items():
        payload = _artifact_payload(kind, epoch, owner_id)
        _atomic_write_json(path, payload)
        payloads[kind] = payload
    return payloads


def _stage_and_pause_old_cache_writer(
    control: Path,
    old_payload_dir: Path,
    lease_path: Path,
    ready_path: Path,
) -> None:
    lease = _read_object(lease_path)
    if lease.get("epoch") != 1 or lease.get("owner_id") != "old-owner":
        raise RuntimeError(f"old writer lease check failed: {lease}")
    staged: dict[str, str] = {}
    for filename in FIXED_ARTIFACTS:
        source = old_payload_dir / filename
        destination = control / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.old-writer.", dir=destination.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        staged[filename] = temp_name
    _atomic_write_json(
        ready_path,
        {"pid": os.getpid(), "lease_checked_epoch": 1, "staged": staged},
    )
    os.kill(os.getpid(), signal.SIGSTOP)
    for filename, temp_name in staged.items():
        os.replace(temp_name, control / filename)


def _select_highest_epoch(control: Path, kind: str) -> dict[str, Any]:
    if kind not in CANONICAL_PATTERNS:
        raise ValueError(f"unknown artifact kind: {kind}")
    candidates: list[dict[str, Any]] = []
    for path in (control / "syncer_epochs").glob(CANONICAL_PATTERNS[kind]):
        payload = _read_object(path)
        if payload.get("artifact_kind") != kind:
            raise RuntimeError(f"canonical artifact kind mismatch at {path}: {payload}")
        payload["relative_path"] = str(path.relative_to(control.parent))
        candidates.append(payload)
    if not candidates:
        raise RuntimeError("canonical epoch discovery unexpectedly returned no entries")
    return max(candidates, key=lambda payload: int(payload["published_by_epoch"]))


def old_cache_writer_probe(work_dir: Path) -> dict[str, Any]:
    control = work_dir / "run" / "control"
    lease_path = work_dir / "leader.json"
    old_payload_dir = work_dir / "old_fixed_payloads"
    ready_path = work_dir / "old_writer_ready.json"
    old_payloads = _publish_epoch(control, 1, "old-owner")
    for kind, payload in old_payloads.items():
        _atomic_write_json(old_payload_dir / f"{kind}.json", payload)
        _atomic_write_json(control / f"{kind}.json", payload)
    _atomic_write_json(lease_path, {"epoch": 1, "owner_id": "old-owner"})

    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_old-cache-writer",
            "--control",
            str(control),
            "--old-payload-dir",
            str(old_payload_dir),
            "--lease",
            str(lease_path),
            "--ready",
            str(ready_path),
        ]
    )
    try:
        _wait_for(ready_path.is_file, timeout_seconds=10.0, description="old cache writer pause")
        _wait_for(
            lambda: _process_state(child.pid) == "T",
            timeout_seconds=5.0,
            description="old cache writer SIGSTOP state",
        )
        new_payloads = _publish_epoch(control, 2, "new-owner")
        _atomic_write_json(lease_path, {"epoch": 2, "owner_id": "new-owner"})
        for kind, payload in new_payloads.items():
            _atomic_write_json(control / f"{kind}.json", payload)
        os.kill(child.pid, signal.SIGCONT)
        child_status = child.wait(timeout=10.0)
        if child_status != 0:
            raise RuntimeError(f"old cache writer failed after SIGCONT: {child_status}")

        polluted = {kind: _read_object(control / f"{kind}.json") for kind in old_payloads}
        pollution_detected = all(
            int(payload["published_by_epoch"]) == 1 for payload in polluted.values()
        )
        selected = {kind: _select_highest_epoch(control, kind) for kind in CANONICAL_PATTERNS}
        selected_current = all(
            int(payload["published_by_epoch"]) == 2 and payload["artifact_kind"] == kind
            for kind, payload in selected.items()
        )
        business_state_failed = not selected_current
        if not pollution_detected or business_state_failed:
            raise RuntimeError(
                f"old cache counterexample failed: pollution={polluted}, selected={selected}"
            )
        for kind, payload in new_payloads.items():
            _atomic_write_json(control / f"{kind}.json", payload)
        repaired = {kind: _read_object(control / f"{kind}.json") for kind in new_payloads}
        if not all(int(payload["published_by_epoch"]) == 2 for payload in repaired.values()):
            raise RuntimeError(f"cache repair failed: {repaired}")
        return {
            "status": "PASS",
            "old_writer_pid": child.pid,
            "old_writer_exit_status": child_status,
            "counterexample_reproduced": pollution_detected,
            "polluted_cache_epochs": {
                kind: int(payload["published_by_epoch"]) for kind, payload in polluted.items()
            },
            "selected_canonical": selected,
            "business_state_failed": business_state_failed,
            "cache_pollution_reported": pollution_detected,
            "repair_epochs": {
                kind: int(payload["published_by_epoch"]) for kind, payload in repaired.items()
            },
            "canonical_discovery_counts": {
                kind: len(list((control / "syncer_epochs").glob(pattern)))
                for kind, pattern in CANONICAL_PATTERNS.items()
            },
        }
    finally:
        if child.poll() is None:
            if _process_state(child.pid) == "T":
                os.kill(child.pid, signal.SIGCONT)
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10.0)


def _business_db_snapshot(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("syncer_leader", "learner_instances", "global_versions")
        }
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()
    return {"sha256": _sha256_file(path), "counts": counts, "integrity_check": integrity}


def _initialize_business_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE syncer_leader(epoch INTEGER, owner_id TEXT);
            CREATE TABLE learner_instances(instance_id TEXT);
            CREATE TABLE global_versions(version INTEGER);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _source_runtime_stub(*, project_root: Path, db_path: Path, case_name: str) -> dict[str, Any]:
    sys.path.insert(0, str(project_root.resolve()))
    import fs_diloco  # noqa: F401

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO syncer_leader(epoch, owner_id) VALUES (?, ?)",
            (1, case_name),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "runtime_started": True,
        "fs_diloco_imported": "fs_diloco" in sys.modules,
        "business_writes": 1,
    }


def _write_descriptor_and_marker(
    case_dir: Path,
    *,
    source: dict[str, Any],
    config_path: Path,
    run_id: str = "plan02-source-probe",
    protocol_version: int = 1,
    schema_version: int = 2,
) -> tuple[Path, Path]:
    descriptor_path = case_dir / "run_descriptor.json"
    marker_path = case_dir / "bootstrap_complete.json"
    descriptor = {
        "descriptor_format_version": 1,
        "run_id": run_id,
        "git_commit": source["git_commit"],
        "git_dirty": source["git_dirty"],
        "source_fingerprint": source["source_fingerprint"],
        "resolved_config_path": str(config_path.resolve()),
        "resolved_config_sha256": _sha256_file(config_path),
        "protocol_version": protocol_version,
        "schema_version": schema_version,
    }
    _atomic_write_json(descriptor_path, descriptor)
    marker = {
        key: descriptor[key]
        for key in (
            "run_id",
            "git_commit",
            "git_dirty",
            "source_fingerprint",
            "protocol_version",
            "schema_version",
        )
    }
    marker.update(
        {
            "descriptor_sha256": _sha256_file(descriptor_path),
            "config_sha256": descriptor["resolved_config_sha256"],
        }
    )
    _atomic_write_json(marker_path, marker)
    return descriptor_path, marker_path


def source_pinning_probe(project_root: Path, work_dir: Path) -> dict[str, Any]:
    capture_script = Path(__file__).with_name("capture_source_identity.py")
    gate_script = Path(__file__).with_name("plan02_source_gate.py")
    identity_path = work_dir / "source_identity.json"
    identity_env = work_dir / "source_identity.env"
    subprocess.run(
        [
            sys.executable,
            str(capture_script),
            "--project-root",
            str(project_root),
            "--output-json",
            str(identity_path),
            "--output-env",
            str(identity_env),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    source = _read_object(identity_path)
    db_path = work_dir / "business.sqlite3"
    _initialize_business_db(db_path)
    db_before = _business_db_snapshot(db_path)
    cases: dict[str, dict[str, Any]] = {}
    for case_name in (
        "matching_identity",
        "commit_mismatch",
        "dirty_fingerprint_mismatch",
        "config_mismatch",
        "descriptor_mismatch",
        "protocol_mismatch",
        "schema_mismatch",
        "run_id_mismatch",
    ):
        case_dir = work_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        config_path = case_dir / "run_config.resolved.yaml"
        config_path.write_text("run:\n  run_id: plan02-source-probe\n", encoding="utf-8")
        case_source = dict(source)
        if case_name == "commit_mismatch":
            case_source["git_commit"] = f"{source['git_commit']}-mismatch"
        elif case_name == "dirty_fingerprint_mismatch":
            case_source["git_dirty"] = not bool(source["git_dirty"])
            case_source["source_fingerprint"] = "sha256:" + "0" * 64
        protocol_version = 2 if case_name == "protocol_mismatch" else 1
        schema_version = 3 if case_name == "schema_mismatch" else 2
        run_id = (
            "plan02-source-probe-other" if case_name == "run_id_mismatch" else "plan02-source-probe"
        )
        descriptor_path, marker_path = _write_descriptor_and_marker(
            case_dir,
            source=case_source,
            config_path=config_path,
            run_id=run_id,
            protocol_version=protocol_version,
            schema_version=schema_version,
        )
        if case_name == "config_mismatch":
            config_path.write_text(
                "run:\n  run_id: plan02-source-probe-mutated\n", encoding="utf-8"
            )
        elif case_name == "descriptor_mismatch":
            with descriptor_path.open("a", encoding="utf-8") as handle:
                handle.write(" \n")
        result_path = case_dir / "gate_result.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(gate_script),
                "--project-root",
                str(project_root),
                "--descriptor",
                str(descriptor_path),
                "--bootstrap-marker",
                str(marker_path),
                "--output-json",
                str(result_path),
                "--expected-run-id",
                "plan02-source-probe",
                "--expected-protocol-version",
                "1",
                "--expected-schema-version",
                "2",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        runtime_result = {
            "runtime_started": False,
            "fs_diloco_imported": False,
            "business_writes": 0,
        }
        if completed.returncode == 0:
            runtime_result_path = case_dir / "runtime_result.json"
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_source-runtime-stub",
                    "--project-root",
                    str(project_root),
                    "--db",
                    str(db_path),
                    "--case-name",
                    case_name,
                    "--output-json",
                    str(runtime_result_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            runtime_result = _read_object(runtime_result_path)
        result = _read_object(result_path)
        result["gate_fs_diloco_imported"] = result.pop("fs_diloco_imported")
        result.update(
            {
                "returncode": completed.returncode,
                **runtime_result,
            }
        )
        cases[case_name] = result
    db_after = _business_db_snapshot(db_path)
    expected_pass = cases["matching_identity"]
    mismatch_cases = {key: value for key, value in cases.items() if key != "matching_identity"}
    if (
        expected_pass["status"] != "PASS"
        or not expected_pass["runtime_started"]
        or expected_pass["fs_diloco_imported"] is not True
        or int(expected_pass["business_writes"]) != 1
        or expected_pass["gate_fs_diloco_imported"] is not False
    ):
        raise RuntimeError(f"matching source gate did not pass: {expected_pass}")
    if any(
        value["status"] != "BLOCKED"
        or value["runtime_started"]
        or value.get("fs_diloco_imported") is not False
        or int(value.get("business_writes", -1)) != 0
        or value.get("gate_fs_diloco_imported") is not False
        for value in mismatch_cases.values()
    ):
        raise RuntimeError(f"source mismatch gate failed closed incorrectly: {mismatch_cases}")
    expected_after_counts = {
        "syncer_leader": 1,
        "learner_instances": 0,
        "global_versions": 0,
    }
    if any(db_before["counts"].values()) or db_after["counts"] != expected_after_counts:
        raise RuntimeError(
            f"guarded runtime writes were incorrect: before={db_before}, after={db_after}"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        matching_actor_business_writes = int(
            conn.execute(
                "SELECT COUNT(*) FROM syncer_leader WHERE owner_id = 'matching_identity'"
            ).fetchone()[0]
        )
        mismatch_actor_business_writes = int(
            conn.execute(
                "SELECT COUNT(*) FROM syncer_leader WHERE owner_id != 'matching_identity'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return {
        "status": "PASS",
        "source_identity": {
            key: source[key] for key in ("git_commit", "git_dirty", "source_fingerprint")
        },
        "cases": cases,
        "business_db_before": db_before,
        "business_db_after": db_after,
        "matching_actor_business_writes": matching_actor_business_writes,
        "mismatch_actor_business_writes": mismatch_actor_business_writes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("writer-lock", "old-cache-writer", "source-pinning"):
        sub = subparsers.add_parser(mode)
        sub.add_argument("--work-dir", type=Path, required=True)
        sub.add_argument("--output-json", type=Path, required=True)
        if mode == "source-pinning":
            sub.add_argument("--project-root", type=Path, required=True)

    cross_node = subparsers.add_parser("writer-lock-cross-node")
    cross_node.add_argument("--work-dir", type=Path, required=True)
    cross_node.add_argument("--role", choices=("holder", "contender"), required=True)
    cross_node.add_argument("--output-json", type=Path, required=True)
    combine = subparsers.add_parser("writer-lock-combine")
    combine.add_argument("--single-node", type=Path, required=True)
    combine.add_argument("--holder", type=Path, required=True)
    combine.add_argument("--contender", type=Path, required=True)
    combine.add_argument("--output-json", type=Path, required=True)

    holder = subparsers.add_parser("_hold-writer-lock")
    holder.add_argument("--db", type=Path, required=True)
    holder.add_argument("--ready", type=Path, required=True)
    old_writer = subparsers.add_parser("_old-cache-writer")
    old_writer.add_argument("--control", type=Path, required=True)
    old_writer.add_argument("--old-payload-dir", type=Path, required=True)
    old_writer.add_argument("--lease", type=Path, required=True)
    old_writer.add_argument("--ready", type=Path, required=True)
    runtime_stub = subparsers.add_parser("_source-runtime-stub")
    runtime_stub.add_argument("--project-root", type=Path, required=True)
    runtime_stub.add_argument("--db", type=Path, required=True)
    runtime_stub.add_argument("--case-name", required=True)
    runtime_stub.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "_hold-writer-lock":
        _hold_writer_lock(args.db, args.ready)
        return
    if args.mode == "_old-cache-writer":
        _stage_and_pause_old_cache_writer(
            args.control,
            args.old_payload_dir,
            args.lease,
            args.ready,
        )
        return
    if args.mode == "_source-runtime-stub":
        payload = _source_runtime_stub(
            project_root=args.project_root,
            db_path=args.db,
            case_name=args.case_name,
        )
        _atomic_write_json(args.output_json, payload)
        return
    started = time.monotonic()
    if args.mode == "writer-lock":
        payload = writer_lock_probe(args.work_dir)
    elif args.mode == "writer-lock-cross-node":
        payload = cross_node_writer_lock_role(args.work_dir, args.role)
    elif args.mode == "writer-lock-combine":
        payload = combine_writer_lock_evidence(args.single_node, args.holder, args.contender)
    elif args.mode == "old-cache-writer":
        payload = old_cache_writer_probe(args.work_dir)
    else:
        payload = source_pinning_probe(args.project_root.resolve(), args.work_dir)
    payload.update(
        {
            "probe": args.mode,
            "hostname": os.uname().nodename,
            "elapsed_seconds": time.monotonic() - started,
        }
    )
    _atomic_write_json(args.output_json, payload)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
