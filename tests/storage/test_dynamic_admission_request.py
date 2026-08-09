from __future__ import annotations

from pathlib import Path

from fs_diloco.storage.admission import (
    admission_request_error,
    publish_dynamic_request_with_sha256,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.paths import RunPaths


PLAN03_REQUIREMENTS = frozenset({"DMB-09", "P5-ARCH", "SCHED-01"})


def test_dynamic_request_carries_exact_bootstrap_authorization(tmp_path: Path) -> None:
    path, _digest = publish_dynamic_request_with_sha256(
        RunPaths(tmp_path),
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        instance_id="instance-1",
        stream_id=0,
        bootstrap_slot=0,
        admission_token_sha256="a" * 64,
    )
    request = read_json(path)

    assert request["bootstrap_slot"] == 0
    assert request["launch_request_id"] is None
    assert admission_request_error(request, run_id="run-v4", descriptor_sha256="d" * 64) is None

    request["bootstrap_slot"] = None
    assert admission_request_error(request, run_id="run-v4", descriptor_sha256="d" * 64) == (
        "MalformedAdmissionRequest",
        "dynamic request requires exactly one bootstrap or launch authorization",
    )
