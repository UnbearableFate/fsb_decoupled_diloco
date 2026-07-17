"""Filesystem-backed Decoupled DiLoCo learner."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch

from ..core.config import Config, resolve_config
from ..core.constants import (
    FORMAT_VERSION,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_STOPPED,
    learner_index_from_id,
)
from ..modeling.hf_data import Batch, build_batch_iterator
from ..modeling.hf_model import choose_device, load_causal_lm_and_tokenizer
from ..modeling.outer_optim import outer_optimizer_step
from ..modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
    load_param_index,
    trainable_params_l2_norm,
    validate_compatible_index,
)
from ..observability.logging_utils import JsonlLogger, log_uncaught_exception
from ..observability.metrics import LEARNER_METRIC_FIELDS, UPDATE_MANIFEST_FIELDS, append_csv_row
from ..observability.resource_monitor import ResourceMonitor, finite_resource_metrics
from ..protocol.fragment_codec import (
    extract_fragment_from_model,
    load_fragment_weight,
    materialize_full_from_fragments,
    save_fragment_update,
    scatter_fragment,
)
from ..protocol.fragment_index import load_fragment_index
from ..protocol.fragment_scheduler import select_fragment
from ..storage.atomic_io import atomic_write_json, file_size, safe_read_json, sha256_file
from ..storage.paths import RunPaths, prepare_run_dirs
from ..storage.tensor_codec import (
    dtype_from_name,
    load_global_weights_flat,
    load_global_weights_into_model,
    load_outer_state,
    save_update_vector,
)
from .adoption import (
    AdoptionContext,
    PublishResult,
    StrategyAction,
    make_global_adoption_strategy,
)
from .failure_sim import maybe_crash, maybe_sleep_jitter, should_skip_upload


PREDICTION_WORKING_VECTOR_COUNT = 14
RECONCILE_WORKING_VECTOR_COUNT = 5
REFERENCE_WORKING_VECTOR_COUNT = 3
CUDA_COMPUTE_MIN_RESERVE_BYTES = 1 << 30
CUDA_COMPUTE_RESERVE_FRACTION = 0.05


@dataclass(frozen=True)
class LearnerComputePlacement:
    device: torch.device
    dtype: torch.dtype
    estimated_working_bytes: int
    cuda_free_bytes: int | None = None
    cuda_total_bytes: int | None = None
    cuda_reserve_bytes: int | None = None
    fallback_reason: str | None = None

    def log_fields(self, prefix: str) -> dict[str, Any]:
        return {
            f"{prefix}_compute_device": str(self.device),
            f"{prefix}_compute_dtype": str(self.dtype).removeprefix("torch."),
            f"{prefix}_estimated_working_bytes": self.estimated_working_bytes,
            f"{prefix}_cuda_free_bytes": self.cuda_free_bytes,
            f"{prefix}_cuda_total_bytes": self.cuda_total_bytes,
            f"{prefix}_cuda_reserve_bytes": self.cuda_reserve_bytes,
            f"{prefix}_fallback_reason": self.fallback_reason,
        }


def learner_compute_dtype(config: Config) -> torch.dtype:
    """Use the syncer's configured arithmetic dtype for learner reconciliation."""

    return dtype_from_name(config.syncer.compute_dtype)


def choose_learner_compute_placement(
    *,
    preferred_device: torch.device | str,
    dtype: torch.dtype,
    total_numel: int,
    working_vector_count: int,
) -> LearnerComputePlacement:
    """Prefer CUDA unless the estimated temporary footprint risks OOM."""

    requested = torch.device(preferred_device)
    element_size = torch.empty((), dtype=dtype).element_size()
    estimated_bytes = max(0, int(total_numel)) * element_size * int(working_vector_count)
    if requested.type != "cuda":
        return LearnerComputePlacement(
            device=torch.device("cpu"),
            dtype=dtype,
            estimated_working_bytes=estimated_bytes,
            fallback_reason="learner_device_not_cuda",
        )
    if not torch.cuda.is_available():
        return LearnerComputePlacement(
            device=torch.device("cpu"),
            dtype=dtype,
            estimated_working_bytes=estimated_bytes,
            fallback_reason="cuda_unavailable",
        )

    try:
        free_bytes, total_bytes = torch.cuda.mem_get_info(requested)
    except (RuntimeError, ValueError):
        return LearnerComputePlacement(
            device=torch.device("cpu"),
            dtype=dtype,
            estimated_working_bytes=estimated_bytes,
            fallback_reason="cuda_memory_query_failed",
        )

    reserve_bytes = max(
        CUDA_COMPUTE_MIN_RESERVE_BYTES,
        int(total_bytes * CUDA_COMPUTE_RESERVE_FRACTION),
    )
    usable_bytes = max(0, int(free_bytes) - reserve_bytes)
    if estimated_bytes > usable_bytes:
        return LearnerComputePlacement(
            device=torch.device("cpu"),
            dtype=dtype,
            estimated_working_bytes=estimated_bytes,
            cuda_free_bytes=int(free_bytes),
            cuda_total_bytes=int(total_bytes),
            cuda_reserve_bytes=reserve_bytes,
            fallback_reason="estimated_cuda_oom_risk",
        )
    return LearnerComputePlacement(
        device=requested,
        dtype=dtype,
        estimated_working_bytes=estimated_bytes,
        cuda_free_bytes=int(free_bytes),
        cuda_total_bytes=int(total_bytes),
        cuda_reserve_bytes=reserve_bytes,
    )


def cpu_fallback_placement(
    placement: LearnerComputePlacement,
    *,
    reason: str,
) -> LearnerComputePlacement:
    return LearnerComputePlacement(
        device=torch.device("cpu"),
        dtype=placement.dtype,
        estimated_working_bytes=placement.estimated_working_bytes,
        cuda_free_bytes=placement.cuda_free_bytes,
        cuda_total_bytes=placement.cuda_total_bytes,
        cuda_reserve_bytes=placement.cuda_reserve_bytes,
        fallback_reason=reason,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--shared-root")
    parser.add_argument("--learner-id", required=True)
    parser.add_argument("--num-learners", type=int)
    return parser.parse_args(argv)


def write_heartbeat(
    *,
    paths: RunPaths,
    config: Config,
    learner_id: str,
    status: str,
    phase: str,
    last_loaded_global_version: int,
    last_local_step: int,
    last_update_id: str | None,
    tokens_per_sec: float | None = None,
    last_loaded_global_merge_event: int | None = None,
    last_loaded_fragment_versions: dict[int, int] | None = None,
    last_adopted_fragments: list[int] | None = None,
    resource_metrics: dict[str, float | int] | None = None,
) -> None:
    payload = {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "learner_id": learner_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "timestamp": time.time(),
        "status": status,
        "phase": phase,
        "last_loaded_global_version": last_loaded_global_version,
        "last_local_step": last_local_step,
        "last_update_id": last_update_id,
        "tokens_per_sec": tokens_per_sec,
    }
    if last_loaded_global_merge_event is not None:
        payload["last_loaded_global_merge_event"] = last_loaded_global_merge_event
    if last_loaded_fragment_versions is not None:
        payload["last_loaded_fragment_versions"] = {
            str(fragment_id): int(version)
            for fragment_id, version in sorted(last_loaded_fragment_versions.items())
        }
    if last_adopted_fragments is not None:
        payload["last_adopted_fragments"] = [
            int(fragment_id) for fragment_id in last_adopted_fragments
        ]
    if resource_metrics:
        payload.update(resource_metrics)
    atomic_write_json(paths.heartbeats / f"{learner_id}.json", payload)


def create_resource_monitor(device: torch.device) -> ResourceMonitor:
    gpu_reader = None
    if device.type == "cuda":
        utilization = getattr(torch.cuda, "utilization", None)
        if utilization is not None:

            def read_gpu_utilization() -> float:
                return float(utilization(device))

            gpu_reader = read_gpu_utilization
    return ResourceMonitor(gpu_utilization_reader=gpu_reader, sample_interval_seconds=1.0)


def wait_for_json(
    path: Path, *, timeout_seconds: float = 1800.0, poll_seconds: float = 1.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = safe_read_json(path)
        if payload is not None:
            return payload
        time.sleep(poll_seconds)
    raise TimeoutError(f"timed out waiting for {path}")


def read_latest_if_newer(paths: RunPaths, last_loaded_global_version: int) -> dict[str, Any] | None:
    payload = safe_read_json(paths.latest_json)
    if payload is None:
        return None
    if int(payload.get("version", -1)) <= last_loaded_global_version:
        return None
    return payload


def wait_for_latest_if_newer(
    paths: RunPaths,
    last_loaded_global_version: int,
    *,
    wait_seconds: float,
    poll_seconds: float,
) -> tuple[dict[str, Any] | None, float]:
    """Poll briefly for the first newer global after a proposal publication."""

    wait_seconds = max(0.0, float(wait_seconds))
    poll_seconds = float(poll_seconds)
    if poll_seconds <= 0.0:
        raise ValueError("poll_seconds must be > 0")
    started = time.monotonic()
    deadline = started + wait_seconds
    while True:
        payload = read_latest_if_newer(paths, last_loaded_global_version)
        if payload is not None:
            return payload, time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0.0 or paths.stop_json.exists():
            return None, time.monotonic() - started
        time.sleep(min(poll_seconds, remaining))


def read_fragment_latest_if_newer(
    paths: RunPaths, last_loaded_global_merge_event: int
) -> dict[str, Any] | None:
    payload = safe_read_json(paths.latest_json)
    if payload is None or payload.get("latest_kind") != "fragment":
        return None
    if (
        int(payload.get("global_merge_event", payload.get("version", -1)))
        <= last_loaded_global_merge_event
    ):
        return None
    return payload


def wait_for_fragment_latest_if_newer(
    paths: RunPaths,
    last_loaded_global_merge_event: int,
    config: Config,
) -> dict[str, Any] | None:
    grace_seconds = (
        config.sync.grace_window.initial_seconds
        if config.sync.grace_window.mode == "adaptive_fastest_upload_eta"
        else config.sync.grace_window.fixed_seconds
    )
    deadline = time.monotonic() + max(
        config.sync.stop_file_poll_seconds,
        config.sync.scan_interval_seconds + grace_seconds + 1.0,
    )
    while time.monotonic() <= deadline:
        payload = read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)
        if payload is not None:
            return payload
        time.sleep(min(config.sync.stop_file_poll_seconds, max(0.0, deadline - time.monotonic())))
    return None


def stop_requested(paths: RunPaths, local_step: int, config: Config) -> bool:
    if paths.stop_json.exists():
        return True
    if config.training.completion_mode == "global_only":
        return False
    if (
        config.training.max_local_steps is not None
        and local_step >= config.training.max_local_steps
    ):
        return True
    return False


def build_inner_optimizer_and_scheduler(
    model: torch.nn.Module,
    config: Config,
) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler | None]:
    name = config.inner_optimizer.name.lower()
    if name != "adamw":
        raise ValueError(f"unsupported inner optimizer: {config.inner_optimizer.name}")
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config.inner_optimizer.lr,
        betas=tuple(config.inner_optimizer.betas),
        eps=config.inner_optimizer.eps,
        weight_decay=config.inner_optimizer.weight_decay,
    )
    if config.inner_optimizer.scheduler == "none":
        return optimizer, None

    def lr_lambda(step: int) -> float:
        if config.inner_optimizer.warmup_steps and step < config.inner_optimizer.warmup_steps:
            return max(1e-8, float(step + 1) / float(config.inner_optimizer.warmup_steps))
        if config.inner_optimizer.scheduler == "cosine" and config.training.max_local_steps:
            progress = min(1.0, step / max(1, config.training.max_local_steps))
            return 0.5 * (1.0 + math.cos(math.pi * progress))
        return 1.0

    return optimizer, torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lr_lambda)


def inner_training_state_metrics(
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler | None,
) -> dict[str, Any]:
    """Return lightweight evidence that optimizer/scheduler state was retained."""

    return {
        "optimizer_state_entries": len(optimizer.state),
        "optimizer_lrs": [float(group["lr"]) for group in optimizer.param_groups],
        "scheduler_last_epoch": scheduler.last_epoch if scheduler is not None else None,
    }


def maybe_autocast(device: torch.device, precision: str) -> torch.autocast:
    enabled = device.type == "cuda" and precision.lower() in {"bf16", "bfloat16"}
    return torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=enabled)


def train_one_step(
    model: torch.nn.Module,
    batch_iter: Any,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler | None,
    *,
    device: torch.device,
    config: Config,
) -> tuple[float, int, int, float | None]:
    optimizer.zero_grad(set_to_none=True)
    total_loss = 0.0
    total_tokens = 0
    total_examples = 0
    for _ in range(config.training.gradient_accumulation_steps):
        batch: Batch = next(batch_iter).to(device)
        with maybe_autocast(device, config.training.precision):
            output = model(input_ids=batch.input_ids, labels=batch.labels)
            loss = output.loss / config.training.gradient_accumulation_steps
        if loss is None:
            raise RuntimeError("model did not return a loss")
        if not torch.isfinite(loss.detach()):
            raise FloatingPointError(f"non-finite loss: {loss.item()}")
        loss.backward()
        total_loss += float(loss.detach().cpu()) * config.training.gradient_accumulation_steps
        total_tokens += batch.num_tokens
        total_examples += batch.num_examples
    grad_norm = None
    if config.training.grad_clip is not None:
        grad_norm = float(
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.training.grad_clip)
            .detach()
            .cpu()
        )
    optimizer.step()
    if scheduler is not None:
        scheduler.step()
    return (
        total_loss / config.training.gradient_accumulation_steps,
        total_tokens,
        total_examples,
        grad_norm,
    )


def adopt_global(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    device: torch.device,
) -> int:
    load_global_weights_into_model(latest["weight_path"], model, param_index)
    model.to(device)
    return int(latest["version"])


def rebase_local_delta_onto_global(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    device: torch.device,
    reference_flat: torch.Tensor,
    config: Config,
) -> tuple[int, float, dict[str, Any]]:
    """Move unsubmitted local progress onto a newer full global checkpoint.

    The reference and arithmetic use ``syncer.compute_dtype``.  CUDA is
    preferred; a conservative memory estimate or a real CUDA OOM falls back to
    CPU without changing the configured dtype.
    """

    expected_numel = int(param_index["total_numel"])
    if int(reference_flat.numel()) != expected_numel:
        raise ValueError(
            f"rebase reference has {reference_flat.numel()} values, expected {expected_numel}"
        )
    placement = choose_learner_compute_placement(
        preferred_device=device,
        dtype=learner_compute_dtype(config),
        total_numel=expected_numel,
        working_vector_count=RECONCILE_WORKING_VECTOR_COUNT,
    )

    def compute_rebased(
        selected: LearnerComputePlacement,
    ) -> tuple[torch.Tensor, float]:
        reference = reference_flat.detach().to(
            device=selected.device,
            dtype=selected.dtype,
        )
        local_delta = flatten_trainable_params(
            model,
            param_index,
            dtype=selected.dtype,
            device=selected.device,
        )
        global_flat = load_global_weights_flat(
            latest["weight_path"],
            param_index,
            device=selected.device,
            dtype=selected.dtype,
        )
        if int(global_flat.numel()) != expected_numel:
            raise ValueError(
                f"new global has {global_flat.numel()} values, expected {expected_numel}"
            )

        local_delta.sub_(reference)
        delta_norm = float(
            torch.linalg.vector_norm(local_delta, ord=2, dtype=torch.float32).item()
        )
        local_delta.add_(global_flat)
        return local_delta, delta_norm

    try:
        rebased_flat, delta_norm = compute_rebased(placement)
    except torch.OutOfMemoryError:
        if placement.device.type != "cuda":
            raise
        placement = cpu_fallback_placement(placement, reason="cuda_oom")
        torch.cuda.empty_cache()
        rebased_flat, delta_norm = compute_rebased(placement)

    load_flat_into_model(model, rebased_flat, param_index)
    model.to(device)
    return int(latest["version"]), delta_norm, placement.log_fields("reconcile")


def snapshot_model_for_reconcile(
    *,
    model: torch.nn.Module,
    param_index: dict[str, Any],
    device: torch.device,
    config: Config,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Snapshot a publication anchor using the configured compute placement."""

    placement = choose_learner_compute_placement(
        preferred_device=device,
        dtype=learner_compute_dtype(config),
        total_numel=int(param_index["total_numel"]),
        working_vector_count=REFERENCE_WORKING_VECTOR_COUNT,
    )

    def snapshot(selected: LearnerComputePlacement) -> torch.Tensor:
        return flatten_trainable_params(
            model,
            param_index,
            dtype=selected.dtype,
            device=selected.device,
        )

    try:
        reference = snapshot(placement)
    except torch.OutOfMemoryError:
        if placement.device.type != "cuda":
            raise
        placement = cpu_fallback_placement(placement, reason="cuda_oom")
        torch.cuda.empty_cache()
        reference = snapshot(placement)
    return reference, placement.log_fields("reference")


def _predict_next_global_weight_on_placement(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    config: Config,
    local_tokens: int,
    placement: LearnerComputePlacement,
) -> tuple[torch.Tensor, dict[str, Any]]:
    expected_numel = int(param_index["total_numel"])
    local_flat = flatten_trainable_params(
        model,
        param_index,
        dtype=placement.dtype,
        device=placement.device,
    )
    global_flat = load_global_weights_flat(
        latest["weight_path"],
        param_index,
        device=placement.device,
        dtype=placement.dtype,
    )
    outer_theta, outer_state = load_outer_state(
        latest["optim_path"],
        device=placement.device,
        dtype=placement.dtype,
    )
    if any(
        int(tensor.numel()) != expected_numel
        for tensor in (local_flat, global_flat, outer_theta)
    ):
        raise ValueError("prediction inputs do not match the parameter index")
    if not torch.equal(global_flat, outer_theta):
        raise RuntimeError("global weight and outer checkpoint theta do not match")
    momentum = outer_state.get("momentum")
    if momentum is None or int(momentum.numel()) != expected_numel:
        raise ValueError("outer checkpoint does not contain compatible momentum")

    previous_total_update_tokens = int(latest.get("total_update_tokens", 0) or 0)
    bootstrapped_total_tokens = previous_total_update_tokens <= 0
    estimated_total_tokens = previous_total_update_tokens
    if bootstrapped_total_tokens:
        estimated_total_tokens = local_tokens * max(1, int(config.sync.quorum_min))
    estimated_total_tokens = max(local_tokens, estimated_total_tokens)
    local_weight = min(1.0, float(local_tokens) / float(estimated_total_tokens))

    local_delta = local_flat.sub(global_flat)
    historical_delta = momentum.mul(-(1.0 - float(config.outer_optimizer.momentum)))
    predicted_aggregate_delta = historical_delta.mul(1.0 - local_weight).add(
        local_delta,
        alpha=local_weight,
    )
    predicted_grad = predicted_aggregate_delta.neg()
    predicted_flat, _predicted_state = outer_optimizer_step(
        global_flat,
        predicted_grad,
        outer_state,
        config.outer_optimizer,
    )
    predicted_flat = predicted_flat.detach().contiguous()
    prediction_delta_norm = float(
        torch.linalg.vector_norm(
            predicted_flat - global_flat,
            ord=2,
            dtype=torch.float32,
        ).item()
    )
    local_delta_norm = float(
        torch.linalg.vector_norm(local_delta, ord=2, dtype=torch.float32).item()
    )
    historical_delta_norm = float(
        torch.linalg.vector_norm(historical_delta, ord=2, dtype=torch.float32).item()
    )
    return predicted_flat, {
        "base_version": int(latest["version"]),
        "predicted_version": int(latest["version"]) + 1,
        "local_tokens": local_tokens,
        "previous_total_update_tokens": previous_total_update_tokens,
        "estimated_total_tokens": estimated_total_tokens,
        "bootstrapped_total_tokens": bootstrapped_total_tokens,
        "local_weight": local_weight,
        "local_delta_norm": local_delta_norm,
        "historical_delta_norm": historical_delta_norm,
        "prediction_delta_norm": prediction_delta_norm,
    }


def predict_next_global_weight(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    device: torch.device,
    config: Config,
    local_tokens: int,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Predict one full outer step from one local proposal and historical momentum.

    The outer momentum is a gradient-space state, so it is first converted to a
    displacement proxy.  The predicted aggregate displacement is then passed
    through the real outer optimizer instead of being added directly to the
    model parameters.
    """

    if config.outer_optimizer.name.lower() != "nesterov":
        raise ValueError("global prediction currently requires outer nesterov")
    if config.outer_optimizer.weight_decay != 0.0:
        raise ValueError("global prediction currently requires outer weight_decay=0")
    local_tokens = int(local_tokens)
    if local_tokens <= 0:
        raise ValueError("global prediction requires positive local_tokens")

    placement = choose_learner_compute_placement(
        preferred_device=device,
        dtype=learner_compute_dtype(config),
        total_numel=int(param_index["total_numel"]),
        working_vector_count=PREDICTION_WORKING_VECTOR_COUNT,
    )
    try:
        predicted_flat, stats = _predict_next_global_weight_on_placement(
            model=model,
            latest=latest,
            param_index=param_index,
            config=config,
            local_tokens=local_tokens,
            placement=placement,
        )
    except torch.OutOfMemoryError:
        if placement.device.type != "cuda":
            raise
        placement = cpu_fallback_placement(placement, reason="cuda_oom")
        torch.cuda.empty_cache()
        predicted_flat, stats = _predict_next_global_weight_on_placement(
            model=model,
            latest=latest,
            param_index=param_index,
            config=config,
            local_tokens=local_tokens,
            placement=placement,
        )

    load_flat_into_model(model, predicted_flat, param_index)
    model.to(device)
    stats.update(placement.log_fields("prediction"))
    return predicted_flat, stats


def prepare_prediction_or_find_newer_latest(
    *,
    paths: RunPaths,
    model: torch.nn.Module,
    cached_latest: dict[str, Any],
    param_index: dict[str, Any],
    device: torch.device,
    config: Config,
    local_tokens: int,
) -> tuple[
    torch.Tensor | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Prepare a prediction without racing current-only checkpoint GC.

    A learner may observe no newer ``latest.json`` immediately before the
    syncer publishes the next version.  In that window the cached checkpoint
    can be collected while its weight and outer files are being loaded.  A
    newer latest always wins: check immediately before and after the snapshot,
    and recover a missing cached file by waiting boundedly for that latest.
    """

    base_version = int(cached_latest["version"])
    newer_latest = read_latest_if_newer(paths, base_version)
    if newer_latest is not None:
        return (
            None,
            None,
            newer_latest,
            {
                "reason": "newer_latest_before_prediction",
                "cached_version": base_version,
                "waited_seconds": 0.0,
            },
        )

    try:
        reference_flat, stats = predict_next_global_weight(
            model=model,
            latest=cached_latest,
            param_index=param_index,
            device=device,
            config=config,
            local_tokens=local_tokens,
        )
    except FileNotFoundError as exc:
        newer_latest, waited_seconds = wait_for_latest_if_newer(
            paths,
            base_version,
            wait_seconds=config.learner.prediction.reconcile_timeout_seconds,
            poll_seconds=config.learner.post_publish_latest_poll_seconds,
        )
        if newer_latest is None:
            raise
        return (
            None,
            None,
            newer_latest,
            {
                "reason": "cached_checkpoint_collected",
                "cached_version": base_version,
                "missing_path": getattr(exc, "filename", None) or str(exc),
                "waited_seconds": waited_seconds,
            },
        )

    newer_latest = read_latest_if_newer(paths, base_version)
    if newer_latest is not None:
        return (
            None,
            None,
            newer_latest,
            {
                "reason": "newer_latest_after_prediction_snapshot",
                "cached_version": base_version,
                "waited_seconds": 0.0,
            },
        )
    return reference_flat, stats, None, None


def load_fragment_latest_into_model(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    fragment_index: dict[str, Any],
    device: torch.device,
) -> tuple[int, dict[int, int]]:
    fragments = latest.get("fragments") or {}
    fragment_tensors = {
        int(fragment_id): load_fragment_weight(info["weight_path"])
        for fragment_id, info in fragments.items()
    }
    flat = materialize_full_from_fragments(
        fragment_tensors,
        fragment_index,
        int(param_index["total_numel"]),
    )
    load_flat_into_model(model, flat, param_index)
    model.to(device)
    versions = {int(fragment_id): int(info["version"]) for fragment_id, info in fragments.items()}
    return int(latest.get("global_merge_event", latest.get("version", 0))), versions


def adopt_fragment_updates(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    fragment_index: dict[str, Any],
    last_loaded_fragment_versions: dict[int, int],
    device: torch.device,
) -> tuple[int, dict[int, int], list[int]]:
    fragments = latest.get("fragments") or {}
    changed: list[int] = []
    flat = flatten_trainable_params(model, param_index, dtype=torch.float32)
    for fragment_id_text, info in sorted(fragments.items(), key=lambda item: int(item[0])):
        fragment_id = int(fragment_id_text)
        version = int(info["version"])
        if version <= int(last_loaded_fragment_versions.get(fragment_id, -1)):
            continue
        fragment_tensor = load_fragment_weight(info["weight_path"])
        flat = scatter_fragment(flat, fragment_index, fragment_id, fragment_tensor)
        last_loaded_fragment_versions[fragment_id] = version
        changed.append(fragment_id)
    if changed:
        load_flat_into_model(model, flat, param_index)
        model.to(device)
    return (
        int(latest.get("global_merge_event", latest.get("version", 0))),
        last_loaded_fragment_versions,
        changed,
    )


@dataclass(frozen=True)
class FragmentAdoptionResult:
    global_merge_event: int
    fragment_versions: dict[int, int]
    tokens_since_fragment_load: dict[int, int]
    fragment_adopt_count: int
    last_adopted_fragments: list[int]
    optimizer: Any
    scheduler: Any


def apply_fragment_adoption(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    fragment_index: dict[str, Any],
    last_loaded_fragment_versions: dict[int, int],
    tokens_since_fragment_load: dict[int, int],
    fragment_adopt_count: int,
    last_adopted_fragments: list[int],
    optimizer: Any,
    scheduler: Any,
    device: torch.device,
    config: Config,
    logger: JsonlLogger,
    event_type: str,
    reset_tokens: bool,
    reset_optimizer: bool,
    include_fragment_versions: bool,
    adopt_fn: Callable[..., tuple[int, dict[int, int], list[int]]] = adopt_fragment_updates,
    rebuild_inner_state_fn: Callable[..., tuple[Any, Any]] = (
        build_inner_optimizer_and_scheduler
    ),
) -> FragmentAdoptionResult:
    """Apply one fragment latest snapshot with call-site-specific legacy semantics."""

    global_merge_event, fragment_versions, changed = adopt_fn(
        model=model,
        latest=latest,
        param_index=param_index,
        fragment_index=fragment_index,
        last_loaded_fragment_versions=last_loaded_fragment_versions,
        device=device,
    )
    next_tokens = dict(tokens_since_fragment_load)
    next_count = fragment_adopt_count
    next_last_adopted = last_adopted_fragments
    next_optimizer = optimizer
    next_scheduler = scheduler
    if changed:
        next_count += len(changed)
        next_last_adopted = changed
        if reset_tokens:
            for fragment_id in changed:
                next_tokens[fragment_id] = 0
        if reset_optimizer and config.fragments.reset_inner_optimizer_on_fragment_adopt:
            next_optimizer, next_scheduler = rebuild_inner_state_fn(model, config)
            logger.event(
                "inner_optimizer_reset",
                version=global_merge_event,
                fragments=changed,
            )
        event_fields: dict[str, Any] = {
            "global_merge_event": global_merge_event,
            "fragments": changed,
        }
        if include_fragment_versions:
            event_fields["fragment_versions"] = fragment_versions
        logger.event(event_type, **event_fields)
    return FragmentAdoptionResult(
        global_merge_event=global_merge_event,
        fragment_versions=fragment_versions,
        tokens_since_fragment_load=next_tokens,
        fragment_adopt_count=next_count,
        last_adopted_fragments=next_last_adopted,
        optimizer=next_optimizer,
        scheduler=next_scheduler,
    )


def write_update(
    *,
    paths: RunPaths,
    config: Config,
    learner_id: str,
    base_global_version: int,
    interval_start_step: int,
    local_step: int,
    inner_steps: int,
    tokens_this_update: int,
    tokens_since_global_load: int,
    num_examples: int,
    train_loss: float,
    grad_norm: float | None,
    param_norm: float,
    flat: torch.Tensor,
    resource_metrics: dict[str, float | int],
) -> tuple[str, Path, Path, dict[str, Any]]:
    update_uuid = uuid.uuid4().hex[:12]
    update_id = f"{learner_id}_{local_step:08d}_{update_uuid}"
    update_dir = paths.update_payload_dir(learner_id)
    tensor_path = update_dir / f"{update_id}.params.safetensors"
    meta_path = paths.update_pointer_path(learner_id)
    created_at = time.time()
    save_update_vector(tensor_path, flat, dtype=dtype_from_name(config.io.tensor_dtype))
    digest = sha256_file(tensor_path) if config.io.compute_sha256 else None
    metadata = {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "update_id": update_id,
        "learner_id": learner_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "base_global_version": base_global_version,
        "local_step_start": interval_start_step,
        "local_step_end": local_step,
        "inner_steps": inner_steps,
        "tokens_this_update": tokens_this_update,
        "tokens_since_global_load": tokens_since_global_load,
        "num_examples_this_update": num_examples,
        "train_loss": train_loss,
        "grad_norm": grad_norm,
        "param_norm": param_norm,
        "delta_norm": None,
        "tensor_dtype": config.io.tensor_dtype,
        "file_path": str(tensor_path),
        "file_size_bytes": file_size(tensor_path),
        "sha256": digest,
        "created_at": created_at,
        "committed_at": time.time(),
    }
    metadata.update(resource_metrics)
    atomic_write_json(meta_path, metadata)
    return update_id, tensor_path, meta_path, metadata


def write_fragment_update(
    *,
    paths: RunPaths,
    config: Config,
    learner_id: str,
    fragment_id: int,
    base_fragment_version: int,
    base_global_merge_event: int,
    interval_start_step: int,
    local_step: int,
    inner_steps: int,
    tokens_this_update: int,
    tokens_since_fragment_load: int,
    num_examples: int,
    train_loss: float,
    grad_norm: float | None,
    param_norm: float,
    fragment_norm: float,
    fragment_tensor: torch.Tensor,
    resource_metrics: dict[str, float | int],
) -> tuple[str, Path, Path, dict[str, Any]]:
    update_uuid = uuid.uuid4().hex[:12]
    update_id = f"{learner_id}_{local_step:08d}_f{fragment_id:03d}_{update_uuid}"
    update_dir = paths.updates_pending / learner_id
    tensor_path = update_dir / f"update_{update_uuid}_fragment_{fragment_id:03d}.params.safetensors"
    meta_path = update_dir / f"update_{update_uuid}_fragment_{fragment_id:03d}.meta.json"
    created_at = time.time()
    save_fragment_update(tensor_path, fragment_tensor, dtype_from_name(config.io.tensor_dtype))
    digest = sha256_file(tensor_path) if config.io.compute_sha256 else None
    metadata = {
        "format_version": FORMAT_VERSION,
        "update_kind": "fragment",
        "run_id": config.run.run_id,
        "update_id": update_id,
        "learner_id": learner_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "fragment_id": int(fragment_id),
        "base_fragment_version": int(base_fragment_version),
        "base_global_merge_event": int(base_global_merge_event),
        "local_step_start": interval_start_step,
        "local_step_end": local_step,
        "inner_steps": inner_steps,
        "tokens_this_update": tokens_this_update,
        "tokens_since_fragment_load": tokens_since_fragment_load,
        "num_examples_this_update": num_examples,
        "train_loss": train_loss,
        "grad_norm": grad_norm,
        "param_norm": param_norm,
        "fragment_norm": fragment_norm,
        "tensor_dtype": config.io.tensor_dtype,
        "file_path": str(tensor_path),
        "file_size_bytes": file_size(tensor_path),
        "sha256": digest,
        "created_at": created_at,
        "committed_at": time.time(),
    }
    metadata.update(resource_metrics)
    atomic_write_json(meta_path, metadata)
    return update_id, tensor_path, meta_path, metadata


def run_fragment_learner(config: Config, learner_id: str) -> None:
    paths = RunPaths(Path(config.run.shared_root or "."))
    prepare_run_dirs(paths, config.sync.num_learners)
    logger = JsonlLogger(paths.logs / f"{learner_id}.jsonl", learner_id)
    log_uncaught_exception(logger)
    learner_index = learner_index_from_id(learner_id)
    torch.manual_seed(config.training.seed + learner_index)
    device = choose_device()
    logger.event(
        "process_start",
        run_id=config.run.run_id,
        learner_id=learner_id,
        device=str(device),
        hostname=socket.gethostname(),
        fragment_mode=True,
    )
    model, tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    model.train()
    wait_for_json(paths.param_index_json)
    wait_for_json(paths.fragment_index_json)
    param_index = load_param_index(paths.param_index_json)
    fragment_index = load_fragment_index(paths.fragment_index_json)
    current_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_compatible_index(current_index, param_index)
    latest = wait_for_json(paths.latest_json)
    if latest.get("latest_kind") != "fragment":
        raise ValueError("fragment learner requires latest_kind=fragment")
    last_loaded_global_merge_event, last_loaded_fragment_versions = load_fragment_latest_into_model(
        model=model,
        latest=latest,
        param_index=param_index,
        fragment_index=fragment_index,
        device=device,
    )
    optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
    tokens_since_fragment_load = {fragment_id: 0 for fragment_id in last_loaded_fragment_versions}
    local_update_index = 0
    fragment_adopt_count = 0
    last_adopted_fragments: list[int] = []
    logger.event(
        "loaded_fragment_latest",
        global_merge_event=last_loaded_global_merge_event,
        fragment_versions=last_loaded_fragment_versions,
    )
    logger.event("inner_optimizer_reset", version=last_loaded_global_merge_event)
    write_heartbeat(
        paths=paths,
        config=config,
        learner_id=learner_id,
        status=LEARNER_STATUS_ACTIVE,
        phase="loaded_fragment_latest",
        last_loaded_global_version=last_loaded_global_merge_event,
        last_loaded_global_merge_event=last_loaded_global_merge_event,
        last_loaded_fragment_versions=last_loaded_fragment_versions,
        last_adopted_fragments=last_adopted_fragments,
        last_local_step=0,
        last_update_id=None,
    )

    batch_iter = build_batch_iterator(
        config,
        tokenizer,
        learner_index=learner_index,
        num_learners=config.sync.num_learners,
    )
    resource_monitor = create_resource_monitor(device)
    resource_monitor.start()
    local_step = 0
    last_heartbeat = time.monotonic()
    last_update_id: str | None = None
    had_error = False

    def handle_fragment_latest(
        latest_payload: dict[str, Any],
        *,
        event_type: str,
        reset_tokens: bool,
        reset_optimizer: bool,
        include_fragment_versions: bool,
    ) -> None:
        nonlocal fragment_adopt_count
        nonlocal last_adopted_fragments
        nonlocal last_loaded_fragment_versions
        nonlocal last_loaded_global_merge_event
        nonlocal optimizer
        nonlocal scheduler
        nonlocal tokens_since_fragment_load
        result = apply_fragment_adoption(
            model=model,
            latest=latest_payload,
            param_index=param_index,
            fragment_index=fragment_index,
            last_loaded_fragment_versions=last_loaded_fragment_versions,
            tokens_since_fragment_load=tokens_since_fragment_load,
            fragment_adopt_count=fragment_adopt_count,
            last_adopted_fragments=last_adopted_fragments,
            optimizer=optimizer,
            scheduler=scheduler,
            device=device,
            config=config,
            logger=logger,
            event_type=event_type,
            reset_tokens=reset_tokens,
            reset_optimizer=reset_optimizer,
            include_fragment_versions=include_fragment_versions,
        )
        last_loaded_global_merge_event = result.global_merge_event
        last_loaded_fragment_versions = result.fragment_versions
        tokens_since_fragment_load = result.tokens_since_fragment_load
        fragment_adopt_count = result.fragment_adopt_count
        last_adopted_fragments = result.last_adopted_fragments
        optimizer = result.optimizer
        scheduler = result.scheduler

    try:
        while not stop_requested(paths, local_step, config):
            resource_monitor.begin_cycle()
            interval_start_time = time.monotonic()
            interval_start_step = local_step
            base_global_merge_event = last_loaded_global_merge_event
            losses: list[float] = []
            interval_tokens = 0
            interval_examples = 0
            grad_norm: float | None = None

            for _ in range(config.training.inner_steps):
                if stop_requested(paths, local_step, config):
                    break
                step_start = time.monotonic()
                loss, step_tokens, step_examples, grad_norm = train_one_step(
                    model,
                    batch_iter,
                    optimizer,
                    scheduler,
                    device=device,
                    config=config,
                )
                resource_monitor.record_step_duration(time.monotonic() - step_start)
                local_step += 1
                if (
                    config.training.completion_mode == "global_only"
                    and config.training.max_local_steps is not None
                    and local_step == config.training.max_local_steps
                ):
                    logger.event(
                        "local_step_horizon_reached",
                        local_step=local_step,
                        awaiting_global_stop=True,
                    )
                interval_tokens += step_tokens
                interval_examples += step_examples
                for fragment_id in tokens_since_fragment_load:
                    tokens_since_fragment_load[fragment_id] += step_tokens
                losses.append(loss)
                if local_step % max(1, config.training.log_every_steps) == 0:
                    logger.event(
                        "inner_step_summary",
                        local_step=local_step,
                        train_loss=loss,
                        global_merge_event=last_loaded_global_merge_event,
                    )
                if time.monotonic() - last_heartbeat >= config.liveness.heartbeat_interval_seconds:
                    elapsed = max(1e-6, time.monotonic() - interval_start_time)
                    write_heartbeat(
                        paths=paths,
                        config=config,
                        learner_id=learner_id,
                        status=LEARNER_STATUS_ACTIVE,
                        phase="inner_steps",
                        last_loaded_global_version=last_loaded_global_merge_event,
                        last_loaded_global_merge_event=last_loaded_global_merge_event,
                        last_loaded_fragment_versions=last_loaded_fragment_versions,
                        last_adopted_fragments=last_adopted_fragments,
                        last_local_step=local_step,
                        last_update_id=last_update_id,
                        tokens_per_sec=interval_tokens / elapsed,
                    )
                    logger.event("heartbeat_written", local_step=local_step)
                    last_heartbeat = time.monotonic()
                if config.learner.poll_latest_during_inner_steps:
                    maybe_latest = read_fragment_latest_if_newer(
                        paths, last_loaded_global_merge_event
                    )
                    if maybe_latest is not None:
                        handle_fragment_latest(
                            maybe_latest,
                            event_type="fragments_adopted",
                            reset_tokens=True,
                            reset_optimizer=True,
                            include_fragment_versions=True,
                        )

            if not losses:
                continue
            cycle_resources = finite_resource_metrics(resource_monitor.cycle_snapshot())
            maybe_sleep_jitter(config.failure_sim)
            if should_skip_upload(config.failure_sim):
                logger.event("update_skipped", local_step=local_step)
                maybe_crash(config.failure_sim)
                continue

            write_start = time.monotonic()
            upload_dtype = dtype_from_name(config.io.tensor_dtype)
            param_norm = float(trainable_params_l2_norm(model).item())
            mean_loss = sum(losses) / len(losses)
            fragment_id = select_fragment(
                local_update_index,
                int(fragment_index["num_fragments"]),
                schedule=config.fragments.schedule,
            )
            base_fragment_version = int(last_loaded_fragment_versions[fragment_id])
            fragment_tensor = extract_fragment_from_model(
                model,
                fragment_index,
                fragment_id,
                dtype=upload_dtype,
            )
            fragment_norm = float(
                torch.linalg.vector_norm(fragment_tensor, ord=2, dtype=torch.float32).item()
            )
            update_id, tensor_path, _meta_path, metadata = write_fragment_update(
                paths=paths,
                config=config,
                learner_id=learner_id,
                fragment_id=fragment_id,
                base_fragment_version=base_fragment_version,
                base_global_merge_event=base_global_merge_event,
                interval_start_step=interval_start_step,
                local_step=local_step,
                inner_steps=len(losses),
                tokens_this_update=interval_tokens,
                tokens_since_fragment_load=tokens_since_fragment_load[fragment_id],
                num_examples=interval_examples,
                train_loss=mean_loss,
                grad_norm=grad_norm,
                param_norm=param_norm,
                fragment_norm=fragment_norm,
                fragment_tensor=fragment_tensor,
                resource_metrics=cycle_resources,
            )
            # Fragment updates are consumed on a per-fragment schedule, so local
            # step order is not a safe retention key for pending files.
            write_seconds = time.monotonic() - write_start
            last_update_id = update_id
            local_update_index += 1
            elapsed = max(1e-6, time.monotonic() - interval_start_time)
            tokens_per_sec = interval_tokens / elapsed
            logger.event(
                "fragment_update_written",
                update_id=update_id,
                fragment_id=fragment_id,
                base_fragment_version=base_fragment_version,
                file_path=str(tensor_path),
                local_step=local_step,
                train_loss=mean_loss,
                tokens=interval_tokens,
            )
            append_csv_row(
                paths.metrics / "learner_metrics.csv",
                {
                    "timestamp": time.time(),
                    "learner_id": learner_id,
                    "local_step": local_step,
                    "global_version": last_loaded_global_merge_event,
                    "global_merge_event": last_loaded_global_merge_event,
                    "fragment_id": fragment_id,
                    "base_fragment_version": base_fragment_version,
                    "train_loss": mean_loss,
                    "tokens": interval_tokens,
                    "tokens_per_sec": tokens_per_sec,
                    "update_write_seconds": write_seconds,
                    "param_norm": param_norm,
                    "fragment_norm": fragment_norm,
                    "last_loaded_fragment_versions_json": json.dumps(
                        {str(k): v for k, v in sorted(last_loaded_fragment_versions.items())},
                        sort_keys=True,
                    ),
                    "fragment_adopt_count": fragment_adopt_count,
                    "phase": "fragment_update_written",
                    **cycle_resources,
                },
                LEARNER_METRIC_FIELDS,
            )
            append_csv_row(
                paths.metrics / "update_manifest.csv",
                {
                    "timestamp": time.time(),
                    "update_id": update_id,
                    "learner_id": learner_id,
                    "update_kind": "fragment",
                    "fragment_id": fragment_id,
                    "base_fragment_version": base_fragment_version,
                    "base_global_merge_event": base_global_merge_event,
                    "base_global_version": base_global_merge_event,
                    "local_step_start": interval_start_step,
                    "local_step_end": local_step,
                    "tokens_this_update": interval_tokens,
                    "tensor_dtype": metadata["tensor_dtype"],
                    "file_path": metadata["file_path"],
                    "file_size_bytes": metadata["file_size_bytes"],
                    "sha256": metadata["sha256"],
                },
                UPDATE_MANIFEST_FIELDS,
            )
            write_heartbeat(
                paths=paths,
                config=config,
                learner_id=learner_id,
                status=LEARNER_STATUS_ACTIVE,
                phase="fragment_update_written",
                last_loaded_global_version=last_loaded_global_merge_event,
                last_loaded_global_merge_event=last_loaded_global_merge_event,
                last_loaded_fragment_versions=last_loaded_fragment_versions,
                last_adopted_fragments=last_adopted_fragments,
                last_local_step=local_step,
                last_update_id=last_update_id,
                tokens_per_sec=tokens_per_sec,
                resource_metrics=cycle_resources,
            )
            logger.event("heartbeat_written", local_step=local_step)

            if config.learner.adopt_global_after_upload:
                maybe_latest = wait_for_fragment_latest_if_newer(
                    paths, last_loaded_global_merge_event, config
                )
                logger.event(
                    "fragment_latest_polled",
                    current_global_merge_event=last_loaded_global_merge_event,
                    found_global_merge_event=(
                        maybe_latest.get("global_merge_event") if maybe_latest else None
                    ),
                )
                if maybe_latest is not None:
                    handle_fragment_latest(
                        maybe_latest,
                        event_type="fragments_adopted",
                        reset_tokens=True,
                        reset_optimizer=True,
                        include_fragment_versions=True,
                    )
            maybe_crash(config.failure_sim)
    except Exception:
        had_error = True
        logger.exception(
            "error", local_step=local_step, global_version=last_loaded_global_merge_event
        )
        raise
    finally:
        resource_monitor.stop()
        training_resources = finite_resource_metrics(resource_monitor.training_snapshot())
        try:
            target_event = config.sync.stop_after_outer_steps
            if not had_error and target_event is not None:
                deadline = time.monotonic() + config.liveness.no_progress_timeout_seconds
                while (
                    last_loaded_global_merge_event < int(target_event)
                    and not paths.stop_json.exists()
                ):
                    if time.monotonic() >= deadline:
                        logger.event(
                            "final_fragment_wait_timeout",
                            current_global_merge_event=last_loaded_global_merge_event,
                            target_global_merge_event=int(target_event),
                        )
                        break
                    maybe_latest = read_fragment_latest_if_newer(
                        paths, last_loaded_global_merge_event
                    )
                    if maybe_latest is not None:
                        handle_fragment_latest(
                            maybe_latest,
                            event_type="final_wait_fragments_adopted",
                            reset_tokens=True,
                            reset_optimizer=False,
                            include_fragment_versions=False,
                        )
                        continue
                    time.sleep(config.sync.stop_file_poll_seconds)
            maybe_latest = safe_read_json(paths.latest_json)
            if maybe_latest and maybe_latest.get("latest_kind") == "fragment":
                handle_fragment_latest(
                    maybe_latest,
                    event_type="final_fragments_adopted",
                    reset_tokens=False,
                    reset_optimizer=False,
                    include_fragment_versions=False,
                )
        except Exception as exc:
            logger.event("final_fragment_adoption_failed", error=repr(exc))
        if paths.stop_json.exists():
            stop_payload = safe_read_json(paths.stop_json) or {}
            logger.event("stop_seen", reason=stop_payload.get("reason"))
        write_heartbeat(
            paths=paths,
            config=config,
            learner_id=learner_id,
            status=LEARNER_STATUS_STOPPED,
            phase="process_exit",
            last_loaded_global_version=last_loaded_global_merge_event,
            last_loaded_global_merge_event=last_loaded_global_merge_event,
            last_loaded_fragment_versions=last_loaded_fragment_versions,
            last_adopted_fragments=last_adopted_fragments,
            last_local_step=local_step,
            last_update_id=last_update_id,
            resource_metrics=training_resources,
        )
        logger.event(
            "process_exit",
            local_step=local_step,
            global_version=last_loaded_global_merge_event,
            fragment_versions=last_loaded_fragment_versions,
            fragment_adopt_count=fragment_adopt_count,
            **training_resources,
        )


def build_adoption_context(
    *,
    model: torch.nn.Module,
    paths: RunPaths,
    param_index: dict[str, Any],
    device: torch.device,
    config: Config,
    logger: JsonlLogger,
    last_loaded_global_version: int,
    last_loaded_latest: dict[str, Any],
    tokens_since_global_load: int,
) -> AdoptionContext:
    return AdoptionContext(
        model=model,
        paths=paths,
        param_index=param_index,
        device=device,
        config=config,
        logger=logger,
        last_loaded_global_version=last_loaded_global_version,
        last_loaded_latest=last_loaded_latest,
        tokens_since_global_load=tokens_since_global_load,
        read_latest_if_newer_fn=read_latest_if_newer,
        wait_for_latest_if_newer_fn=wait_for_latest_if_newer,
        adopt_global_fn=adopt_global,
        rebase_local_delta_fn=rebase_local_delta_onto_global,
        snapshot_model_fn=snapshot_model_for_reconcile,
        prepare_prediction_fn=prepare_prediction_or_find_newer_latest,
    )


def finalize_strategy_action(
    action: StrategyAction,
    *,
    model: torch.nn.Module,
    config: Config,
    logger: JsonlLogger,
    current_version: int,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler | None,
) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler | None]:
    outcome = action.adoption
    if outcome is not None:
        logger.event("global_adopted", version=outcome.version)
        if outcome.preserve_inner_state:
            logger.event(
                "inner_training_state_preserved",
                version=outcome.version,
                reason=outcome.reason,
                optimizer_state_preserved=True,
                scheduler_state_preserved=True,
                **inner_training_state_metrics(optimizer, scheduler),
            )
            return optimizer, scheduler
        optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
        logger.event("inner_optimizer_reset", version=outcome.version)
        return optimizer, scheduler
    if action.reset_optimizer_reason is not None:
        optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
        logger.event(
            "inner_optimizer_reset",
            version=current_version,
            reason=action.reset_optimizer_reason,
            **inner_training_state_metrics(optimizer, scheduler),
        )
    return optimizer, scheduler


def run_learner(config: Config, learner_id: str) -> None:
    if config.fragments.enabled:
        run_fragment_learner(config, learner_id)
        return
    paths = RunPaths(Path(config.run.shared_root or "."))
    prepare_run_dirs(paths, config.sync.num_learners)
    logger = JsonlLogger(paths.logs / f"{learner_id}.jsonl", learner_id)
    log_uncaught_exception(logger)
    learner_index = learner_index_from_id(learner_id)
    torch.manual_seed(config.training.seed + learner_index)
    device = choose_device()
    logger.event(
        "process_start",
        run_id=config.run.run_id,
        learner_id=learner_id,
        device=str(device),
        reconcile_compute_dtype=config.syncer.compute_dtype,
        hostname=socket.gethostname(),
    )
    model, tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    model.train()
    wait_for_json(paths.param_index_json)
    param_index = load_param_index(paths.param_index_json)
    current_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_compatible_index(current_index, param_index)
    latest = wait_for_json(paths.latest_json)
    last_loaded_global_version = adopt_global(
        model=model,
        latest=latest,
        param_index=param_index,
        device=device,
    )
    last_loaded_latest = latest
    optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
    adoption_strategy = make_global_adoption_strategy(config)
    logger.event("loaded_global", version=last_loaded_global_version)
    logger.event("inner_optimizer_reset", version=last_loaded_global_version)
    write_heartbeat(
        paths=paths,
        config=config,
        learner_id=learner_id,
        status=LEARNER_STATUS_ACTIVE,
        phase="loaded_global",
        last_loaded_global_version=last_loaded_global_version,
        last_local_step=0,
        last_update_id=None,
    )

    batch_iter = build_batch_iterator(
        config,
        tokenizer,
        learner_index=learner_index,
        num_learners=config.sync.num_learners,
    )
    resource_monitor = create_resource_monitor(device)
    resource_monitor.start()
    local_step = 0
    tokens_since_global_load = 0
    last_heartbeat = time.monotonic()
    last_update_id: str | None = None

    def current_adoption_context() -> AdoptionContext:
        return build_adoption_context(
            model=model,
            paths=paths,
            param_index=param_index,
            device=device,
            config=config,
            logger=logger,
            last_loaded_global_version=last_loaded_global_version,
            last_loaded_latest=last_loaded_latest,
            tokens_since_global_load=tokens_since_global_load,
        )

    def apply_strategy_action(action: StrategyAction) -> None:
        nonlocal last_loaded_global_version
        nonlocal last_loaded_latest
        nonlocal optimizer
        nonlocal scheduler
        nonlocal tokens_since_global_load
        optimizer, scheduler = finalize_strategy_action(
            action,
            model=model,
            config=config,
            logger=logger,
            current_version=last_loaded_global_version,
            optimizer=optimizer,
            scheduler=scheduler,
        )
        if action.adoption is not None:
            last_loaded_global_version = action.adoption.version
            last_loaded_latest = action.adoption.latest
            tokens_since_global_load = action.adoption.tokens_since_global_load
        elif action.tokens_since_global_load is not None:
            tokens_since_global_load = action.tokens_since_global_load

    try:
        while not stop_requested(paths, local_step, config):
            resource_monitor.begin_cycle()
            interval_start_time = time.monotonic()
            interval_start_step = local_step
            base_global_version = last_loaded_global_version
            losses: list[float] = []
            interval_tokens = 0
            interval_examples = 0
            grad_norm: float | None = None

            for _ in range(config.training.inner_steps):
                if stop_requested(paths, local_step, config):
                    break
                step_start = time.monotonic()
                loss, step_tokens, step_examples, grad_norm = train_one_step(
                    model,
                    batch_iter,
                    optimizer,
                    scheduler,
                    device=device,
                    config=config,
                )
                resource_monitor.record_step_duration(time.monotonic() - step_start)
                local_step += 1
                if (
                    config.training.completion_mode == "global_only"
                    and config.training.max_local_steps is not None
                    and local_step == config.training.max_local_steps
                ):
                    logger.event(
                        "local_step_horizon_reached",
                        local_step=local_step,
                        awaiting_global_stop=True,
                    )
                interval_tokens += step_tokens
                interval_examples += step_examples
                tokens_since_global_load += step_tokens
                adoption_strategy.on_local_tokens(step_tokens)
                losses.append(loss)
                if local_step % max(1, config.training.log_every_steps) == 0:
                    logger.event(
                        "inner_step_summary",
                        local_step=local_step,
                        train_loss=loss,
                        global_version=last_loaded_global_version,
                    )
                if time.monotonic() - last_heartbeat >= config.liveness.heartbeat_interval_seconds:
                    elapsed = max(1e-6, time.monotonic() - interval_start_time)
                    write_heartbeat(
                        paths=paths,
                        config=config,
                        learner_id=learner_id,
                        status=LEARNER_STATUS_ACTIVE,
                        phase="inner_steps",
                        last_loaded_global_version=last_loaded_global_version,
                        last_local_step=local_step,
                        last_update_id=last_update_id,
                        tokens_per_sec=interval_tokens / elapsed,
                    )
                    logger.event("heartbeat_written", local_step=local_step)
                    last_heartbeat = time.monotonic()
                if adoption_strategy.wants_inner_poll(config):
                    maybe_latest = read_latest_if_newer(paths, last_loaded_global_version)
                    if maybe_latest is not None:
                        action = adoption_strategy.on_newer_latest(
                            current_adoption_context(),
                            maybe_latest,
                        )
                        apply_strategy_action(action)
                        base_global_version = last_loaded_global_version

            adoption_ctx = current_adoption_context()
            if paths.stop_json.exists() and adoption_strategy.on_stop(adoption_ctx):
                continue
            cycle_end_action = adoption_strategy.on_cycle_end(adoption_ctx)
            if cycle_end_action.adoption is not None:
                apply_strategy_action(cycle_end_action)
                base_global_version = last_loaded_global_version

            if not losses:
                continue
            cycle_resources = finite_resource_metrics(resource_monitor.cycle_snapshot())
            maybe_sleep_jitter(config.failure_sim)
            if should_skip_upload(config.failure_sim):
                logger.event("update_skipped", local_step=local_step)
                maybe_crash(config.failure_sim)
                continue

            write_start = time.monotonic()
            upload_dtype = dtype_from_name(config.io.tensor_dtype)
            adoption_strategy.before_publish(current_adoption_context())
            flat = flatten_trainable_params(
                model,
                param_index,
                dtype=upload_dtype,
            )
            param_norm = float(torch.linalg.vector_norm(flat, ord=2, dtype=torch.float32).item())
            mean_loss = sum(losses) / len(losses)
            update_id, tensor_path, _meta_path, metadata = write_update(
                paths=paths,
                config=config,
                learner_id=learner_id,
                base_global_version=base_global_version,
                interval_start_step=interval_start_step,
                local_step=local_step,
                inner_steps=len(losses),
                tokens_this_update=interval_tokens,
                tokens_since_global_load=tokens_since_global_load,
                num_examples=interval_examples,
                train_loss=mean_loss,
                grad_norm=grad_norm,
                param_norm=param_norm,
                flat=flat,
                resource_metrics=cycle_resources,
            )
            write_seconds = time.monotonic() - write_start
            last_update_id = update_id
            elapsed = max(1e-6, time.monotonic() - interval_start_time)
            tokens_per_sec = interval_tokens / elapsed
            logger.event(
                "update_written",
                update_id=update_id,
                file_path=str(tensor_path),
                local_step=local_step,
                train_loss=mean_loss,
                tokens=interval_tokens,
            )
            append_csv_row(
                paths.metrics / "learner_metrics.csv",
                {
                    "timestamp": time.time(),
                    "learner_id": learner_id,
                    "local_step": local_step,
                    "global_version": last_loaded_global_version,
                    "train_loss": mean_loss,
                    "tokens": interval_tokens,
                    "tokens_per_sec": tokens_per_sec,
                    "update_write_seconds": write_seconds,
                    "param_norm": param_norm,
                    "phase": "update_written",
                    **cycle_resources,
                },
                LEARNER_METRIC_FIELDS,
            )
            append_csv_row(
                paths.metrics / "update_manifest.csv",
                {
                    "timestamp": time.time(),
                    "update_id": update_id,
                    "learner_id": learner_id,
                    "base_global_version": base_global_version,
                    "local_step_start": interval_start_step,
                    "local_step_end": local_step,
                    "tokens_this_update": interval_tokens,
                    "tensor_dtype": metadata["tensor_dtype"],
                    "file_path": metadata["file_path"],
                    "file_size_bytes": metadata["file_size_bytes"],
                    "sha256": metadata["sha256"],
                },
                UPDATE_MANIFEST_FIELDS,
            )
            write_heartbeat(
                paths=paths,
                config=config,
                learner_id=learner_id,
                status=LEARNER_STATUS_ACTIVE,
                phase="update_written",
                last_loaded_global_version=last_loaded_global_version,
                last_local_step=local_step,
                last_update_id=last_update_id,
                tokens_per_sec=tokens_per_sec,
                resource_metrics=cycle_resources,
            )
            logger.event("heartbeat_written", local_step=local_step)
            del flat

            if config.learner.adopt_global_after_upload:
                post_publish_action = adoption_strategy.on_after_publish(
                    current_adoption_context(),
                    PublishResult(
                        update_id=update_id,
                        base_global_version=base_global_version,
                    ),
                )
                apply_strategy_action(post_publish_action)
            maybe_crash(config.failure_sim)
    except Exception:
        logger.exception("error", local_step=local_step, global_version=last_loaded_global_version)
        raise
    finally:
        resource_monitor.stop()
        training_resources = finite_resource_metrics(resource_monitor.training_snapshot())
        if paths.stop_json.exists():
            stop_payload = safe_read_json(paths.stop_json) or {}
            logger.event("stop_seen", reason=stop_payload.get("reason"))
        write_heartbeat(
            paths=paths,
            config=config,
            learner_id=learner_id,
            status=LEARNER_STATUS_STOPPED,
            phase="process_exit",
            last_loaded_global_version=last_loaded_global_version,
            last_local_step=local_step,
            last_update_id=last_update_id,
            resource_metrics=training_resources,
        )
        logger.event(
            "process_exit",
            local_step=local_step,
            global_version=last_loaded_global_version,
            **training_resources,
        )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = resolve_config(
        args.config,
        run_id=args.run_id,
        shared_root=args.shared_root,
        num_learners=args.num_learners,
    )
    run_learner(config, args.learner_id)


if __name__ == "__main__":
    main()
