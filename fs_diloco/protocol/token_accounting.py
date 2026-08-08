"""Deterministic learner-side segment and authority token accounting.

The accumulator deliberately contains no filesystem or framework code.  A
learner records completed steps, tells it when a pre-publication base replace
occurs, and uses the resulting snapshot to build either a proposal plus cycle
receipt or a receipt-only cycle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


PLAN03_REQUIREMENTS = frozenset({"P3-REBASE", "TOK-01", "TOK-02", "TOK-03", "TOK-06"})


def _nonnegative_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class SegmentSnapshot:
    base_global_version: int
    local_step_start: int
    local_step_end: int
    effective_tokens: int
    examples: int
    weighted_loss_sum: float
    grad_norm_sum: float
    grad_norm_samples: int

    @property
    def mean_loss(self) -> float | None:
        if self.examples == 0:
            return None
        return self.weighted_loss_sum / self.examples

    @property
    def mean_grad_norm(self) -> float | None:
        if self.grad_norm_samples == 0:
            return None
        return self.grad_norm_sum / self.grad_norm_samples


@dataclass(frozen=True)
class CycleAccounting:
    processed_tokens: int
    effective_tokens: int
    local_discarded_tokens: int
    retained_tokens_since_base: int
    processed_examples: int
    effective_examples: int
    segment: SegmentSnapshot

    def __post_init__(self) -> None:
        if self.processed_tokens != self.effective_tokens + self.local_discarded_tokens:
            raise ValueError("processed tokens must balance effective and local-discarded")
        if self.retained_tokens_since_base < self.effective_tokens:
            raise ValueError("retained ancestry must cover the effective segment")

    @property
    def proposal_expected(self) -> bool:
        return self.effective_tokens > 0


class TrainingSegmentAccumulator:
    """Account one upload cycle across zero or more base segments.

    ``replace_base(..., retain_effective_work=False)`` is the destructive
    replace used by inner-poll and cycle-end adoption.  It closes the old
    effective segment into local-discarded and resets loss/example/gradient
    statistics.  Predict/rebase paths that mathematically retain the update use
    ``retain_effective_work=True`` and therefore do not lose or double count it.
    """

    def __init__(self, *, base_global_version: int, interval_start_step: int) -> None:
        self._base_global_version = _nonnegative_int(
            base_global_version, name="base_global_version"
        )
        self._interval_start_step = _nonnegative_int(
            interval_start_step, name="interval_start_step"
        )
        self._last_step = self._interval_start_step
        self._processed_tokens = 0
        self._processed_examples = 0
        self._discarded_tokens = 0
        self._effective_tokens = 0
        self._effective_examples = 0
        self._weighted_loss_sum = 0.0
        self._grad_norm_sum = 0.0
        self._grad_norm_samples = 0
        self._retained_ancestry = 0

    @property
    def interval_start_step(self) -> int:
        return self._interval_start_step

    @property
    def base_global_version(self) -> int:
        return self._base_global_version

    def record_step(
        self,
        *,
        local_step_end: int,
        tokens: int,
        examples: int,
        loss: float,
        grad_norm: float | None = None,
    ) -> None:
        step = _nonnegative_int(local_step_end, name="local_step_end")
        if step != self._last_step + 1:
            raise ValueError(f"local steps must be contiguous: expected {self._last_step + 1}")
        token_count = _nonnegative_int(tokens, name="tokens")
        example_count = _nonnegative_int(examples, name="examples")
        if token_count == 0 or example_count == 0:
            raise ValueError("a completed training step must contain tokens and examples")
        numeric_loss = float(loss)
        if not math.isfinite(numeric_loss):
            raise ValueError("loss must be finite")
        if grad_norm is not None:
            numeric_grad_norm = float(grad_norm)
            if not math.isfinite(numeric_grad_norm) or numeric_grad_norm < 0.0:
                raise ValueError("grad_norm must be finite and non-negative")
            self._grad_norm_sum += numeric_grad_norm
            self._grad_norm_samples += 1
        self._last_step = step
        self._processed_tokens += token_count
        self._processed_examples += example_count
        self._effective_tokens += token_count
        self._effective_examples += example_count
        self._weighted_loss_sum += numeric_loss * example_count

    def replace_base(
        self,
        *,
        new_base_global_version: int,
        retain_effective_work: bool = False,
    ) -> SegmentSnapshot:
        version = _nonnegative_int(new_base_global_version, name="new_base_global_version")
        if version <= self._base_global_version:
            raise ValueError("a base replacement must advance the global version")
        closed = self._snapshot()
        if retain_effective_work:
            self._retained_ancestry = max(self._retained_ancestry, self._effective_tokens)
        else:
            self._discarded_tokens += self._effective_tokens
            self._effective_tokens = 0
            self._effective_examples = 0
            self._weighted_loss_sum = 0.0
            self._grad_norm_sum = 0.0
            self._grad_norm_samples = 0
            self._retained_ancestry = 0
            self._interval_start_step = self._last_step
        self._base_global_version = version
        return closed

    def finalize_cycle(self) -> CycleAccounting:
        segment = self._snapshot()
        return CycleAccounting(
            processed_tokens=self._processed_tokens,
            effective_tokens=self._effective_tokens,
            local_discarded_tokens=self._discarded_tokens,
            retained_tokens_since_base=max(self._effective_tokens, self._retained_ancestry),
            processed_examples=self._processed_examples,
            effective_examples=self._effective_examples,
            segment=segment,
        )

    def _snapshot(self) -> SegmentSnapshot:
        return SegmentSnapshot(
            base_global_version=self._base_global_version,
            local_step_start=self._interval_start_step,
            local_step_end=self._last_step,
            effective_tokens=self._effective_tokens,
            examples=self._effective_examples,
            weighted_loss_sum=self._weighted_loss_sum,
            grad_norm_sum=self._grad_norm_sum,
            grad_norm_samples=self._grad_norm_samples,
        )
