"""Exercise learner completion decisions without importing the training runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs_diloco.runtime.learner_control import (
    completed_local_steps_from_cycle,
    configured_global_close_target_visible,
)


def _config(
    *,
    policy: str = "global_target",
    target: int | None = 2,
    completion_mode: str = "global_only",
):
    """Build the control-only subset needed for close-target decisions."""

    return SimpleNamespace(
        terminal=SimpleNamespace(admission_close_policy=policy),
        sync=SimpleNamespace(stop_after_outer_steps=target),
        training=SimpleNamespace(
            completion_mode=completion_mode,
            max_local_steps=2000 if completion_mode == "local_and_global" else None,
        ),
    )


@pytest.mark.parametrize("version", (2, 3))
def test_configured_target_latest_enters_await_close_without_requiring_drain(
    version: int,
) -> None:
    """A visible configured global target is sufficient for global-only completion."""

    current = SimpleNamespace(latest={"version": version}, drain=None, terminal=None)

    assert (
        configured_global_close_target_visible(_config(), current, completed_local_steps=0) is True
    )


@pytest.mark.parametrize("policy", ("manual", "deadline"))
def test_non_global_close_policy_does_not_stop_training_from_latest_alone(policy: str) -> None:
    """Manual and deadline policies must ignore a global target without their own trigger."""

    current = SimpleNamespace(latest={"version": 100}, drain=None, terminal=None)

    assert (
        configured_global_close_target_visible(
            _config(policy=policy), current, completed_local_steps=0
        )
        is False
    )


def test_missing_or_pre_target_latest_does_not_enter_await_close() -> None:
    """Missing, disabled, or pre-target global state must keep the learner active."""

    assert configured_global_close_target_visible(_config(), None, completed_local_steps=0) is False
    assert (
        configured_global_close_target_visible(
            _config(),
            SimpleNamespace(latest=None, drain=None, terminal=None),
            completed_local_steps=0,
        )
        is False
    )
    assert (
        configured_global_close_target_visible(
            _config(),
            SimpleNamespace(latest={"version": 1}, drain=None, terminal=None),
            completed_local_steps=0,
        )
        is False
    )
    assert (
        configured_global_close_target_visible(
            _config(target=None),
            SimpleNamespace(latest={"version": 100}),
            completed_local_steps=0,
        )
        is False
    )


def test_local_and_global_completion_waits_for_the_exact_local_horizon() -> None:
    """A reached global target must not truncate the configured local workload."""

    current = SimpleNamespace(latest={"version": 10}, drain=None, terminal=None)
    config = _config(target=10, completion_mode="local_and_global")

    assert (
        configured_global_close_target_visible(
            config,
            current,
            completed_local_steps=1999,
        )
        is False
    )
    assert (
        configured_global_close_target_visible(
            config,
            current,
            completed_local_steps=2000,
        )
        is True
    )


def test_replacement_resumes_the_stable_stream_local_step_coordinate() -> None:
    """A replacement must finish the existing stream horizon instead of restarting it."""

    assert completed_local_steps_from_cycle(next_cycle_seq=1, inner_steps=200) == 0
    assert completed_local_steps_from_cycle(next_cycle_seq=4, inner_steps=200) == 600
    assert completed_local_steps_from_cycle(next_cycle_seq=11, inner_steps=200) == 2000
