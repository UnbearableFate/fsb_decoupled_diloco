"""Ordered, single-use fault injection without monkeypatch timing races."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from typing import Any


class FaultTape:
    def __init__(self, events: Iterable[tuple[str, BaseException | Any]] = ()) -> None:
        self._events: dict[str, deque[BaseException | Any]] = defaultdict(deque)
        for point, outcome in events:
            self._events[point].append(outcome)
        self.observed: list[str] = []

    def at(self, point: str, default: Any = None) -> Any:
        self.observed.append(point)
        outcomes = self._events[point]
        if not outcomes:
            return default
        outcome = outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def exhausted(self) -> bool:
        return all(not outcomes for outcomes in self._events.values())
