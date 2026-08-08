"""Signed paired non-inferiority statistics shared by Plan 03 evidence tools."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PairedPerformanceResult:
    pairs: int
    signed_overheads: tuple[float, ...]
    median_overhead: float
    bootstrap_upper_95: float
    margin: float

    @property
    def passes(self) -> bool:
        return self.median_overhead <= self.margin and self.bootstrap_upper_95 <= self.margin


def paired_noninferiority(
    baseline_seconds: Sequence[float],
    candidate_seconds: Sequence[float],
    *,
    margin: float = 0.10,
    bootstrap_samples: int = 10_000,
    seed: int = 20_260_808,
) -> PairedPerformanceResult:
    if len(baseline_seconds) != len(candidate_seconds) or not baseline_seconds:
        raise ValueError("paired arms must have the same positive sample count")
    if bootstrap_samples < 100 or not 0.0 < margin < 1.0:
        raise ValueError("invalid bootstrap or margin")
    signed = tuple(
        (float(candidate) - float(baseline)) / float(baseline)
        for baseline, candidate in zip(baseline_seconds, candidate_seconds, strict=True)
    )
    if any(
        not math.isfinite(value) or float(baseline) <= 0.0
        for value, baseline in zip(signed, baseline_seconds, strict=True)
    ):
        raise ValueError("durations and signed overheads must be positive/finite")
    rng = random.Random(seed)
    medians = sorted(
        statistics.median(rng.choices(signed, k=len(signed))) for _ in range(bootstrap_samples)
    )
    upper_index = math.ceil(0.95 * bootstrap_samples) - 1
    return PairedPerformanceResult(
        pairs=len(signed),
        signed_overheads=signed,
        median_overhead=float(statistics.median(signed)),
        bootstrap_upper_95=float(medians[upper_index]),
        margin=float(margin),
    )
