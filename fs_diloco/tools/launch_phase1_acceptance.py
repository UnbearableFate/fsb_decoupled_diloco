"""Submit the Phase 1 crash/successor/learner jobs with durable receipts."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..storage.atomic_io import atomic_write_json
from .launch_independent_run import _qsub, _walltime_resource


def submit_acceptance_jobs(
    *,
    project_root: Path,
    run_id: str,
    shared_root: Path,
    descriptor_sha256: str,
    launcher_job_id: str,
    crash_walltime: str,
    successor_walltime: str,
    learner_walltime: str,
    pending_artifact: Path,
    pass_artifact: Path,
    qsub_fn: Callable[[list[str]], dict[str, Any]] = _qsub,
) -> dict[str, Any]:
    for value in (crash_walltime, successor_walltime, learner_walltime):
        _walltime_resource(value, required=True)
    base_variables = ",".join(
        (
            f"FS_DILOCO_SHARED_ROOT={shared_root}",
            f"FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256={descriptor_sha256}",
            f"PROJECT_ROOT={project_root}",
        )
    )
    crash_variables = (
        f"{base_variables},FS_DILOCO_PUBLICATION_FAILPOINT=after_db_commit,"
        "FS_DILOCO_FAILPOINT_ACTION=kill"
    )
    payload: dict[str, Any] = {
        "status": "pending",
        "run_id": run_id,
        "shared_root": str(shared_root),
        "descriptor_sha256": descriptor_sha256,
        "launcher_job_id": launcher_job_id,
        "requested_walltimes": {
            "crash_syncer": crash_walltime,
            "successor_syncer": successor_walltime,
            "learner_array": learner_walltime,
        },
        "takeover_injection": {
            "failpoint": "after_db_commit",
            "action": "SIGKILL",
            "successor_dependency": "afterany",
            "learner_dependency": "after-successor-start",
        },
        "submission_receipts": [],
    }
    atomic_write_json(pending_artifact, payload)

    crash_command = [
        "qsub",
        "-q",
        "debug-g",
        "-l",
        f"walltime={crash_walltime}",
        "-v",
        crash_variables,
        str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
    ]
    crash_receipt = qsub_fn(crash_command)
    payload["submission_receipts"].append({"role": "crash_syncer", **crash_receipt})
    if crash_receipt.get("status") != "submitted":
        payload["status"] = "failed"
        atomic_write_json(pending_artifact, payload)
        return payload
    crash_job_id = str(crash_receipt["job_id"])
    payload["crash_syncer_job_id"] = crash_job_id
    atomic_write_json(pending_artifact, payload)

    successor_command = [
        "qsub",
        "-q",
        "debug-g",
        "-l",
        f"walltime={successor_walltime}",
        "-W",
        f"depend=afterany:{crash_job_id}",
        "-v",
        base_variables,
        str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
    ]
    successor_receipt = qsub_fn(successor_command)
    payload["submission_receipts"].append({"role": "successor_syncer", **successor_receipt})
    if successor_receipt.get("status") != "submitted":
        payload["status"] = "partial"
        atomic_write_json(pending_artifact, payload)
        return payload
    successor_job_id = str(successor_receipt["job_id"])
    payload["successor_syncer_job_id"] = successor_job_id
    atomic_write_json(pending_artifact, payload)

    learner_command = [
        "qsub",
        "-q",
        "debug-g",
        "-l",
        f"walltime={learner_walltime}",
        "-r",
        "y",
        "-W",
        f"depend=after:{successor_job_id}",
        "-v",
        base_variables,
        "-J",
        "0-7",
        str(project_root / "scripts/miyabi/run_static_learner.pbs"),
    ]
    learner_receipt = qsub_fn(learner_command)
    payload["submission_receipts"].append({"role": "learner_array", **learner_receipt})
    if learner_receipt.get("status") != "submitted":
        payload["status"] = "partial"
        atomic_write_json(pending_artifact, payload)
        return payload
    payload["learner_array_job_id"] = str(learner_receipt["job_id"])
    payload["status"] = "PASS"
    atomic_write_json(pass_artifact, payload)
    pending_artifact.unlink(missing_ok=True)
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--descriptor-sha256", required=True)
    parser.add_argument("--launcher-job-id", default=os.environ.get("PBS_JOBID", ""))
    parser.add_argument("--crash-walltime", required=True)
    parser.add_argument("--successor-walltime", required=True)
    parser.add_argument("--learner-walltime", required=True)
    parser.add_argument("--pending-artifact", type=Path, required=True)
    parser.add_argument("--pass-artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = submit_acceptance_jobs(
        project_root=args.project_root.resolve(),
        run_id=args.run_id,
        shared_root=args.shared_root.resolve(),
        descriptor_sha256=args.descriptor_sha256,
        launcher_job_id=args.launcher_job_id,
        crash_walltime=args.crash_walltime,
        successor_walltime=args.successor_walltime,
        learner_walltime=args.learner_walltime,
        pending_artifact=args.pending_artifact,
        pass_artifact=args.pass_artifact,
    )
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(0 if payload["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
