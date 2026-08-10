from __future__ import annotations

from pathlib import Path

import pytest

from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.terminal_request import (
    publish_manual_terminal_request,
    read_manual_terminal_request,
)



def test_manual_terminal_request_is_immutable_identity_bound_and_replayable(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path)
    first = publish_manual_terminal_request(
        paths,
        run_id="run-current",
        descriptor_sha256="d" * 64,
        reason="operator maintenance",
        created_at=100.0,
    )
    replay = publish_manual_terminal_request(
        paths,
        run_id="run-current",
        descriptor_sha256="d" * 64,
        reason="operator maintenance",
        created_at=100.0,
    )

    assert replay == first
    assert read_manual_terminal_request(paths, run_id="run-current", descriptor_sha256="d" * 64) == first
    assert (
        read_manual_terminal_request(paths, run_id="another-run", descriptor_sha256="d" * 64)
        is None
    )


def test_manual_terminal_request_rejects_control_characters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="printable"):
        publish_manual_terminal_request(
            RunPaths(tmp_path),
            run_id="run-current",
            descriptor_sha256="d" * 64,
            reason="unsafe\nreason",
            created_at=100.0,
        )
