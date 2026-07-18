#!/usr/bin/env python3
"""Benchmark an 8-vector syncer merge and publication on CPU or CUDA."""

from __future__ import annotations

import argparse
import gc
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.modeling.hf_model import load_causal_lm_and_tokenizer
from fs_diloco.modeling.outer_optim import init_outer_state, outer_optimizer_step
from fs_diloco.modeling.param_index import build_param_index, flatten_trainable_params
from fs_diloco.protocol.merge import weighted_average_tensors
from fs_diloco.storage.atomic_io import atomic_write_json, safe_read_json
from fs_diloco.storage.tensor_codec import (
    dtype_from_name,
    load_global_weights_flat,
    load_update_vector,
    save_global_weights,
    save_outer_state,
    save_update_vector,
)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "min": min(values),
        "mean": sum(values) / len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": max(values),
    }


def _synchronize(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize(device)


def prepare_fixtures(root: Path, config: Any, update_count: int) -> tuple[dict[str, Any], list[Path]]:
    fixture_root = root / "fixtures"
    index_path = fixture_root / "param_index.json"
    base_path = fixture_root / "base.safetensors"
    update_paths = [fixture_root / f"update_{index:03d}.safetensors" for index in range(update_count)]
    existing_index = safe_read_json(index_path)
    if existing_index and base_path.is_file() and all(path.is_file() for path in update_paths):
        return existing_index, update_paths

    fixture_root.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(int(config.training.seed))
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to("cpu")
    param_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    theta = flatten_trainable_params(
        model,
        param_index,
        dtype=torch.float32,
        device="cpu",
    )
    atomic_write_json(index_path, param_index)
    save_global_weights(base_path, theta, param_index, dtype=torch.float32)
    update_dtype = dtype_from_name(config.io.tensor_dtype)
    for index, path in enumerate(update_paths):
        save_update_vector(path, theta.add((index + 1) * 1e-4), dtype=update_dtype)
    del model, theta
    gc.collect()
    return param_index, update_paths


@torch.no_grad()
def benchmark(
    *,
    root: Path,
    config: Any,
    device: torch.device,
    update_count: int,
    repetitions: int,
) -> dict[str, Any]:
    if repetitions <= 0 or update_count <= 0:
        raise ValueError("repetitions and update_count must be positive")
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA benchmark requested but CUDA is unavailable")
    param_index, update_paths = prepare_fixtures(root, config, update_count)
    base_path = root / "fixtures" / "base.safetensors"
    compute_dtype = dtype_from_name(config.syncer.compute_dtype)
    publish_dtype = dtype_from_name(config.syncer.publish_dtype)
    theta = load_global_weights_flat(
        base_path,
        param_index,
        device=device,
        dtype=compute_dtype,
    )
    outer_state = init_outer_state(theta, config.outer_optimizer)
    weights = [1.0 / update_count] * update_count
    output_root = root / "benchmark_outputs"
    output_root.mkdir(parents=True, exist_ok=True)

    samples: list[dict[str, float | int]] = []
    for iteration in range(repetitions):
        _synchronize(device)
        read_start = time.monotonic()
        vectors = [
            load_update_vector(path, device=device, dtype=compute_dtype)
            for path in update_paths
        ]
        _synchronize(device)
        read_seconds = time.monotonic() - read_start

        aggregation_start = time.monotonic()
        p_bar = weighted_average_tensors(vectors, weights)
        grad = theta - p_bar
        _synchronize(device)
        aggregation_seconds = time.monotonic() - aggregation_start

        outer_start = time.monotonic()
        theta, outer_state = outer_optimizer_step(
            theta,
            grad,
            outer_state,
            config.outer_optimizer,
        )
        _synchronize(device)
        outer_seconds = time.monotonic() - outer_start

        weight_path = output_root / f"{device.type}_weight_{iteration:03d}.safetensors"
        optim_path = output_root / f"{device.type}_outer_{iteration:03d}.safetensors"
        publish_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            weight_future = pool.submit(
                save_global_weights,
                weight_path,
                theta,
                param_index,
                dtype=publish_dtype,
            )
            optim_future = pool.submit(
                save_outer_state,
                optim_path,
                theta,
                outer_state,
                dtype=publish_dtype,
            )
            weight_future.result()
            optim_future.result()
        publish_seconds = time.monotonic() - publish_start
        published_bytes = weight_path.stat().st_size + optim_path.stat().st_size
        weight_path.unlink()
        optim_path.unlink()

        samples.append(
            {
                "iteration": iteration,
                "read_seconds": read_seconds,
                "aggregation_seconds": aggregation_seconds,
                "outer_step_seconds": outer_seconds,
                "merge_compute_seconds": read_seconds
                + aggregation_seconds
                + outer_seconds,
                "publish_seconds": publish_seconds,
                "published_bytes": published_bytes,
            }
        )
        del vectors, p_bar, grad
        gc.collect()
        if device.type == "cuda":
            torch.cuda.empty_cache()

    keys = (
        "read_seconds",
        "aggregation_seconds",
        "outer_step_seconds",
        "merge_compute_seconds",
        "publish_seconds",
    )
    return {
        "status": "PASS",
        "root": str(root.resolve()),
        "model_name_or_path": config.model.name_or_path,
        "device": str(device),
        "update_count": update_count,
        "repetitions": repetitions,
        "total_numel": int(param_index["total_numel"]),
        "compute_dtype": config.syncer.compute_dtype,
        "publish_dtype": config.syncer.publish_dtype,
        "update_dtype": config.io.tensor_dtype,
        "summaries": {
            key: _summary([float(sample[key]) for sample in samples]) for key in keys
        },
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--update-count", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    config = resolve_config(args.config, shared_root=str(args.root))
    result = benchmark(
        root=args.root,
        config=config,
        device=torch.device(args.device),
        update_count=args.update_count,
        repetitions=args.repetitions,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
