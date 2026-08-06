"""Initialize and submit independent HA syncer/learner PBS jobs."""

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
    return ["-l", f"walltime={value}"]


def _qsub(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    job_id = completed.stdout.strip().splitlines()[-1]
    if not job_id:
        raise RuntimeError(f"qsub returned no job id: {command}")
    return job_id


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
    initialized = initialize_run(
        config,
        project_root=project_root,
        allow_dirty_snapshot=allow_dirty_snapshot,
    )
    descriptor = initialized["descriptor"]
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
        str(project_root / "scripts/miyabi/run_syncer_candidate.pbs"),
    ]
    learner_command = [
        "qsub",
        *learner_walltime_resource,
        "-r",
        "y",
        "-v",
        variables,
        "-J",
        f"0-{int(config.sync.num_learners) - 1}",
        str(project_root / "scripts/miyabi/run_static_learner.pbs"),
    ]
    result: dict[str, Any] = {
        **initialized,
        "syncer_qsub": syncer_command,
        "learner_qsub": learner_command,
        "submitted": bool(submit),
    }
    if submit:
        result["syncer_job_id"] = _qsub(syncer_command)
        result["learner_array_job_id"] = _qsub(learner_command)
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


if __name__ == "__main__":
    main()
