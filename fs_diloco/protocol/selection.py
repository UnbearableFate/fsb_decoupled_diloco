"""Pure deterministic model for persistent fair contributor service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ._validation import identity, strict_int


@dataclass
class PersistentFairSelector:
    """Select least-served contributors with deterministic persistent ties."""

    committed_service_count: dict[str, int] = field(default_factory=dict)
    last_selected_committed_version: dict[str, int] = field(default_factory=dict)

    def select(self, eligible_keys: Iterable[str], *, quorum_max: int) -> tuple[str, ...]:
        limit = strict_int(quorum_max, name="quorum_max", minimum=1)
        keys = tuple(identity(item, name="stable_contributor_key") for item in eligible_keys)
        if len(set(keys)) != len(keys):
            raise ValueError("eligible contributor keys must be unique after proposal choice")
        ordered = sorted(
            keys,
            key=lambda key: (
                self.committed_service_count.get(key, 0),
                self.last_selected_committed_version.get(key, -1),
                key,
            ),
        )
        # Selection itself intentionally consumes no service credit.
        return tuple(ordered[:limit])

    def commit(self, selected_keys: Iterable[str], *, committed_version: int) -> None:
        version = strict_int(committed_version, name="committed_version", minimum=1)
        keys = tuple(identity(item, name="stable_contributor_key") for item in selected_keys)
        if len(set(keys)) != len(keys):
            raise ValueError("a committed selection cannot contain duplicate contributors")
        if any(version <= self.last_selected_committed_version.get(key, -1) for key in keys):
            raise ValueError("committed service versions must advance monotonically")
        for key in keys:
            self.committed_service_count[key] = self.committed_service_count.get(key, 0) + 1
            self.last_selected_committed_version[key] = version
