"""Merge selection and token/staleness weighting."""

from __future__ import annotations

import math
from typing import Any


PLAN03_REQUIREMENTS = frozenset({"TOK-04"})


def staleness(current_version: int, base_global_version: int) -> int:
    return max(0, current_version - base_global_version)


def raw_update_weight(tokens: int, staleness_versions: int, staleness_lambda: float) -> float:
    return float(tokens) / (1.0 + float(staleness_lambda) * float(staleness_versions))


def normalized_update_weights(
    updates: list[dict[str, Any]],
    *,
    current_version: int,
    staleness_lambda: float,
) -> dict[str, float]:
    if isinstance(current_version, bool) or not isinstance(current_version, int):
        raise ValueError("current_version must be a non-negative integer")
    if current_version < 0:
        raise ValueError("current_version must be a non-negative integer")
    if isinstance(staleness_lambda, bool):
        raise ValueError("staleness_lambda must be finite and non-negative")
    numeric_lambda = float(staleness_lambda)
    if not math.isfinite(numeric_lambda) or numeric_lambda < 0.0:
        raise ValueError("staleness_lambda must be finite and non-negative")
    if not updates:
        raise ValueError("selected updates must not be empty")
    raw: dict[str, float] = {}
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, str) or not update_id or update_id in raw:
            raise ValueError("selected update IDs must be non-empty and unique")
        tokens = update.get("tokens_this_update")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("each selected update must have positive integer direct tokens")
        base = update.get("base_global_version")
        if isinstance(base, bool) or not isinstance(base, int) or not 0 <= base <= current_version:
            raise ValueError("selected update base version must be current or historical")
        try:
            weight = raw_update_weight(tokens, staleness(current_version, base), numeric_lambda)
        except OverflowError as exc:
            raise ValueError("each raw merge weight must be positive and finite") from exc
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("each raw merge weight must be positive and finite")
        raw[update_id] = weight
    try:
        total = math.fsum(raw.values())
    except OverflowError as exc:
        raise ValueError(
            "selected updates have non-positive or non-finite total merge weight"
        ) from exc
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("selected updates have non-positive or non-finite total merge weight")
    return {update_id: weight / total for update_id, weight in raw.items()}


def select_one_per_learner(
    updates: list[dict[str, Any]],
    *,
    policy: str = "most_recent_per_learner",
    quorum_max: int | None = None,
) -> list[dict[str, Any]]:
    by_learner: dict[str, dict[str, Any]] = {}
    for update in updates:
        learner_id = update["learner_id"]
        current = by_learner.get(learner_id)
        if current is None:
            by_learner[learner_id] = update
            continue
        if policy == "oldest_pending":
            if float(update["committed_at"]) < float(current["committed_at"]):
                by_learner[learner_id] = update
        elif policy == "most_recent_per_learner":
            key = (int(update["local_step_end"]), float(update["committed_at"]))
            current_key = (int(current["local_step_end"]), float(current["committed_at"]))
            if key > current_key:
                by_learner[learner_id] = update
        else:
            raise ValueError(f"unsupported selection_policy: {policy}")
    selected = list(by_learner.values())
    if policy == "oldest_pending":
        selected.sort(key=lambda row: (float(row["committed_at"]), row["learner_id"]))
    else:
        selected.sort(key=lambda row: (row["learner_id"], int(row["local_step_end"])))
    if quorum_max is not None:
        selected = selected[:quorum_max]
    return selected


def stale_update_ids(
    updates: list[dict[str, Any]],
    *,
    current_version: int,
    max_staleness_versions: int,
) -> list[str]:
    return [
        update["update_id"]
        for update in updates
        if staleness(current_version, int(update["base_global_version"])) > max_staleness_versions
    ]


def weighted_average_tensors(tensors: list[Any], weights: list[float]) -> Any:
    if not tensors:
        raise ValueError("cannot average zero tensors")
    if len(tensors) != len(weights):
        raise ValueError("tensor and weight counts differ")
    result = tensors[0].mul(float(weights[0]))
    for tensor, weight in zip(tensors[1:], weights[1:]):
        result = result.add(tensor, alpha=float(weight))
    return result
