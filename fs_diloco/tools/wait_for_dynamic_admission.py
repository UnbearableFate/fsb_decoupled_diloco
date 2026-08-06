"""Wait until the dynamic learner belonging to a PBS job is admitted."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ..runtime.pbs_scheduler import normalize_job_id


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def find_admitted_instance(shared_root: Path, *, pbs_job_id: str) -> str | None:
    """Return this physical job's admitted instance, if publication is complete."""

    expected_job_id = normalize_job_id(pbs_job_id)
    if not expected_job_id:
        raise ValueError("pbs_job_id must not be empty")
    request_dir = shared_root / "control" / "registration_requests"
    for request_path in sorted(request_dir.glob("*.json")):
        request = _read_json(request_path)
        if request is None:
            continue
        if normalize_job_id(str(request.get("pbs_job_id") or "")) != expected_job_id:
            continue
        instance_id = str(request.get("instance_id") or "")
        if not instance_id:
            continue
        epoch_root = shared_root / "control" / "syncer_epochs"
        for admission_path in sorted(
            epoch_root.glob(f"*/membership/admissions/{instance_id}.json")
        ):
            admission = _read_json(admission_path)
            if (
                admission is not None
                and admission.get("state") == "admitted"
                and admission.get("instance_id") == instance_id
            ):
                return instance_id
    return None


def wait_for_admission(
    shared_root: Path,
    *,
    pbs_job_id: str,
    timeout_seconds: float,
    poll_seconds: float = 0.1,
) -> str:
    if timeout_seconds <= 0.0:
        raise ValueError("timeout_seconds must be positive")
    if poll_seconds <= 0.0:
        raise ValueError("poll_seconds must be positive")
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        instance_id = find_admitted_instance(shared_root, pbs_job_id=pbs_job_id)
        if instance_id is not None:
            return instance_id
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out waiting for dynamic admission of PBS job {pbs_job_id}"
            )
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--pbs-job-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    args = parser.parse_args()
    print(
        wait_for_admission(
            args.shared_root,
            pbs_job_id=args.pbs_job_id,
            timeout_seconds=args.timeout_seconds,
        )
    )


if __name__ == "__main__":
    main()
