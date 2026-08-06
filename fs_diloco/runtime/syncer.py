"""Filesystem-backed Decoupled DiLoCo syncer."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import socket
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch

from ..core.config import Config, config_to_dict, resolve_config, write_resolved_config
from ..core.run_descriptor import load_run_descriptor
from ..core.constants import (
    FORMAT_VERSION,
    GLOBAL_STATUS_COMMITTED,
    LEARNER_STATUS_STOPPED,
    PROTOCOL_VERSION,
    learner_id_from_index,
)
from ..modeling.hf_model import choose_device, load_causal_lm_and_tokenizer
from ..modeling.outer_optim import init_outer_state, outer_optimizer_step
from ..modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_param_index,
    validate_compatible_index,
)
from ..observability.logging_utils import JsonlLogger, log_uncaught_exception
from ..observability.metrics import SYNCER_METRIC_FIELDS, append_csv_row
from ..observability.wandb_logging import (
    selected_resource_summary,
    selected_update_summary,
    syncer_wandb_project_name,
    syncer_wandb_run_name,
    syncer_wandb_tags,
    wandb_config,
    wandb_is_disabled,
)
from ..protocol.fragment_codec import (
    extract_fragment,
    load_fragment_update,
    materialize_full_from_fragments,
    save_fragment_weight,
)
from ..protocol.fragment_index import build_fragment_index, save_fragment_index
from ..protocol.fragment_scheduler import select_fragment
from ..protocol.control_epoch import EpochControlPublisher
from ..protocol.liveness import (
    capture_heartbeat_fences,
    ingest_heartbeats,
    no_progress_timed_out,
    update_liveness_statuses,
)
from ..protocol.merge import (
    normalized_fragment_update_weights,
    normalized_update_weights,
    select_one_per_learner,
    weighted_average_tensors,
)
from ..storage.atomic_io import (
    atomic_write_json,
    atomic_write_with_writer,
    ensure_dir,
    safe_read_json,
    sha256_file,
)
from ..storage.maintenance import run_maintenance
from ..storage.fenced_store import LeaderBoundSQLiteStore
from ..storage.paths import RunPaths, prepare_run_dirs
from ..storage.sqlite_store import SQLiteStore
from .syncer_ha import LeaseRenewalThread, acquire_candidate, open_leader_store
from ..storage.tensor_codec import (
    dtype_from_name,
    load_global_weights_flat,
    load_outer_state,
    load_update_vector,
    save_global_weights,
    save_outer_state,
)


def resolve_syncer_device(config: Config) -> torch.device:
    configured = config.syncer.device.lower()
    if configured == "auto":
        return choose_device()
    if configured == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("syncer.device=cuda requires an available CUDA device")
    return torch.device(configured)


def syncer_compute_dtype(config: Config) -> torch.dtype:
    return dtype_from_name(config.syncer.compute_dtype)


def syncer_publish_dtype(config: Config) -> torch.dtype:
    return dtype_from_name(config.syncer.publish_dtype)


def maybe_capture_terminal_predecessor_for_eval(
    config: Config,
    paths: RunPaths,
    *,
    input_closed: bool,
    terminal_drain: bool,
    version: int,
    selected: list[dict[str, Any]],
    store: SQLiteStore | LeaderBoundSQLiteStore | None = None,
) -> dict[str, Any] | None:
    """Retain pre-merge evidence without creating another runtime authority."""
    if (
        not config.sync.capture_terminal_predecessor_for_eval
        or not input_closed
        or not terminal_drain
    ):
        return None

    source = paths.global_weight_path(version)
    if isinstance(store, LeaderBoundSQLiteStore):
        version_row = store.get_global_version(version)
        if version_row is None:
            raise RuntimeError(f"global version {version} is missing from SQLite")
        source = Path(str(version_row["weight_path"]))
        if not source.is_absolute():
            source = paths.shared_root / source
    if not source.is_file():
        raise FileNotFoundError(f"authoritative terminal predecessor weight is missing: {source}")
    ensure_dir(paths.eval_checkpoints)
    stem = f"terminal_predecessor_v{version:06d}"
    checkpoint = paths.eval_checkpoints / f"{stem}.safetensors"
    manifest_path = paths.eval_checkpoints / f"{stem}.manifest.json"

    relative_checkpoint = checkpoint.relative_to(paths.shared_root).as_posix()
    relative_manifest = manifest_path.relative_to(paths.shared_root).as_posix()
    source_relative = source.relative_to(paths.shared_root).as_posix()
    expected_identity: dict[str, Any] = {
        "schema_version": 1,
        "checkpoint_role": "terminal_predecessor_evidence",
        "source_global_version": int(version),
        "source_weight_path": source_relative,
        "checkpoint_path": relative_checkpoint,
        "manifest_path": relative_manifest,
        "selected_count": len(selected),
        "quorum_min": int(config.sync.quorum_min),
        "quorum_max": int(config.sync.quorum_max),
        "selected_update_ids": [str(row["update_id"]) for row in selected],
        "selected_learner_ids": [str(row["learner_id"]) for row in selected],
    }
    source_sha = f"sha256:{sha256_file(source)}"

    if manifest_path.exists():
        existing_manifest = safe_read_json(manifest_path)
        if existing_manifest is None:
            raise RuntimeError(f"terminal predecessor manifest is unreadable: {manifest_path}")
        if not checkpoint.is_file():
            raise RuntimeError(
                f"committed terminal predecessor checkpoint is missing: {checkpoint}"
            )
        mismatched = [
            key
            for key, expected in expected_identity.items()
            if existing_manifest.get(key) != expected
        ]
        checkpoint_sha = f"sha256:{sha256_file(checkpoint)}"
        if existing_manifest.get("checkpoint_sha256") != checkpoint_sha:
            mismatched.append("checkpoint_sha256")
        if checkpoint_sha != source_sha:
            mismatched.append("source_checksum")
        if mismatched:
            raise RuntimeError(
                "committed terminal predecessor evidence conflicts with source: "
                f"{sorted(set(mismatched))}"
            )
        return existing_manifest

    def copy_source() -> None:
        atomic_write_with_writer(
            checkpoint,
            lambda temporary: shutil.copyfile(source, temporary),
        )

    try:
        os.link(source, checkpoint)
        capture_method = "hardlink"
    except FileExistsError:
        if not checkpoint.is_file():
            raise
        if os.path.samefile(source, checkpoint):
            capture_method = "hardlink"
        elif f"sha256:{sha256_file(checkpoint)}" == source_sha:
            capture_method = "copy"
        else:
            copy_source()
            capture_method = "copy"
    except OSError:
        copy_source()
        capture_method = "copy"

    checkpoint_sha = f"sha256:{sha256_file(checkpoint)}"
    source_sha_after = f"sha256:{sha256_file(source)}"
    if source_sha_after != source_sha or checkpoint_sha != source_sha:
        raise RuntimeError("terminal predecessor source changed or checkpoint copy is inconsistent")
    manifest: dict[str, Any] = {
        **expected_identity,
        "checkpoint_sha256": checkpoint_sha,
        "capture_method": capture_method,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def align_state_to_publication_dtype(
    config: Config,
    theta: torch.Tensor,
    state: dict[str, torch.Tensor],
    *,
    roundtrip_metrics_out: dict[str, float] | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    """Make the next published values the authoritative in-memory state."""
    compute_dtype = syncer_compute_dtype(config)
    publish_dtype = syncer_publish_dtype(config)

    def align(value: torch.Tensor) -> torch.Tensor:
        result = value.detach()
        if result.is_floating_point():
            result = result.to(dtype=publish_dtype).to(dtype=compute_dtype)
        return result.contiguous()

    aligned_theta = align(theta)
    if roundtrip_metrics_out is not None:
        roundtrip_metrics_out.update(_publication_error_metrics(theta, aligned_theta))
    return aligned_theta, {key: align(value) for key, value in state.items()}


def _publication_error_metrics(
    reference: torch.Tensor,
    roundtripped: torch.Tensor,
    *,
    chunk_numel: int = 1 << 20,
) -> dict[str, float]:
    error_squared = 0.0
    reference_squared = 0.0
    error_max = 0.0
    reference_flat = reference.detach().reshape(-1)
    roundtripped_flat = roundtripped.detach().reshape(-1)
    for start in range(0, int(reference_flat.numel()), int(chunk_numel)):
        reference_chunk = reference_flat[start : start + int(chunk_numel)]
        roundtripped_chunk = roundtripped_flat[start : start + int(chunk_numel)]
        difference = roundtripped_chunk.sub(reference_chunk).to(dtype=torch.float32)
        reference_fp32 = reference_chunk.to(dtype=torch.float32)
        error_squared += float(torch.sum(difference * difference).item())
        reference_squared += float(torch.sum(reference_fp32 * reference_fp32).item())
        if difference.numel():
            error_max = max(error_max, float(torch.max(torch.abs(difference)).item()))
    error_l2 = math.sqrt(error_squared)
    reference_l2 = math.sqrt(reference_squared)
    return {
        "publish_roundtrip_l2_error": error_l2,
        "publish_roundtrip_linf_error": error_max,
        "publish_roundtrip_relative_l2_error": (
            error_l2 / reference_l2 if reference_l2 > 0.0 else 0.0
        ),
    }


def publication_roundtrip_metrics(
    theta: torch.Tensor,
    publish_dtype: torch.dtype,
    *,
    chunk_numel: int = 1 << 20,
) -> dict[str, float]:
    """Measure publication-cast error without materializing full extra vectors."""
    if not theta.is_floating_point() or theta.dtype == publish_dtype:
        return {
            "publish_roundtrip_l2_error": 0.0,
            "publish_roundtrip_linf_error": 0.0,
            "publish_roundtrip_relative_l2_error": 0.0,
        }
    error_squared = 0.0
    reference_squared = 0.0
    error_max = 0.0
    flat = theta.detach().reshape(-1)
    for start in range(0, int(flat.numel()), int(chunk_numel)):
        reference = flat[start : start + int(chunk_numel)]
        roundtripped = reference.to(dtype=publish_dtype).to(dtype=reference.dtype)
        difference = roundtripped.sub(reference).to(dtype=torch.float32)
        reference_fp32 = reference.to(dtype=torch.float32)
        error_squared += float(torch.sum(difference * difference).item())
        reference_squared += float(torch.sum(reference_fp32 * reference_fp32).item())
        if difference.numel():
            error_max = max(error_max, float(torch.max(torch.abs(difference)).item()))
    error_l2 = math.sqrt(error_squared)
    reference_l2 = math.sqrt(reference_squared)
    return {
        "publish_roundtrip_l2_error": error_l2,
        "publish_roundtrip_linf_error": error_max,
        "publish_roundtrip_relative_l2_error": (
            error_l2 / reference_l2 if reference_l2 > 0.0 else 0.0
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--shared-root")
    parser.add_argument("--num-learners", type=int)
    parser.add_argument("--training-seed", type=int)
    parser.add_argument("--scan-interval-seconds", type=float)
    parser.add_argument("--syncer-device", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--syncer-publish-dtype", choices=("float32", "bfloat16"))
    parser.add_argument("--staleness-lambda", type=float)
    parser.add_argument("--max-staleness-versions", type=int)
    parser.add_argument(
        "--global-adoption-strategy",
        choices=("replace", "rebase_post_publish_delta", "predict_post_publish_global"),
    )
    parser.add_argument("--completion-mode", choices=("local_or_global", "global_only"))
    parser.add_argument(
        "--parallel-checkpoint-writes",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument("--materialize-full-every-events", type=int)
    parser.add_argument(
        "--ingest-during-publish",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument(
        "--capture-terminal-predecessor-for-eval",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    return parser.parse_args(argv)


def sqlite_path(config: Config) -> Path:
    return RunPaths(Path(config.run.shared_root or ".")).sqlite_db


def run_identity(config: Config) -> dict[str, Any]:
    return {
        "run_id": config.run.run_id,
        "format_version": FORMAT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "mode": "fragment" if config.fragments.enabled else "full",
        "model_name_or_path": config.model.name_or_path,
        "num_fragments": config.fragments.num_fragments if config.fragments.enabled else None,
        "git_commit": config.run.git_commit,
        "git_dirty": config.run.git_dirty,
        "source_fingerprint": config.run.source_fingerprint,
    }


def publication_failpoint(name: str) -> None:
    """Inject a deterministic publication crash for the recovery matrix."""
    if os.environ.get("FS_DILOCO_PUBLICATION_FAILPOINT") != name:
        return
    action = os.environ.get("FS_DILOCO_FAILPOINT_ACTION", "raise")
    if action == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    if action != "raise":
        raise ValueError(f"unsupported FS_DILOCO_FAILPOINT_ACTION: {action}")
    raise RuntimeError(f"injected publication failpoint: {name}")


def latest_payload(
    *,
    config: Config,
    paths: RunPaths,
    version: int,
    weight_path: Path,
    optim_path: Path,
    total_update_tokens: int,
    total_seen_tokens: int,
    created_at: float | None = None,
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "version": version,
        "weight_path": str(weight_path),
        "optim_path": str(optim_path),
        "param_index_path": str(paths.param_index_json),
        "created_at": time.time() if created_at is None else float(created_at),
        "total_update_tokens": int(total_update_tokens),
        "total_seen_tokens": total_seen_tokens,
    }


def publish_global(
    *,
    config: Config,
    paths: RunPaths,
    store: SQLiteStore,
    version: int,
    theta: torch.Tensor,
    outer_state: dict[str, torch.Tensor],
    param_index: dict[str, Any],
    num_updates: int,
    total_update_tokens: int,
    total_seen_tokens: int,
    selected_updates: list[dict[str, Any]] | None = None,
    effective_weights: dict[str, float] | None = None,
    predecessor_version: int | None = None,
    roundtrip_metrics: dict[str, float] | None = None,
    during_checkpoint_wait: Callable[[], dict[str, int] | int | None] | None = None,
    checkpoint_poll_seconds: float = 0.2,
) -> dict[str, Any]:
    ha_store = store if isinstance(store, LeaderBoundSQLiteStore) else None
    publication_id = uuid.uuid4().hex if ha_store is not None else None
    if ha_store is None:
        weight_path = paths.global_weight_path(version)
        optim_path = paths.outer_optim_path(version)
    else:
        assert publication_id is not None
        weight_path = paths.epoch_weight_path(
            ha_store.token.epoch,
            ha_store.token.owner_id,
            version,
            publication_id,
        )
        optim_path = paths.epoch_outer_optim_path(
            ha_store.token.epoch,
            ha_store.token.owner_id,
            version,
            publication_id,
        )
        if weight_path.exists() or optim_path.exists():
            raise RuntimeError(f"HA checkpoint publication collision: {publication_id}")
    if os.environ.get("FS_DILOCO_PUBLICATION_FAILPOINT") == "weight_temp":
        temp_path = weight_path.parent / f".{weight_path.name}.injected.tmp"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("wb") as handle:
            handle.write(b"incomplete-weight")
            handle.flush()
            os.fsync(handle.fileno())
        publication_failpoint("weight_temp")
    publish_dtype = syncer_publish_dtype(config)
    if roundtrip_metrics is None:
        roundtrip_metrics = publication_roundtrip_metrics(theta, publish_dtype)

    def write_weight() -> tuple[float, int]:
        started = time.monotonic()
        save_global_weights(weight_path, theta, param_index, dtype=publish_dtype)
        elapsed = time.monotonic() - started
        publication_failpoint("after_weight")
        return elapsed, weight_path.stat().st_size

    def write_outer() -> tuple[float, int]:
        started = time.monotonic()
        save_outer_state(optim_path, theta, outer_state, dtype=publish_dtype)
        elapsed = time.monotonic() - started
        publication_failpoint("after_outer")
        return elapsed, optim_path.stat().st_size

    checkpoint_started = time.monotonic()
    publish_ingest_passes = 0
    publish_ingested_updates = 0
    publish_ingested_heartbeats = 0
    publish_ingest_seconds = 0.0
    if config.syncer.parallel_checkpoint_writes:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="publish-checkpoint") as pool:
            weight_future = pool.submit(write_weight)
            outer_future = pool.submit(write_outer)
            futures = (weight_future, outer_future)
            while during_checkpoint_wait is not None:
                done, _pending = wait_futures(
                    futures,
                    timeout=max(0.001, float(checkpoint_poll_seconds)),
                )
                if len(done) == len(futures) or any(
                    future.done() and future.exception() is not None for future in futures
                ):
                    break
                ingest_started = time.monotonic()
                counts = during_checkpoint_wait()
                publish_ingest_seconds += time.monotonic() - ingest_started
                publish_ingest_passes += 1
                if isinstance(counts, dict):
                    publish_ingested_updates += int(counts.get("metadata", 0))
                    publish_ingested_heartbeats += int(counts.get("heartbeats", 0))
                elif counts is not None:
                    publish_ingested_updates += int(counts)
            publish_weight_seconds, publish_weight_bytes = weight_future.result()
            publish_outer_seconds, publish_outer_bytes = outer_future.result()
    else:
        publish_weight_seconds, publish_weight_bytes = write_weight()
        publish_outer_seconds, publish_outer_bytes = write_outer()
    publish_checkpoint_seconds = time.monotonic() - checkpoint_started
    weight_sha256 = None
    optim_sha256 = None
    if ha_store is not None and config.io.checkpoint_digest_mode == "always":
        weight_sha256 = sha256_file(weight_path)
        optim_sha256 = sha256_file(optim_path)
    database_weight_path = paths.relative(weight_path) if ha_store is not None else str(weight_path)
    database_optim_path = paths.relative(optim_path) if ha_store is not None else str(optim_path)
    sqlite_start = time.monotonic()
    if version == 0:
        publication_fields = (
            {
                "publication_id": publication_id,
                "weight_size_bytes": publish_weight_bytes,
                "optim_size_bytes": publish_outer_bytes,
                "weight_sha256": weight_sha256,
                "optim_sha256": optim_sha256,
            }
            if ha_store is not None
            else {}
        )
        row = store.initialize_full_run(
            weight_path=database_weight_path,
            optim_path=database_optim_path,
            outer_optimizer=config.outer_optimizer.name,
            identity=run_identity(config),
            config_snapshot=config_to_dict(config),
            **publication_fields,
        )
    else:
        if predecessor_version is None or selected_updates is None or effective_weights is None:
            raise ValueError("full merge publication requires predecessor and selected updates")
        publication_fields = (
            {
                "publication_id": publication_id,
                "weight_size_bytes": publish_weight_bytes,
                "optim_size_bytes": publish_outer_bytes,
                "weight_sha256": weight_sha256,
                "optim_sha256": optim_sha256,
            }
            if ha_store is not None
            else {}
        )
        row = store.commit_full_merge(
            predecessor_version=predecessor_version,
            target_version=version,
            weight_path=database_weight_path,
            optim_path=database_optim_path,
            selected_updates=selected_updates,
            effective_weights=effective_weights,
            total_update_tokens=total_update_tokens,
            total_seen_tokens=total_seen_tokens,
            outer_optimizer=config.outer_optimizer.name,
            max_staleness_versions=config.sync.max_staleness_versions,
            before_commit=lambda: publication_failpoint("sqlite_transaction"),
            **publication_fields,
        )
    sqlite_commit_seconds = time.monotonic() - sqlite_start
    publication_failpoint("after_db_commit")
    if ha_store is not None:
        EpochControlPublisher(paths, ha_store.fenced_store, ha_store.token).publish_latest(row)
    else:
        atomic_write_json(
            paths.latest_json,
            latest_payload(
                config=config,
                paths=paths,
                version=version,
                weight_path=weight_path,
                optim_path=optim_path,
                total_update_tokens=total_update_tokens,
                total_seen_tokens=total_seen_tokens,
                created_at=float(row["created_at"]),
            ),
        )
    publication_failpoint("after_latest")
    return {
        **row,
        "sqlite_commit_seconds": sqlite_commit_seconds,
        "publish_weight_seconds": publish_weight_seconds,
        "publish_outer_seconds": publish_outer_seconds,
        "publish_checkpoint_seconds": publish_checkpoint_seconds,
        "publish_dtype": config.syncer.publish_dtype,
        "publish_weight_bytes": publish_weight_bytes,
        "publish_outer_bytes": publish_outer_bytes,
        "publish_ingest_passes": publish_ingest_passes,
        "publish_ingested_updates": publish_ingested_updates,
        "publish_ingested_heartbeats": publish_ingested_heartbeats,
        "publish_ingest_seconds": publish_ingest_seconds,
        **roundtrip_metrics,
    }


def checkpoint_wait_ingestion_callback(
    config: Config,
    callback: Callable[[], dict[str, int] | int | None],
) -> Callable[[], dict[str, int] | int | None] | None:
    """Return the publication overlap callback only when ingestion is enabled."""

    return callback if config.sync.ingest_during_publish else None


def fragment_latest_payload(
    *,
    config: Config,
    paths: RunPaths,
    global_merge_event: int,
    fragment_versions: dict[int, int],
    fragment_updated_events: dict[int, int],
    total_seen_tokens: int,
    materialized_weight_path: Path,
) -> dict[str, Any]:
    fragments: dict[str, dict[str, Any]] = {}
    for fragment_id, version in sorted(fragment_versions.items()):
        fragments[str(fragment_id)] = {
            "version": int(version),
            "weight_path": str(paths.fragment_weight_path(fragment_id, version)),
            "optim_path": str(paths.fragment_outer_optim_path(fragment_id, version)),
            "updated_at_global_merge_event": int(fragment_updated_events.get(fragment_id, 0)),
        }
    return {
        "format_version": FORMAT_VERSION,
        "latest_kind": "fragment",
        "latest_layout_version": 2,
        "run_id": config.run.run_id,
        "version": int(global_merge_event),
        "global_merge_event": int(global_merge_event),
        "param_index_path": str(paths.param_index_json),
        "fragment_index_path": str(paths.fragment_index_json),
        "materialized_weight_path": str(materialized_weight_path),
        "created_at": time.time(),
        "total_seen_tokens": int(total_seen_tokens),
        "fragments": fragments,
    }


def should_materialize_fragment_full(config: Config, global_merge_event: int) -> bool:
    interval = config.fragments.materialize_full_every_events
    target = config.sync.stop_after_outer_steps
    if global_merge_event == 0:
        return True
    if target is not None and global_merge_event >= int(target):
        return True
    if interval is None or int(interval) <= 0:
        raise ValueError("fragments.materialize_full_every_events must be a positive integer")
    return global_merge_event % int(interval) == 0


@dataclass(frozen=True)
class FragmentLatestPublication:
    materialized_weight_path: Path
    materialize_full_seconds: float
    materialized_this_event: bool
    materialized_bytes: int


def publish_fragment_latest(
    *,
    config: Config,
    paths: RunPaths,
    param_index: dict[str, Any],
    fragment_index: dict[str, Any],
    fragment_thetas: dict[int, torch.Tensor],
    fragment_versions: dict[int, int],
    fragment_updated_events: dict[int, int],
    total_seen_tokens: int,
    global_merge_event: int,
    previous_materialized_weight_path: Path | None = None,
    force_materialize: bool = False,
) -> FragmentLatestPublication:
    materialize_seconds = 0.0
    materialized_this_event = False
    materialized_bytes = 0
    materialized_weight_path = previous_materialized_weight_path
    if (
        force_materialize
        or materialized_weight_path is None
        or should_materialize_fragment_full(config, global_merge_event)
    ):
        materialize_start = time.monotonic()
        full = materialize_full_from_fragments(
            fragment_thetas, fragment_index, int(param_index["total_numel"])
        )
        materialized_weight_path = paths.global_weight_path(global_merge_event)
        save_global_weights(
            materialized_weight_path,
            full,
            param_index,
            dtype=syncer_publish_dtype(config),
        )
        materialize_seconds = time.monotonic() - materialize_start
        materialized_this_event = True
        materialized_bytes = materialized_weight_path.stat().st_size
    atomic_write_json(
        paths.latest_json,
        fragment_latest_payload(
            config=config,
            paths=paths,
            global_merge_event=global_merge_event,
            fragment_versions=fragment_versions,
            fragment_updated_events=fragment_updated_events,
            total_seen_tokens=total_seen_tokens,
            materialized_weight_path=materialized_weight_path,
        ),
    )
    return FragmentLatestPublication(
        materialized_weight_path=materialized_weight_path,
        materialize_full_seconds=materialize_seconds,
        materialized_this_event=materialized_this_event,
        materialized_bytes=materialized_bytes,
    )


def initialize_run(
    config: Config,
    paths: RunPaths,
    store: SQLiteStore,
    logger: JsonlLogger,
    *,
    device: torch.device | str = "cpu",
) -> tuple[int, torch.Tensor, dict[str, torch.Tensor], dict[str, Any], int]:
    if store.committed_global_count() != 0:
        raise RuntimeError("init.resume=false cannot overwrite an existing committed run")
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    param_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    compute_dtype = syncer_compute_dtype(config)
    theta = flatten_trainable_params(
        model,
        param_index,
        dtype=compute_dtype,
        device=device,
    )
    outer_state = init_outer_state(theta, config.outer_optimizer)
    theta, outer_state = align_state_to_publication_dtype(config, theta, outer_state)
    atomic_write_json(paths.param_index_json, param_index)
    if not isinstance(store, LeaderBoundSQLiteStore):
        write_resolved_config(config, paths.resolved_config_yaml)
        write_resolved_config(config, paths.run_root_config_yaml)
    publish_global(
        config=config,
        paths=paths,
        store=store,
        version=0,
        theta=theta,
        outer_state=outer_state,
        param_index=param_index,
        num_updates=0,
        total_update_tokens=0,
        total_seen_tokens=0,
    )
    maintenance = run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
        scan_interval_seconds=config.sync.scan_interval_seconds,
    )
    logger.event("run_initialized", version=0, total_numel=int(theta.numel()))
    logger.event("state_maintenance_completed", **maintenance)
    return 0, theta, outer_state, param_index, 0


def initialize_fragment_run(
    config: Config,
    paths: RunPaths,
    store: SQLiteStore,
    logger: JsonlLogger,
    *,
    device: torch.device | str = "cpu",
) -> tuple[
    int,
    dict[int, torch.Tensor],
    dict[int, dict[str, torch.Tensor]],
    dict[str, Any],
    dict[str, Any],
    dict[int, int],
    dict[int, int],
    int,
    Path,
]:
    if store.current_fragment_versions():
        raise RuntimeError("init.resume=false cannot overwrite existing committed fragments")
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    param_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    compute_dtype = syncer_compute_dtype(config)
    publish_dtype = syncer_publish_dtype(config)
    theta = flatten_trainable_params(
        model,
        param_index,
        dtype=compute_dtype,
        device=device,
    )
    fragment_index = build_fragment_index(
        param_index,
        strategy=config.fragments.strategy,
        num_fragments=config.fragments.num_fragments,
        source_param_index_path=paths.param_index_json,
    )
    atomic_write_json(paths.param_index_json, param_index)
    save_fragment_index(fragment_index, paths.fragment_index_json)
    write_resolved_config(config, paths.resolved_config_yaml)
    write_resolved_config(config, paths.run_root_config_yaml)

    fragment_thetas: dict[int, torch.Tensor] = {}
    outer_states: dict[int, dict[str, torch.Tensor]] = {}
    fragment_versions: dict[int, int] = {}
    fragment_updated_events: dict[int, int] = {}
    for fragment in fragment_index["fragments"]:
        fragment_id = int(fragment["fragment_id"])
        theta_f = extract_fragment(theta, fragment_index, fragment_id).to(
            device=device, dtype=compute_dtype
        )
        state_f = init_outer_state(theta_f, config.outer_optimizer)
        theta_f, state_f = align_state_to_publication_dtype(config, theta_f, state_f)
        weight_path = paths.fragment_weight_path(fragment_id, 0)
        optim_path = paths.fragment_outer_optim_path(fragment_id, 0)
        save_fragment_weight(weight_path, theta_f, dtype=publish_dtype)
        save_outer_state(optim_path, theta_f, state_f, dtype=publish_dtype)
        store.upsert_fragment_definition(fragment, strategy=config.fragments.strategy)
        store.upsert_fragment_version(
            fragment_id=fragment_id,
            version=0,
            global_merge_event=0,
            weight_path=str(weight_path),
            optim_path=str(optim_path),
            num_updates=0,
            total_update_tokens=0,
            total_seen_tokens=0,
            outer_optimizer=config.outer_optimizer.name,
            status=GLOBAL_STATUS_COMMITTED,
            notes="initialized",
        )
        fragment_thetas[fragment_id] = theta_f
        outer_states[fragment_id] = state_f
        fragment_versions[fragment_id] = 0
        fragment_updated_events[fragment_id] = 0

    initial_publication = publish_fragment_latest(
        config=config,
        paths=paths,
        param_index=param_index,
        fragment_index=fragment_index,
        fragment_thetas=fragment_thetas,
        fragment_versions=fragment_versions,
        fragment_updated_events=fragment_updated_events,
        total_seen_tokens=0,
        global_merge_event=0,
        previous_materialized_weight_path=None,
    )
    materialized_weight_path = initial_publication.materialized_weight_path
    store.set_run_state("config", config_to_dict(config))
    store.set_run_state("identity", run_identity(config))
    maintenance = run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
        scan_interval_seconds=config.sync.scan_interval_seconds,
    )
    logger.event(
        "fragment_run_initialized",
        global_merge_event=0,
        total_numel=int(theta.numel()),
        num_fragments=int(fragment_index["num_fragments"]),
        strategy=fragment_index["strategy"],
    )
    logger.event("state_maintenance_completed", **maintenance)
    return (
        0,
        fragment_thetas,
        outer_states,
        param_index,
        fragment_index,
        fragment_versions,
        fragment_updated_events,
        0,
        materialized_weight_path,
    )


def resume_run(
    config: Config,
    paths: RunPaths,
    store: SQLiteStore,
    logger: JsonlLogger,
    *,
    device: torch.device | str = "cpu",
) -> tuple[int, torch.Tensor, dict[str, torch.Tensor], dict[str, Any], int]:
    store.integrity_check()
    identity = store.get_run_state("identity")
    expected_identity = run_identity(config)
    if identity != expected_identity:
        raise RuntimeError(
            f"run identity mismatch: expected {expected_identity!r}, found {identity!r}"
        )
    committed = store.latest_global_version()
    if committed is None:
        raise RuntimeError("resume requires a committed global version in persistent SQLite")
    if isinstance(store, LeaderBoundSQLiteStore):
        terminal = store.terminal_state()
        terminal_reasons = (
            set()
            if terminal is None or terminal.get("stop_reason") in (None, "", "error")
            else {str(terminal["stop_reason"])}
        )
    else:
        stop_payload = safe_read_json(paths.stop_json)
        summary_payload = safe_read_json(paths.summary_json)
        terminal_reasons = {
            str(payload.get(key))
            for payload, key in (
                (stop_payload or {}, "reason"),
                (summary_payload or {}, "stop_reason"),
            )
            if payload.get(key) not in (None, "", "error")
        }
    if terminal_reasons:
        raise RuntimeError(
            "resume cannot reopen a completed run; use a new run_id/shared_root "
            f"(terminal reasons: {sorted(terminal_reasons)})"
        )
    param_index = load_param_index(paths.param_index_json)
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    current_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_compatible_index(current_index, param_index)
    weight_path = Path(str(committed["weight_path"]))
    optim_path = Path(str(committed["optim_path"]))
    if not weight_path.is_absolute():
        weight_path = paths.shared_root / weight_path
    if not optim_path.is_absolute():
        optim_path = paths.shared_root / optim_path
    if not weight_path.is_file() or not optim_path.is_file():
        raise FileNotFoundError(
            f"committed checkpoint is incomplete: weight={weight_path}, outer={optim_path}"
        )
    compute_dtype = syncer_compute_dtype(config)
    theta = load_global_weights_flat(
        weight_path,
        param_index,
        device=device,
        dtype=compute_dtype,
    )
    optim_theta, outer_state = load_outer_state(
        optim_path,
        device=device,
        dtype=compute_dtype,
    )
    if theta.shape != optim_theta.shape or not torch.equal(theta, optim_theta):
        raise RuntimeError("committed weight and outer checkpoint theta do not match")

    resume_id = uuid.uuid4().hex
    resumed_at = time.time()
    heartbeat_fences = capture_heartbeat_fences(
        paths.heartbeats,
        run_id=config.run.run_id or "",
        num_learners=config.sync.num_learners,
    )
    resume_generation = store.prepare_full_resume(
        resume_id=resume_id,
        resumed_at=resumed_at,
        expected_learner_ids=(
            learner_id_from_index(index) for index in range(config.sync.num_learners)
        ),
        heartbeat_fences=heartbeat_fences,
    )
    if not isinstance(store, LeaderBoundSQLiteStore):
        for marker_name, marker_path in (
            ("stop", paths.stop_json),
            ("summary", paths.summary_json),
        ):
            if marker_path.exists():
                ensure_dir(paths.logs)
                os.replace(
                    marker_path,
                    paths.logs / f"resume_{resume_id}_previous_{marker_name}.json",
                )
    total_seen_tokens = int(committed["total_seen_tokens"])
    if isinstance(store, LeaderBoundSQLiteStore):
        EpochControlPublisher(paths, store.fenced_store, store.token).publish_latest(committed)
    else:
        repaired_latest = latest_payload(
            config=config,
            paths=paths,
            version=int(committed["version"]),
            weight_path=weight_path,
            optim_path=optim_path,
            total_update_tokens=int(committed["total_update_tokens"]),
            total_seen_tokens=total_seen_tokens,
            created_at=float(committed["created_at"]),
        )
        atomic_write_json(paths.latest_json, repaired_latest)
    maintenance = run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
        scan_interval_seconds=config.sync.scan_interval_seconds,
    )
    logger.event(
        "run_resumed",
        version=int(committed["version"]),
        resume_id=resume_id,
        reset_selected=resume_generation["reset_selected"],
        reset_learners=resume_generation["reset_learners"],
        heartbeat_fence_count=len(heartbeat_fences),
    )
    logger.event("state_maintenance_completed", **maintenance)
    return (
        int(committed["version"]),
        theta.to(device=device, dtype=compute_dtype),
        outer_state,
        param_index,
        total_seen_tokens,
    )


def validate_update_metadata(payload: dict[str, Any], *, config: Config, paths: RunPaths) -> bool:
    if payload.get("format_version") != FORMAT_VERSION:
        return False
    if payload.get("run_id") != config.run.run_id:
        return False
    if config.fragments.enabled:
        if payload.get("update_kind") != "fragment":
            return False
        try:
            fragment_id = int(payload.get("fragment_id"))
        except (TypeError, ValueError):
            return False
        if fragment_id < 0 or fragment_id >= int(config.fragments.num_fragments):
            return False
        required = [
            "base_fragment_version",
            "base_global_merge_event",
            "tokens_since_fragment_load",
        ]
        if any(key not in payload for key in required):
            return False
    elif payload.get("update_kind") == "fragment":
        return False
    valid_ids = {learner_id_from_index(i) for i in range(config.sync.num_learners)}
    if payload.get("learner_id") not in valid_ids:
        return False
    file_path = Path(payload.get("file_path", ""))
    if not file_path.exists():
        return False
    try:
        file_path.resolve(strict=False).relative_to(paths.shared_root.resolve(strict=False))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class UpdateFirstSeen:
    monotonic: float
    wall: float
    learner_id: str | None = None
    fragment_id: int | None = None


class UpdateFirstSeenRegistry:
    """Bounded process-local clock-domain registry for adaptive grace."""

    def __init__(self, *, capacity: int) -> None:
        capacity = int(capacity)
        if capacity <= 0:
            raise ValueError("first-seen registry capacity must be > 0")
        self.capacity = capacity
        self._entries: OrderedDict[str, UpdateFirstSeen] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def observe(
        self,
        update_id: str,
        *,
        learner_id: str | None = None,
        fragment_id: int | None = None,
        now_monotonic: float | None = None,
        now_wall: float | None = None,
    ) -> bool:
        update_id = str(update_id)
        if update_id in self._entries:
            return False
        self._entries[update_id] = UpdateFirstSeen(
            monotonic=time.monotonic() if now_monotonic is None else float(now_monotonic),
            wall=time.time() if now_wall is None else float(now_wall),
            learner_id=None if learner_id is None else str(learner_id),
            fragment_id=None if fragment_id is None else int(fragment_id),
        )
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)
        return True

    def get(self, update_id: str) -> UpdateFirstSeen | None:
        return self._entries.get(str(update_id))

    def discard_many(self, update_ids: list[str]) -> None:
        for update_id in update_ids:
            self._entries.pop(str(update_id), None)

    def discard_full_learner(self, learner_id: str) -> None:
        learner_id = str(learner_id)
        self.discard_many(
            [
                update_id
                for update_id, entry in self._entries.items()
                if entry.learner_id == learner_id and entry.fragment_id is None
            ]
        )


def update_first_seen_capacity(config: Config) -> int:
    num_fragments = int(config.fragments.num_fragments) if config.fragments.enabled else 1
    return max(64, 4 * int(config.sync.num_learners) * max(1, num_fragments))


def _pointer_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    return (
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def ingest_update_metadata(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    first_seen: UpdateFirstSeenRegistry | None = None,
) -> int:
    inserted = 0
    if config.fragments.enabled:
        metadata_paths = [
            paths.fragment_update_pointer_path(learner_id_from_index(learner_index), fragment_id)
            for learner_index in range(config.sync.num_learners)
            for fragment_id in range(config.fragments.num_fragments)
        ]
    else:
        metadata_paths = [
            paths.update_pointer_path(learner_id_from_index(index))
            for index in range(config.sync.num_learners)
        ]
    for path in metadata_paths:
        signature = _pointer_signature(path) if config.fragments.enabled else None
        if signature is None and config.fragments.enabled:
            continue
        if signature is not None and store.pointer_signature_is_cached(path, signature):
            continue
        payload = safe_read_json(path)
        if payload is None or not validate_update_metadata(payload, config=config, paths=paths):
            continue
        inserted_payload = (
            store.insert_fragment_update_metadata(payload, pointer_path=path)
            if config.fragments.enabled
            else store.insert_update_metadata(payload, pointer_path=path)
        )
        if signature is not None:
            store.cache_pointer_signature(path, signature)
        if inserted_payload:
            inserted += 1
            if first_seen is not None:
                if not config.fragments.enabled:
                    first_seen.discard_full_learner(str(payload["learner_id"]))
                first_seen.observe(
                    str(payload["update_id"]),
                    learner_id=str(payload["learner_id"]),
                    fragment_id=(int(payload["fragment_id"]) if config.fragments.enabled else None),
                )
    if inserted:
        logger.event("metadata_ingested", count=inserted)
    return inserted


def sync_liveness_and_metadata(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    first_seen: UpdateFirstSeenRegistry | None = None,
    heartbeat_fences: dict[str, str] | None = None,
) -> dict[str, int]:
    heartbeat_count = ingest_heartbeats(
        store,
        paths.heartbeats,
        run_id=config.run.run_id or "",
        num_learners=config.sync.num_learners,
        heartbeat_fences=heartbeat_fences,
    )
    counts = update_liveness_statuses(
        store,
        stale_after_seconds=config.liveness.stale_after_seconds,
        dead_after_seconds=config.liveness.dead_after_seconds,
    )
    if heartbeat_count:
        logger.event("heartbeats_ingested", count=heartbeat_count)
        logger.event("learner_liveness_updated", **counts)
    metadata_count = ingest_update_metadata(
        store,
        paths,
        config,
        logger,
        first_seen=first_seen,
    )
    return {"heartbeats": heartbeat_count, "metadata": metadata_count}


def configured_grace_seconds(config: Config) -> float:
    grace = config.sync.grace_window
    requested = (
        grace.initial_seconds
        if grace.mode == "adaptive_fastest_upload_eta"
        else grace.fixed_seconds
    )
    return max(0.0, min(float(requested), float(grace.max_seconds)))


def interval_breakdown(
    *,
    total_seconds: float,
    discovery_seconds: float,
    idle_seconds: float,
    grace_seconds: float,
    read_seconds: float,
    merge_seconds: float,
    publish_seconds: float,
    maintenance_seconds: float,
    quorum_trigger: str,
) -> dict[str, float | str]:
    components = {
        "discovery_seconds": float(discovery_seconds),
        "idle_seconds": float(idle_seconds),
        "grace_seconds": float(grace_seconds),
        "read_seconds": float(read_seconds),
        "merge_seconds": float(merge_seconds),
        "publish_seconds": float(publish_seconds),
        "maintenance_seconds": float(maintenance_seconds),
    }
    if float(total_seconds) < 0.0 or any(value < 0.0 for value in components.values()):
        raise ValueError("interval durations must be non-negative")
    accounted = sum(components.values())
    tolerance = max(1e-9, float(total_seconds) * 1e-9)
    if accounted > float(total_seconds) + tolerance:
        raise ValueError(f"interval components exceed total: {accounted} > {float(total_seconds)}")
    return {
        "discovery_seconds": components["discovery_seconds"],
        "idle_seconds": components["idle_seconds"],
        "grace_seconds": components["grace_seconds"],
        "merge_seconds": components["merge_seconds"],
        "interval_residual_seconds": max(0.0, float(total_seconds) - accounted),
        "quorum_trigger": str(quorum_trigger),
    }


def fastest_next_upload_eta_seconds(
    updates: list[dict[str, Any]],
    *,
    first_seen: UpdateFirstSeenRegistry,
    inner_steps: int,
    now_monotonic: float | None = None,
) -> float | None:
    """Estimate time until the earliest selected learner's next upload.

    The estimate intentionally uses measured compute time only. Serialization and
    adoption overhead therefore act as a conservative safety margin before the
    actual next pointer publication.
    """
    now_monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
    estimates: list[float] = []
    for update in updates:
        step_seconds = update.get("local_cycle_step_time_seconds_mean")
        observed = first_seen.get(str(update.get("update_id", "")))
        if step_seconds is None or observed is None:
            continue
        try:
            cycle_seconds = float(step_seconds) * max(1, int(inner_steps))
            eta_seconds = observed.monotonic + cycle_seconds - now_monotonic
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(cycle_seconds) or cycle_seconds <= 0.0:
            continue
        if not math.isfinite(eta_seconds):
            continue
        estimates.append(max(0.0, eta_seconds))
    return min(estimates) if estimates else None


def maybe_shorten_grace_deadline(
    *,
    deadline: float,
    selected: list[dict[str, Any]],
    config: Config,
    first_seen: UpdateFirstSeenRegistry | None,
    now_monotonic: float,
) -> tuple[float, float | None]:
    if config.sync.grace_window.mode != "adaptive_fastest_upload_eta":
        return deadline, None
    if first_seen is None:
        return deadline, None
    eta_seconds = fastest_next_upload_eta_seconds(
        selected,
        first_seen=first_seen,
        inner_steps=config.training.inner_steps,
        now_monotonic=now_monotonic,
    )
    if eta_seconds is None:
        return deadline, None
    return min(deadline, now_monotonic + eta_seconds), eta_seconds


@dataclass(frozen=True)
class UpdateProposalSource:
    """Parameterize full/fragment proposal discovery without changing events."""

    context_fields: dict[str, int]
    missing_files_event: str
    eligible_updates_fn: Callable[[SQLiteStore], list[dict[str, Any]]]
    drop_updates_fn: Callable[[SQLiteStore, list[str], str], Any]

    def eligible_updates(self, store: SQLiteStore) -> list[dict[str, Any]]:
        return self.eligible_updates_fn(store)

    def drop_updates(
        self,
        store: SQLiteStore,
        update_ids: list[str],
        reason: str,
    ) -> None:
        self.drop_updates_fn(store, update_ids, reason)


def full_update_proposal_source(
    *,
    current_version: int,
    max_staleness_versions: int,
) -> UpdateProposalSource:
    return UpdateProposalSource(
        context_fields={"version": current_version},
        missing_files_event="updates_dropped_missing_files",
        eligible_updates_fn=lambda store: store.eligible_updates(
            current_version,
            max_staleness_versions,
        ),
        drop_updates_fn=lambda store, update_ids, reason: store.drop_updates(
            update_ids,
            reason,
        ),
    )


def fragment_update_proposal_source(
    *,
    fragment_id: int,
    current_fragment_version: int,
    max_staleness_versions: int,
) -> UpdateProposalSource:
    return UpdateProposalSource(
        context_fields={
            "fragment_id": fragment_id,
            "fragment_version": current_fragment_version,
        },
        missing_files_event="fragment_updates_dropped_missing_files",
        eligible_updates_fn=lambda store: store.eligible_fragment_updates(
            fragment_id=fragment_id,
            current_fragment_version=current_fragment_version,
            max_staleness_versions=max_staleness_versions,
        ),
        drop_updates_fn=lambda store, update_ids, reason: store.drop_fragment_updates(
            update_ids,
            reason,
        ),
    )


def collect_with_grace_window(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    source: UpdateProposalSource,
    first_seen: UpdateFirstSeenRegistry | None = None,
    heartbeat_fences: dict[str, str] | None = None,
    telemetry_out: dict[str, float | str] | None = None,
) -> list[dict[str, Any]]:
    started_at = time.monotonic()
    initial_seconds = configured_grace_seconds(config)
    deadline = started_at + initial_seconds
    deadline_source = "initial"
    logger.event(
        "grace_window_started",
        mode=config.sync.grace_window.mode,
        initial_seconds=initial_seconds,
        **source.context_fields,
    )
    selected: list[dict[str, Any]] = []
    while True:
        eligible = source.eligible_updates(store)
        eligible = drop_missing_update_files(
            store,
            eligible,
            logger,
            source=source,
            first_seen=first_seen,
        )
        selected = select_one_per_learner(
            eligible,
            policy=config.sync.selection_policy,
            quorum_max=config.sync.quorum_max,
        )
        now_monotonic = time.monotonic()
        shortened_deadline, eta_seconds = maybe_shorten_grace_deadline(
            deadline=deadline,
            selected=selected,
            config=config,
            first_seen=first_seen,
            now_monotonic=now_monotonic,
        )
        if shortened_deadline < deadline:
            deadline = shortened_deadline
            deadline_source = "fastest_upload_eta"
            logger.event(
                "grace_window_shortened",
                selected=len(selected),
                fastest_next_upload_eta_seconds=eta_seconds,
                remaining_seconds=max(0.0, deadline - now_monotonic),
                **source.context_fields,
            )
        if len(selected) >= config.sync.quorum_max:
            deadline_source = "quorum_max"
            break
        if now_monotonic >= deadline:
            break
        time.sleep(min(config.sync.scan_interval_seconds, max(0.0, deadline - time.monotonic())))
        sync_liveness_and_metadata(
            store,
            paths,
            config,
            logger,
            first_seen=first_seen,
            heartbeat_fences=heartbeat_fences,
        )
    elapsed_seconds = time.monotonic() - started_at
    logger.event(
        "grace_window_completed",
        selected=len(selected),
        elapsed_seconds=elapsed_seconds,
        deadline_source=deadline_source,
        **source.context_fields,
    )
    if telemetry_out is not None:
        telemetry_out.update(
            grace_seconds=elapsed_seconds,
            quorum_trigger=deadline_source,
        )
    return selected


def drop_missing_update_files(
    store: SQLiteStore,
    updates: list[dict[str, Any]],
    logger: JsonlLogger,
    *,
    source: UpdateProposalSource,
    first_seen: UpdateFirstSeenRegistry | None = None,
) -> list[dict[str, Any]]:
    missing = [row["update_id"] for row in updates if not Path(row["file_path"]).exists()]
    if missing:
        source.drop_updates(store, missing, "missing_file")
        if first_seen is not None:
            first_seen.discard_many(missing)
        logger.event(source.missing_files_event, count=len(missing), update_ids=missing)
    missing_ids = set(missing)
    return [row for row in updates if row["update_id"] not in missing_ids]


def all_expected_learners_stopped(store: SQLiteStore, config: Config) -> bool:
    expected = {learner_id_from_index(index) for index in range(config.sync.num_learners)}
    stopped = {
        str(row["learner_id"])
        for row in store.list_learners()
        if row.get("status") == LEARNER_STATUS_STOPPED
    }
    return stopped == expected


@dataclass(frozen=True)
class TerminalDrainDecision:
    state: str
    selected: list[dict[str, Any]]

    def __post_init__(self) -> None:
        if self.state not in {"open", "closed_empty", "closed_selected"}:
            raise ValueError(f"invalid terminal drain state: {self.state}")
        if self.state == "closed_selected" and not self.selected:
            raise ValueError("closed_selected requires at least one proposal")
        if self.state != "closed_selected" and self.selected:
            raise ValueError(f"{self.state} cannot contain selected proposals")


def select_terminal_drain_updates(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    current_version: int,
) -> TerminalDrainDecision:
    if not all_expected_learners_stopped(store, config):
        return TerminalDrainDecision(state="open", selected=[])
    store.drop_ineligible_updates(current_version, config.sync.max_staleness_versions)
    source = full_update_proposal_source(
        current_version=current_version,
        max_staleness_versions=config.sync.max_staleness_versions,
    )
    selected = _select_terminal_drain_from_source(
        store,
        paths,
        config,
        logger,
        source=source,
        selected_event="terminal_drain_selected",
        empty_event="terminal_drain_no_pending_updates",
        remaining_outer_steps=(
            int(config.sync.stop_after_outer_steps) - current_version
            if config.sync.stop_after_outer_steps is not None
            else None
        ),
    )
    return TerminalDrainDecision(
        state="closed_selected" if selected else "closed_empty",
        selected=selected,
    )


def select_terminal_drain_fragment_updates(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    fragment_id: int,
    current_fragment_version: int,
    global_merge_event: int,
) -> TerminalDrainDecision:
    if not all_expected_learners_stopped(store, config):
        return TerminalDrainDecision(state="open", selected=[])
    store.drop_ineligible_fragment_updates(
        fragment_id=fragment_id,
        current_fragment_version=current_fragment_version,
        max_staleness_versions=config.sync.max_staleness_versions,
    )
    source = fragment_update_proposal_source(
        fragment_id=fragment_id,
        current_fragment_version=current_fragment_version,
        max_staleness_versions=config.sync.max_staleness_versions,
    )
    selected = _select_terminal_drain_from_source(
        store,
        paths,
        config,
        logger,
        source=source,
        selected_event="fragment_terminal_drain_selected",
        empty_event="fragment_terminal_drain_no_pending_updates",
        global_merge_event=global_merge_event,
    )
    return TerminalDrainDecision(
        state="closed_selected" if selected else "closed_empty",
        selected=selected,
    )


def _select_terminal_drain_from_source(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    source: UpdateProposalSource,
    selected_event: str,
    empty_event: str,
    **event_fields: Any,
) -> list[dict[str, Any]]:
    pending = source.eligible_updates(store)
    pending = drop_missing_update_files(store, pending, logger, source=source)
    selected = select_one_per_learner(
        pending,
        policy=config.sync.selection_policy,
        quorum_max=config.sync.quorum_max,
    )
    if selected:
        logger.event(
            selected_event,
            selected_count=len(selected),
            learners=[row["learner_id"] for row in selected],
            **source.context_fields,
            **event_fields,
        )
    else:
        logger.event(empty_event, **source.context_fields, **event_fields)
    return selected


def init_wandb_run(
    *,
    config: Config,
    paths: RunPaths,
    logger: JsonlLogger,
    device: torch.device,
    hostname: str,
) -> Any | None:
    if wandb_is_disabled(config):
        logger.event("wandb_disabled")
        return None
    try:
        import wandb
    except Exception as exc:
        logger.event("wandb_unavailable", error=repr(exc))
        return None

    project_name = syncer_wandb_project_name(config)
    run_name = syncer_wandb_run_name(config)
    mode = os.environ.get("WANDB_MODE") or config.wandb.mode
    run_id = f"syncer-{config.run.run_id}" if config.run.run_id else None
    kwargs: dict[str, Any] = {
        "project": project_name,
        "name": run_name,
        "id": run_id,
        "resume": "allow",
        "config": wandb_config(
            config,
            device=str(device),
            hostname=hostname,
            shared_root=str(paths.shared_root),
        ),
        "tags": syncer_wandb_tags(config),
        "dir": str(paths.logs),
    }
    if mode:
        kwargs["mode"] = mode
    if config.wandb.entity:
        kwargs["entity"] = config.wandb.entity
    kwargs["group"] = config.wandb.group or config.run.name

    try:
        run = wandb.init(**kwargs)
        wandb.define_metric("syncer/version")
        wandb.define_metric("*", step_metric="syncer/version")
    except Exception as exc:
        logger.event("wandb_init_failed", project=project_name, run_name=run_name, error=repr(exc))
        return None
    logger.event("wandb_initialized", project=project_name, run_name=run_name, mode=mode)
    return run


def publish_stop(
    paths: RunPaths,
    *,
    config: Config,
    reason: str,
    version: int,
    total_seen_tokens: int,
) -> None:
    atomic_write_json(
        paths.stop_json,
        {
            "format_version": FORMAT_VERSION,
            "run_id": config.run.run_id,
            "reason": reason,
            "version": version,
            "total_seen_tokens": total_seen_tokens,
            "timestamp": time.time(),
        },
    )


def _selected_resource_csv_fields(metrics: dict[str, float]) -> dict[str, float]:
    return {key.replace("learner/", "learner_", 1): value for key, value in metrics.items()}


def learner_shutdown_timeout_seconds(config: Config) -> float:
    configured = config.liveness.learner_shutdown_timeout_seconds
    if configured is not None:
        return float(configured)
    return max(120.0, 2.0 * float(config.liveness.heartbeat_interval_seconds))


def wait_for_learner_shutdown(
    *,
    paths: RunPaths,
    store: SQLiteStore,
    config: Config,
    logger: JsonlLogger,
    stop_reason: str,
    first_seen: UpdateFirstSeenRegistry | None = None,
    heartbeat_fences: dict[str, str] | None = None,
) -> bool:
    if stop_reason == "error":
        return False
    expected = {learner_id_from_index(index) for index in range(config.sync.num_learners)}
    timeout_seconds = learner_shutdown_timeout_seconds(config)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        sync_liveness_and_metadata(
            store,
            paths,
            config,
            logger,
            first_seen=first_seen,
            heartbeat_fences=heartbeat_fences,
        )
        if all_expected_learners_stopped(store, config):
            logger.event("all_learners_stopped", count=len(expected))
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    learners_by_id = {str(row["learner_id"]): row for row in store.list_learners()}
    unconfirmed_learners = []
    for learner_id in sorted(expected):
        row = learners_by_id.get(learner_id)
        if row is not None and row.get("status") == LEARNER_STATUS_STOPPED:
            continue
        unconfirmed_learners.append(
            {
                "learner_id": learner_id,
                "status": row.get("status", "unknown") if row is not None else "unknown",
                "status_reason": row.get("status_reason") if row is not None else None,
                "last_seen": row.get("last_seen") if row is not None else None,
            }
        )
    logger.event(
        "learner_shutdown_timeout",
        timeout_seconds=timeout_seconds,
        expected_count=len(expected),
        unconfirmed_learners=unconfirmed_learners,
    )
    return False


def learner_resource_summary(
    *,
    paths: RunPaths,
    store: SQLiteStore,
    config: Config,
) -> dict[str, Any]:
    per_learner: dict[str, dict[str, float | int | str | None]] = {
        learner_id_from_index(index): {} for index in range(config.sync.num_learners)
    }
    for row in store.learner_resource_peaks(fragment_mode=config.fragments.enabled):
        learner_id = str(row["learner_id"])
        target = per_learner.setdefault(learner_id, {})
        for key in (
            "training_cpu_utilization_peak_percent",
            "training_gpu_utilization_peak_percent",
        ):
            value = row.get(key)
            if value is not None and math.isfinite(float(value)):
                target[key] = float(value)

    for learner_id, target in per_learner.items():
        heartbeat = safe_read_json(paths.heartbeats / f"{learner_id}.json") or {}
        target["status"] = heartbeat.get("status")
        target["last_local_step"] = int(heartbeat.get("last_local_step") or 0)
        for key in (
            "training_cpu_utilization_peak_percent",
            "training_gpu_utilization_peak_percent",
        ):
            value = heartbeat.get(key)
            if value is None or not math.isfinite(float(value)):
                continue
            target[key] = max(float(value), float(target.get(key) or 0.0))

    def values(key: str) -> list[float]:
        return [
            float(row[key])
            for row in per_learner.values()
            if row.get(key) is not None and math.isfinite(float(row[key]))
        ]

    cpu = values("training_cpu_utilization_peak_percent")
    gpu = values("training_gpu_utilization_peak_percent")
    return {
        "cpu_utilization_scope": "whole_node",
        "gpu_utilization_scope": "learner_cuda_visible_device",
        "per_learner": per_learner,
        "training_cpu_utilization_peak_percent_max": max(cpu) if cpu else None,
        "training_cpu_utilization_peak_percent_mean": sum(cpu) / len(cpu) if cpu else None,
        "training_gpu_utilization_peak_percent_max": max(gpu) if gpu else None,
        "training_gpu_utilization_peak_percent_mean": sum(gpu) / len(gpu) if gpu else None,
    }


def write_training_summary(
    *,
    paths: RunPaths,
    store: SQLiteStore,
    config: Config,
    logger: JsonlLogger,
    wandb_run: Any | None,
    stop_reason: str,
    final_version: int,
    total_seen_tokens: int,
    run_started_at: float,
    run_start_monotonic: float,
    all_learners_stopped: bool,
) -> dict[str, Any]:
    completed_at = time.time()
    complete_duration_seconds = time.monotonic() - run_start_monotonic
    resources = learner_resource_summary(paths=paths, store=store, config=config)
    summary = {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "stop_reason": stop_reason,
        "final_version": int(final_version),
        "total_seen_tokens": int(total_seen_tokens),
        "training_started_at": run_started_at,
        "training_completed_at": completed_at,
        "complete_training_time_seconds": complete_duration_seconds,
        "all_learners_stopped": all_learners_stopped,
        "learner_resources": resources,
    }
    atomic_write_json(paths.summary_json, summary)
    store.set_run_state("summary", summary)
    if wandb_run is not None:
        wandb_run.summary["training/complete_time_seconds"] = complete_duration_seconds
        wandb_run.summary["training/all_learners_stopped"] = all_learners_stopped
        for key in (
            "training_cpu_utilization_peak_percent_max",
            "training_cpu_utilization_peak_percent_mean",
            "training_gpu_utilization_peak_percent_max",
            "training_gpu_utilization_peak_percent_mean",
        ):
            value = resources.get(key)
            if value is not None:
                wandb_run.summary[f"learner/{key}"] = value
    logger.event(
        "training_summary_written",
        path=str(paths.summary_json),
        complete_training_time_seconds=complete_duration_seconds,
        all_learners_stopped=all_learners_stopped,
    )
    return summary


def _terminal_summary_matches(
    payload: dict[str, Any] | None,
    *,
    config: Config,
    terminal: dict[str, Any],
) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        return (
            payload.get("run_id") == config.run.run_id
            and payload.get("stop_reason") == terminal["stop_reason"]
            and int(payload.get("final_version", -1)) == int(terminal["final_version"])
            and int(payload.get("total_seen_tokens", -1)) == int(terminal["total_seen_tokens"])
        )
    except (TypeError, ValueError):
        return False


def _summary_for_terminal_repair(
    *,
    paths: RunPaths,
    store: LeaderBoundSQLiteStore,
    config: Config,
    terminal: dict[str, Any],
) -> dict[str, Any]:
    candidates = (
        store.get_run_state("summary"),
        safe_read_json(paths.summary_json),
    )
    for candidate in candidates:
        if _terminal_summary_matches(candidate, config=config, terminal=terminal):
            assert isinstance(candidate, dict)
            return dict(candidate)
    descriptor = safe_read_json(paths.run_descriptor_json) or {}
    completed_at = float(terminal["finalized_at"])
    started_at = float(descriptor.get("created_at", completed_at))
    return {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "stop_reason": str(terminal["stop_reason"]),
        "final_version": int(terminal["final_version"]),
        "total_seen_tokens": int(terminal["total_seen_tokens"]),
        "training_started_at": started_at,
        "training_completed_at": completed_at,
        "complete_training_time_seconds": max(0.0, completed_at - started_at),
        "all_learners_stopped": all_expected_learners_stopped(store, config),
        "learner_resources": learner_resource_summary(
            paths=paths,
            store=store,
            config=config,
        ),
        "terminal_summary_reconstructed": True,
    }


def repair_completed_ha_terminal(
    *,
    paths: RunPaths,
    store: LeaderBoundSQLiteStore,
    lease_store: Any,
    config: Config,
    logger: JsonlLogger,
) -> dict[str, Any]:
    """Finish a normal terminal generation interrupted before its full publication."""
    previous = store.terminal_state()
    if previous is None or previous.get("stop_reason") in (None, "", "error"):
        raise RuntimeError("terminal repair requires a completed terminal row")
    committed = store.latest_global_version()
    if committed is None or int(committed["version"]) != int(previous["final_version"]):
        raise RuntimeError("terminal repair DB version does not match terminal state")
    summary = _summary_for_terminal_repair(
        paths=paths,
        store=store,
        config=config,
        terminal=previous,
    )
    store.set_run_state("summary", summary)
    maintenance = run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
        scan_interval_seconds=config.sync.scan_interval_seconds,
        input_closed=bool(summary.get("all_learners_stopped")),
    )
    generation = int(previous["generation"]) + 1
    repaired = store.finalize_terminal_state(
        generation=generation,
        stop_reason=str(previous["stop_reason"]),
        final_version=int(previous["final_version"]),
        total_seen_tokens=int(previous["total_seen_tokens"]),
    )
    publisher = EpochControlPublisher(paths, store.fenced_store, store.token)
    publisher.repair_latest_from_db()
    lease_row = lease_store.observe()
    if lease_row is None:
        raise RuntimeError("terminal repair leader row disappeared")
    publisher.publish_heartbeat(lease_row)
    publisher.publish_terminal(repaired, summary=summary)
    logger.event(
        "terminal_repair_completed",
        previous_generation=int(previous["generation"]),
        generation=generation,
        final_version=int(repaired["final_version"]),
        **maintenance,
    )
    return repaired


def _fragment_staleness_stats(
    selected: list[dict[str, Any]], current_fragment_version: int
) -> dict[str, float | int]:
    values = [
        max(0, int(current_fragment_version) - int(row["base_fragment_version"]))
        for row in selected
    ]
    if not values:
        return {"min": 0, "mean": 0.0, "max": 0}
    return {
        "min": min(values),
        "mean": sum(values) / len(values),
        "max": max(values),
    }


def merge_staleness_evidence(
    selected: list[dict[str, Any]],
    effective_weights: dict[str, float],
    *,
    current_version: int,
    base_version_field: str,
) -> dict[str, float | str]:
    counts: dict[int, int] = {}
    weighted_mean = 0.0
    fresh_weight = 0.0
    for row in selected:
        update_id = str(row["update_id"])
        if update_id not in effective_weights:
            raise ValueError(f"missing effective weight for {update_id}")
        weight = float(effective_weights[update_id])
        if not math.isfinite(weight) or weight < 0.0:
            raise ValueError(f"invalid effective weight for {update_id}: {weight}")
        staleness = max(0, int(current_version) - int(row[base_version_field]))
        counts[staleness] = counts.get(staleness, 0) + 1
        weighted_mean += staleness * weight
        if staleness == 0:
            fresh_weight += weight
    return {
        "effective_staleness_mean": weighted_mean,
        "fresh_effective_weight": fresh_weight,
        "staleness_counts_json": json.dumps(
            {str(key): value for key, value in sorted(counts.items())},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def run_fragment_syncer(
    *,
    config: Config,
    paths: RunPaths,
    store: SQLiteStore,
    logger: JsonlLogger,
    device: torch.device,
    wandb_run: Any | None,
    run_started_at: float,
    run_start_monotonic: float,
) -> None:
    if config.init.resume:
        raise NotImplementedError("fragment mode resume is not implemented yet")
    (
        global_merge_event,
        fragment_thetas,
        outer_states,
        param_index,
        fragment_index,
        fragment_versions,
        fragment_updated_events,
        total_seen_tokens,
        materialized_weight_path,
    ) = initialize_fragment_run(config, paths, store, logger, device=device)
    first_seen = UpdateFirstSeenRegistry(capacity=update_first_seen_capacity(config))

    last_progress_time = time.time()
    cycle_started_monotonic = time.monotonic()
    cycle_discovery_seconds = 0.0
    cycle_idle_seconds = 0.0
    cycle_grace_seconds = 0.0
    cycle_quorum_trigger = "not_reached"
    stop_reason = "completed"
    terminal_grace_complete = False
    try:
        while True:
            if config.sync.stop_after_outer_steps is not None and global_merge_event >= int(
                config.sync.stop_after_outer_steps
            ):
                stop_reason = "stop_after_outer_steps"
                break
            if (
                config.sync.stop_after_global_tokens is not None
                and total_seen_tokens >= config.sync.stop_after_global_tokens
            ):
                stop_reason = "stop_after_global_tokens"
                break

            target_fragment = select_fragment(
                global_merge_event,
                int(fragment_index["num_fragments"]),
                schedule=config.fragments.schedule,
            )
            current_fragment_version = int(fragment_versions[target_fragment])
            proposal_source = fragment_update_proposal_source(
                fragment_id=target_fragment,
                current_fragment_version=current_fragment_version,
                max_staleness_versions=config.sync.max_staleness_versions,
            )
            discovery_start = time.monotonic()
            sync_liveness_and_metadata(
                store,
                paths,
                config,
                logger,
                first_seen=first_seen,
            )
            input_closed = all_expected_learners_stopped(store, config)
            cycle_discovery_seconds += time.monotonic() - discovery_start
            if not input_closed and terminal_grace_complete:
                terminal_grace_complete = False
                logger.event(
                    "fragment_terminal_input_reopened",
                    global_merge_event=global_merge_event,
                    fragment_id=target_fragment,
                )
            if input_closed and not terminal_grace_complete:
                logger.event(
                    "fragment_terminal_input_closed",
                    global_merge_event=global_merge_event,
                    fragment_id=target_fragment,
                )
                terminal_grace_start = time.monotonic()
                grace_seconds = configured_grace_seconds(config)
                if grace_seconds > 0:
                    time.sleep(grace_seconds)
                sync_liveness_and_metadata(
                    store,
                    paths,
                    config,
                    logger,
                    first_seen=first_seen,
                )
                input_closed = all_expected_learners_stopped(store, config)
                cycle_grace_seconds += time.monotonic() - terminal_grace_start
                if input_closed:
                    cycle_quorum_trigger = "terminal_drain"
                    terminal_grace_complete = True
                else:
                    logger.event(
                        "fragment_terminal_input_reopened",
                        global_merge_event=global_merge_event,
                        fragment_id=target_fragment,
                    )

            terminal_drain = False
            if input_closed:
                discovery_start = time.monotonic()
                terminal_decision = select_terminal_drain_fragment_updates(
                    store,
                    paths,
                    config,
                    logger,
                    fragment_id=target_fragment,
                    current_fragment_version=current_fragment_version,
                    global_merge_event=global_merge_event,
                )
                cycle_discovery_seconds += time.monotonic() - discovery_start
                if terminal_decision.state == "open":
                    input_closed = False
                    terminal_grace_complete = False
                    logger.event(
                        "fragment_terminal_input_reopened",
                        global_merge_event=global_merge_event,
                        fragment_id=target_fragment,
                    )
                elif terminal_decision.state == "closed_empty":
                    stop_reason = "input_exhausted"
                    logger.event(
                        "input_exhausted",
                        global_merge_event=global_merge_event,
                        fragment_id=target_fragment,
                    )
                    break
                else:
                    selected = terminal_decision.selected
                    terminal_drain = len(selected) < config.sync.quorum_min

            if not input_closed:
                discovery_start = time.monotonic()
                eligible = proposal_source.eligible_updates(store)
                eligible = drop_missing_update_files(
                    store,
                    eligible,
                    logger,
                    source=proposal_source,
                    first_seen=first_seen,
                )
                one_per_learner = select_one_per_learner(
                    eligible,
                    policy=config.sync.selection_policy,
                    quorum_max=config.sync.quorum_max,
                )
                cycle_discovery_seconds += time.monotonic() - discovery_start
                if len(one_per_learner) < config.sync.quorum_min:
                    logger.event(
                        "fragment_quorum_wait",
                        eligible=len(one_per_learner),
                        quorum_min=config.sync.quorum_min,
                        global_merge_event=global_merge_event,
                        fragment_id=target_fragment,
                        fragment_version=current_fragment_version,
                    )
                    if no_progress_timed_out(
                        last_progress_time,
                        config.liveness.no_progress_timeout_seconds,
                    ):
                        stop_reason = "no_progress_timeout"
                        logger.event(
                            "no_progress_timeout",
                            global_merge_event=global_merge_event,
                            fragment_id=target_fragment,
                        )
                        break
                    idle_start = time.monotonic()
                    time.sleep(config.sync.scan_interval_seconds)
                    cycle_idle_seconds += time.monotonic() - idle_start
                    continue

                grace_telemetry: dict[str, float | str] = {}
                selected = collect_with_grace_window(
                    store,
                    paths,
                    config,
                    logger,
                    source=proposal_source,
                    first_seen=first_seen,
                    telemetry_out=grace_telemetry,
                )
                cycle_grace_seconds += float(grace_telemetry["grace_seconds"])
                cycle_quorum_trigger = str(grace_telemetry["quorum_trigger"])
            if not terminal_drain and len(selected) < config.sync.quorum_min:
                logger.event(
                    "fragment_quorum_wait",
                    eligible=len(selected),
                    quorum_min=config.sync.quorum_min,
                    global_merge_event=global_merge_event,
                    fragment_id=target_fragment,
                    fragment_version=current_fragment_version,
                )
                continue

            run_selection_id = (
                f"{config.run.run_id}_g{global_merge_event + 1:06d}_f{target_fragment:03d}"
            )
            store.mark_fragment_updates_selected(
                [row["update_id"] for row in selected], run_selection_id
            )
            logger.event(
                "fragment_updates_selected",
                global_merge_event=global_merge_event,
                fragment_id=target_fragment,
                fragment_version=current_fragment_version,
                update_ids=[row["update_id"] for row in selected],
                learners=[row["learner_id"] for row in selected],
            )

            read_start = time.monotonic()
            missing_after_select = [
                row["update_id"] for row in selected if not Path(row["file_path"]).exists()
            ]
            if missing_after_select:
                missing_ids = set(missing_after_select)
                store.drop_fragment_updates(missing_after_select, "missing_file")
                first_seen.discard_many(missing_after_select)
                store.reset_fragment_selected_to_pending(
                    [row["update_id"] for row in selected if row["update_id"] not in missing_ids]
                )
                logger.event(
                    "selected_fragment_updates_missing_files", count=len(missing_after_select)
                )
                continue
            try:
                vectors = [
                    load_fragment_update(
                        row["file_path"],
                        device=device,
                        dtype=syncer_compute_dtype(config),
                    )
                    for row in selected
                ]
            except FileNotFoundError:
                missing = [
                    row["update_id"] for row in selected if not Path(row["file_path"]).exists()
                ]
                if missing:
                    missing_ids = set(missing)
                    store.drop_fragment_updates(missing, "missing_file")
                    first_seen.discard_many(missing)
                    store.reset_fragment_selected_to_pending(
                        [
                            row["update_id"]
                            for row in selected
                            if row["update_id"] not in missing_ids
                        ]
                    )
                    logger.event("selected_fragment_updates_missing_files", count=len(missing))
                    continue
                raise
            read_seconds = time.monotonic() - read_start

            weights_by_update = normalized_fragment_update_weights(
                selected,
                current_fragment_version=current_fragment_version,
                staleness_lambda=config.sync.staleness_lambda,
            )
            weights = [weights_by_update[row["update_id"]] for row in selected]
            staleness_evidence = merge_staleness_evidence(
                selected,
                weights_by_update,
                current_version=current_fragment_version,
                base_version_field="base_fragment_version",
            )
            aggregation_start = time.monotonic()
            p_bar = weighted_average_tensors(vectors, weights)
            theta_f = fragment_thetas[target_fragment]
            grad = theta_f - p_bar
            aggregation_seconds = time.monotonic() - aggregation_start

            outer_start = time.monotonic()
            theta_f, outer_state_f = outer_optimizer_step(
                theta_f,
                grad,
                outer_states[target_fragment],
                config.outer_optimizer,
            )
            theta_f, outer_state_f = align_state_to_publication_dtype(
                config,
                theta_f,
                outer_state_f,
            )
            outer_seconds = time.monotonic() - outer_start

            new_fragment_version = current_fragment_version + 1
            new_global_merge_event = global_merge_event + 1
            total_update_tokens = sum(int(row["tokens_this_update"]) for row in selected)
            total_seen_tokens += total_update_tokens
            fragment_thetas[target_fragment] = theta_f
            outer_states[target_fragment] = outer_state_f
            fragment_versions[target_fragment] = new_fragment_version
            fragment_updated_events[target_fragment] = new_global_merge_event

            publish_start = time.monotonic()
            weight_path = paths.fragment_weight_path(target_fragment, new_fragment_version)
            optim_path = paths.fragment_outer_optim_path(target_fragment, new_fragment_version)
            publish_dtype = syncer_publish_dtype(config)
            save_fragment_weight(weight_path, theta_f, dtype=publish_dtype)
            save_outer_state(
                optim_path,
                theta_f,
                outer_state_f,
                dtype=publish_dtype,
            )
            store.upsert_fragment_version(
                fragment_id=target_fragment,
                version=new_fragment_version,
                global_merge_event=new_global_merge_event,
                weight_path=str(weight_path),
                optim_path=str(optim_path),
                num_updates=len(selected),
                total_update_tokens=total_update_tokens,
                total_seen_tokens=total_seen_tokens,
                outer_optimizer=config.outer_optimizer.name,
                status=GLOBAL_STATUS_COMMITTED,
            )
            fragment_publication = publish_fragment_latest(
                config=config,
                paths=paths,
                param_index=param_index,
                fragment_index=fragment_index,
                fragment_thetas=fragment_thetas,
                fragment_versions=fragment_versions,
                fragment_updated_events=fragment_updated_events,
                total_seen_tokens=total_seen_tokens,
                global_merge_event=new_global_merge_event,
                previous_materialized_weight_path=materialized_weight_path,
            )
            materialized_weight_path = fragment_publication.materialized_weight_path
            materialize_seconds = fragment_publication.materialize_full_seconds
            publish_seconds = time.monotonic() - publish_start

            store.mark_fragment_updates_applied(
                selected,
                applied_fragment_version=new_fragment_version,
                applied_global_merge_event=new_global_merge_event,
                effective_weights=weights_by_update,
            )
            first_seen.discard_many([row["update_id"] for row in selected])
            dropped = store.drop_superseded_fragment_updates(selected)
            dropped += store.drop_obsolete_fragment_updates(
                fragment_id=target_fragment,
                current_fragment_version=new_fragment_version,
                max_staleness_versions=config.sync.max_staleness_versions,
            )
            maintenance_start = time.monotonic()
            maintenance = run_maintenance(
                store,
                paths,
                heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
                scan_interval_seconds=config.sync.scan_interval_seconds,
            )
            maintenance_seconds = time.monotonic() - maintenance_start
            logger.event(
                "state_maintenance_completed",
                maintenance_seconds=maintenance_seconds,
                **maintenance,
            )

            interval_total_seconds = time.monotonic() - cycle_started_monotonic
            breakdown = interval_breakdown(
                total_seconds=interval_total_seconds,
                discovery_seconds=cycle_discovery_seconds,
                idle_seconds=cycle_idle_seconds,
                grace_seconds=cycle_grace_seconds,
                read_seconds=read_seconds,
                merge_seconds=aggregation_seconds + outer_seconds,
                publish_seconds=publish_seconds,
                maintenance_seconds=maintenance_seconds,
                quorum_trigger=cycle_quorum_trigger,
            )
            stale_stats = _fragment_staleness_stats(selected, current_fragment_version)
            learner_resources = selected_resource_summary(selected)
            append_csv_row(
                paths.metrics / "syncer_metrics.csv",
                {
                    "timestamp": time.time(),
                    "version": new_global_merge_event,
                    "global_merge_event": new_global_merge_event,
                    "fragment_id": target_fragment,
                    "fragment_version": new_fragment_version,
                    "selected_count": len(selected),
                    "total_update_tokens": total_update_tokens,
                    "read_seconds": read_seconds,
                    "fragment_read_seconds": read_seconds,
                    "aggregation_seconds": aggregation_seconds,
                    "fragment_aggregation_seconds": aggregation_seconds,
                    "outer_step_seconds": outer_seconds,
                    "publish_seconds": publish_seconds,
                    "maintenance_seconds": maintenance_seconds,
                    "maintenance_scan_seconds": maintenance["maintenance_scan_seconds"],
                    "maintenance_scanned_rows": maintenance["maintenance_scanned_rows"],
                    "gc_pending_rows": maintenance["gc_pending_rows"],
                    "materialize_full_seconds": materialize_seconds,
                    "materialized_this_event": fragment_publication.materialized_this_event,
                    "materialized_bytes": fragment_publication.materialized_bytes,
                    "fragment_staleness_min": stale_stats["min"],
                    "fragment_staleness_mean": stale_stats["mean"],
                    "fragment_staleness_max": stale_stats["max"],
                    **staleness_evidence,
                    "stale_updates_dropped": dropped,
                    "global_interval_seconds": interval_total_seconds,
                    **breakdown,
                    **_selected_resource_csv_fields(learner_resources),
                },
                SYNCER_METRIC_FIELDS,
            )
            if wandb_run is not None:
                wandb_run.log(
                    {
                        "syncer/version": new_global_merge_event,
                        "syncer/global_merge_event": new_global_merge_event,
                        "syncer/fragment_id": target_fragment,
                        "syncer/fragment_version": new_fragment_version,
                        "syncer/selected_count": len(selected),
                        "syncer/total_update_tokens": total_update_tokens,
                        "syncer/total_seen_tokens": total_seen_tokens,
                        "syncer/read_seconds": read_seconds,
                        "syncer/aggregation_seconds": aggregation_seconds,
                        "syncer/outer_step_seconds": outer_seconds,
                        "syncer/publish_seconds": publish_seconds,
                        "syncer/materialize_full_seconds": materialize_seconds,
                        "syncer/materialized_this_event": int(
                            fragment_publication.materialized_this_event
                        ),
                        "syncer/materialized_bytes": fragment_publication.materialized_bytes,
                        "syncer/stale_updates_dropped": dropped,
                        "syncer/effective_staleness_mean": staleness_evidence[
                            "effective_staleness_mean"
                        ],
                        "syncer/fresh_effective_weight": staleness_evidence[
                            "fresh_effective_weight"
                        ],
                        "syncer/global_interval_seconds": interval_total_seconds,
                        **{f"syncer/{key}": value for key, value in breakdown.items()},
                        **learner_resources,
                    },
                    step=new_global_merge_event,
                )
            logger.event(
                "fragment_outer_step_applied",
                global_merge_event=new_global_merge_event,
                fragment_id=target_fragment,
                fragment_version=new_fragment_version,
                selected_count=len(selected),
                total_update_tokens=total_update_tokens,
                **staleness_evidence,
            )
            logger.event("fragment_latest_published", global_merge_event=new_global_merge_event)
            if dropped:
                logger.event(
                    "fragment_updates_dropped",
                    global_merge_event=new_global_merge_event,
                    fragment_id=target_fragment,
                    count=dropped,
                )
            global_merge_event = new_global_merge_event
            last_progress_time = time.time()
            cycle_started_monotonic = time.monotonic()
            cycle_discovery_seconds = 0.0
            cycle_idle_seconds = 0.0
            cycle_grace_seconds = 0.0
            cycle_quorum_trigger = "not_reached"
    except Exception:
        stop_reason = "error"
        logger.exception("error", global_merge_event=global_merge_event)
        raise
    finally:
        try:
            if stop_reason != "error":
                terminal_publication = publish_fragment_latest(
                    config=config,
                    paths=paths,
                    param_index=param_index,
                    fragment_index=fragment_index,
                    fragment_thetas=fragment_thetas,
                    fragment_versions=fragment_versions,
                    fragment_updated_events=fragment_updated_events,
                    total_seen_tokens=total_seen_tokens,
                    global_merge_event=global_merge_event,
                    previous_materialized_weight_path=materialized_weight_path,
                    force_materialize=True,
                )
                materialized_weight_path = terminal_publication.materialized_weight_path
            publish_stop(
                paths,
                config=config,
                reason=stop_reason,
                version=global_merge_event,
                total_seen_tokens=total_seen_tokens,
            )
            logger.event("stop_published", reason=stop_reason, version=global_merge_event)
            all_learners_stopped = wait_for_learner_shutdown(
                paths=paths,
                store=store,
                config=config,
                logger=logger,
                stop_reason=stop_reason,
                first_seen=first_seen,
            )
            ingest_update_metadata(
                store,
                paths,
                config,
                logger,
                first_seen=first_seen,
            )
            if stop_reason != "error" and all_learners_stopped:
                finalized = store.finalize_unconsumed_updates(
                    fragment_mode=True,
                    reason=stop_reason,
                )
                if finalized:
                    logger.event(
                        "unconsumed_updates_finalized",
                        count=finalized,
                        reason=stop_reason,
                    )
            write_training_summary(
                paths=paths,
                store=store,
                config=config,
                logger=logger,
                wandb_run=wandb_run,
                stop_reason=stop_reason,
                final_version=global_merge_event,
                total_seen_tokens=total_seen_tokens,
                run_started_at=run_started_at,
                run_start_monotonic=run_start_monotonic,
                all_learners_stopped=all_learners_stopped,
            )
            if stop_reason != "error":
                maintenance = run_maintenance(
                    store,
                    paths,
                    heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
                    scan_interval_seconds=config.sync.scan_interval_seconds,
                    input_closed=all_learners_stopped,
                )
                logger.event("state_maintenance_completed", **maintenance)
            if wandb_run is not None:
                wandb_run.summary["stop_reason"] = stop_reason
                wandb_run.summary["final_version"] = global_merge_event
                wandb_run.summary["total_seen_tokens"] = total_seen_tokens
            logger.event("process_exit", reason=stop_reason, version=global_merge_event)
        finally:
            try:
                if wandb_run is not None:
                    wandb_run.finish(exit_code=1 if stop_reason == "error" else 0)
            finally:
                store.close()


def run_syncer(config: Config) -> None:
    run_started_at = time.time()
    run_start_monotonic = time.monotonic()
    device = resolve_syncer_device(config)
    paths = RunPaths(Path(config.run.shared_root or "."))
    database_path = sqlite_path(config)
    ha_mode = bool(config.coordination.syncer_ha.enabled)
    lease_store = None
    leader_token = None
    lease_renewer = None
    store = None

    def cleanup_acquired_leadership(startup_error: BaseException) -> None:
        """Best-effort cleanup for every failure after a successful acquire."""
        for cleanup_name, cleanup in (
            (
                "lease renewer stop",
                None if lease_renewer is None else lease_renewer.stop,
            ),
            ("leader store close", None if store is None else store.close),
            (
                "leader lease release",
                (
                    None
                    if lease_store is None or leader_token is None
                    else lambda: lease_store.release(leader_token)
                ),
            ),
            ("leader lease close", None if lease_store is None else lease_store.close),
        ):
            if cleanup is None:
                continue
            try:
                cleanup()
            except BaseException as cleanup_error:
                startup_error.add_note(f"{cleanup_name} failed: {cleanup_error!r}")

    if ha_mode:
        loaded = load_run_descriptor(
            paths.shared_root,
            expected_run_id=config.run.run_id,
            expected_git_commit=config.run.git_commit,
            expected_git_dirty=config.run.git_dirty,
            expected_source_fingerprint=config.run.source_fingerprint,
            expected_descriptor_sha256=os.environ.get("FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"),
        )
        if config_to_dict(loaded.config) != config_to_dict(config):
            raise RuntimeError(
                "candidate config differs from the immutable resolved run descriptor"
            )
        try:
            lease_store, leader_token, lease_safety_tracker, _candidate_logger = (
                acquire_candidate(
                    paths=paths,
                    identity=loaded.identity,
                    config=config,
                )
            )
            store = open_leader_store(
                paths=paths,
                identity=loaded.identity,
                config=config,
                token=leader_token,
                safety_tracker=lease_safety_tracker,
            )
            logger = JsonlLogger(
                paths.logs
                / "syncers"
                / f"e{leader_token.epoch:06d}_{paths.owner_short(leader_token.owner_id)}.jsonl",
                "syncer",
            )
            lease_renewer = LeaseRenewalThread(
                paths=paths,
                identity=loaded.identity,
                config=config,
                token=leader_token,
                fenced_store=store.fenced_store,
                safety_tracker=lease_safety_tracker,
            )
            lease_renewer.start()
        except BaseException as startup_error:
            # Leadership ownership starts at acquire(), not at the main-loop
            # try/finally below.  Every later setup boundary therefore belongs
            # to this partial-initialization cleanup guard.
            cleanup_acquired_leadership(startup_error)
            raise
    else:
        prepare_run_dirs(paths, config.sync.num_learners)
        if config.init.resume and not database_path.is_file():
            raise FileNotFoundError(
                "resume requires persistent SQLite at "
                f"{database_path}; latest.json is not authoritative"
            )
        store = SQLiteStore(database_path)
        logger = JsonlLogger(paths.logs / "syncer.jsonl", "syncer")
    assert store is not None
    try:
        log_uncaught_exception(logger)
        hostname = socket.gethostname()
        logger.event(
            "process_start",
            run_id=config.run.run_id,
            shared_root=str(paths.shared_root),
            sqlite_path=str(store.path),
            hostname=hostname,
            device=str(device),
            compute_dtype=config.syncer.compute_dtype,
            publish_dtype=config.syncer.publish_dtype,
            leader_epoch=None if leader_token is None else leader_token.epoch,
            leader_owner_id=None if leader_token is None else leader_token.owner_id,
            cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"),
        )
        existing_terminal = store.terminal_state() if ha_mode else None
    except BaseException as startup_error:
        if ha_mode:
            cleanup_acquired_leadership(startup_error)
        else:
            try:
                store.close()
            except BaseException as cleanup_error:
                startup_error.add_note(f"store close failed: {cleanup_error!r}")
        raise
    if ha_mode:
        if existing_terminal is not None and existing_terminal.get("stop_reason") not in (
            None,
            "",
            "error",
        ):
            assert lease_store is not None and leader_token is not None
            try:
                repaired = repair_completed_ha_terminal(
                    paths=paths,
                    store=store,
                    lease_store=lease_store,
                    config=config,
                    logger=logger,
                )
                logger.event(
                    "process_exit",
                    reason="terminal_repaired",
                    version=int(repaired["final_version"]),
                )
            finally:
                try:
                    if lease_renewer is not None:
                        lease_renewer.stop()
                finally:
                    try:
                        store.close()
                    finally:
                        try:
                            lease_store.release(leader_token)
                        finally:
                            lease_store.close()
            return
    wandb_run = None
    if config.fragments.enabled:
        wandb_run = init_wandb_run(
            config=config,
            paths=paths,
            logger=logger,
            device=device,
            hostname=hostname,
        )
        run_fragment_syncer(
            config=config,
            paths=paths,
            store=store,
            logger=logger,
            device=device,
            wandb_run=wandb_run,
            run_started_at=run_started_at,
            run_start_monotonic=run_start_monotonic,
        )
        return
    try:
        if not ha_mode:
            wandb_run = init_wandb_run(
                config=config,
                paths=paths,
                logger=logger,
                device=device,
                hostname=hostname,
            )
        resume_requested = store.committed_global_count() > 0 if ha_mode else config.init.resume
        if resume_requested:
            version, theta, outer_state, param_index, total_seen_tokens = resume_run(
                config,
                paths,
                store,
                logger,
                device=device,
            )
            resume_generation = store.get_run_state("resume_generation") or {}
            heartbeat_fences = {
                str(learner_id): str(fingerprint)
                for learner_id, fingerprint in dict(
                    resume_generation.get("heartbeat_fences") or {}
                ).items()
            }
        else:
            version, theta, outer_state, param_index, total_seen_tokens = initialize_run(
                config,
                paths,
                store,
                logger,
                device=device,
            )
            heartbeat_fences = {}
        if lease_renewer is not None:
            lease_renewer.enable_heartbeats()
        if ha_mode:
            wandb_run = init_wandb_run(
                config=config,
                paths=paths,
                logger=logger,
                device=device,
                hostname=hostname,
            )
    except Exception:
        if lease_renewer is not None:
            try:
                lease_renewer.stop()
            except Exception:
                logger.exception("lease_renewer_stop_failed")
        store.close()
        if lease_store is not None and leader_token is not None:
            try:
                lease_store.release(leader_token)
            finally:
                lease_store.close()
        raise

    first_seen = UpdateFirstSeenRegistry(capacity=update_first_seen_capacity(config))
    last_progress_time = time.time()
    cycle_started_monotonic = time.monotonic()
    cycle_discovery_seconds = 0.0
    cycle_idle_seconds = 0.0
    cycle_grace_seconds = 0.0
    cycle_quorum_trigger = "not_reached"
    stop_reason = "completed"
    terminal_grace_complete = False
    reported_lease_busy_retries = 0
    try:
        while True:
            if lease_renewer is not None:
                lease_renewer.raise_if_failed()
                if lease_renewer.busy_retry_count > reported_lease_busy_retries:
                    logger.event(
                        "lease_renew_busy_retry",
                        count=lease_renewer.busy_retry_count,
                        delta=lease_renewer.busy_retry_count - reported_lease_busy_retries,
                    )
                    reported_lease_busy_retries = lease_renewer.busy_retry_count
            if (
                config.sync.stop_after_outer_steps is not None
                and version >= config.sync.stop_after_outer_steps
            ):
                stop_reason = "stop_after_outer_steps"
                break
            if (
                config.sync.stop_after_global_tokens is not None
                and total_seen_tokens >= config.sync.stop_after_global_tokens
            ):
                stop_reason = "stop_after_global_tokens"
                break

            discovery_start = time.monotonic()
            sync_liveness_and_metadata(
                store,
                paths,
                config,
                logger,
                first_seen=first_seen,
                heartbeat_fences=heartbeat_fences,
            )
            input_closed = all_expected_learners_stopped(store, config)
            cycle_discovery_seconds += time.monotonic() - discovery_start
            if not input_closed and terminal_grace_complete:
                terminal_grace_complete = False
                logger.event("terminal_input_reopened", version=version)
            if input_closed and not terminal_grace_complete:
                logger.event("terminal_input_closed", version=version)
                terminal_grace_start = time.monotonic()
                grace_seconds = configured_grace_seconds(config)
                if grace_seconds > 0:
                    time.sleep(grace_seconds)
                sync_liveness_and_metadata(
                    store,
                    paths,
                    config,
                    logger,
                    first_seen=first_seen,
                    heartbeat_fences=heartbeat_fences,
                )
                input_closed = all_expected_learners_stopped(store, config)
                cycle_grace_seconds += time.monotonic() - terminal_grace_start
                if input_closed:
                    cycle_quorum_trigger = "terminal_drain"
                    terminal_grace_complete = True
                else:
                    logger.event("terminal_input_reopened", version=version)

            terminal_drain = False
            if input_closed:
                discovery_start = time.monotonic()
                terminal_decision = select_terminal_drain_updates(
                    store,
                    paths,
                    config,
                    logger,
                    current_version=version,
                )
                cycle_discovery_seconds += time.monotonic() - discovery_start
                if terminal_decision.state == "open":
                    input_closed = False
                    terminal_grace_complete = False
                    logger.event("terminal_input_reopened", version=version)
                elif terminal_decision.state == "closed_empty":
                    stop_reason = "input_exhausted"
                    logger.event("input_exhausted", version=version)
                    break
                else:
                    selected = terminal_decision.selected
                    terminal_drain = len(selected) < config.sync.quorum_min

            if not input_closed:
                proposal_source = full_update_proposal_source(
                    current_version=version,
                    max_staleness_versions=config.sync.max_staleness_versions,
                )
                discovery_start = time.monotonic()
                eligible = proposal_source.eligible_updates(store)
                eligible = drop_missing_update_files(
                    store,
                    eligible,
                    logger,
                    source=proposal_source,
                    first_seen=first_seen,
                )
                one_per_learner = select_one_per_learner(
                    eligible,
                    policy=config.sync.selection_policy,
                    quorum_max=config.sync.quorum_max,
                )
                cycle_discovery_seconds += time.monotonic() - discovery_start
                if len(one_per_learner) < config.sync.quorum_min:
                    logger.event(
                        "quorum_wait",
                        eligible=len(one_per_learner),
                        quorum_min=config.sync.quorum_min,
                        version=version,
                    )
                    if no_progress_timed_out(
                        last_progress_time,
                        config.liveness.no_progress_timeout_seconds,
                    ):
                        stop_reason = "no_progress_timeout"
                        logger.event("no_progress_timeout", version=version)
                        break
                    idle_start = time.monotonic()
                    time.sleep(config.sync.scan_interval_seconds)
                    cycle_idle_seconds += time.monotonic() - idle_start
                    continue
                else:
                    grace_telemetry: dict[str, float | str] = {}
                    selected = collect_with_grace_window(
                        store,
                        paths,
                        config,
                        logger,
                        source=proposal_source,
                        first_seen=first_seen,
                        heartbeat_fences=heartbeat_fences,
                        telemetry_out=grace_telemetry,
                    )
                    cycle_grace_seconds += float(grace_telemetry["grace_seconds"])
                    cycle_quorum_trigger = str(grace_telemetry["quorum_trigger"])
                    if len(selected) < config.sync.quorum_min:
                        continue

            if not terminal_drain and len(selected) < config.sync.quorum_min:
                logger.event(
                    "quorum_wait",
                    eligible=len(selected),
                    quorum_min=config.sync.quorum_min,
                    version=version,
                )
                continue

            terminal_predecessor = maybe_capture_terminal_predecessor_for_eval(
                config,
                paths,
                input_closed=input_closed,
                terminal_drain=terminal_drain,
                version=version,
                selected=selected,
                store=store,
            )
            if terminal_predecessor is not None:
                logger.event(
                    "terminal_predecessor_captured",
                    version=version,
                    checkpoint_path=terminal_predecessor["checkpoint_path"],
                    checkpoint_sha256=terminal_predecessor["checkpoint_sha256"],
                    capture_method=terminal_predecessor["capture_method"],
                    selected_count=terminal_predecessor["selected_count"],
                    quorum_min=terminal_predecessor["quorum_min"],
                )

            run_selection_id = f"{config.run.run_id}_v{version + 1:06d}"
            store.mark_updates_selected([row["update_id"] for row in selected], run_selection_id)
            logger.event(
                "updates_selected",
                version=version,
                update_ids=[row["update_id"] for row in selected],
                learners=[row["learner_id"] for row in selected],
            )

            read_start = time.monotonic()
            missing_after_select = [
                row["update_id"] for row in selected if not Path(row["file_path"]).exists()
            ]
            if missing_after_select:
                store.drop_updates(missing_after_select, "missing_file")
                first_seen.discard_many(missing_after_select)
                store.reset_selected_to_pending(
                    [
                        row["update_id"]
                        for row in selected
                        if row["update_id"] not in set(missing_after_select)
                    ]
                )
                logger.event("selected_updates_missing_files", count=len(missing_after_select))
                continue
            try:
                vectors = [
                    load_update_vector(
                        row["file_path"],
                        device=device,
                        dtype=syncer_compute_dtype(config),
                    )
                    for row in selected
                ]
            except FileNotFoundError:
                missing = [
                    row["update_id"] for row in selected if not Path(row["file_path"]).exists()
                ]
                if missing:
                    store.drop_updates(missing, "missing_file")
                    first_seen.discard_many(missing)
                    store.reset_selected_to_pending(
                        [
                            row["update_id"]
                            for row in selected
                            if row["update_id"] not in set(missing)
                        ]
                    )
                    logger.event("selected_updates_missing_files", count=len(missing))
                    continue
                raise
            read_seconds = time.monotonic() - read_start

            weights_by_update = normalized_update_weights(
                selected,
                current_version=version,
                staleness_lambda=config.sync.staleness_lambda,
            )
            weights = [weights_by_update[row["update_id"]] for row in selected]
            staleness_evidence = merge_staleness_evidence(
                selected,
                weights_by_update,
                current_version=version,
                base_version_field="base_global_version",
            )
            aggregation_start = time.monotonic()
            p_bar = weighted_average_tensors(vectors, weights)
            grad = theta - p_bar
            aggregation_seconds = time.monotonic() - aggregation_start

            outer_start = time.monotonic()
            theta, outer_state = outer_optimizer_step(
                theta, grad, outer_state, config.outer_optimizer
            )
            roundtrip_metrics: dict[str, float] = {}
            theta, outer_state = align_state_to_publication_dtype(
                config,
                theta,
                outer_state,
                roundtrip_metrics_out=roundtrip_metrics,
            )
            outer_seconds = time.monotonic() - outer_start

            new_version = version + 1
            total_update_tokens = sum(int(row["tokens_this_update"]) for row in selected)
            total_seen_tokens += total_update_tokens
            publish_start = time.monotonic()
            publication = publish_global(
                config=config,
                paths=paths,
                store=store,
                version=new_version,
                theta=theta,
                outer_state=outer_state,
                param_index=param_index,
                num_updates=len(selected),
                total_update_tokens=total_update_tokens,
                total_seen_tokens=total_seen_tokens,
                selected_updates=selected,
                effective_weights=weights_by_update,
                predecessor_version=version,
                roundtrip_metrics=roundtrip_metrics,
                during_checkpoint_wait=checkpoint_wait_ingestion_callback(
                    config,
                    lambda: sync_liveness_and_metadata(
                        store,
                        paths,
                        config,
                        logger,
                        first_seen=first_seen,
                        heartbeat_fences=heartbeat_fences,
                    ),
                ),
                checkpoint_poll_seconds=min(
                    0.2,
                    max(0.001, float(config.sync.scan_interval_seconds)),
                ),
            )
            first_seen.discard_many([row["update_id"] for row in selected])
            publish_seconds = time.monotonic() - publish_start
            sqlite_commit_seconds = float(publication["sqlite_commit_seconds"])
            publication_metrics = {
                key: publication[key]
                for key in (
                    "publish_weight_seconds",
                    "publish_outer_seconds",
                    "publish_checkpoint_seconds",
                    "publish_dtype",
                    "publish_weight_bytes",
                    "publish_outer_bytes",
                    "publish_ingest_passes",
                    "publish_ingested_updates",
                    "publish_ingested_heartbeats",
                    "publish_ingest_seconds",
                    "publish_roundtrip_l2_error",
                    "publish_roundtrip_linf_error",
                    "publish_roundtrip_relative_l2_error",
                )
            }

            dropped = sum(
                1
                for row in store.terminal_update_rows()
                if row["update_kind"] == "full" and row["status"] == "dropped"
            )
            maintenance_start = time.monotonic()
            maintenance = run_maintenance(
                store,
                paths,
                heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
                scan_interval_seconds=config.sync.scan_interval_seconds,
            )
            maintenance_seconds = time.monotonic() - maintenance_start
            logger.event(
                "state_maintenance_completed",
                maintenance_seconds=maintenance_seconds,
                **maintenance,
            )
            interval_total_seconds = time.monotonic() - cycle_started_monotonic
            breakdown = interval_breakdown(
                total_seconds=interval_total_seconds,
                discovery_seconds=cycle_discovery_seconds,
                idle_seconds=cycle_idle_seconds,
                grace_seconds=cycle_grace_seconds,
                read_seconds=read_seconds,
                merge_seconds=aggregation_seconds + outer_seconds,
                publish_seconds=publish_seconds,
                maintenance_seconds=maintenance_seconds,
                quorum_trigger=cycle_quorum_trigger,
            )
            learner_resources = selected_resource_summary(selected)
            append_csv_row(
                paths.metrics / "syncer_metrics.csv",
                {
                    "timestamp": time.time(),
                    "version": new_version,
                    "selected_count": len(selected),
                    "total_update_tokens": total_update_tokens,
                    "read_seconds": read_seconds,
                    "aggregation_seconds": aggregation_seconds,
                    "outer_step_seconds": outer_seconds,
                    "publish_seconds": publish_seconds,
                    "sqlite_commit_seconds": sqlite_commit_seconds,
                    **publication_metrics,
                    "maintenance_seconds": maintenance_seconds,
                    "maintenance_scan_seconds": maintenance["maintenance_scan_seconds"],
                    "maintenance_scanned_rows": maintenance["maintenance_scanned_rows"],
                    "gc_pending_rows": maintenance["gc_pending_rows"],
                    "stale_updates_dropped": dropped,
                    **staleness_evidence,
                    "global_interval_seconds": interval_total_seconds,
                    **breakdown,
                    **_selected_resource_csv_fields(learner_resources),
                },
                SYNCER_METRIC_FIELDS,
            )
            if wandb_run is not None:
                wandb_run.log(
                    {
                        "syncer/version": new_version,
                        "syncer/selected_count": len(selected),
                        "syncer/total_update_tokens": total_update_tokens,
                        "syncer/total_seen_tokens": total_seen_tokens,
                        "syncer/read_seconds": read_seconds,
                        "syncer/aggregation_seconds": aggregation_seconds,
                        "syncer/outer_step_seconds": outer_seconds,
                        "syncer/publish_seconds": publish_seconds,
                        "syncer/sqlite_commit_seconds": sqlite_commit_seconds,
                        **{f"syncer/{key}": value for key, value in publication_metrics.items()},
                        "syncer/maintenance_seconds": maintenance_seconds,
                        "syncer/stale_updates_dropped": dropped,
                        "syncer/effective_staleness_mean": staleness_evidence[
                            "effective_staleness_mean"
                        ],
                        "syncer/fresh_effective_weight": staleness_evidence[
                            "fresh_effective_weight"
                        ],
                        "syncer/global_interval_seconds": interval_total_seconds,
                        **{f"syncer/{key}": value for key, value in breakdown.items()},
                        **selected_update_summary(selected, current_version=version),
                        **learner_resources,
                    },
                    step=new_version,
                )
            logger.event(
                "outer_step_applied",
                version=new_version,
                selected_count=len(selected),
                total_update_tokens=total_update_tokens,
                sqlite_commit_seconds=sqlite_commit_seconds,
                maintenance_seconds=maintenance_seconds,
                **staleness_evidence,
            )
            logger.event(
                "global_published",
                version=new_version,
                **publication_metrics,
            )
            logger.event("updates_marked_applied", version=new_version)
            if dropped:
                logger.event("updates_dropped", version=new_version, count=dropped)
            version = new_version
            last_progress_time = time.time()
            cycle_started_monotonic = time.monotonic()
            cycle_discovery_seconds = 0.0
            cycle_idle_seconds = 0.0
            cycle_grace_seconds = 0.0
            cycle_quorum_trigger = "not_reached"
    except Exception:
        stop_reason = "error"
        logger.exception("error", version=version)
        raise
    finally:
        try:
            terminal_row = None
            if isinstance(store, LeaderBoundSQLiteStore):
                previous_terminal = store.terminal_state()
                generation = (
                    1 if previous_terminal is None else int(previous_terminal["generation"]) + 1
                )
                terminal_row = store.finalize_terminal_state(
                    generation=generation,
                    stop_reason=stop_reason,
                    final_version=version,
                    total_seen_tokens=total_seen_tokens,
                )
                EpochControlPublisher(paths, store.fenced_store, store.token).publish_terminal(
                    terminal_row
                )
            else:
                publish_stop(
                    paths,
                    config=config,
                    reason=stop_reason,
                    version=version,
                    total_seen_tokens=total_seen_tokens,
                )
            logger.event("stop_published", reason=stop_reason, version=version)
            all_learners_stopped = wait_for_learner_shutdown(
                paths=paths,
                store=store,
                config=config,
                logger=logger,
                stop_reason=stop_reason,
                first_seen=first_seen,
                heartbeat_fences=heartbeat_fences,
            )
            ingest_update_metadata(
                store,
                paths,
                config,
                logger,
                first_seen=first_seen,
            )
            if stop_reason != "error" and all_learners_stopped:
                finalized = store.finalize_unconsumed_updates(
                    fragment_mode=False,
                    reason=stop_reason,
                )
                if finalized:
                    logger.event(
                        "unconsumed_updates_finalized",
                        count=finalized,
                        reason=stop_reason,
                    )
            summary = write_training_summary(
                paths=paths,
                store=store,
                config=config,
                logger=logger,
                wandb_run=wandb_run,
                stop_reason=stop_reason,
                final_version=version,
                total_seen_tokens=total_seen_tokens,
                run_started_at=run_started_at,
                run_start_monotonic=run_start_monotonic,
                all_learners_stopped=all_learners_stopped,
            )
            if stop_reason != "error":
                maintenance = run_maintenance(
                    store,
                    paths,
                    heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
                    scan_interval_seconds=config.sync.scan_interval_seconds,
                    input_closed=all_learners_stopped,
                )
                logger.event("state_maintenance_completed", **maintenance)
                if terminal_row is not None:
                    terminal_row = store.finalize_terminal_state(
                        generation=int(terminal_row["generation"]) + 1,
                        stop_reason=stop_reason,
                        final_version=version,
                        total_seen_tokens=total_seen_tokens,
                    )
                    EpochControlPublisher(paths, store.fenced_store, store.token).publish_terminal(
                        terminal_row, summary=summary
                    )
            elif terminal_row is not None:
                EpochControlPublisher(paths, store.fenced_store, store.token).publish_terminal(
                    terminal_row, summary=summary
                )
            if wandb_run is not None:
                wandb_run.summary["stop_reason"] = stop_reason
                wandb_run.summary["final_version"] = version
                wandb_run.summary["total_seen_tokens"] = total_seen_tokens
            logger.event(
                "process_exit",
                reason=stop_reason,
                version=version,
                **(
                    {
                        "lease_renew_count": 0,
                        "lease_renew_failure_count": 0,
                        "lease_renew_busy_retry_count": 0,
                        "lease_renew_seconds": [],
                        "lease_renew_wall_seconds": 0.0,
                        "lease_renew_cpu_seconds": 0.0,
                        "heartbeat_publish_count": 0,
                        "heartbeat_publish_wall_seconds": 0.0,
                        "heartbeat_publish_cpu_seconds": 0.0,
                    }
                    if lease_renewer is None
                    else lease_renewer.observation_metrics()
                ),
                **(
                    {}
                    if not isinstance(store, LeaderBoundSQLiteStore)
                    else store.business_transaction_metrics()
                ),
            )
        finally:
            try:
                if wandb_run is not None:
                    wandb_run.finish(exit_code=1 if stop_reason == "error" else 0)
            finally:
                if lease_renewer is not None:
                    try:
                        lease_renewer.stop()
                    except Exception:
                        logger.exception("lease_renewer_stop_failed")
                store.close()
                if lease_store is not None and leader_token is not None:
                    try:
                        lease_store.release(leader_token)
                    except Exception:
                        logger.exception("leader_release_failed")
                    finally:
                        lease_store.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = resolve_config(
        args.config,
        run_id=args.run_id,
        shared_root=args.shared_root,
        num_learners=args.num_learners,
        training_seed=args.training_seed,
        scan_interval_seconds=args.scan_interval_seconds,
        ingest_during_publish=args.ingest_during_publish,
        syncer_device=args.syncer_device,
        syncer_publish_dtype=args.syncer_publish_dtype,
        staleness_lambda=args.staleness_lambda,
        max_staleness_versions=args.max_staleness_versions,
        global_adoption_strategy=args.global_adoption_strategy,
        capture_terminal_predecessor_for_eval=(args.capture_terminal_predecessor_for_eval),
        completion_mode=args.completion_mode,
        parallel_checkpoint_writes=args.parallel_checkpoint_writes,
        materialize_full_every_events=args.materialize_full_every_events,
    )
    run_syncer(config)


if __name__ == "__main__":
    main()
