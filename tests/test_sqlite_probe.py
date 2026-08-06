import json
import importlib.util
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


SCRIPT = Path("scripts/miyabi/sqlite_shared_fs_probe.py")


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("sqlite_shared_fs_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_stress_probe_and_kill_reopen(tmp_path):
    db = tmp_path / "probe.sqlite3"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "stress",
            "--db",
            str(db),
            "--writer-id",
            "pytest",
            "--count",
            "100",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "kill-reopen",
            "--db",
            str(db),
            "--cycles",
            "10",
            "--seed",
            "7",
        ],
        check=True,
    )


def test_sqlite_contend_records_waits_without_starvation(tmp_path):
    db = tmp_path / "contend.sqlite3"
    subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--db", str(db)],
        check=True,
        capture_output=True,
        text=True,
    )
    processes = []
    outputs = []
    for writer in range(4):
        output = tmp_path / f"writer_{writer}.json"
        outputs.append(output)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "contend",
                    "--db",
                    str(db),
                    "--writer-id",
                    f"writer-{writer}",
                    "--count",
                    "20",
                    "--busy-timeout-ms",
                    "5",
                    "--retry-timeout-seconds",
                    "20",
                    "--hold-milliseconds",
                    "2",
                    "--seed",
                    str(100 + writer),
                    "--output-json",
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    results = [json.loads(path.read_text(encoding="utf-8")) for path in outputs]
    assert all(result["committed_transactions"] == 20 for result in results)
    assert all(result["starved"] is False for result in results)
    assert all(result["hostname"] and int(result["pid"]) > 0 for result in results)
    assert sum(result["busy_errors"] for result in results) > 0


def test_sqlite_contender_connection_waits_for_existing_writer(tmp_path):
    db = tmp_path / "startup-lock.sqlite3"
    subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--db", str(db)],
        check=True,
        capture_output=True,
        text=True,
    )
    holder = sqlite3.connect(db)
    holder.execute("BEGIN IMMEDIATE")
    output = tmp_path / "contender.json"
    contender = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "contend",
            "--db",
            str(db),
            "--writer-id",
            "startup-contender",
            "--count",
            "1",
            "--busy-timeout-ms",
            "5",
            "--retry-timeout-seconds",
            "5",
            "--output-json",
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        holder.rollback()
    finally:
        holder.close()
    stdout, stderr = contender.communicate(timeout=10)
    assert contender.returncode == 0, (stdout, stderr)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["committed_transactions"] == 1
    assert result["integrity_check"] == ["ok"]


def test_clock_offset_interval_bounds_asymmetric_observation_delay():
    module = _load_probe_module()
    lower, upper = module._clock_offset_interval(
        local_sent_wall_ns=1_000,
        local_received_wall_ns=1_300,
        remote_received_wall_ns=1_125,
        remote_sent_wall_ns=1_140,
    )
    assert (lower, upper) == (-160, 125)
    assert lower <= 100 <= upper


def test_verify_readonly_does_not_initialize_missing_database(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "verify-readonly", "--db", str(missing)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert not missing.exists()
