from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fs_diloco.runtime.syncer_entrypoint import _admit_before_runtime_import


PLAN03_REQUIREMENTS = frozenset({"P4-MIGRATE", "MODE-02"})
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
            shared=SimpleNamespace(
                membership=SimpleNamespace(
                    mode=mode,
                    registration_scan_interval_seconds=2.0,
                ),
                sync=SimpleNamespace(scan_interval_seconds=0.2),
            )
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
                "import fs_diloco.runtime.syncer_v4; "
                "assert 'torch' not in sys.modules"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
