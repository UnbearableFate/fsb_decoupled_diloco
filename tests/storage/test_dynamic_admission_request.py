from __future__ import annotations

from pathlib import Path

from fs_diloco.protocol.contributor import StaticContributorFence
from fs_diloco.protocol.data_cursor import ContributorResumeState
from fs_diloco.storage.admission import (
    ADMISSION_RESPONSE_FORMAT_VERSION,
    admission_request_error,
    publish_dynamic_request_with_sha256,
    publish_admission_response,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.paths import RunPaths


def test_dynamic_request_carries_exact_bootstrap_authorization(tmp_path: Path) -> None:
    path, _digest = publish_dynamic_request_with_sha256(
        RunPaths(tmp_path),
        run_id="run-current",
        descriptor_sha256="d" * 64,
        instance_id="instance-1",
        stream_id=0,
        bootstrap_slot=0,
        admission_token_sha256="a" * 64,
    )
    request = read_json(path)

    assert request["bootstrap_slot"] == 0
    assert request["launch_request_id"] is None
    assert (
        admission_request_error(
            request,
            run_id="run-current",
            descriptor_sha256="d" * 64,
        )
        is None
    )

    request["bootstrap_slot"] = None
    assert admission_request_error(request, run_id="run-current", descriptor_sha256="d" * 64) == (
        "MalformedAdmissionRequest",
        "dynamic request requires exactly one bootstrap or launch authorization",
    )


def test_admission_response_carries_last_planned_update_identity(tmp_path: Path) -> None:
    fence = StaticContributorFence(
        kind="static",
        learner_id="learner_000",
        logical_launch_id="launch-0",
        attempt_id="attempt-1",
        binding_generation=2,
    )
    last_update_id = "00000000-0000-4000-8000-000000000001"
    path = publish_admission_response(
        RunPaths(tmp_path),
        epoch=1,
        owner_id="owner-1",
        request={
            "mode": "static",
            "run_id": "run-current",
            "descriptor_sha256": "d" * 64,
            "learner_id": "learner_000",
            "attempt_id": "attempt-1",
        },
        fence=fence,
        resume=ContributorResumeState(
            cursor=8,
            last_receipt_id="receipt-learner_000-1",
            last_receipt_sha256="a" * 64,
            last_update_id=last_update_id,
            next_cycle_seq=2,
        ),
    )

    response = read_json(path)
    assert response["format_version"] == ADMISSION_RESPONSE_FORMAT_VERSION == 2
    assert response["resume"]["last_update_id"] == last_update_id
