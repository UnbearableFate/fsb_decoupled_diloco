"""Verify syncer pre-import admission readiness and candidate cleanup."""

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
    """Provide deterministic startup polling time."""

    def __init__(self) -> None:
        """Start the monotonic clock at zero."""

        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current monotonic timestamp."""

        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance deterministic time instead of blocking."""

        self.now += seconds


class _Telemetry:
    """Record startup telemetry events in call order."""

    def __init__(self) -> None:
        """Start with no recorded events."""

        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        """Append one structured startup event."""

        self.events.append((name, fields))


def _loaded():
    """Build the minimal unique-protocol startup descriptor fixture."""

    return SimpleNamespace(
        config=SimpleNamespace(
            membership=SimpleNamespace(
                registration_scan_interval_seconds=2.0,
            ),
            sync=SimpleNamespace(scan_interval_seconds=0.2),
        ),
        descriptor={
            "stream_pool_size": 2,
            "bootstrap_slots": 2,
        },
    )


def test_initial_requests_are_admitted_before_runtime_import() -> None:
    """Bootstrap readiness is reached before importing the training runtime."""

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
    """A bounded startup window leaves late admission requests to the main loop."""

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
    """Authority and admission startup modules remain Torch-free."""

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
    """Candidate cleanup preserves a primary failure and otherwise surfaces release loss."""

    config_path = tmp_path / "resolved.yaml"
    config_path.write_text("config_schema_version: 2\n", encoding="utf-8")
    token = SimpleNamespace(epoch=1, owner_id="owner-1")
    closed: list[bool] = []
    release_calls: list[bool] = []
    failure_calls: list[bool] = []

    class FakeAuthority:
        """Model authority lifecycle and injected cleanup failures."""

        def __init__(self, *_args, **_kwargs) -> None:
            """Accept the production constructor without side effects."""

            pass

        def acquire_leader(self, **_kwargs):
            """Return the stable fake leader token."""

            return token

        def open_leader(self, _token):
            """Return one inert leader session."""

            return SimpleNamespace()

        def committed_leader_lease(self, _token):
            """Return one inert committed lease."""

            return SimpleNamespace()

        def release_leader(self, _token) -> None:
            """Record release and inject a stale-token failure."""

            release_calls.append(True)
            raise StaleLeaderTokenError("release crossed the safety boundary")

        def fail_leader(self, _token) -> None:
            """Record error fencing and inject its secondary failure."""

            failure_calls.append(True)
            raise RuntimeError("error fencing failed")

        def close(self) -> None:
            """Record authority closure."""

            closed.append(True)

    class FakeControl:
        """Provide inert control publication methods for candidate startup."""

        def __init__(self, *_args, **_kwargs) -> None:
            """Accept the production constructor without side effects."""

            pass

        def publish_heartbeat(self, _lease) -> None:
            """Accept a heartbeat publication."""

            pass

        def publish_error(self, **_kwargs) -> None:
            """Accept an error publication."""

            pass

    class FakeRenewer:
        """Provide an inert lease-renewer lifecycle."""

        def __init__(self, **_kwargs) -> None:
            """Accept the production constructor without side effects."""

            pass

        def start(self) -> None:
            """Accept renewer startup."""

            pass

        def stop(self) -> None:
            """Accept renewer shutdown."""

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
            membership=SimpleNamespace(),
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
        descriptor={"stream_pool_size": 1, "bootstrap_slots": 1},
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
        """Optionally inject the primary candidate-body failure."""

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
