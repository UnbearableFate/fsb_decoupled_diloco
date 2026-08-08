"""Stable identities for replayable protocol event tapes."""

from __future__ import annotations

from collections import defaultdict


class DeterministicIds:
    def __init__(self, namespace: str = "test") -> None:
        self.namespace = namespace
        self._counters: dict[str, int] = defaultdict(int)

    def next(self, kind: str) -> str:
        value = self._counters[kind]
        self._counters[kind] += 1
        return f"{self.namespace}-{kind}-{value:06d}"
