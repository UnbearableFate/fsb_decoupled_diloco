"""Independent DDP and periodic parameter-averaging training baselines."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel

from ..core.config import Config, config_to_dict, resolve_config
from ..modeling.hf_data import build_batch_iterator
from ..modeling.hf_model import load_causal_lm_and_tokenizer
from ..observability.logging_utils import JsonlLogger
from ..runtime.learner import (
    build_inner_optimizer_and_scheduler,
    current_inner_learning_rate,
)
from ..storage.atomic_io import atomic_write_json
from .artifacts import (
    RANK_METRIC_FIELDS,
    SYNC_METRIC_FIELDS,
    BaselineRunPaths,
    append_durable_csv,
    initialize_run_root,
    write_heartbeat,
)
from .protocol import (
    average_trainable_parameters,
    broadcast_trainable_parameters,
    should_average,
    train_optimizer_step,
)


MODES = ("ddp", "periodic_average")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--average-interval", type=int)
    parser.add_argument("--backend", choices=("gloo", "nccl"))
    return parser


def _resolve_baseline_config(args: argparse.Namespace) -> tuple[Config, int, int, str]:
    config = resolve_config(
        args.config,
        run_id=args.run_id,
        shared_root=args.shared_root,
    )
    if not config.torch_baseline.enabled:
        raise ValueError("config must set torch_baseline.enabled=true")
    max_steps = int(
        args.max_steps
        if args.max_steps is not None
        else config.training.max_local_steps
    )
    average_interval = int(
        args.average_interval
        if args.average_interval is not None
        else config.training.inner_steps
    )
    backend = str(args.backend or config.torch_baseline.backend).lower()
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    if average_interval < 1:
        raise ValueError("average_interval must be >= 1")
    if backend not in {"gloo", "nccl"}:
        raise ValueError(f"unsupported backend: {backend}")
    if max_steps == 5000 and max_steps % average_interval != 0:
        raise ValueError("the 5000-step formal run must end on an average interval")
    config.training.max_local_steps = max_steps
    config.training.inner_steps = average_interval
    config.torch_baseline.backend = backend
    return config, max_steps, average_interval, backend


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _runtime_device(backend: str, local_rank: int) -> torch.device:
    if backend == "nccl":
        if not torch.cuda.is_available():
            raise RuntimeError("NCCL baseline requires CUDA")
        if local_rank < 0 or local_rank >= torch.cuda.device_count():
            raise RuntimeError(
                f"LOCAL_RANK={local_rank} is outside {torch.cuda.device_count()} visible GPUs"
            )
        torch.cuda.set_device(local_rank)
        return torch.device("cuda", local_rank)
    if torch.cuda.is_available() and os.environ.get("TORCH_BASELINE_GLOO_USE_CUDA") == "1":
        torch.cuda.set_device(local_rank)
        return torch.device("cuda", local_rank)
    return torch.device("cpu")


def _validate_runtimes(config: Config, runtimes: list[dict[str, Any]], backend: str) -> None:
    expected = int(config.sync.num_learners)
    if len(runtimes) != expected:
        raise RuntimeError(
            f"expected exactly {expected} distributed ranks, got {len(runtimes)}"
        )
    ranks = sorted(int(runtime["rank"]) for runtime in runtimes)
    if ranks != list(range(expected)):
        raise RuntimeError(f"rank set mismatch: {ranks}")
    if any(runtime["backend"] != backend for runtime in runtimes):
        raise RuntimeError("distributed ranks disagree on backend")
    if backend == "nccl" and any(runtime["device_type"] != "cuda" for runtime in runtimes):
        raise RuntimeError("NCCL formal run requires CUDA on every rank")
    if config.torch_baseline.require_distinct_hosts:
        hosts = {str(runtime["hostname"]) for runtime in runtimes}
        if len(hosts) != expected:
            raise RuntimeError(
                f"expected {expected} distinct rank hosts, got {len(hosts)}: {sorted(hosts)}"
            )


class _OptionalWandb:
    def __init__(
        self,
        *,
        config: Config,
        mode: str,
        rank: int,
        logger: JsonlLogger,
        runtime: dict[str, Any],
    ) -> None:
        self.run: Any | None = None
        self.logger = logger
        if rank != 0 or not config.wandb.enabled:
            return
        try:
            if config.wandb.mode and "WANDB_MODE" not in os.environ:
                os.environ["WANDB_MODE"] = str(config.wandb.mode)
            import wandb

            self.run = wandb.init(
                project="fs-diloco-torch-baselines",
                name=f"{config.run.run_id}_{mode}",
                entity=config.wandb.entity,
                group=config.wandb.group,
                tags=[mode, "torch-distributed", *config.wandb.tags],
                config={
                    **config_to_dict(config),
                    "runtime": runtime,
                    "baseline_mode": mode,
                },
            )
        except Exception as exc:
            self.logger.event("wandb_init_failed", error=repr(exc))
            self.run = None

    def log(self, payload: dict[str, Any], *, step: int) -> None:
        if self.run is None:
            return
        try:
            self.run.log(payload, step=step)
        except Exception as exc:
            self.logger.event("wandb_log_failed", step=step, error=repr(exc))

    def finish(self) -> None:
        if self.run is None:
            return
        try:
            self.run.finish()
        except Exception as exc:
            self.logger.event("wandb_finish_failed", error=repr(exc))


def _save_final_checkpoint(
    paths: BaselineRunPaths,
    model: torch.nn.Module,
    tokenizer: Any,
) -> None:
    if paths.final_checkpoint.exists():
        raise FileExistsError(f"final checkpoint already exists: {paths.final_checkpoint}")
    checkpoint_parent = paths.final_checkpoint.parent
    staging = Path(tempfile.mkdtemp(prefix=".final.", dir=checkpoint_parent))
    unwrapped = model.module if isinstance(model, DistributedDataParallel) else model
    try:
        if not hasattr(unwrapped, "save_pretrained"):
            raise TypeError("5000-step final checkpoint model does not support save_pretrained")
        unwrapped.save_pretrained(staging, safe_serialization=True)
        if not hasattr(tokenizer, "save_pretrained"):
            raise TypeError("5000-step final checkpoint tokenizer cannot be saved")
        tokenizer.save_pretrained(staging)
        os.replace(staging, paths.final_checkpoint)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def run(args: argparse.Namespace) -> None:
    config, max_steps, average_interval, backend = _resolve_baseline_config(args)
    rank = int(os.environ.get("RANK", "0"))
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    device = _runtime_device(backend, local_rank)
    dist.init_process_group(backend=backend, timeout=dt.timedelta(seconds=300))
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    hostname = socket.gethostname()
    paths = BaselineRunPaths(Path(config.run.shared_root).resolve())
    logger: JsonlLogger | None = None
    initialized_run = False
    completed_step = 0
    last_loss: float | None = None
    last_model_average_step = 0
    wandb_run: _OptionalWandb | None = None

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
        runtimes: list[dict[str, Any] | None] = [None] * world_size
        dist.all_gather_object(runtimes, runtime)
        gathered_runtimes = [item for item in runtimes if item is not None]

        setup_result: list[str | None] = [None]
        if rank == 0:
            try:
                _validate_runtimes(config, gathered_runtimes, backend)
                initialize_run_root(
                    paths,
                    config=config,
                    mode=args.mode,
                    backend=backend,
                    max_steps=max_steps,
                    average_interval=average_interval,
                    runtimes=gathered_runtimes,
                )
            except BaseException as exc:
                setup_result[0] = f"{type(exc).__name__}: {exc}"
        dist.broadcast_object_list(setup_result, src=0)
        if setup_result[0] is not None:
            raise RuntimeError(setup_result[0])
        initialized_run = True
        dist.barrier()

        logger = JsonlLogger(paths.rank_log(rank), f"torch_baseline_rank_{rank:03d}")
        logger.event(
            "baseline_started",
            mode=args.mode,
            backend=backend,
            rank=rank,
            world_size=world_size,
            local_rank=local_rank,
            device=str(device),
            max_steps=max_steps,
            average_interval=average_interval,
            git_commit=config.run.git_commit,
            source_fingerprint=config.run.source_fingerprint,
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

        _seed_everything(int(config.training.seed))
        model, tokenizer = load_causal_lm_and_tokenizer(config.model)
        model.to(device)
        if args.mode == "periodic_average":
            broadcast_trainable_parameters(model, source_rank=0)
            training_model: torch.nn.Module = model
        else:
            ddp_kwargs: dict[str, Any] = {}
            if device.type == "cuda":
                ddp_kwargs = {"device_ids": [local_rank], "output_device": local_rank}
            training_model = DistributedDataParallel(model, **ddp_kwargs)

        # Preserve identical initialization while giving stochastic training on
        # each data shard a deterministic rank-specific random stream.
        _seed_everything(int(config.training.seed) + rank * 100_003)
        batch_iter = build_batch_iterator(
            config,
            tokenizer,
            learner_index=rank,
            num_learners=world_size,
        )
        optimizer, scheduler = build_inner_optimizer_and_scheduler(
            training_model,
            config,
            completed_local_steps=0,
        )
        wandb_run = _OptionalWandb(
            config=config,
            mode=args.mode,
            rank=rank,
            logger=logger,
            runtime=runtime,
        )

        cumulative_tokens = 0
        parameter_average_count = 0
        for step in range(1, max_steps + 1):
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            step_started = time.monotonic()
            learning_rate = current_inner_learning_rate(optimizer)
            result = train_optimizer_step(
                training_model,
                batch_iter,
                optimizer,
                scheduler,
                device=device,
                config=config,
                ddp_gradient_sync=args.mode == "ddp",
            )

            sync_duration = result.gradient_sync_window_seconds
            sync_numel = 0
            sync_kind = "gradient_all_reduce"
            if args.mode == "periodic_average" and should_average(step, average_interval):
                sync_duration, sync_numel = average_trainable_parameters(
                    training_model,
                    world_size=world_size,
                    communication_dtype=torch.bfloat16,
                )
                parameter_average_count += 1
                last_model_average_step = step
                sync_kind = "parameter_average"
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            step_elapsed = time.monotonic() - step_started
            completed_step = step
            last_loss = result.loss
            cumulative_tokens += result.tokens
            local_throughput = result.tokens / max(step_elapsed, 1.0e-12)
            global_throughput = result.tokens * world_size / max(step_elapsed, 1.0e-12)
            gradient_sync_count = step if args.mode == "ddp" else 0

            metric_row = {
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
                "step_time_seconds": step_elapsed,
                "grad_norm": result.grad_norm,
                "gradient_sync_count": gradient_sync_count,
                "parameter_average_count": parameter_average_count,
                "last_model_average_step": last_model_average_step,
            }
            append_durable_csv(
                paths.rank_metrics(rank),
                metric_row,
                RANK_METRIC_FIELDS,
            )
            if rank == 0 and (
                args.mode == "ddp" or should_average(step, average_interval)
            ):
                cumulative_sync_count = (
                    gradient_sync_count
                    if args.mode == "ddp"
                    else parameter_average_count
                )
                append_durable_csv(
                    paths.sync_metrics,
                    {
                        "timestamp": time.time(),
                        "mode": args.mode,
                        "step": step,
                        "sync_kind": sync_kind,
                        "duration_seconds": sync_duration,
                        "flattened_numel": sync_numel,
                        "world_size": world_size,
                        "cumulative_sync_count": cumulative_sync_count,
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
            if rank == 0:
                wandb_run.log(
                    {
                        "train/loss": result.loss,
                        "train/learning_rate": learning_rate,
                        "train/grad_norm": result.grad_norm,
                        "train/global_tokens_per_second": global_throughput,
                        "train/step_time_seconds": step_elapsed,
                        "sync/duration_seconds": sync_duration,
                        "sync/gradient_count": gradient_sync_count,
                        "sync/parameter_average_count": parameter_average_count,
                        "sync/last_model_average_step": last_model_average_step,
                    },
                    step=step,
                )
            if step == 1 or step % int(config.training.log_every_steps) == 0:
                logger.event(
                    "optimizer_step",
                    step=step,
                    loss=result.loss,
                    learning_rate=learning_rate,
                    grad_norm=result.grad_norm,
                    step_time_seconds=step_elapsed,
                    tokens_per_second=local_throughput,
                    last_model_average_step=last_model_average_step,
                )

        dist.barrier()
        if max_steps == 5000:
            if rank == 0:
                _save_final_checkpoint(paths, training_model, tokenizer)
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
                    "run_id": config.run.run_id,
                    "mode": args.mode,
                    "backend": backend,
                    "world_size": world_size,
                    "max_steps": max_steps,
                    "final_step": completed_step,
                    "gradient_sync_count": max_steps if args.mode == "ddp" else 0,
                    "parameter_average_count": (
                        max_steps // average_interval
                        if args.mode == "periodic_average"
                        else 0
                    ),
                    "final_checkpoint": (
                        str(paths.final_checkpoint) if max_steps == 5000 else None
                    ),
                    "completed_at": time.time(),
                },
            )
        wandb_run.finish()
    except BaseException as exc:
        if logger is not None:
            logger.exception(
                "baseline_failed",
                rank=rank,
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
                            "run_id": config.run.run_id,
                            "mode": args.mode,
                            "backend": backend,
                            "world_size": world_size,
                            "final_step": completed_step,
                            "error": repr(exc),
                            "failed_at": time.time(),
                        },
                    )
            except Exception:
                pass
        if wandb_run is not None:
            wandb_run.finish()
        raise
    finally:
        if dist.is_initialized():
            dist.destroy_process_group()


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
