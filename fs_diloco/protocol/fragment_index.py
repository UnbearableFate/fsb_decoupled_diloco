"""Fragment index construction for flat trainable parameter vectors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..storage.atomic_io import atomic_write_json, read_json
from ..core.constants import FORMAT_VERSION


def _slice_from_param(entry: dict[str, Any]) -> dict[str, Any]:
    offset = int(entry["offset"])
    numel = int(entry["numel"])
    return {
        "param_name": entry["name"],
        "param_offset": 0,
        "param_numel": numel,
        "flat_start": offset,
        "flat_end": offset + numel,
        "shape": list(entry["shape"]),
        "dtype": entry["dtype"],
    }


def _fragment_payload(fragment_id: int, slices: list[dict[str, Any]]) -> dict[str, Any]:
    slices = sorted(slices, key=lambda item: (int(item["flat_start"]), item["param_name"]))
    numel = sum(int(item["flat_end"]) - int(item["flat_start"]) for item in slices)
    return {
        "fragment_id": fragment_id,
        "numel": numel,
        "size_bytes_float32": numel * 4,
        "slices": slices,
    }


def build_fragment_index(
    param_index: dict[str, Any],
    *,
    strategy: str = "full",
    num_fragments: int = 1,
    source_param_index_path: str | Path | None = None,
) -> dict[str, Any]:
    if int(param_index.get("format_version", -1)) != FORMAT_VERSION:
        raise ValueError(f"unsupported param index format: {param_index.get('format_version')}")
    if num_fragments < 1:
        raise ValueError("num_fragments must be >= 1")
    params = list(param_index.get("params") or [])
    if not params:
        raise ValueError("cannot build fragments for an empty parameter index")
    strategy = strategy.lower()
    if strategy == "full":
        if num_fragments != 1:
            raise ValueError("full fragment strategy requires num_fragments=1")
        fragments = [_fragment_payload(0, [_slice_from_param(entry) for entry in params])]
    elif strategy == "balanced_tensor":
        if num_fragments > len(params):
            raise ValueError("balanced_tensor requires num_fragments <= trainable tensor count")
        buckets: list[list[dict[str, Any]]] = [[] for _ in range(num_fragments)]
        bucket_numels = [0 for _ in range(num_fragments)]
        sorted_params = sorted(params, key=lambda item: (-int(item["numel"]), int(item["offset"]), item["name"]))
        for entry in sorted_params:
            target = min(range(num_fragments), key=lambda index: (bucket_numels[index], index))
            buckets[target].append(_slice_from_param(entry))
            bucket_numels[target] += int(entry["numel"])
        fragments = [_fragment_payload(index, bucket) for index, bucket in enumerate(buckets)]
    else:
        raise ValueError(f"unsupported fragment strategy: {strategy}")

    payload = {
        "format_version": FORMAT_VERSION,
        "strategy": strategy,
        "num_fragments": num_fragments,
        "total_numel": int(param_index["total_numel"]),
        "source_param_index_path": str(source_param_index_path) if source_param_index_path is not None else None,
        "fragments": fragments,
    }
    validate_fragment_index(payload, param_index)
    return payload


def validate_fragment_index(fragment_index: dict[str, Any], param_index: dict[str, Any] | None = None) -> None:
    if int(fragment_index.get("format_version", -1)) != FORMAT_VERSION:
        raise ValueError(f"unsupported fragment index format: {fragment_index.get('format_version')}")
    fragments = fragment_index.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        raise ValueError("fragment index must contain non-empty fragments")
    expected_ids = list(range(int(fragment_index["num_fragments"])))
    found_ids = [int(fragment["fragment_id"]) for fragment in fragments]
    if found_ids != expected_ids:
        raise ValueError(f"fragment ids must be contiguous from 0: {found_ids}")

    total_numel = int(fragment_index["total_numel"])
    covered: list[tuple[int, int, str]] = []
    for fragment in fragments:
        if int(fragment["numel"]) <= 0:
            raise ValueError(f"fragment {fragment['fragment_id']} is empty")
        slice_numel = 0
        for item in fragment.get("slices") or []:
            start = int(item["flat_start"])
            end = int(item["flat_end"])
            if start < 0 or end <= start or end > total_numel:
                raise ValueError(f"invalid fragment slice range: {start}:{end}")
            slice_numel += end - start
            covered.append((start, end, str(item["param_name"])))
        if slice_numel != int(fragment["numel"]):
            raise ValueError(f"fragment {fragment['fragment_id']} numel does not match slices")

    covered.sort()
    cursor = 0
    for start, end, name in covered:
        if start != cursor:
            raise ValueError(f"fragment slices do not exactly cover flat vector near {cursor}, next={name}:{start}")
        cursor = end
    if cursor != total_numel:
        raise ValueError(f"fragment slices cover {cursor}, expected {total_numel}")

    if param_index is not None:
        params = {entry["name"]: entry for entry in param_index.get("params") or []}
        for _start, _end, name in covered:
            if name not in params:
                raise ValueError(f"fragment references unknown parameter: {name}")


def fragment_by_id(fragment_index: dict[str, Any], fragment_id: int) -> dict[str, Any]:
    for fragment in fragment_index["fragments"]:
        if int(fragment["fragment_id"]) == int(fragment_id):
            return fragment
    raise KeyError(f"unknown fragment id: {fragment_id}")


def save_fragment_index(fragment_index: dict[str, Any], path: str | Path) -> Path:
    return atomic_write_json(path, fragment_index)


def load_fragment_index(path: str | Path) -> dict[str, Any]:
    payload = read_json(path)
    validate_fragment_index(payload)
    return payload


def fragment_size_summary(fragment_index: dict[str, Any]) -> dict[str, float | int]:
    sizes = [int(fragment["numel"]) for fragment in fragment_index.get("fragments") or []]
    if not sizes:
        return {"min": 0, "max": 0, "mean": 0.0, "imbalance_ratio": 0.0}
    min_size = min(sizes)
    max_size = max(sizes)
    return {
        "min": min_size,
        "max": max_size,
        "mean": sum(sizes) / len(sizes),
        "imbalance_ratio": (float(max_size) / float(min_size)) if min_size > 0 else float("inf"),
    }
