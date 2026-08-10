"""Initialize and submit independent Full Protocol PBS actors."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..core.config import resolve_config
from ..core.source_identity import bind_source_identity
from .init_run import initialize_run


_WALLTIME_RE = re.compile(r"^[0-9]{2,4}:[0-5][0-9]:[0-5][0-9]$")


def _walltime_resource(value: str | None, *, required: bool) -> list[str]:
    if value is None:
        if required:
            raise ValueError(
                "submitting independent jobs requires an estimated syncer and learner walltime"
            )
        return []
    if _WALLTIME_RE.fullmatch(value) is None or value == "00:00:00":
        raise ValueError(f"invalid PBS walltime: {value!r}")
    hours, minutes, seconds = (int(item) for item in value.split(":"))
    if hours * 3600 + minutes * 60 + seconds < 600:
        raise ValueError("PBS walltime must be at least 00:10:00")
    return ["-l", f"walltime={value}"]


def _qsub(command: list[str]) -> dict[str, Any]:
    """Return an auditable receipt even when PBS rejects the submission."""

    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        return {
            "status": "failed",
            "returncode": -1,
            "stdout": "",
            "stderr": repr(exc),
            "command": list(command),
        }
    stdout = completed.stdout.strip()
    receipt: dict[str, Any] = {
        "status": "failed",
        "returncode": int(completed.returncode),
        "stdout": stdout,
        "stderr": completed.stderr.strip(),
        "command": list(command),
    }
    if completed.returncode == 0 and stdout:
        receipt["status"] = "submitted"
        receipt["job_id"] = stdout.splitlines()[-1]
    elif completed.returncode == 0:
        receipt["stderr"] = "qsub returned no job id"
    return receipt


def launch(
    *,
    config_path: str | Path,
    run_id: str | None,
    shared_root: str | None,
    project_root: str | Path,
    submit: bool,
    allow_dirty_snapshot: bool,
    syncer_walltime: str | None = None,
    learner_walltime: str | None = None,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    # Validate scheduler resources before init_run creates the immutable run
    # root.  A missing walltime must not leave a bootstrapped but unsubmitted
    # run behind.
    syncer_walltime_resource = _walltime_resource(
        syncer_walltime,
        required=submit,
    )
    learner_walltime_resource = _walltime_resource(
        learner_walltime,
        required=submit,
    )
    config = resolve_config(
        config_path,
        run_id=run_id,
        shared_root=shared_root,
        project_root=project_root,
    )
    bind_source_identity(config, project_root)
    initialized = initialize_run(
        config,
        project_root=project_root,
        allow_dirty_snapshot=allow_dirty_snapshot,
    )
    descriptor = initialized["descriptor"]
    shared = config
    variables = ",".join(
        (
            f"FS_DILOCO_SHARED_ROOT={descriptor['shared_root']}",
            f"FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256={descriptor['descriptor_sha256']}",
            f"PROJECT_ROOT={project_root}",
        )
    )
    syncer_command = [
        "qsub",
        *syncer_walltime_resource,
        "-v",
        variables,
        str(project_root / "scripts/miyabi/run_syncer.pbs"),
    ]
    dynamic = shared.membership.mode == "dynamic"
    if dynamic:
        learner_commands = [
            [
                "qsub",
                *learner_walltime_resource,
                "-v",
                f"{variables},BOOTSTRAP_SLOT={slot}",
                str(project_root / "scripts/miyabi/run_learner.pbs"),
            ]
            for slot in range(shared.membership.bootstrap_instances)
        ]
    else:
        learner_commands = [
            [
                "qsub",
                *learner_walltime_resource,
                "-r",
                "y",
                "-v",
                (f"{variables},FS_DILOCO_STATIC_LAUNCH_PREFIX={descriptor['descriptor_sha256']}"),
                "-J",
                f"0-{int(shared.sync.num_learners) - 1}",
                str(project_root / "scripts/miyabi/run_learner.pbs"),
            ]
        ]
    result: dict[str, Any] = {
        **initialized,
        "syncer_qsub": syncer_command,
        "membership_mode": shared.membership.mode,
        "learner_qsubs": learner_commands,
        "submitted": bool(submit),
    }
    if submit:
        syncer_receipt = _qsub(syncer_command)
        result["syncer_submission"] = syncer_receipt
        if syncer_receipt["status"] != "submitted":
            result["submission_status"] = "failed"
            return result
        result["syncer_job_id"] = syncer_receipt["job_id"]

        learner_receipts: list[dict[str, Any]] = []
        result["learner_submissions"] = learner_receipts
        for slot, learner_command in enumerate(learner_commands):
            learner_receipt = _qsub(learner_command)
            learner_receipt["bootstrap_slot"] = slot if dynamic else None
            learner_receipts.append(learner_receipt)
            if learner_receipt["status"] != "submitted":
                # Accepted IDs are the durable operator receipt.  Never qdel a
                # partial topology implicitly.
                result["accepted_learner_job_ids"] = [
                    receipt["job_id"]
                    for receipt in learner_receipts
                    if receipt["status"] == "submitted"
                ]
                result["submission_status"] = "partial"
                return result
        if dynamic:
            result["learner_job_ids"] = [receipt["job_id"] for receipt in learner_receipts]
        else:
            result["learner_array_job_id"] = learner_receipts[0]["job_id"]
        result["submission_status"] = "submitted"
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--shared-root")
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--allow-dirty-snapshot", action="store_true")
    parser.add_argument(
        "--syncer-walltime",
        help="estimated shortest practical PBS walltime (HH:MM:SS); required with --submit",
    )
    parser.add_argument(
        "--learner-walltime",
        help="estimated shortest practical PBS walltime (HH:MM:SS); required with --submit",
    )
    args = parser.parse_args(argv)
    result = launch(
        config_path=args.config,
        run_id=args.run_id,
        shared_root=args.shared_root,
        project_root=args.project_root,
        submit=args.submit,
        allow_dirty_snapshot=args.allow_dirty_snapshot,
        syncer_walltime=args.syncer_walltime,
        learner_walltime=args.learner_walltime,
    )
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    if args.submit and result.get("submission_status") != "submitted":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
