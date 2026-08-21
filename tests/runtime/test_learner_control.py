"""Exercise learner completion decisions without importing the training runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs_diloco.runtime.learner_control import (
    completed_local_steps_from_cycle,
    configured_global_work_target_reached,
)
from fs_diloco.storage import control as control_module


def _config(
    *,
    target: int | None = 2,
):
    """Build the control-only subset needed for work-target decisions."""

    return SimpleNamespace(sync=SimpleNamespace(stop_after_outer_steps=target))


@pytest.mark.parametrize(("version", "expected"), [(1, False), (2, True), (3, True)])
def test_configured_work_target_compares_the_committed_version(
    version: int,
    expected: bool,
) -> None:
    """Syncer manual-close waiting starts only at the configured work horizon."""

    assert configured_global_work_target_reached(_config(), version) is expected
    assert configured_global_work_target_reached(_config(target=None), version) is False


def test_replacement_resumes_the_stable_stream_local_step_coordinate() -> None:
    """A replacement must finish the existing stream horizon instead of restarting it."""

    assert completed_local_steps_from_cycle(next_cycle_seq=1, inner_steps=200) == 0
    assert completed_local_steps_from_cycle(next_cycle_seq=4, inner_steps=200) == 600
    assert completed_local_steps_from_cycle(next_cycle_seq=11, inner_steps=200) == 2000


def test_initial_latest_wait_returns_terminal_as_normal_control_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal close during model startup must not escape as an iterator exception."""

    terminal = {"state": "finalized", "final_version": 10}
    monkeypatch.setattr(
        control_module,
        "read_current_control",
        lambda *_args, **_kwargs: SimpleNamespace(terminal=terminal, latest=None),
    )

    result = control_module.wait_for_current_latest(
        SimpleNamespace(),
        run_id="run-current",
        timeout_seconds=1.0,
        poll_seconds=0.01,
        max_clock_skew_seconds=0.0,
    )

    assert result.kind == "terminal"
    assert result.payload == terminal
