"""Injectable wall and monotonic clocks for deterministic recovery tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VirtualClock:
    monotonic_seconds: float = 0.0
    wall_seconds: float = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def wall(self) -> float:
        return self.wall_seconds

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("virtual time cannot move backwards")
        self.monotonic_seconds += float(seconds)
        self.wall_seconds += float(seconds)

    def jump_wall(self, seconds: float) -> None:
        """Model an audit-clock correction without changing timeout time."""
        self.wall_seconds += float(seconds)
