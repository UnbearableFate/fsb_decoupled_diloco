from __future__ import annotations

from types import SimpleNamespace

import pytest

from fs_diloco.runtime.learner_control import configured_global_close_target_visible


def _config(*, policy: str = "global_target", target: int | None = 2):
    return SimpleNamespace(
        terminal=SimpleNamespace(admission_close_policy=policy),
        sync=SimpleNamespace(stop_after_outer_steps=target),
    )


@pytest.mark.parametrize("version", (2, 3))
def test_configured_target_latest_enters_await_close_without_requiring_drain(
    version: int,
) -> None:
    current = SimpleNamespace(latest={"version": version}, drain=None, terminal=None)

    assert configured_global_close_target_visible(_config(), current) is True


@pytest.mark.parametrize("policy", ("manual", "deadline"))
def test_non_global_close_policy_does_not_stop_training_from_latest_alone(policy: str) -> None:
    current = SimpleNamespace(latest={"version": 100}, drain=None, terminal=None)

    assert configured_global_close_target_visible(_config(policy=policy), current) is False


def test_missing_or_pre_target_latest_does_not_enter_await_close() -> None:
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
