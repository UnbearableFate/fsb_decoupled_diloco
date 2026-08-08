from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import time

import pytest

from scripts.miyabi import plan03_p0_performance as performance


def _trial(pair: int, arm: str, version: int = 2, tokens: int = 256) -> dict[str, object]:
    return {
        "pair": pair,
        "arm": arm,
        "workload": {"final_version": version, "total_seen_tokens": tokens},
    }


def test_measured_workload_validation_rejects_mismatch_and_duplicate() -> None:
    matched = [_trial(pair, arm) for pair in range(2) for arm in ("classic", "static_ha")]
    assert performance._validate_measured_workloads(matched, pairs=2) == (2, 256)

    mismatched = [dict(row) for row in matched]
    mismatched[-1] = _trial(1, "static_ha", version=3)
    with pytest.raises(RuntimeError, match="workload mismatch"):
        performance._validate_measured_workloads(mismatched, pairs=2)

    duplicate = matched[:-1] + [_trial(1, "classic")]
    with pytest.raises(RuntimeError, match="coverage mismatch|duplicate"):
        performance._validate_measured_workloads(duplicate, pairs=2)


def test_performance_runner_loads_source_identity_without_sys_path_mutation() -> None:
    before = list(sys.path)
    source = performance._capture_source_identity(Path(__file__).resolve().parents[1])

    assert sys.path == before
    assert len(str(source["git_commit"])) == 40
    assert str(source["source_fingerprint"]).startswith("sha256:")


def test_process_result_collection_does_not_pipe_block_on_large_output(tmp_path: Path) -> None:
    roles = ("one", "two", "three")
    handles = []
    processes = []
    try:
        for role in roles:
            handle = (tmp_path / f"{role}.log").open("w", encoding="utf-8")
            handles.append(handle)
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import sys; sys.stdout.write('x\\n' * 100000)"],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        results = performance._collect_process_results(
            roles,
            processes,
            log_dir=tmp_path,
            deadline=time.monotonic() + 30.0,
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        for handle in handles:
            handle.close()

    assert [row["returncode"] for row in results] == [0, 0, 0]
    assert all(row["output_tail"] == ["x"] * 20 for row in results)


def test_ha_arm_timer_starts_before_initializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    clock = iter((10.0, 10.5, 12.0, 15.0))

    def monotonic() -> float:
        events.append("clock")
        return next(clock)

    def run(*_args, **_kwargs):
        events.append("init")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(performance.time, "monotonic", monotonic)
    monkeypatch.setattr(performance.subprocess, "run", run)
    monkeypatch.setattr(
        performance,
        "_run_processes",
        lambda *_args, **_kwargs: [
            {"role": role, "returncode": 0, "output_tail": []}
            for role in ("syncer", "learner_000", "learner_001")
        ],
    )
    monkeypatch.setattr(
        performance,
        "_authority_summary",
        lambda _root: {"final_version": 2, "total_seen_tokens": 256, "integrity": ["ok"]},
    )
    monkeypatch.setattr(performance.shutil, "rmtree", lambda _path: None)

    result = performance._run_arm(
        tmp_path,
        tmp_path / "scratch",
        arm="static_ha",
        config=tmp_path / "config.yaml",
        environment={},
        pair=0,
        warmup=False,
        timeout_seconds=90.0,
    )

    assert events[0] == "clock"
    assert events.index("init") > 0
    assert result["elapsed_seconds"] == 5.0
    assert result["timing"] == {
        "pre_actor_initialization_seconds": 2.0,
        "actor_process_seconds": 3.0,
    }
