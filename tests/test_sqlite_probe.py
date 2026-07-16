import subprocess
import sys
from pathlib import Path


SCRIPT = Path("scripts/miyabi/sqlite_shared_fs_probe.py")


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
