"""Pure decoders for archived, unsupported Fragment V0 metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


FRAGMENT_INDEX_FORMAT_VERSION = 1


def validate_fragment_index(
    fragment_index: Mapping[str, Any],
    param_index: Mapping[str, Any] | None = None,
) -> None:
    if int(fragment_index.get("format_version", -1)) != FRAGMENT_INDEX_FORMAT_VERSION:
        raise ValueError(
            f"unsupported fragment index format: {fragment_index.get('format_version')}"
        )
    fragments = fragment_index.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        raise ValueError("fragment index must define a non-empty fragment list")
    num_fragments = fragment_index.get("num_fragments")
    if isinstance(num_fragments, bool) or not isinstance(num_fragments, int):
        raise ValueError("fragment index num_fragments must be an integer")
    if num_fragments != len(fragments):
        raise ValueError("fragment index num_fragments does not match fragments")
    expected_ids = list(range(num_fragments))
    actual_ids = [int(fragment.get("fragment_id", -1)) for fragment in fragments]
    if actual_ids != expected_ids:
        raise ValueError("fragment IDs must be contiguous and ordered from zero")
    total_numel = fragment_index.get("total_numel")
    if isinstance(total_numel, bool) or not isinstance(total_numel, int) or total_numel < 1:
        raise ValueError("fragment index total_numel must be positive")
    covered: list[tuple[int, int]] = []
    for fragment in fragments:
        slices = fragment.get("slices")
        if not isinstance(slices, list) or not slices:
            raise ValueError("every fragment must define non-empty slices")
        computed = 0
        for item in slices:
            start = int(item["flat_start"])
            end = int(item["flat_end"])
            if start < 0 or end <= start or end > total_numel:
                raise ValueError("fragment slice is outside the flat parameter vector")
            covered.append((start, end))
            computed += end - start
        if int(fragment.get("numel", -1)) != computed:
            raise ValueError("fragment numel does not match its slices")
    cursor = 0
    for start, end in sorted(covered):
        if start != cursor:
            raise ValueError("fragment slices must cover the flat vector exactly once")
        cursor = end
    if cursor != total_numel:
        raise ValueError("fragment slices do not cover the full parameter vector")
    if param_index is not None and int(param_index.get("total_numel", -1)) != total_numel:
        raise ValueError("fragment and parameter indexes disagree on total_numel")


def load_fragment_index(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fragment index must contain an object")
    validate_fragment_index(payload)
    return payload


def fragment_size_summary(fragment_index: Mapping[str, Any]) -> dict[str, float | int]:
    validate_fragment_index(fragment_index)
    sizes = [int(fragment["numel"]) for fragment in fragment_index["fragments"]]
    minimum = min(sizes)
    maximum = max(sizes)
    mean = sum(sizes) / len(sizes)
    return {
        "min": minimum,
        "max": maximum,
        "mean": mean,
        "imbalance_ratio": 0.0 if mean == 0.0 else (maximum - minimum) / mean,
    }


def expected_fragment_versions_after_events(
    num_fragments: int,
    global_merge_events: int,
) -> dict[int, int]:
    if num_fragments < 1 or global_merge_events < 0:
        raise ValueError("fragment count must be positive and event count non-negative")
    return {
        fragment_id: global_merge_events // num_fragments
        + (1 if fragment_id < global_merge_events % num_fragments else 0)
        for fragment_id in range(num_fragments)
    }
