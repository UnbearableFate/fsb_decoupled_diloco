"""Independent DDP and periodic parameter-averaging training entrypoint."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import re
import socket
import time
from pathlib import Path
from typing import Any

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel

from .artifacts import (
    RANK_METRIC_FIELDS,
    SYNC_METRIC_FIELDS,
    BaselineRunPaths,
    JsonlLogger,
    append_csv,
    atomic_write_json,
    initialize_run,
    save_final_checkpoint,
    write_heartbeat,
)
from .config import BaselineConfig, load_config
from .data import build_batch_iterator
from .protocol import (
    average_trainable_parameters,
    broadcast_trainable_parameters,
    build_optimizer_and_scheduler,
    should_average,
    train_optimizer_step,
)


MODES = ("ddp", "periodic_average")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


def build_parser() -> argparse.ArgumentParser:
    """Construct the standalone training command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    return parser


def seed_everything(seed: int) -> None:
    """Seed Python and PyTorch random streams in the current worker."""

    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def runtime_device(backend: str, local_rank: int) -> torch.device:
    """Bind one worker to its torchrun-provided local CUDA ordinal."""

    if backend == "nccl":
        if not torch.cuda.is_available():
            raise RuntimeError("NCCL baseline requires CUDA")
        if not 0 <= local_rank < torch.cuda.device_count():
            raise RuntimeError(
                f"LOCAL_RANK={local_rank} is outside "
                f"{torch.cuda.device_count()} visible GPUs"
            )
        torch.cuda.set_device(local_rank)
        return torch.device("cuda", local_rank)
    return torch.device("cpu")


def torch_dtype(name: str) -> torch.dtype:
    """Translate the two supported config dtype names to PyTorch dtypes."""

    if name == "bfloat16":
        return torch.bfloat16
    if name == "float32":
        return torch.float32
    raise ValueError(f"unsupported model dtype: {name}")


def load_model_and_tokenizer(config: BaselineConfig) -> tuple[torch.nn.Module, Any]:
    """Load the pinned Hugging Face causal LM and tokenizer without fs_diloco imports."""

    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        config.model.name_or_path,
        revision=config.model.tokenizer_revision,
        trust_remote_code=config.model.trust_remote_code,
    )
    if tokenizer.pad_token is None and tokenizer.eos_token is not None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        config.model.name_or_path,
        revision=config.model.revision,
        trust_remote_code=config.model.trust_remote_code,
        dtype=torch_dtype(config.model.dtype),
    )
    return model, tokenizer


def validate_runtimes(
    config: BaselineConfig,
    runtimes: list[dict[str, Any]],
) -> None:
    """Reject process groups that do not match the declared formal topology."""

    expected = config.distributed.world_size
    if len(runtimes) != expected:
        raise RuntimeError(f"expected {expected} ranks, got {len(runtimes)}")
    ranks = sorted(int(runtime["rank"]) for runtime in runtimes)
    if ranks != list(range(expected)):
        raise RuntimeError(f"rank set mismatch: {ranks}")
    if any(runtime["backend"] != config.distributed.backend for runtime in runtimes):
        raise RuntimeError("distributed ranks disagree on backend")
    if config.distributed.backend == "nccl" and any(
        runtime["device_type"] != "cuda" for runtime in runtimes
    ):
        raise RuntimeError("NCCL baseline requires CUDA on every rank")
    if config.distributed.require_distinct_hosts:
        hosts = {str(runtime["hostname"]) for runtime in runtimes}
        if len(hosts) != expected:
            raise RuntimeError(
                f"expected {expected} distinct hosts, got {len(hosts)}: {sorted(hosts)}"
            )


def _source_commit() -> str:
    """Return the clean source commit injected by the PBS launcher."""

    commit = os.environ.get("TORCH_BASELINE_GIT_COMMIT", "")
    if COMMIT_SHA.fullmatch(commit) is None:
        raise RuntimeError("TORCH_BASELINE_GIT_COMMIT must be a 40-character commit SHA")
    return commit


def run(args: argparse.Namespace) -> None:
    """Execute one complete distributed baseline experiment."""

    config = load_config(args.config)
    source_commit = _source_commit()
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    device = runtime_device(config.distributed.backend, local_rank)
    dist.init_process_group(
        backend=config.distributed.backend,
        timeout=dt.timedelta(seconds=300),
    )
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    hostname = socket.gethostname()
    paths = BaselineRunPaths(args.run_root.resolve())
    logger: JsonlLogger | None = None
    initialized_run = False
    completed_step = 0
    last_loss: float | None = None
    last_model_average_step = 0

    try:
        runtime = {
            "rank": rank,
            "world_size": world_size,
            "local_rank": local_rank,
            "hostname": hostname,
            "backend": dist.get_backend(),
            "device": str(device),
            "device_type": device.type,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "cuda_device_name": (
                torch.cuda.get_device_name(device) if device.type == "cuda" else None
            ),
        }
        gathered: list[dict[str, Any] | None] = [None] * world_size
        dist.all_gather_object(gathered, runtime)
        runtimes = [item for item in gathered if item is not None]
        setup_error: list[str | None] = [None]
        if rank == 0:
            try:
                validate_runtimes(config, runtimes)
                initialize_run(
                    paths,
                    config=config,
                    mode=args.mode,
                    run_id=args.run_id,
                    runtimes=runtimes,
                    source_commit=source_commit,
                )
            except BaseException as exc:
                setup_error[0] = f"{type(exc).__name__}: {exc}"
        dist.broadcast_object_list(setup_error, src=0)
        if setup_error[0] is not None:
            raise RuntimeError(setup_error[0])
        initialized_run = True
        dist.barrier()

        logger = JsonlLogger(paths.rank_log(rank), rank=rank)
        logger.event(
            "baseline_started",
            mode=args.mode,
            backend=config.distributed.backend,
            hostname=hostname,
            world_size=world_size,
            local_rank=local_rank,
            device=str(device),
            max_steps=config.training.max_steps,
            periodic_average_interval=config.distributed.periodic_average_interval,
            git_commit=source_commit,
        )
        write_heartbeat(
            paths,
            rank=rank,
            status="starting",
            step=0,
            mode=args.mode,
            hostname=hostname,
            loss=None,
            last_model_average_step=0,
        )

        seed_everything(config.training.seed)
        model, tokenizer = load_model_and_tokenizer(config)
        model.to(device)
        if args.mode == "periodic_average":
            broadcast_trainable_parameters(model)
            training_model: torch.nn.Module = model
        else:
            training_model = DistributedDataParallel(
                model,
                device_ids=[local_rank] if device.type == "cuda" else None,
                output_device=local_rank if device.type == "cuda" else None,
            )
        seed_everything(config.training.seed + rank * 100_003)
        batch_iter = build_batch_iterator(
            config,
            tokenizer,
            rank=rank,
            world_size=world_size,
        )
        optimizer, scheduler = build_optimizer_and_scheduler(
            training_model,
            config.optimizer,
            total_steps=config.training.max_steps,
        )

        cumulative_tokens = 0
        parameter_average_count = 0
        for step in range(1, config.training.max_steps + 1):
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            step_started = time.monotonic()
            learning_rate = float(optimizer.param_groups[0]["lr"])
            result = train_optimizer_step(
                training_model,
                batch_iter,
                optimizer,
                scheduler,
                device=device,
                accumulation_steps=config.training.gradient_accumulation_steps,
                grad_clip=config.training.grad_clip,
                ddp_gradient_sync=args.mode == "ddp",
            )
            sync_duration = result.gradient_sync_window_seconds
            flattened_numel = 0
            sync_kind = "gradient_all_reduce"
            if args.mode == "periodic_average" and should_average(
                step, config.distributed.periodic_average_interval
            ):
                sync_duration, flattened_numel = average_trainable_parameters(
                    training_model,
                    world_size=world_size,
                )
                parameter_average_count += 1
                last_model_average_step = step
                sync_kind = "parameter_average"
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            step_time = time.monotonic() - step_started
            completed_step = step
            last_loss = result.loss
            cumulative_tokens += result.tokens
            local_throughput = result.tokens / max(step_time, 1.0e-12)
            global_throughput = result.tokens * world_size / max(step_time, 1.0e-12)
            gradient_sync_count = step if args.mode == "ddp" else 0
            append_csv(
                paths.rank_metrics(rank),
                {
                    "timestamp": time.time(),
                    "mode": args.mode,
                    "rank": rank,
                    "hostname": hostname,
                    "step": step,
                    "loss": result.loss,
                    "learning_rate": learning_rate,
                    "tokens": result.tokens,
                    "cumulative_tokens": cumulative_tokens,
                    "tokens_per_second": local_throughput,
                    "global_tokens_per_second": global_throughput if rank == 0 else "",
                    "step_time_seconds": step_time,
                    "grad_norm": result.grad_norm,
                    "gradient_sync_count": gradient_sync_count,
                    "parameter_average_count": parameter_average_count,
                    "last_model_average_step": last_model_average_step,
                },
                RANK_METRIC_FIELDS,
            )
            if rank == 0 and (
                args.mode == "ddp"
                or should_average(step, config.distributed.periodic_average_interval)
            ):
                append_csv(
                    paths.sync_metrics,
                    {
                        "timestamp": time.time(),
                        "mode": args.mode,
                        "step": step,
                        "sync_kind": sync_kind,
                        "duration_seconds": sync_duration,
                        "flattened_numel": flattened_numel,
                        "world_size": world_size,
                        "cumulative_sync_count": (
                            gradient_sync_count
                            if args.mode == "ddp"
                            else parameter_average_count
                        ),
                    },
                    SYNC_METRIC_FIELDS,
                )
            write_heartbeat(
                paths,
                rank=rank,
                status="running",
                step=step,
                mode=args.mode,
                hostname=hostname,
                loss=result.loss,
                last_model_average_step=last_model_average_step,
            )
            if step == 1 or step % config.training.log_every_steps == 0:
                logger.event(
                    "optimizer_step",
                    step=step,
                    loss=result.loss,
                    learning_rate=learning_rate,
                    grad_norm=result.grad_norm,
                    step_time_seconds=step_time,
                    tokens_per_second=local_throughput,
                    last_model_average_step=last_model_average_step,
                )

        dist.barrier()
        if rank == 0:
            save_final_checkpoint(paths, training_model, tokenizer)
        dist.barrier()
        write_heartbeat(
            paths,
            rank=rank,
            status="completed",
            step=completed_step,
            mode=args.mode,
            hostname=hostname,
            loss=last_loss,
            last_model_average_step=last_model_average_step,
        )
        logger.event(
            "baseline_completed",
            step=completed_step,
            loss=last_loss,
            last_model_average_step=last_model_average_step,
        )
        dist.barrier()
        if rank == 0:
            atomic_write_json(
                paths.summary,
                {
                    "format_version": 1,
                    "status": "completed",
                    "exit_status": 0,
                    "run_id": args.run_id,
                    "mode": args.mode,
                    "backend": config.distributed.backend,
                    "world_size": world_size,
                    "max_steps": config.training.max_steps,
                    "final_step": completed_step,
                    "gradient_sync_count": (
                        config.training.max_steps if args.mode == "ddp" else 0
                    ),
                    "parameter_average_count": (
                        config.training.max_steps
                        // config.distributed.periodic_average_interval
                        if args.mode == "periodic_average"
                        else 0
                    ),
                    "final_checkpoint": str(paths.final_checkpoint),
                    "completed_at": time.time(),
                },
            )
    except BaseException as exc:
        if logger is not None:
            logger.exception(
                "baseline_failed",
                step=completed_step,
                error=repr(exc),
            )
        if initialized_run:
            try:
                write_heartbeat(
                    paths,
                    rank=rank,
                    status="failed",
                    step=completed_step,
                    mode=args.mode,
                    hostname=hostname,
                    loss=last_loss,
                    last_model_average_step=last_model_average_step,
                    error=repr(exc),
                )
                if rank == 0 and not paths.summary.exists():
                    atomic_write_json(
                        paths.summary,
                        {
                            "format_version": 1,
                            "status": "failed",
                            "exit_status": 1,
                            "run_id": args.run_id,
                            "mode": args.mode,
                            "backend": config.distributed.backend,
                            "world_size": world_size,
                            "final_step": completed_step,
                            "error": repr(exc),
                            "failed_at": time.time(),
                        },
                    )
            except Exception:
                pass
        raise
    finally:
        if dist.is_initialized():
            dist.destroy_process_group()


def main() -> None:
    """Parse CLI arguments and run the selected baseline mode."""

    run(build_parser().parse_args())


if __name__ == "__main__":
    main()

