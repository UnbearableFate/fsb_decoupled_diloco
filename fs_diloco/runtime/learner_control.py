"""Torch-free learner loop control decisions for the Full Protocol."""

from __future__ import annotations

from typing import Any


def configured_global_close_target_visible(config: Any, current: Any) -> bool:
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
    return (
        not isinstance(version, bool) and isinstance(version, int) and int(version) >= int(target)
    )
