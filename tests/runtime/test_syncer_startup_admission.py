from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.runtime import syncer_entrypoint
from fs_diloco.runtime.syncer_entrypoint import _admit_before_runtime_import
from fs_diloco.storage.authority import AuthoritySchemaError
from fs_diloco.storage.leader_lease import StaleLeaderTokenError


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


def _loaded(mode: str = "static"):
    return SimpleNamespace(
        config=SimpleNamespace(
            membership=SimpleNamespace(
                mode=mode,
                registration_scan_interval_seconds=2.0,
            ),
            sync=SimpleNamespace(scan_interval_seconds=0.2),
        ),
        descriptor={
            "static_learner_ids": ["learner_000", "learner_001"],
            "bootstrap_slots": 2,
        },
    )


def test_initial_static_requests_are_admitted_before_runtime_import() -> None:
    fences: list[SimpleNamespace] = []
    authority = SimpleNamespace(
        read=SimpleNamespace(current_contributor_fences=lambda: tuple(fences))
    )
    calls = 0

    def admit(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            fences.extend(
                [
                    SimpleNamespace(stable_contributor_key="learner_000"),
                    SimpleNamespace(stable_contributor_key="learner_001"),
                ]
            )

    clock = _Clock()
    telemetry = _Telemetry()
    _admit_before_runtime_import(
        _loaded(),
        authority,
        object(),
        telemetry,
        admit=admit,
        monotonic_clock=clock.monotonic,
        sleep=clock.sleep,
    )

    assert calls == 2
    assert clock.now == 0.2
    assert telemetry.events[-1][0] == "initial_admission_ready_before_runtime_import"


def test_bounded_startup_window_leaves_late_requests_for_main_loop() -> None:
    authority = SimpleNamespace(read=SimpleNamespace(current_contributor_fences=lambda: ()))
    clock = _Clock()
    telemetry = _Telemetry()
    calls = 0

    def admit(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1

    _admit_before_runtime_import(
        _loaded(),
        authority,
        object(),
        telemetry,
        admit=admit,
        monotonic_clock=clock.monotonic,
        sleep=clock.sleep,
    )

    assert calls == 6
    assert clock.now == 1.0
    assert telemetry.events[-1] == (
        "initial_admission_window_expired",
        {"admitted_count": 0, "window_seconds": 1.0},
    )


def test_syncer_admission_modules_load_without_torch() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import fs_diloco.storage.authority; "
                "import fs_diloco.runtime.syncer_entrypoint; "
                "import fs_diloco.runtime.syncer; "
                "assert 'torch' not in sys.modules"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.parametrize("body_failure", [False, True])
def test_candidate_cleanup_preserves_primary_failure_and_surfaces_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_failure: bool
) -> None:
    config_path = tmp_path / "resolved.yaml"
    config_path.write_text("config_schema_version: 1\n", encoding="utf-8")
    token = SimpleNamespace(epoch=1, owner_id="owner-1")
    closed: list[bool] = []
    release_calls: list[bool] = []
    failure_calls: list[bool] = []

    class FakeAuthority:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def acquire_leader(self, **_kwargs):
            return token

        def open_leader(self, _token):
            return SimpleNamespace()

        def committed_leader_lease(self, _token):
            return SimpleNamespace()

        def release_leader(self, _token) -> None:
            release_calls.append(True)
            raise StaleLeaderTokenError("release crossed the safety boundary")

        def fail_leader(self, _token) -> None:
            failure_calls.append(True)
            raise RuntimeError("error fencing failed")

        def close(self) -> None:
            closed.append(True)

    class FakeControl:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def publish_heartbeat(self, _lease) -> None:
            pass

        def publish_error(self, **_kwargs) -> None:
            pass

    class FakeRenewer:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    loaded = SimpleNamespace(
        paths=SimpleNamespace(
            resolved_config_yaml=config_path,
            sqlite_db=tmp_path / "authority.sqlite3",
            bootstrap_complete_json=tmp_path / "bootstrap_complete.json",
            shared_root=tmp_path,
            actor_metrics_path=lambda *_args: tmp_path / "syncer.jsonl",
        ),
        config=SimpleNamespace(
            membership=SimpleNamespace(mode="static"),
            leader=SimpleNamespace(
                lease_duration_seconds=30.0,
                max_clock_skew_seconds=1.0,
                business_busy_timeout_ms=5000,
                candidate_wait_seconds=60.0,
                candidate_acquire_poll_seconds=1.0,
                renew_interval_seconds=5.0,
            ),
            maintenance=SimpleNamespace(
                publication_orphan_grace_seconds=40.0,
                quarantine_records_per_contributor=64,
            ),
        ),
        descriptor={"static_learner_ids": ["learner_000"], "bootstrap_slots": 1},
        identity=SimpleNamespace(
            as_dict=lambda: {
                "run_id": "run-current",
                "source_fingerprint": "source",
                "config_sha256": "a" * 64,
            }
        ),
    )
    monkeypatch.setattr(syncer_entrypoint, "load_run_descriptor", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(syncer_entrypoint, "_LeaseRenewer", FakeRenewer)
    monkeypatch.setattr(
        syncer_entrypoint, "_admit_before_runtime_import", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        syncer_entrypoint,
        "ActorTelemetryWriter",
        lambda *_args, **_kwargs: SimpleNamespace(event=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr("fs_diloco.storage.authority.LeaderAuthority", FakeAuthority)
    monkeypatch.setattr("fs_diloco.storage.control.ControlPublisher", FakeControl)
    monkeypatch.setattr("fs_diloco.runtime.syncer._admit_requests", lambda *_args, **_kwargs: None)

    def run_candidate(*_args, **_kwargs) -> None:
        if body_failure:
            raise AuthoritySchemaError("primary candidate failure")

    monkeypatch.setattr("fs_diloco.runtime.syncer.run_fenced_syncer", run_candidate)

    expected_type = AuthoritySchemaError if body_failure else StaleLeaderTokenError
    expected_message = "primary candidate failure" if body_failure else "release crossed"
    with pytest.raises(expected_type, match=expected_message):
        syncer_entrypoint.main(["--config", str(config_path), "--shared-root", str(tmp_path)])

    assert closed == [True]
    assert failure_calls == ([True] if body_failure else [])
    assert release_calls == ([] if body_failure else [True])
