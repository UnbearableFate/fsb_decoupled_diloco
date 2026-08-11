"""Torch-free learner loop control decisions for the Full Protocol."""

from __future__ import annotations

from typing import Any


def completed_local_steps_from_cycle(*, next_cycle_seq: int, inner_steps: int) -> int:
    """Recover the stable stream's completed optimizer steps from its next cycle."""

    if next_cycle_seq < 1 or inner_steps < 1:
        raise ValueError("cycle sequence and inner steps must be positive")
    return (next_cycle_seq - 1) * inner_steps


def configured_global_close_target_visible(
    config: Any,
    current: Any,
    *,
    completed_local_steps: int,
) -> bool:
    """Return whether a current latest requires waiting for leader-owned close.

    The learner must remain alive to acknowledge the eventual drain, but once a
    durable configured global target is visible it must not begin another data
    cycle while the leader advances from latest publication to terminal close.
    """

    if config.terminal.admission_close_policy not in {
        "global_target",
        "global_target_or_launch_budget",
    }:
        return False
    target = config.sync.stop_after_outer_steps
    if target is None or current is None or current.latest is None:
        return False
    version = current.latest.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or int(version) < int(target):
        return False
    if config.training.completion_mode != "local_and_global":
        return True
    local_target = config.training.max_local_steps
    if local_target is None:
        raise RuntimeError("local_and_global completion has no local step target")
    return completed_local_steps >= int(local_target)
