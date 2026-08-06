"""Submit source-pinned Phase 2 G8/G9 jobs with durable receipts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..protocol.membership import write_bootstrap_scheduler_jobs
from ..storage.atomic_io import atomic_write_json
from ..storage.paths import RunPaths
from .launch_independent_run import _qsub, _walltime_resource


def submit_jobs(
    *,
    kind: str,
    project_root: Path,
    run_id: str,
    shared_root: Path,
    descriptor_sha256: str,
    source_fingerprint: str,
    config_sha256: str,
    launcher_job_id: str,
    crash_walltime: str,
    syncer_walltime: str,
    learner_walltime: str,
    checker_walltime: str,
    pending_artifact: Path,
    pass_artifact: Path,
    evidence_artifact: Path,
    qsub_fn: Callable[[list[str]], dict[str, Any]] = _qsub,
) -> dict[str, Any]:
    if kind not in {"g8", "g9"}:
        raise ValueError("kind must be g8 or g9")
    for value in (
        crash_walltime,
        syncer_walltime,
        learner_walltime,
        checker_walltime,
    ):
        _walltime_resource(value, required=True)
    base_variables = ",".join(
        (
            f"FS_DILOCO_SHARED_ROOT={shared_root}",
            f"FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256={descriptor_sha256}",
            f"PROJECT_ROOT={project_root}",
        )
    )
    payload: dict[str, Any] = {
        "checker": "plan02_phase2_acceptance_launcher",
        "kind": kind,
        "status": "pending",
        "run_id": run_id,
        "shared_root": str(shared_root),
        "descriptor_sha256": descriptor_sha256,
        "launcher_job_id": launcher_job_id,
        "requested_walltimes": {
            "crash_syncer": crash_walltime,
            "successor_syncer": syncer_walltime,
            "learner": learner_walltime,
            "evidence_checker": checker_walltime,
        },
        "submission_receipts": [],
    }
    atomic_write_json(pending_artifact, payload)

    def submit(role: str, command: list[str]) -> str | None:
        receipt = qsub_fn(command)
        payload["submission_receipts"].append({"role": role, **receipt})
        atomic_write_json(pending_artifact, payload)
        if receipt.get("status") != "submitted":
            payload["status"] = "partial"
            atomic_write_json(pending_artifact, payload)
            return None
        return str(receipt["job_id"])

    crash_variables = (
        f"{base_variables},FS_DILOCO_PUBLICATION_FAILPOINT=after_db_commit,"
        "FS_DILOCO_FAILPOINT_ACTION=kill"
    )
    crash_command = [
        "qsub",
        "-q",
        "debug-g",
        "-l",
        f"walltime={crash_walltime}",
    ]
    if launcher_job_id:
        crash_command.extend(("-W", f"depend=afterany:{launcher_job_id}"))
    crash_command.extend(
        (
            "-v",
            crash_variables,
            str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
        )
    )
    crash_id = submit("crash_syncer", crash_command)
    if crash_id is None:
        return payload

    successor_id = submit(
        "successor_syncer",
        [
            "qsub",
            "-q",
            "debug-g",
            "-l",
            f"walltime={syncer_walltime}",
            "-W",
            f"depend=afterany:{crash_id}",
            "-v",
            base_variables,
            str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
        ],
    )
    if successor_id is None:
        return payload

    bootstrap_count = 1 if kind == "g8" else 8
    bootstrap_ids: list[str] = []
    bootstrap_jobs_by_slot: dict[int, str] = {}
    for slot in range(bootstrap_count):
        variables = f"{base_variables},BOOTSTRAP_SLOT={slot}"
        role = f"bootstrap_{slot}"
        if slot == 0:
            variables += ",FS_DILOCO_TEST_TERMINATE_AFTER_SECONDS=8"
            role = "victim_bootstrap_0"
        elif kind == "g9" and slot == 1:
            variables += (
                ",FS_DILOCO_TEST_PAUSE_AFTER_SECONDS=10,"
                "FS_DILOCO_TEST_PAUSE_DURATION_SECONDS=4"
            )
            role = "paused_bootstrap_1"
        job_id = submit(
            role,
            [
                "qsub",
                "-q",
                "debug-g",
                "-l",
                f"walltime={learner_walltime}",
                "-W",
                f"depend=after:{successor_id}",
                "-v",
                variables,
                str(project_root / "scripts/miyabi/run_dynamic_learner.pbs"),
            ],
        )
        if job_id is None:
            return payload
        bootstrap_ids.append(job_id)
        bootstrap_jobs_by_slot[slot] = job_id
        # Persist each accepted qsub independently.  A launcher crash after a
        # partial submission must not leave queued bootstrap capacity
        # invisible to the leader's scheduler reconciler.
        write_bootstrap_scheduler_jobs(
            RunPaths(shared_root),
            run_id=run_id,
            source_fingerprint=source_fingerprint,
            config_sha256=config_sha256,
            config_fingerprint=descriptor_sha256,
            jobs_by_slot=bootstrap_jobs_by_slot,
        )

    if kind == "g9":
        duplicate_id = submit(
            "duplicate_bootstrap_0",
            [
                "qsub",
                "-q",
                "debug-g",
                "-l",
                f"walltime={learner_walltime}",
                "-W",
                f"depend=afterany:{bootstrap_ids[0]}",
                "-v",
                f"{base_variables},BOOTSTRAP_SLOT=0",
                str(project_root / "scripts/miyabi/run_dynamic_learner.pbs"),
            ],
        )
        if duplicate_id is None:
            return payload

    checker_variables = ",".join(
        (
            f"PROJECT_ROOT={project_root}",
            f"PHASE2_ACCEPTANCE_KIND={kind}",
            f"FS_DILOCO_SHARED_ROOT={shared_root}",
            f"PHASE2_RECEIPTS={pass_artifact}",
            f"PHASE2_EVIDENCE_OUTPUT={evidence_artifact}",
        )
    )
    checker_id = submit(
        "evidence_checker",
        [
            "qsub",
            "-q",
            "debug-g",
            "-l",
            f"walltime={checker_walltime}",
            "-W",
            f"depend=afterany:{successor_id}",
            "-v",
            checker_variables,
            str(project_root / "scripts/miyabi/run_plan02_phase2_chaos_checker.pbs"),
        ],
    )
    if checker_id is None:
        return payload

    payload["status"] = "PASS"
    payload["job_ids"] = {
        "crash_syncer": crash_id,
        "successor_syncer": successor_id,
        "bootstrap": bootstrap_ids,
        "evidence_checker": checker_id,
    }
    atomic_write_json(pass_artifact, payload)
    pending_artifact.unlink(missing_ok=True)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("g8", "g9"), required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--descriptor-sha256", required=True)
    parser.add_argument("--source-fingerprint", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--launcher-job-id", default="")
    parser.add_argument("--crash-walltime", required=True)
    parser.add_argument("--syncer-walltime", required=True)
    parser.add_argument("--learner-walltime", required=True)
    parser.add_argument("--checker-walltime", required=True)
    parser.add_argument("--pending-artifact", type=Path, required=True)
    parser.add_argument("--pass-artifact", type=Path, required=True)
    parser.add_argument("--evidence-artifact", type=Path, required=True)
    args = parser.parse_args()
    payload = submit_jobs(
        kind=args.kind,
        project_root=args.project_root.resolve(),
        run_id=args.run_id,
        shared_root=args.shared_root.resolve(),
        descriptor_sha256=args.descriptor_sha256,
        source_fingerprint=args.source_fingerprint,
        config_sha256=args.config_sha256,
        launcher_job_id=args.launcher_job_id,
        crash_walltime=args.crash_walltime,
        syncer_walltime=args.syncer_walltime,
        learner_walltime=args.learner_walltime,
        checker_walltime=args.checker_walltime,
        pending_artifact=args.pending_artifact,
        pass_artifact=args.pass_artifact,
        evidence_artifact=args.evidence_artifact,
    )
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(0 if payload["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
