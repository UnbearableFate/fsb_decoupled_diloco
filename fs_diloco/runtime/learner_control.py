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
