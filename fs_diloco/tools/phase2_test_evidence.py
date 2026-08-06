"""Run Phase 2 compute-node tests and persist source-bound structured evidence."""

from __future__ import annotations

import argparse
import hashlib
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..storage.atomic_io import atomic_write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("g7", "compatibility"), required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--source-fingerprint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = "tests/test_plan02_phase2_dynamic.py" if args.kind == "g7" else "tests"
    command = [sys.executable, "-m", "pytest", "-q", target]
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=args.project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout
    stderr = completed.stderr
    status = "PASS" if completed.returncode == 0 else "BLOCKED"
    payload = {
        "checker": "plan02_phase2_test_evidence",
        "kind": args.kind,
        "status": status,
        "identity": {
            "git_commit": args.git_commit,
            "source_fingerprint": args.source_fingerprint,
        },
        "environment": {
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
            "pbs_job_id": __import__("os").environ.get("PBS_JOBID"),
        },
        "command": command,
        "target": target,
        "started_at": started,
        "completed_at": time.time(),
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "contracts": (
            {
                "synthetic_state_machine": True,
                "mock_scheduler": True,
                "churn_cycles": 1000,
                "uuid_generation_count": 1000,
                "sqlite_post_warmup_page_growth_limit": 32,
            }
            if args.kind == "g7"
            else {
                "static_full": True,
                "fragment": True,
                "historical_readonly": True,
                "full_repository_suite": True,
            }
        ),
    }
    atomic_write_json(args.output, payload)
    print(status)
    raise SystemExit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()
