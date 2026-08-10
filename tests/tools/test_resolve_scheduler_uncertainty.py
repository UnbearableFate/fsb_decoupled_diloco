from __future__ import annotations

import json
from pathlib import Path

import pytest

from fs_diloco.tools.resolve_scheduler_uncertainty import main


def test_scheduler_resolution_is_dry_run_by_default_and_apply_is_create_no_replace(
    tmp_path: Path, capsys
) -> None:
    arguments = [
        "--shared-root",
        str(tmp_path),
        "--launch-request-id",
        "launch-1",
        "--action",
        "mark_failed",
        "--expected-state-sha256",
        "a" * 64,
        "--reason",
        "operator verified accounting history",
    ]
    main(arguments)
    dry_run = json.loads(capsys.readouterr().out)
    target = Path(dry_run["target"])
    assert dry_run["mode"] == "dry-run"
    assert not target.exists()

    main([*arguments, "--apply"])
    applied = json.loads(capsys.readouterr().out)
    assert applied["created"] is True
    assert target.stat().st_mode & 0o222 == 0
    main([*arguments, "--apply"])
    replay = json.loads(capsys.readouterr().out)
    assert replay["created"] is False


def test_scheduler_resolution_module_has_no_db_pbs_or_cancel_capability() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "fs_diloco/tools/resolve_scheduler_uncertainty.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("sqlite3", "qstat", "qsub", "qdel", "admit_dynamic"):
        assert forbidden not in source


def test_scheduler_resolution_refuses_broken_symlink_request_collision(
    tmp_path: Path, capsys
) -> None:
    arguments = [
        "--shared-root",
        str(tmp_path),
        "--launch-request-id",
        "launch-1",
        "--action",
        "mark_failed",
        "--expected-state-sha256",
        "a" * 64,
        "--reason",
        "operator verified accounting history",
    ]
    main(arguments)
    target = Path(json.loads(capsys.readouterr().out)["target"])
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing-request")

    with pytest.raises(FileExistsError, match="symlink collision"):
        main([*arguments, "--apply"])
    assert target.is_symlink()
