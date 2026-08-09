"""Validate matched-workload identity before reporting signed paired performance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .paired_performance import paired_noninferiority


PLAN03_REQUIREMENTS = frozenset({"P6-PERF-CLASSIC", "P6-PERF-DYNAMIC"})
MATCHED_FIELDS = (
    "source_identity",
    "config_identity",
    "model_identity",
    "data_identity",
    "seed",
    "cursor_identity",
    "outer_target",
    "processed_tokens",
    "direct_weight_tokens",
    "carried_ancestry_tokens",
    "selected_count",
    "failure_tape",
    "timer_anchor",
    "resource_allocation",
)


def compare_workloads(payload: dict[str, Any]) -> dict[str, Any]:
    baseline = payload.get("baseline")
    candidate = payload.get("candidate")
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        return _blocked("baseline and candidate workload objects are required")
    missing = [
        f"{arm}.{field}"
        for arm, row in (("baseline", baseline), ("candidate", candidate))
        for field in MATCHED_FIELDS
        if field not in row
    ]
    if missing:
        return _blocked("missing comparability fields", details=missing)
    mismatches = {
        field: {"baseline": baseline[field], "candidate": candidate[field]}
        for field in MATCHED_FIELDS
        if baseline[field] != candidate[field]
    }
    if mismatches:
        return {
            "comparison_status": "INCOMPARABLE",
            "reason": "workload identity mismatch",
            "mismatches": mismatches,
            "signed_delta_ratio": None,
            "paired_raw_repeats": [],
            "confidence_interval": None,
        }
    baseline_seconds = payload.get("baseline_seconds")
    candidate_seconds = payload.get("candidate_seconds")
    if not isinstance(baseline_seconds, list) or not isinstance(candidate_seconds, list):
        return _blocked("paired duration arrays are required")
    try:
        statistics_result = paired_noninferiority(
            baseline_seconds,
            candidate_seconds,
            margin=float(payload.get("margin", 0.10)),
            bootstrap_samples=int(payload.get("bootstrap_samples", 10_000)),
            seed=int(payload.get("bootstrap_seed", 20_260_808)),
        )
    except (TypeError, ValueError) as exc:
        return _blocked(str(exc))
    raw = [
        {
            "baseline_seconds": float(base),
            "candidate_seconds": float(candidate_value),
            "signed_delta_ratio": delta,
        }
        for base, candidate_value, delta in zip(
            baseline_seconds,
            candidate_seconds,
            statistics_result.signed_overheads,
            strict=True,
        )
    ]
    absolute_signed_delta = abs(float(statistics_result.median_overhead))
    status = "COMPARABLE" if absolute_signed_delta <= 0.20 else "INCOMPARABLE"
    return {
        "comparison_status": status,
        "reason": (
            None
            if status == "COMPARABLE"
            else "absolute signed median delta exceeds the 20% workload-audit threshold"
        ),
        "signed_delta_ratio": statistics_result.median_overhead,
        "paired_raw_repeats": raw,
        "confidence_interval": {
            "kind": "one-sided-paired-bootstrap-upper-95",
            "upper": statistics_result.bootstrap_upper_95,
            "samples": int(payload.get("bootstrap_samples", 10_000)),
        },
        "absolute_signed_delta_ratio": absolute_signed_delta,
        "noninferiority_margin": statistics_result.margin,
        "noninferiority_pass": status == "COMPARABLE" and statistics_result.passes,
        "clipping_applied": False,
    }


def _blocked(reason: str, *, details: list[str] | None = None) -> dict[str, Any]:
    return {
        "comparison_status": "BLOCKED",
        "reason": reason,
        "details": details or [],
        "signed_delta_ratio": None,
        "paired_raw_repeats": [],
        "confidence_interval": None,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = compare_workloads(payload)
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
