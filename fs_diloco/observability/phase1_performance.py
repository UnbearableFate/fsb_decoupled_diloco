"""Frozen Plan 02 Phase 1 matched-performance gate definitions."""

from __future__ import annotations

import math


MATCHED_PERFORMANCE_FORMAT_VERSION = 1
BUSINESS_TRANSACTION_MIN_SAMPLES = 400
BUSINESS_TRANSACTION_BATCH_SIZE = 25
BUSINESS_TRANSACTION_MAX_P99_RATIO = 1.25
BUSINESS_TRANSACTION_P99_JITTER_SECONDS = 0.002
CHECKPOINT_PUBLISH_MIN_SAMPLES = 100
CHECKPOINT_PUBLISH_MAX_P99_RATIO = 1.25
CHECKPOINT_PUBLISH_P99_JITTER_SECONDS = 0.002


def nearest_rank_percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        raise ValueError("percentile requires at least one sample")
    ordered = sorted(float(value) for value in samples)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def matched_p99_limit(
    baseline_p99_seconds: float,
    *,
    max_ratio: float,
    jitter_seconds: float,
) -> float:
    """Apply the frozen relative limit plus a small filesystem timing floor."""

    return float(baseline_p99_seconds) * float(max_ratio) + float(jitter_seconds)
