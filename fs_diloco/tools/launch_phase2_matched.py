"""Launch sequential matched static/dynamic 1+8 runs for Phase 2 overhead."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.config import resolve_config
from ..protocol.membership import write_bootstrap_scheduler_jobs
from ..storage.atomic_io import atomic_write_json
from ..storage.paths import RunPaths
from .init_run import initialize_run
from .launch_independent_run import _qsub, _walltime_resource


def submit_jobs(
    *,
    project_root: Path,
    initialized_static: dict[str, Any],
    initialized_dynamic: dict[str, Any],
    launcher_job_id: str,
    syncer_walltime: str,
    learner_walltime: str,
    checker_walltime: str,
    receipts_path: Path,
    output_path: Path,
    qsub_fn: Callable[[list[str]], dict[str, Any]] = _qsub,
) -> dict[str, Any]:
    """Submit two isolated 1+8 runs and persist every accepted job receipt."""

    for value in (syncer_walltime, learner_walltime, checker_walltime):
        _walltime_resource(value, required=True)
    project_root = project_root.resolve()
    static_root = Path(initialized_static["descriptor"]["shared_root"]).resolve()
    dynamic_root = Path(initialized_dynamic["descriptor"]["shared_root"]).resolve()
    payload: dict[str, Any] = {
        "checker": "plan02_phase2_matched_launcher",
        "status": "pending",
        "static_run_root": str(static_root),
        "dynamic_run_root": str(dynamic_root),
        "submission_receipts": [],
    }
    atomic_write_json(receipts_path, payload)

    def submit(role: str, command: list[str]) -> str:
        receipt = qsub_fn(command)
        payload["submission_receipts"].append({"role": role, **receipt})
        atomic_write_json(receipts_path, payload)
        if receipt.get("status") != "submitted":
            payload["status"] = "partial"
            atomic_write_json(receipts_path, payload)
            raise RuntimeError(f"matched submission failed for {role}: {receipt}")
        return str(receipt["job_id"])

    def variables(initialized: dict[str, Any], *extra: str) -> str:
        descriptor = initialized["descriptor"]
        return ",".join(
            (
                f"FS_DILOCO_SHARED_ROOT={descriptor['shared_root']}",
                (
                    "FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256="
                    f"{descriptor['descriptor_sha256']}"
                ),
                f"PROJECT_ROOT={project_root}",
                *extra,
            )
        )

    static_syncer_command = [
        "qsub",
        "-q",
        "debug-g",
        "-l",
        f"walltime={syncer_walltime}",
    ]
    if launcher_job_id:
        static_syncer_command.extend(("-W", f"depend=afterany:{launcher_job_id}"))
    static_syncer_command.extend(
        (
            "-v",
            variables(initialized_static),
            str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
        )
    )
    static_syncer = submit("static_syncer", static_syncer_command)
    for learner_index in range(8):
        submit(
            f"static_learner_{learner_index}",
            [
                "qsub",
                "-q",
                "debug-g",
                "-l",
                f"walltime={learner_walltime}",
                "-W",
                f"depend=after:{static_syncer}",
                "-v",
                variables(initialized_static, f"LEARNER_INDEX={learner_index}"),
                str(project_root / "scripts/miyabi/run_static_learner.pbs"),
            ],
        )
    dynamic_syncer = submit(
        "dynamic_syncer",
        [
            "qsub",
            "-q",
            "debug-g",
            "-l",
            f"walltime={syncer_walltime}",
            "-W",
            f"depend=afterany:{static_syncer}",
            "-v",
            variables(initialized_dynamic),
            str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
        ],
    )
    dynamic_descriptor = initialized_dynamic["descriptor"]
    bootstrap_jobs_by_slot: dict[int, str] = {}
    for bootstrap_slot in range(8):
        job_id = submit(
            f"dynamic_learner_{bootstrap_slot}",
            [
                "qsub",
                "-q",
                "debug-g",
                "-l",
                f"walltime={learner_walltime}",
                "-W",
                f"depend=after:{dynamic_syncer}",
                "-v",
                variables(initialized_dynamic, f"BOOTSTRAP_SLOT={bootstrap_slot}"),
                str(project_root / "scripts/miyabi/run_dynamic_learner.pbs"),
            ],
        )
        bootstrap_jobs_by_slot[bootstrap_slot] = job_id
        write_bootstrap_scheduler_jobs(
            RunPaths(dynamic_root),
            run_id=str(dynamic_descriptor["run_id"]),
            source_fingerprint=str(dynamic_descriptor["source_fingerprint"]),
            config_sha256=str(dynamic_descriptor["resolved_config_sha256"]),
            config_fingerprint=str(dynamic_descriptor["descriptor_sha256"]),
            jobs_by_slot=bootstrap_jobs_by_slot,
        )
    submit(
        "matched_checker",
        [
            "qsub",
            "-q",
            "debug-g",
            "-l",
            f"walltime={checker_walltime}",
            "-W",
            f"depend=afterany:{dynamic_syncer}",
            "-v",
            ",".join(
                (
                    f"PROJECT_ROOT={project_root}",
                    f"PHASE2_STATIC_RUN_ROOT={static_root}",
                    f"PHASE2_DYNAMIC_RUN_ROOT={dynamic_root}",
                    f"PHASE2_MATCHED_RECEIPTS={receipts_path.resolve()}",
                    f"PHASE2_MATCHED_OUTPUT={output_path.resolve()}",
                )
            ),
            str(project_root / "scripts/miyabi/run_plan02_phase2_matched_checker.pbs"),
        ],
    )
    payload["status"] = "PASS"
    atomic_write_json(receipts_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--launcher-job-id", default="")
    parser.add_argument("--syncer-walltime", required=True)
    parser.add_argument("--learner-walltime", required=True)
    parser.add_argument("--checker-walltime", required=True)
    parser.add_argument("--allow-dirty-snapshot", action="store_true")
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    static_root = project_root / "runs/fs_diloco" / f"{args.run_prefix}_static"
    dynamic_root = project_root / "runs/fs_diloco" / f"{args.run_prefix}_dynamic"
    dynamic = resolve_config(
        args.config,
        run_id=f"{args.run_prefix}_dynamic",
        shared_root=str(dynamic_root),
        project_root=project_root,
    )
    static = resolve_config(
        args.config,
        run_id=f"{args.run_prefix}_static",
        shared_root=str(static_root),
        project_root=project_root,
    )
    static.membership.mode = "static"
    static.scaling.enabled = False
    initialized_static = initialize_run(
        static,
        project_root=project_root,
        allow_dirty_snapshot=args.allow_dirty_snapshot,
    )
    initialized_dynamic = initialize_run(
        dynamic,
        project_root=project_root,
        allow_dirty_snapshot=args.allow_dirty_snapshot,
    )

    payload = submit_jobs(
        project_root=project_root,
        initialized_static=initialized_static,
        initialized_dynamic=initialized_dynamic,
        launcher_job_id=args.launcher_job_id,
        syncer_walltime=args.syncer_walltime,
        learner_walltime=args.learner_walltime,
        checker_walltime=args.checker_walltime,
        receipts_path=args.receipts,
        output_path=args.output,
    )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
