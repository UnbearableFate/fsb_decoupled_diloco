"""Initialize and submit independent Full Protocol PBS actors."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..core.config import resolve_config
from ..core.source_identity import bind_source_identity
from .init_run import initialize_run


_WALLTIME_RE = re.compile(r"^[0-9]{2,4}:[0-5][0-9]:[0-5][0-9]$")
_QUEUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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


def _queue_resource(value: str | None, *, required: bool) -> list[str]:
    if value is None:
        if required:
            raise ValueError("submitting independent jobs requires an explicit actor queue")
        return []
    if _QUEUE_RE.fullmatch(value) is None:
        raise ValueError(f"invalid PBS queue: {value!r}")
    return ["-q", value]


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


def _publish_submission_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        os.link(temporary, path)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


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
    log_root: str | Path | None = None,
    actor_queue: str | None = None,
) -> dict[str, Any]:
    """Initialize a run and optionally submit its independent PBS actors."""

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
    actor_queue_resource = _queue_resource(actor_queue, required=submit)
    if submit and log_root is None:
        raise ValueError("submitting independent jobs requires an explicit log root")
    resolved_log_root = None if log_root is None else Path(log_root).resolve()
    if submit and resolved_log_root is not None:
        resolved_log_root.mkdir(parents=True, exist_ok=False)
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
        *actor_queue_resource,
        *syncer_walltime_resource,
        *([] if resolved_log_root is None else ["-o", str(resolved_log_root / "syncer.log")]),
        "-v",
        variables,
        str(project_root / "scripts/miyabi/agent/run_syncer.pbs"),
    ]
    learner_commands = [
        [
            "qsub",
            *actor_queue_resource,
            *learner_walltime_resource,
            *(
                []
                if resolved_log_root is None
                else ["-o", str(resolved_log_root / f"bootstrap_{slot:03d}.log")]
            ),
            "-v",
            f"{variables},BOOTSTRAP_SLOT={slot}",
            str(project_root / "scripts/miyabi/agent/run_learner.pbs"),
        ]
        for slot in range(shared.membership.bootstrap_instances)
    ]
    result: dict[str, Any] = {
        **initialized,
        "syncer_qsub": syncer_command,
        "actor_queue": actor_queue,
        "learner_qsubs": learner_commands,
        "log_root": None if resolved_log_root is None else str(resolved_log_root),
        "submitted": bool(submit),
    }
    if submit:
        assert resolved_log_root is not None
        receipt_path = resolved_log_root / "submission_receipt.json"
        stage_root = resolved_log_root / "submission_receipts"
        syncer_receipt = _qsub(syncer_command)
        result["syncer_submission"] = syncer_receipt
        if syncer_receipt["status"] != "submitted":
            result["submission_status"] = "failed"
            _publish_submission_receipt(stage_root / "000-syncer.json", result)
            _publish_submission_receipt(receipt_path, result)
            return result
        result["syncer_job_id"] = syncer_receipt["job_id"]
        result["submission_status"] = "submitting"
        _publish_submission_receipt(stage_root / "000-syncer.json", result)

        learner_receipts: list[dict[str, Any]] = []
        result["learner_submissions"] = learner_receipts
        for slot, learner_command in enumerate(learner_commands):
            learner_receipt = _qsub(learner_command)
            learner_receipt["bootstrap_slot"] = slot
            learner_receipts.append(learner_receipt)
            result["accepted_learner_job_ids"] = [
                receipt["job_id"]
                for receipt in learner_receipts
                if receipt["status"] == "submitted"
            ]
            if learner_receipt["status"] != "submitted":
                # Accepted IDs are the durable operator receipt.  Never qdel a
                # partial topology implicitly.
                result["submission_status"] = "partial"
                _publish_submission_receipt(
                    stage_root / f"{slot + 1:03d}-learner_{slot:03d}.json",
                    result,
                )
                _publish_submission_receipt(receipt_path, result)
                return result
            _publish_submission_receipt(
                stage_root / f"{slot + 1:03d}-learner_{slot:03d}.json",
                result,
            )
        result["learner_job_ids"] = [receipt["job_id"] for receipt in learner_receipts]
        result["submission_status"] = "submitted"
        _publish_submission_receipt(receipt_path, result)
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
        "--log-root",
        help="absolute actor log directory; required with --submit",
    )
    parser.add_argument(
        "--actor-queue",
        help="PBS queue for every independent actor; required with --submit",
    )
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
        log_root=args.log_root,
        actor_queue=args.actor_queue,
    )
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    if args.submit and result.get("submission_status") != "submitted":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
