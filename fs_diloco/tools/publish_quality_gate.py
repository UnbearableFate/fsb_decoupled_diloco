"""Evaluate the frozen Q6 FP32/BF16 publication quality gate."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

from ..core.config import config_to_dict
from ..legacy.config_v1_v3 import load_query_config_snapshot


_T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    12: 2.179,
    15: 2.131,
    20: 2.086,
    25: 2.060,
    30: 2.042,
    40: 2.021,
    60: 2.000,
    120: 1.980,
}


def _t_critical(df: int) -> float:
    for bound, value in _T_CRITICAL_95.items():
        if df <= bound:
            return value
    return 1.96


def roundtrip_trend(values: list[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if len(finite) != len(values) or len(finite) < 3:
        return {"count": len(finite), "status": "insufficient", "bounded": False}
    count = len(finite)
    x_values = [float(index) for index in range(1, count + 1)]
    x_mean = sum(x_values) / count
    y_mean = sum(finite) / count
    sxx = sum((value - x_mean) ** 2 for value in x_values)
    slope = (
        sum((x_value - x_mean) * (y_value - y_mean) for x_value, y_value in zip(x_values, finite))
        / sxx
    )
    intercept = y_mean - slope * x_mean
    residual_sum_squares = sum(
        (y_value - (intercept + slope * x_value)) ** 2 for x_value, y_value in zip(x_values, finite)
    )
    slope_standard_error = math.sqrt(max(0.0, residual_sum_squares / (count - 2) / sxx))
    margin = _t_critical(count - 2) * slope_standard_error
    ci_low = slope - margin
    ci_high = slope + margin
    split = count // 2
    first_mean = sum(finite[:split]) / split
    second_mean = sum(finite[split:]) / (count - split)
    if first_mean == 0.0:
        half_ratio = 1.0 if second_mean == 0.0 else math.inf
    else:
        half_ratio = second_mean / first_mean
    # The risk being gated is cumulative growth.  A confidence interval that is
    # wholly negative is evidence of decreasing error and must not fail merely
    # because it does not straddle zero.  Fail only on a confidently positive
    # slope (plus the independent half-to-half magnitude guard below).
    bounded = ci_low <= 0.0 and half_ratio <= 1.25
    return {
        "count": count,
        "status": "available",
        "mean": y_mean,
        "slope": slope,
        "slope_standard_error": slope_standard_error,
        "slope_ci95_low": ci_low,
        "slope_ci95_high": ci_high,
        "first_half_mean": first_mean,
        "second_half_mean": second_mean,
        "second_half_to_first_half_ratio": half_ratio,
        "bounded": bounded,
    }


def evaluate_publish_quality_gate(
    *,
    fp32_losses: dict[int, float],
    bf16_losses: dict[int, float],
    bf16_trends: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    fp32_seeds = set(fp32_losses)
    bf16_seeds = set(bf16_losses)
    matched = sorted(fp32_seeds & bf16_seeds)
    if fp32_seeds != bf16_seeds or len(matched) < 3:
        return {
            "status": "NEEDS_MORE_SEEDS",
            "fp32_seeds": sorted(fp32_seeds),
            "bf16_seeds": sorted(bf16_seeds),
            "matched_seeds": matched,
            "minimum_seed_count": 3,
        }
    if any(seed not in bf16_trends for seed in matched):
        return {
            "status": "NEEDS_MORE_SEEDS",
            "reason": "roundtrip trend evidence is incomplete",
            "matched_seeds": matched,
            "minimum_seed_count": 3,
        }

    fp32_values = [float(fp32_losses[seed]) for seed in matched]
    sigma_fp32 = statistics.stdev(fp32_values)
    epsilon = max(0.01, sigma_fp32)
    degradations = {seed: float(bf16_losses[seed]) - float(fp32_losses[seed]) for seed in matched}
    mean_degradation = sum(degradations.values()) / len(degradations)
    worst_degradation = max(degradations.values())
    loss_gate_pass = mean_degradation <= epsilon and all(
        value <= 2.0 * epsilon for value in degradations.values()
    )
    trend_gate_pass = all(bool(bf16_trends[seed].get("bounded")) for seed in matched)
    return {
        "status": "PASS" if loss_gate_pass and trend_gate_pass else "FAIL",
        "matched_seeds": matched,
        "sigma_fp32": sigma_fp32,
        "epsilon": epsilon,
        "paired_degradation_by_seed": degradations,
        "mean_paired_degradation": mean_degradation,
        "worst_seed_degradation": worst_degradation,
        "mean_degradation_limit": epsilon,
        "per_seed_degradation_limit": 2.0 * epsilon,
        "loss_gate_pass": loss_gate_pass,
        "trend_gate_pass": trend_gate_pass,
        "roundtrip_trends": bf16_trends,
    }


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def _read_metric_values(path: Path, field: str) -> list[float]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        values = []
        for row in csv.DictReader(handle):
            raw = row.get(field)
            if raw not in (None, ""):
                values.append(float(raw))
    return values


def _normalized_pair_config(path: Path) -> tuple[dict[str, Any], str, int]:
    config = load_query_config_snapshot(path)
    payload = config_to_dict(config)
    publish_dtype = str(payload["syncer"]["publish_dtype"])
    seed = int(payload["training"]["seed"])
    payload["syncer"]["publish_dtype"] = "<paired-variable>"
    for key in ("run_id", "shared_root", "name"):
        payload["run"][key] = "<run-metadata>"
    return payload, publish_dtype, seed


def _run_evidence(root: Path) -> dict[str, Any]:
    validation = _read_json(root / "metrics" / "validation_eval.json")
    source = _read_json(root / "control" / "source_identity.json")
    config_path = root / "control" / "run_config.resolved.yaml"
    normalized, publish_dtype, seed = _normalized_pair_config(config_path)
    errors = _read_metric_values(
        root / "metrics" / "syncer_metrics.csv",
        "publish_roundtrip_relative_l2_error",
    )
    return {
        "root": str(root.resolve()),
        "seed": seed,
        "publish_dtype": publish_dtype,
        "validation_loss": float(validation["validation_loss"]),
        "validation_protocol_sha256": validation["protocol_sha256"],
        "checkpoint_sha256": validation["checkpoint_sha256"],
        "source_fingerprint": source["source_fingerprint"],
        "normalized_config": normalized,
        "roundtrip_values": errors,
        "roundtrip_trend": roundtrip_trend(errors),
    }


def _parse_seed_roots(values: list[str]) -> dict[int, Path]:
    parsed: dict[int, Path] = {}
    for item in values:
        seed_text, separator, root_text = item.partition("=")
        if not separator:
            raise ValueError(f"expected SEED=RUN_ROOT, got {item!r}")
        seed = int(seed_text)
        if seed in parsed:
            raise ValueError(f"duplicate seed {seed}")
        parsed[seed] = Path(root_text).expanduser().resolve()
    return parsed


def evaluate_run_roots(fp32_roots: dict[int, Path], bf16_roots: dict[int, Path]) -> dict[str, Any]:
    fp32 = {seed: _run_evidence(root) for seed, root in fp32_roots.items()}
    bf16 = {seed: _run_evidence(root) for seed, root in bf16_roots.items()}
    fingerprints = {evidence["source_fingerprint"] for evidence in (*fp32.values(), *bf16.values())}
    if len(fingerprints) != 1:
        raise ValueError(f"source fingerprints differ: {sorted(fingerprints)}")
    protocols = {
        evidence["validation_protocol_sha256"] for evidence in (*fp32.values(), *bf16.values())
    }
    if len(protocols) != 1:
        raise ValueError(f"validation protocols differ: {sorted(protocols)}")
    for seed in sorted(set(fp32) & set(bf16)):
        if fp32[seed]["seed"] != seed or bf16[seed]["seed"] != seed:
            raise ValueError(f"resolved training seed mismatch for pair {seed}")
        if fp32[seed]["publish_dtype"] != "float32":
            raise ValueError(f"FP32 pair {seed} uses {fp32[seed]['publish_dtype']}")
        if bf16[seed]["publish_dtype"] != "bfloat16":
            raise ValueError(f"BF16 pair {seed} uses {bf16[seed]['publish_dtype']}")
        if fp32[seed]["normalized_config"] != bf16[seed]["normalized_config"]:
            raise ValueError(f"pair {seed} differs in more than syncer.publish_dtype")
        if any(value != 0.0 for value in fp32[seed]["roundtrip_values"]):
            raise ValueError(f"FP32 pair {seed} reports nonzero roundtrip error")
    gate = evaluate_publish_quality_gate(
        fp32_losses={seed: item["validation_loss"] for seed, item in fp32.items()},
        bf16_losses={seed: item["validation_loss"] for seed, item in bf16.items()},
        bf16_trends={seed: item["roundtrip_trend"] for seed, item in bf16.items()},
    )
    return {
        **gate,
        "source_fingerprint": next(iter(fingerprints)) if fingerprints else None,
        "validation_protocol_sha256": next(iter(protocols)) if protocols else None,
        "fp32_runs": fp32,
        "bf16_runs": bf16,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fp32", action="append", default=[], metavar="SEED=RUN_ROOT")
    parser.add_argument("--bf16", action="append", default=[], metavar="SEED=RUN_ROOT")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = evaluate_run_roots(_parse_seed_roots(args.fp32), _parse_seed_roots(args.bf16))
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        from ..storage.atomic_io import atomic_write_text

        atomic_write_text(args.output, encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
