"""Exercise learner completion decisions without importing the training runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs_diloco.runtime.learner_control import (
    completed_local_steps_from_cycle,
    configured_global_close_target_visible,
)
from fs_diloco.storage import control as control_module


def _config(
    *,
    policy: str = "global_target",
    target: int | None = 2,
):
    """Build the control-only subset needed for close-target decisions."""

    return SimpleNamespace(
        terminal=SimpleNamespace(admission_close_policy=policy),
        sync=SimpleNamespace(stop_after_outer_steps=target),
    )


@pytest.mark.parametrize("version", (2, 3))
def test_configured_target_latest_enters_await_close_without_requiring_drain(
    version: int,
) -> None:
    """A visible configured global target is sufficient for global-only completion."""

    current = SimpleNamespace(latest={"version": version}, drain=None, terminal=None)

    assert configured_global_close_target_visible(_config(), current) is True


@pytest.mark.parametrize("policy", ("manual", "deadline"))
def test_non_global_close_policy_does_not_stop_training_from_latest_alone(policy: str) -> None:
    """Manual and deadline policies must ignore a global target without their own trigger."""

    current = SimpleNamespace(latest={"version": 100}, drain=None, terminal=None)

    assert configured_global_close_target_visible(_config(policy=policy), current) is False


def test_missing_or_pre_target_latest_does_not_enter_await_close() -> None:
    """Missing, disabled, or pre-target global state must keep the learner active."""

    assert configured_global_close_target_visible(_config(), None) is False
    assert (
        configured_global_close_target_visible(
            _config(), SimpleNamespace(latest=None, drain=None, terminal=None)
        )
        is False
    )
    assert (
        configured_global_close_target_visible(
            _config(), SimpleNamespace(latest={"version": 1}, drain=None, terminal=None)
        )
        is False
    )
    assert (
        configured_global_close_target_visible(
            _config(target=None), SimpleNamespace(latest={"version": 100})
        )
        is False
    )


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
