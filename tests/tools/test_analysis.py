from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.tools import analysis


@dataclass(frozen=True)
class _Fence:
    stable_contributor_key: str


def test_summarize_run_projects_current_read_only_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _Fence(stable_contributor_key="learner_000")
    read = SimpleNamespace(
        current_contributor_fences=lambda: (fence,),
        contributor_progress=lambda key: {"stable_contributor_key": key, "data_cursor": 7},
        metadata=lambda: {"run_id": "run-current"},
        integrity_check=lambda: ("ok",),
        controller_status=lambda: "finalized",
        latest_committed_version=lambda: {"version": 3},
        token_ledger_summary=lambda: {"balance": 0},
        terminal_record=lambda: {"state": "finalized", "final_version": 3},
        terminal_contributor_fences=lambda: ({"stable_contributor_key": "learner_000"},),
        syncer_epochs=lambda: ({"epoch": 1, "final_state": "released"},),
        dynamic_streams=lambda: (),
        dynamic_instances=lambda: (),
        dynamic_launch_requests=lambda: (),
        capacity_observations=lambda: (),
        audit_archive_summary=lambda: {"batches": 0},
    )
    closed: list[bool] = []

    class Reader:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.read = read

        def close(self) -> None:
            closed.append(True)

    loaded = SimpleNamespace(
        paths=SimpleNamespace(
            shared_root=tmp_path / "run-current",
            sqlite_db=tmp_path / "authority.sqlite3",
            bootstrap_complete_json=tmp_path / "bootstrap.json",
        ),
        identity=SimpleNamespace(
            run_id="run-current",
            source_fingerprint="sha256:" + "1" * 64,
            config_sha256="2" * 64,
            as_dict=lambda: {
                "run_id": "run-current",
                "source_fingerprint": "sha256:" + "1" * 64,
                "config_sha256": "2" * 64,
            },
        ),
        descriptor={
            "mode": "static",
            "descriptor_sha256": "3" * 64,
            "static_learner_ids": ["learner_000"],
        },
        config=SimpleNamespace(leader=SimpleNamespace(business_busy_timeout_ms=1000)),
    )
    monkeypatch.setattr(analysis, "load_run_descriptor", lambda _root: loaded)
    monkeypatch.setattr(analysis, "AuthorityReader", Reader)

    summary = analysis.summarize_run(tmp_path / "run-current")

    assert closed == [True]
    assert summary["run"]["run_id"] == "run-current"
    assert summary["integrity_check"] == ["ok"]
    assert summary["latest_version"] == {"version": 3}
    assert summary["contributor_progress"] == {
        "learner_000": {"stable_contributor_key": "learner_000", "data_cursor": 7}
    }
    json.dumps(summary)
    analysis.assert_summary(
        summary,
        expected_learners=1,
        expected_global_steps=3,
        require_terminal=True,
    )


def test_analysis_cli_applies_registered_assertions_before_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = {
        "integrity_check": ["ok"],
        "latest_version": {"version": 2},
        "terminal": {"state": "finalized"},
        "terminal_contributor_fences": [{"learner_id": "0"}],
        "current_contributor_fences": [],
    }
    monkeypatch.setattr(analysis, "summarize_run", lambda _root: summary)

    analysis.main(
        [
            str(tmp_path / "run"),
            "--expected-learners",
            "1",
            "--expected-global-steps",
            "2",
            "--require-terminal",
        ]
    )

    assert json.loads(capsys.readouterr().out) == summary
