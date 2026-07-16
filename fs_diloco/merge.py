"""Merge selection and token/staleness weighting."""

from __future__ import annotations

from typing import Any


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
    raw: dict[str, float] = {}
    for update in updates:
        s = staleness(current_version, int(update["base_global_version"]))
        raw[update["update_id"]] = raw_update_weight(
            int(update["tokens_this_update"]),
            s,
            staleness_lambda,
        )
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("selected updates have non-positive total merge weight")
    return {update_id: weight / total for update_id, weight in raw.items()}


def normalized_fragment_update_weights(
    updates: list[dict[str, Any]],
    *,
    current_fragment_version: int,
    staleness_lambda: float,
) -> dict[str, float]:
    raw: dict[str, float] = {}
    for update in updates:
        s = staleness(current_fragment_version, int(update["base_fragment_version"]))
        raw[update["update_id"]] = raw_update_weight(
            int(update["tokens_this_update"]),
            s,
            staleness_lambda,
        )
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("selected fragment updates have non-positive total merge weight")
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


def stale_fragment_update_ids(
    updates: list[dict[str, Any]],
    *,
    current_fragment_version: int,
    max_staleness_versions: int,
) -> list[str]:
    return [
        update["update_id"]
        for update in updates
        if staleness(current_fragment_version, int(update["base_fragment_version"])) > max_staleness_versions
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
