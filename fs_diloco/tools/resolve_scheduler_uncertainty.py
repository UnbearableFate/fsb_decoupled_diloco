"""Create an immutable operator request for scheduler uncertainty resolution.

This tool intentionally never opens the authority database and never calls PBS.
An active fenced leader must ingest the published request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from ..protocol.scheduler import (
    SchedulerOperatorAction,
    SchedulerOperatorRequest,
)
from ..storage.atomic_io import publish_immutable_bytes
from ..storage.atomic_io import read_json
from ..storage.paths import RunPaths


PLAN03_REQUIREMENTS = frozenset({"SCHED-04", "SCHED-06"})


def build_request(
    *,
    launch_request_id: str,
    action: str,
    expected_state_sha256: str,
    reason: str,
    scheduler_job_id: str | None,
    evidence_source: str | None,
    created_at: float,
) -> SchedulerOperatorRequest:
    seed = json.dumps(
        {
            "launch_request_id": launch_request_id,
            "action": action,
            "expected_state_sha256": expected_state_sha256,
            "reason": reason,
            "scheduler_job_id": scheduler_job_id,
            "evidence_source": evidence_source,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    request_id = "scheduler-op-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return SchedulerOperatorRequest(
        format_version=1,
        request_id=request_id,
        launch_request_id=launch_request_id,
        action=SchedulerOperatorAction(action),
        expected_state_sha256=expected_state_sha256,
        reason=reason,
        created_at=created_at,
        scheduler_job_id=scheduler_job_id,
        evidence_source=evidence_source,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--launch-request-id", required=True)
    parser.add_argument(
        "--action", required=True, choices=[item.value for item in SchedulerOperatorAction]
    )
    parser.add_argument("--expected-state-sha256", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--scheduler-job-id")
    parser.add_argument("--evidence-source")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    request = build_request(
        launch_request_id=args.launch_request_id,
        action=args.action,
        expected_state_sha256=args.expected_state_sha256,
        reason=args.reason,
        scheduler_job_id=args.scheduler_job_id,
        evidence_source=args.evidence_source,
        created_at=time.time(),
    )
    paths = RunPaths(Path(args.shared_root).resolve())
    target = paths.scheduler_operator_requests / f"{request.request_id}.json"
    if args.apply and target.is_symlink():
        raise FileExistsError("scheduler operator request target is a symlink collision")
    if args.apply and target.exists():
        existing = SchedulerOperatorRequest.from_dict(read_json(target))
        comparable_existing = existing.as_dict()
        comparable_new = request.as_dict()
        comparable_existing.pop("created_at")
        comparable_new.pop("created_at")
        if comparable_existing != comparable_new:
            raise FileExistsError("scheduler operator request identity collision")
        request = existing
    result = {
        "mode": "apply" if args.apply else "dry-run",
        "target": str(target),
        "request": request.as_dict(),
    }
    if args.apply:
        publication = publish_immutable_bytes(
            target,
            request.canonical_bytes() + b"\n",
        )
        result["created"] = publication.created
        result["sha256"] = publication.sha256
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
