"""Torch-free learner loop control decisions for the Full Protocol."""

from __future__ import annotations

from typing import Any


def completed_local_steps_from_cycle(*, next_cycle_seq: int, inner_steps: int) -> int:
    """Recover the stable stream's completed optimizer steps from its next cycle."""

    if next_cycle_seq < 1 or inner_steps < 1:
        raise ValueError("cycle sequence and inner steps must be positive")
    return (next_cycle_seq - 1) * inner_steps


def configured_global_work_target_reached(config: Any, version: int) -> bool:
    """Return whether one committed version reaches the configured work horizon."""

    target = config.sync.stop_after_outer_steps
    return target is not None and int(version) >= int(target)


def configured_global_close_target_visible(
    config: Any,
    current: Any,
) -> bool:
    """Stop new learner cycles at the work horizon while preserving drain liveness.

    Terminal policy owns when authority closes, but it cannot authorize work past
    the configured global horizon. The learner therefore remains alive to
    acknowledge a later manual or automatic drain without starting another cycle.
    """

    if current is None or current.latest is None:
        return False
    version = current.latest.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return configured_global_work_target_reached(config, version)
