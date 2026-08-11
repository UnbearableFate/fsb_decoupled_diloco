"""Verify the sole filesystem admission request and response wire protocol."""

from __future__ import annotations

from pathlib import Path

from fs_diloco.protocol.contributor import ContributorFence
from fs_diloco.protocol.data_cursor import ContributorResumeState
from fs_diloco.storage.admission import (
    ADMISSION_RESPONSE_FORMAT_VERSION,
    admission_request_error,
    publish_admission_request_with_sha256,
    publish_admission_response,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.paths import RunPaths


def test_request_carries_exact_bootstrap_authorization(tmp_path: Path) -> None:
    """A bootstrap request carries exactly one admission authorization source."""

    path, _digest = publish_admission_request_with_sha256(
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
    assert "mode" not in request
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
        "request requires exactly one bootstrap or launch authorization",
    )


def test_request_rejects_legacy_mode_and_static_identity_fields(tmp_path: Path) -> None:
    """Removed mode and static identity fields are rejected as unknown wire data."""

    path, _digest = publish_admission_request_with_sha256(
        RunPaths(tmp_path),
        run_id="run-current",
        descriptor_sha256="d" * 64,
        instance_id="instance-1",
        stream_id=0,
        bootstrap_slot=0,
        admission_token_sha256="a" * 64,
    )
    for field, value in (("mode", "dynamic"), ("learner_id", "learner-0")):
        request = read_json(path)
        request[field] = value
        error = admission_request_error(
            request,
            run_id="run-current",
            descriptor_sha256="d" * 64,
        )
        assert error is not None
        assert error[0] == "MalformedAdmissionRequest"


def test_admission_response_carries_last_planned_update_identity(tmp_path: Path) -> None:
    """Admission recovery preserves the final planned update and current fence exactly."""

    fence = ContributorFence(
        instance_id="instance-1",
        placement_id="placement-1",
        placement_epoch=1,
        stream_id=0,
        stream_epoch=2,
        admission_generation=2,
        admission_token_sha256="a" * 64,
    )
    last_update_id = "00000000-0000-4000-8000-000000000001"
    path = publish_admission_response(
        RunPaths(tmp_path),
        epoch=1,
        owner_id="owner-1",
        request={
            "run_id": "run-current",
            "descriptor_sha256": "d" * 64,
            "instance_id": "instance-1",
            "stream_id": 0,
        },
        fence=fence,
        resume=ContributorResumeState(
            cursor=8,
            last_receipt_id="receipt-0-1",
            last_receipt_sha256="a" * 64,
            last_update_id=last_update_id,
            next_cycle_seq=2,
        ),
    )

    response = read_json(path)
    assert response["format_version"] == ADMISSION_RESPONSE_FORMAT_VERSION == 3
    assert response["fence"] == fence.as_dict()
    assert response["resume"]["last_update_id"] == last_update_id
