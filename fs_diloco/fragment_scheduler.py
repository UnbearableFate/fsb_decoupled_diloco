"""Fragment scheduling policies."""

from __future__ import annotations


def select_fragment(index: int, num_fragments: int, *, schedule: str = "round_robin_global") -> int:
    if num_fragments < 1:
        raise ValueError("num_fragments must be >= 1")
    if index < 0:
        raise ValueError("index must be >= 0")
    if schedule != "round_robin_global":
        raise ValueError(f"unsupported fragment schedule: {schedule}")
    return int(index) % int(num_fragments)


def expected_fragment_versions_after_events(
    num_fragments: int,
    global_merge_events: int,
    *,
    schedule: str = "round_robin_global",
) -> dict[int, int]:
    versions = {fragment_id: 0 for fragment_id in range(num_fragments)}
    for event_index in range(global_merge_events):
        versions[select_fragment(event_index, num_fragments, schedule=schedule)] += 1
    return versions
