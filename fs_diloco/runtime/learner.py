"""Filesystem-backed Decoupled DiLoCo learner."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import time
import uuid
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
from ..modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
    load_param_index,
    validate_compatible_index,
)
from ..observability.logging_utils import JsonlLogger, log_uncaught_exception
from ..observability.metrics import LEARNER_METRIC_FIELDS, UPDATE_MANIFEST_FIELDS, append_csv_row
from ..protocol.fragment_codec import (
    extract_fragment,
    load_fragment_weight,
    materialize_full_from_fragments,
    save_fragment_update,
    scatter_fragment,
)
from ..protocol.fragment_index import load_fragment_index
from ..protocol.fragment_scheduler import select_fragment
from ..storage.atomic_io import atomic_write_json, file_size, safe_read_json, sha256_file
from ..storage.paths import RunPaths, prepare_run_dirs
from ..storage.retention import cleanup_learner_update_artifacts
from ..storage.tensor_codec import dtype_from_name, load_global_weights_flat, save_update_vector
from .failure_sim import maybe_crash, maybe_sleep_jitter, should_skip_upload


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
        payload["last_adopted_fragments"] = [int(fragment_id) for fragment_id in last_adopted_fragments]
    atomic_write_json(paths.heartbeats / f"{learner_id}.json", payload)


def wait_for_json(path: Path, *, timeout_seconds: float = 1800.0, poll_seconds: float = 1.0) -> dict[str, Any]:
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


def read_fragment_latest_if_newer(paths: RunPaths, last_loaded_global_merge_event: int) -> dict[str, Any] | None:
    payload = safe_read_json(paths.latest_json)
    if payload is None or payload.get("latest_kind") != "fragment":
        return None
    if int(payload.get("global_merge_event", payload.get("version", -1))) <= last_loaded_global_merge_event:
        return None
    return payload


def wait_for_fragment_latest_if_newer(
    paths: RunPaths,
    last_loaded_global_merge_event: int,
    config: Config,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(
        config.sync.stop_file_poll_seconds,
        config.sync.scan_interval_seconds + config.sync.grace_window.fixed_seconds + 1.0,
    )
    while time.monotonic() <= deadline:
        payload = read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)
        if payload is not None:
            return payload
        time.sleep(min(config.sync.stop_file_poll_seconds, max(0.0, deadline - time.monotonic())))
    return None


def stop_requested(paths: RunPaths, local_step: int, config: Config) -> bool:
    if config.training.max_local_steps is not None and local_step >= config.training.max_local_steps:
        return True
    return paths.stop_json.exists()


def fragment_stop_requested(paths: RunPaths, local_step: int, config: Config) -> bool:
    if config.training.max_local_steps is not None:
        return local_step >= config.training.max_local_steps
    return paths.stop_json.exists()


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
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.training.grad_clip).detach().cpu()
        )
    optimizer.step()
    if scheduler is not None:
        scheduler.step()
    return total_loss / config.training.gradient_accumulation_steps, total_tokens, total_examples, grad_norm


def adopt_global(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    device: torch.device,
) -> int:
    flat = load_global_weights_flat(latest["weight_path"], param_index)
    load_flat_into_model(model, flat, param_index)
    model.to(device)
    return int(latest["version"])


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
    return int(latest.get("global_merge_event", latest.get("version", 0))), last_loaded_fragment_versions, changed


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
) -> tuple[str, Path, Path, dict[str, Any]]:
    update_uuid = uuid.uuid4().hex[:12]
    update_id = f"{learner_id}_{local_step:08d}_{update_uuid}"
    update_dir = paths.updates_pending / learner_id
    tensor_path = update_dir / f"update_{update_uuid}.params.safetensors"
    meta_path = update_dir / f"update_{update_uuid}.meta.json"
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
        "file_path": str(tensor_path),
        "file_size_bytes": file_size(tensor_path),
        "sha256": digest,
        "created_at": created_at,
        "committed_at": time.time(),
    }
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
        "file_path": str(tensor_path),
        "file_size_bytes": file_size(tensor_path),
        "sha256": digest,
        "created_at": created_at,
        "committed_at": time.time(),
    }
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
    local_step = 0
    last_heartbeat = time.monotonic()
    last_update_id: str | None = None
    had_error = False

    try:
        while not fragment_stop_requested(paths, local_step, config):
            interval_start_time = time.monotonic()
            interval_start_step = local_step
            base_global_merge_event = last_loaded_global_merge_event
            losses: list[float] = []
            interval_tokens = 0
            interval_examples = 0
            grad_norm: float | None = None

            for _ in range(config.training.inner_steps):
                if fragment_stop_requested(paths, local_step, config):
                    break
                loss, step_tokens, step_examples, grad_norm = train_one_step(
                    model,
                    batch_iter,
                    optimizer,
                    scheduler,
                    device=device,
                    config=config,
                )
                local_step += 1
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
                    maybe_latest = read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)
                    if maybe_latest is not None:
                        (
                            last_loaded_global_merge_event,
                            last_loaded_fragment_versions,
                            changed,
                        ) = adopt_fragment_updates(
                            model=model,
                            latest=maybe_latest,
                            param_index=param_index,
                            fragment_index=fragment_index,
                            last_loaded_fragment_versions=last_loaded_fragment_versions,
                            device=device,
                        )
                        if changed:
                            fragment_adopt_count += len(changed)
                            last_adopted_fragments = changed
                            for fragment_id in changed:
                                tokens_since_fragment_load[fragment_id] = 0
                            if config.fragments.reset_inner_optimizer_on_fragment_adopt:
                                optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
                                logger.event(
                                    "inner_optimizer_reset",
                                    version=last_loaded_global_merge_event,
                                    fragments=changed,
                                )
                            logger.event(
                                "fragments_adopted",
                                global_merge_event=last_loaded_global_merge_event,
                                fragments=changed,
                                fragment_versions=last_loaded_fragment_versions,
                            )

            if not losses:
                continue
            maybe_sleep_jitter(config.failure_sim)
            if should_skip_upload(config.failure_sim):
                logger.event("update_skipped", local_step=local_step)
                maybe_crash(config.failure_sim)
                continue

            write_start = time.monotonic()
            flat = flatten_trainable_params(model, param_index).float()
            param_norm = float(flat.norm().item())
            mean_loss = sum(losses) / len(losses)
            fragment_id = select_fragment(
                local_update_index,
                int(fragment_index["num_fragments"]),
                schedule=config.fragments.schedule,
            )
            base_fragment_version = int(last_loaded_fragment_versions[fragment_id])
            fragment_tensor = extract_fragment(flat, fragment_index, fragment_id)
            fragment_norm = float(fragment_tensor.norm().item())
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
            )
            logger.event("heartbeat_written", local_step=local_step)

            if config.learner.adopt_global_after_upload:
                maybe_latest = wait_for_fragment_latest_if_newer(paths, last_loaded_global_merge_event, config)
                logger.event(
                    "fragment_latest_polled",
                    current_global_merge_event=last_loaded_global_merge_event,
                    found_global_merge_event=(
                        maybe_latest.get("global_merge_event") if maybe_latest else None
                    ),
                )
                if maybe_latest is not None:
                    (
                        last_loaded_global_merge_event,
                        last_loaded_fragment_versions,
                        changed,
                    ) = adopt_fragment_updates(
                        model=model,
                        latest=maybe_latest,
                        param_index=param_index,
                        fragment_index=fragment_index,
                        last_loaded_fragment_versions=last_loaded_fragment_versions,
                        device=device,
                    )
                    if changed:
                        fragment_adopt_count += len(changed)
                        last_adopted_fragments = changed
                        for changed_fragment_id in changed:
                            tokens_since_fragment_load[changed_fragment_id] = 0
                        if config.fragments.reset_inner_optimizer_on_fragment_adopt:
                            optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
                            logger.event(
                                "inner_optimizer_reset",
                                version=last_loaded_global_merge_event,
                                fragments=changed,
                            )
                        logger.event(
                            "fragments_adopted",
                            global_merge_event=last_loaded_global_merge_event,
                            fragments=changed,
                            fragment_versions=last_loaded_fragment_versions,
                        )
            maybe_crash(config.failure_sim)
    except Exception:
        had_error = True
        logger.exception("error", local_step=local_step, global_version=last_loaded_global_merge_event)
        raise
    finally:
        try:
            target_event = config.sync.stop_after_outer_steps
            if not had_error and target_event is not None:
                deadline = time.monotonic() + config.liveness.no_progress_timeout_seconds
                while last_loaded_global_merge_event < int(target_event) and not paths.stop_json.exists():
                    if time.monotonic() >= deadline:
                        logger.event(
                            "final_fragment_wait_timeout",
                            current_global_merge_event=last_loaded_global_merge_event,
                            target_global_merge_event=int(target_event),
                        )
                        break
                    maybe_latest = read_fragment_latest_if_newer(paths, last_loaded_global_merge_event)
                    if maybe_latest is not None:
                        (
                            last_loaded_global_merge_event,
                            last_loaded_fragment_versions,
                            changed,
                        ) = adopt_fragment_updates(
                            model=model,
                            latest=maybe_latest,
                            param_index=param_index,
                            fragment_index=fragment_index,
                            last_loaded_fragment_versions=last_loaded_fragment_versions,
                            device=device,
                        )
                        if changed:
                            fragment_adopt_count += len(changed)
                            last_adopted_fragments = changed
                            for changed_fragment_id in changed:
                                tokens_since_fragment_load[changed_fragment_id] = 0
                            logger.event(
                                "final_wait_fragments_adopted",
                                global_merge_event=last_loaded_global_merge_event,
                                fragments=changed,
                            )
                        continue
                    time.sleep(config.sync.stop_file_poll_seconds)
            maybe_latest = safe_read_json(paths.latest_json)
            if maybe_latest and maybe_latest.get("latest_kind") == "fragment":
                (
                    last_loaded_global_merge_event,
                    last_loaded_fragment_versions,
                    changed,
                ) = adopt_fragment_updates(
                    model=model,
                    latest=maybe_latest,
                    param_index=param_index,
                    fragment_index=fragment_index,
                    last_loaded_fragment_versions=last_loaded_fragment_versions,
                    device=device,
                )
                if changed:
                    fragment_adopt_count += len(changed)
                    last_adopted_fragments = changed
                    logger.event(
                        "final_fragments_adopted",
                        global_merge_event=last_loaded_global_merge_event,
                        fragments=changed,
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
        )
        logger.event(
            "process_exit",
            local_step=local_step,
            global_version=last_loaded_global_merge_event,
            fragment_versions=last_loaded_fragment_versions,
            fragment_adopt_count=fragment_adopt_count,
        )


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
    optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
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
    local_step = 0
    tokens_since_global_load = 0
    last_heartbeat = time.monotonic()
    last_update_id: str | None = None

    try:
        while not stop_requested(paths, local_step, config):
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
                loss, step_tokens, step_examples, grad_norm = train_one_step(
                    model,
                    batch_iter,
                    optimizer,
                    scheduler,
                    device=device,
                    config=config,
                )
                local_step += 1
                interval_tokens += step_tokens
                interval_examples += step_examples
                tokens_since_global_load += step_tokens
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
                if config.learner.poll_latest_during_inner_steps:
                    maybe_latest = read_latest_if_newer(paths, last_loaded_global_version)
                    if maybe_latest is not None:
                        last_loaded_global_version = adopt_global(
                            model=model,
                            latest=maybe_latest,
                            param_index=param_index,
                            device=device,
                        )
                        optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
                        tokens_since_global_load = 0
                        logger.event("global_adopted", version=last_loaded_global_version)
                        logger.event("inner_optimizer_reset", version=last_loaded_global_version)

            if not losses:
                continue
            maybe_sleep_jitter(config.failure_sim)
            if should_skip_upload(config.failure_sim):
                logger.event("update_skipped", local_step=local_step)
                maybe_crash(config.failure_sim)
                continue

            write_start = time.monotonic()
            flat = flatten_trainable_params(model, param_index).float()
            param_norm = float(flat.norm().item())
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
            )
            cleanup_learner_update_artifacts(
                paths.updates_pending / learner_id,
                keep_last=config.io.keep_last_learner_update_versions,
                logger=logger,
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
            )
            logger.event("heartbeat_written", local_step=local_step)

            if config.learner.adopt_global_after_upload:
                maybe_latest = read_latest_if_newer(paths, last_loaded_global_version)
                logger.event(
                    "latest_polled",
                    current_version=last_loaded_global_version,
                    found_version=maybe_latest.get("version") if maybe_latest else None,
                )
                if maybe_latest is not None:
                    last_loaded_global_version = adopt_global(
                        model=model,
                        latest=maybe_latest,
                        param_index=param_index,
                        device=device,
                    )
                    optimizer, scheduler = build_inner_optimizer_and_scheduler(model, config)
                    tokens_since_global_load = 0
                    logger.event("global_adopted", version=last_loaded_global_version)
                    logger.event("inner_optimizer_reset", version=last_loaded_global_version)
            maybe_crash(config.failure_sim)
    except Exception:
        logger.exception("error", local_step=local_step, global_version=last_loaded_global_version)
        raise
    finally:
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
        )
        logger.event("process_exit", local_step=local_step, global_version=last_loaded_global_version)


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
