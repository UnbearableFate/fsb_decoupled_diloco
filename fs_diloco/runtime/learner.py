"""Filesystem-backed Decoupled DiLoCo learner."""

from __future__ import annotations

import argparse
import hashlib
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

from ..core.config import Config, config_to_dict, resolve_config
from ..core.constants import (
    FORMAT_VERSION,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_STOPPED,
    learner_index_from_id,
)
from ..modeling.hf_data import Batch, build_batch_iterator, build_stream_batch_iterator
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
from ..protocol.control_epoch import EpochControlReader
from ..protocol.dynamic_terminal import read_current_drain
from ..protocol.membership import (
    Admission,
    bootstrap_request_id,
    new_learner_instance_id,
    placement_id,
    read_bootstrap_ready,
    read_admission,
    read_registration_decision,
    write_registration_request,
)
from ..storage.atomic_io import atomic_write_json, file_size, safe_read_json, sha256_file
from ..core.run_descriptor import load_run_descriptor
from ..storage.paths import RunPaths, prepare_learner_instance_dir, prepare_run_dirs
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
from .launch_outbox import RecoveryClaimManager, recovery_observation_key
from .pbs_scheduler import PBSScheduler


PREDICTION_WORKING_VECTOR_COUNT = 14
RECONCILE_WORKING_VECTOR_COUNT = 5
REFERENCE_WORKING_VECTOR_COUNT = 3
CUDA_COMPUTE_MIN_RESERVE_BYTES = 1 << 30
CUDA_COMPUTE_RESERVE_FRACTION = 0.05


def _publish_test_admission_signal(instance_id: str) -> None:
    signal_path = os.environ.get("FS_DILOCO_TEST_ADMISSION_SIGNAL_PATH")
    if not signal_path:
        return
    atomic_write_json(
        Path(signal_path),
        {
            "instance_id": instance_id,
            "state": "admitted",
            "published_at": time.time(),
        },
    )


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
    parser.add_argument("--learner-id")
    parser.add_argument("--num-learners", type=int)
    authorization = parser.add_mutually_exclusive_group()
    authorization.add_argument("--bootstrap-slot", type=int)
    authorization.add_argument("--launch-request-id")
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
    learning_rate: float | None = None,
    scheduler_total_steps: int | None = None,
    status_reason: str | None = None,
    admission: Admission | None = None,
    close_generation: int | None = None,
    final_update_id: str | None = None,
    last_proposal_at: float | None = None,
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
        "learning_rate": learning_rate,
        "scheduler_total_steps": scheduler_total_steps,
    }
    if status_reason is not None:
        payload["status_reason"] = status_reason
    if admission is not None:
        payload.update(
            instance_id=admission.instance_id,
            placement_id=admission.placement_id,
            placement_epoch=admission.placement_epoch,
            stream_id=admission.stream_id,
            stream_epoch=admission.stream_epoch,
            admission_generation=admission.admission_generation,
            admission_token=admission.admission_token,
            launch_request_id=admission.launch_request_id,
            stream_restarted=admission.stream_restarted,
            pbs_job_id=os.environ.get("PBS_JOBID"),
        )
    if close_generation is not None:
        payload["close_generation"] = int(close_generation)
    if final_update_id is not None:
        payload["final_update_id"] = final_update_id
    if last_proposal_at is not None:
        payload["last_proposal_at"] = float(last_proposal_at)
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


_EPOCH_CONTROL_READERS: dict[Path, EpochControlReader] = {}


def _epoch_control_reader(
    paths: RunPaths,
    *,
    run_id: str | None = None,
    canonical_repair_wait_seconds: float | None = None,
    scan_cache_seconds: float | None = None,
) -> EpochControlReader | None:
    if not paths.bootstrap_complete_json.is_file():
        return None
    key = paths.shared_root.resolve()
    reader = _EPOCH_CONTROL_READERS.get(key)
    if reader is None:
        if run_id is None:
            descriptor = safe_read_json(paths.run_descriptor_json)
            if not isinstance(descriptor, dict) or not descriptor.get("run_id"):
                raise RuntimeError("HA epoch control requires a valid run descriptor")
            run_id = str(descriptor["run_id"])
        reader = EpochControlReader(paths, run_id=run_id)
        _EPOCH_CONTROL_READERS[key] = reader
    elif run_id is not None and reader.run_id != run_id:
        raise RuntimeError("cached HA epoch control reader run_id mismatch")
    if canonical_repair_wait_seconds is not None:
        reader.configure_canonical_repair_wait(canonical_repair_wait_seconds)
    if scan_cache_seconds is not None:
        reader.configure_scan_cache(scan_cache_seconds)
    return reader


def close_epoch_control_reader(paths: RunPaths) -> None:
    reader = _EPOCH_CONTROL_READERS.pop(paths.shared_root.resolve(), None)
    if reader is not None:
        reader.close()


def read_authoritative_latest(paths: RunPaths) -> dict[str, Any] | None:
    reader = _epoch_control_reader(paths)
    if reader is not None:
        return reader.read_current_latest()
    return safe_read_json(paths.latest_json)


def wait_for_authoritative_latest(
    paths: RunPaths,
    *,
    timeout_seconds: float = 1800.0,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = read_authoritative_latest(paths)
        if payload is not None:
            return payload
        time.sleep(poll_seconds)
    raise TimeoutError("timed out waiting for canonical latest control")


def _terminal_is_final(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    reason = payload.get("stop_reason", payload.get("reason"))
    return reason not in (None, "", "error")


def read_authoritative_terminal(
    paths: RunPaths,
    *,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    reader = _epoch_control_reader(paths, run_id=run_id)
    if reader is not None:
        payload = reader.read_current_terminal()
        # An HA syncer records an error generation for diagnosis, but error is
        # explicitly resumable.  Learners must keep waiting/reconciling rather
        # than treating that generation as the final stop authority.
        return payload if _terminal_is_final(payload) else None
    return safe_read_json(paths.stop_json)


def read_latest_if_newer(paths: RunPaths, last_loaded_global_version: int) -> dict[str, Any] | None:
    payload = read_authoritative_latest(paths)
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
        if remaining <= 0.0 or read_authoritative_terminal(paths) is not None:
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


def wait_for_final_fragment_progress(
    *,
    paths: RunPaths,
    target_global_merge_event: int,
    current_global_merge_event_fn: Callable[[], int],
    no_progress_timeout_seconds: float,
    heartbeat_interval_seconds: float,
    poll_seconds: float,
    handle_latest_fn: Callable[[dict[str, Any]], None],
    write_heartbeat_fn: Callable[[], None],
    logger: JsonlLogger,
    read_latest_fn: Callable[[RunPaths, int], dict[str, Any] | None] = (
        read_fragment_latest_if_newer
    ),
) -> str:
    """Wait for terminal fragment progress while keeping learner liveness current."""

    started = time.monotonic()
    deadline = started + float(no_progress_timeout_seconds)
    heartbeat_interval = max(0.001, float(heartbeat_interval_seconds))
    poll_interval = max(0.001, float(poll_seconds))
    next_heartbeat_at = started

    while current_global_merge_event_fn() < int(target_global_merge_event):
        if paths.stop_json.exists():
            return "stop_seen"
        now = time.monotonic()
        if now >= deadline:
            logger.event(
                "final_fragment_wait_timeout",
                current_global_merge_event=current_global_merge_event_fn(),
                target_global_merge_event=int(target_global_merge_event),
            )
            return "timeout"
        if now >= next_heartbeat_at:
            write_heartbeat_fn()
            next_heartbeat_at = time.monotonic() + heartbeat_interval

        maybe_latest = read_latest_fn(paths, current_global_merge_event_fn())
        if maybe_latest is not None:
            handle_latest_fn(maybe_latest)
            if time.monotonic() >= next_heartbeat_at:
                write_heartbeat_fn()
                next_heartbeat_at = time.monotonic() + heartbeat_interval
            continue

        now = time.monotonic()
        sleep_seconds = min(
            poll_interval,
            max(0.0, deadline - now),
            max(0.0, next_heartbeat_at - now),
        )
        if sleep_seconds > 0.0:
            time.sleep(sleep_seconds)
    return "target_reached"


def finalize_fragment_adoption_and_heartbeat(
    *,
    final_adoption_fn: Callable[[], None],
    write_stopped_heartbeat_fn: Callable[[], None],
    logger: JsonlLogger,
) -> None:
    """Keep stopped-heartbeat publication outside final-adoption failure scope."""
    try:
        final_adoption_fn()
    except Exception as exc:
        logger.event("final_fragment_adoption_failed", error=repr(exc))
        try:
            write_stopped_heartbeat_fn()
        except Exception as heartbeat_exc:
            raise heartbeat_exc from exc
        raise
    else:
        write_stopped_heartbeat_fn()


@dataclass(frozen=True)
class LatestLoadResult:
    value: Any
    latest: dict[str, Any]
    retry_count: int
    waited_seconds: float
    missing_path: str | None


def _wait_for_latest_payload_if_newer(
    paths: RunPaths,
    current_version: int,
    *,
    version_field: str,
    latest_kind: str | None,
    wait_seconds: float,
    poll_seconds: float,
) -> tuple[dict[str, Any] | None, float]:
    if version_field == "version" and latest_kind is None:
        return wait_for_latest_if_newer(
            paths,
            current_version,
            wait_seconds=wait_seconds,
            poll_seconds=poll_seconds,
        )
    wait_seconds = max(0.0, float(wait_seconds))
    poll_seconds = float(poll_seconds)
    if poll_seconds <= 0.0:
        raise ValueError("poll_seconds must be > 0")
    started = time.monotonic()
    deadline = started + wait_seconds
    while True:
        payload = safe_read_json(paths.latest_json)
        if (
            payload is not None
            and (latest_kind is None or payload.get("latest_kind") == latest_kind)
            and version_field in payload
            and int(payload[version_field]) > int(current_version)
        ):
            return payload, time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0.0 or paths.stop_json.exists():
            return None, time.monotonic() - started
        time.sleep(min(poll_seconds, remaining))


def load_or_refresh_latest(
    *,
    paths: RunPaths,
    latest: dict[str, Any],
    version_field: str,
    load_fn: Callable[[dict[str, Any]], Any],
    wait_seconds: float,
    poll_seconds: float,
    latest_kind: str | None = None,
) -> LatestLoadResult:
    """Load one latest snapshot, retrying the whole callback after GC races."""

    wait_seconds = max(0.0, float(wait_seconds))
    poll_seconds = float(poll_seconds)
    if poll_seconds <= 0.0:
        raise ValueError("poll_seconds must be > 0")
    current_latest = latest
    retry_count = 0
    waited_seconds = 0.0
    first_missing: FileNotFoundError | None = None
    missing_path: str | None = None
    retry_deadline: float | None = None
    while True:
        try:
            value = load_fn(current_latest)
            return LatestLoadResult(
                value=value,
                latest=current_latest,
                retry_count=retry_count,
                waited_seconds=waited_seconds,
                missing_path=missing_path,
            )
        except FileNotFoundError as exc:
            if first_missing is None:
                first_missing = exc
                missing_path = getattr(exc, "filename", None) or str(exc)
                retry_deadline = time.monotonic() + wait_seconds
            assert retry_deadline is not None
            remaining = max(0.0, retry_deadline - time.monotonic())
            current_version = int(current_latest[version_field])
            newer_latest, waited = _wait_for_latest_payload_if_newer(
                paths,
                current_version,
                version_field=version_field,
                latest_kind=latest_kind,
                wait_seconds=remaining,
                poll_seconds=poll_seconds,
            )
            waited_seconds += waited
            if newer_latest is None or int(newer_latest.get(version_field, -1)) <= current_version:
                first_missing.add_note(
                    "latest load retries exhausted: "
                    f"retry_count={retry_count}, waited_seconds={waited_seconds:.6f}, "
                    f"last_version={current_version}"
                )
                if exc is first_missing:
                    raise
                raise first_missing from exc
            current_latest = newer_latest
            retry_count += 1


def _latest_load_retry_kwargs(config: Config) -> dict[str, float]:
    return {
        "wait_seconds": float(config.learner.prediction.reconcile_timeout_seconds),
        "poll_seconds": float(config.learner.post_publish_latest_poll_seconds),
    }


def stop_requested(paths: RunPaths, local_step: int, config: Config) -> bool:
    if read_authoritative_terminal(paths) is not None:
        return True
    if config.training.completion_mode == "global_only":
        return False
    if (
        config.training.max_local_steps is not None
        and local_step >= config.training.max_local_steps
    ):
        return True
    return False


@dataclass
class SyncerProgressWatchdog:
    """Track strictly newer latest versions on a monotonic learner clock."""

    timeout_seconds: float
    last_observed_version: int
    last_signal_monotonic: float
    last_signal_wall: float
    highest_observed_epoch: int = 0
    highest_observed_owner_id: str | None = None
    last_heartbeat_seq: int = 0
    last_heartbeat_monotonic: float | None = None
    last_heartbeat_wall: float | None = None

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float,
        initial_version: int,
        now_monotonic: float | None = None,
        now_wall: float | None = None,
    ) -> SyncerProgressWatchdog:
        timeout_seconds = float(timeout_seconds)
        if timeout_seconds <= 0.0:
            raise ValueError("watchdog timeout_seconds must be > 0")
        monotonic_now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        wall_now = time.time() if now_wall is None else float(now_wall)
        return cls(
            timeout_seconds=timeout_seconds,
            last_observed_version=int(initial_version),
            last_signal_monotonic=monotonic_now,
            last_signal_wall=wall_now,
            last_heartbeat_monotonic=monotonic_now,
            last_heartbeat_wall=wall_now,
        )

    def observe(
        self,
        version: int,
        *,
        now_monotonic: float | None = None,
        now_wall: float | None = None,
    ) -> bool:
        version = int(version)
        if version <= self.last_observed_version:
            return False
        self.last_observed_version = version
        self.last_signal_monotonic = (
            time.monotonic() if now_monotonic is None else float(now_monotonic)
        )
        self.last_signal_wall = time.time() if now_wall is None else float(now_wall)
        return True

    def seconds_since_signal(self, *, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        return max(0.0, now - self.last_signal_monotonic)

    def observe_heartbeat(
        self,
        payload: dict[str, Any],
        *,
        now_monotonic: float | None = None,
        now_wall: float | None = None,
    ) -> bool:
        epoch = int(payload["epoch"])
        owner_id = str(payload["owner_id"])
        sequence = int(payload["heartbeat_seq"])
        if epoch < self.highest_observed_epoch:
            return False
        if epoch == self.highest_observed_epoch:
            if self.highest_observed_owner_id not in (None, owner_id):
                return False
            if sequence <= self.last_heartbeat_seq:
                return False
        self.highest_observed_epoch = epoch
        self.highest_observed_owner_id = owner_id
        self.last_heartbeat_seq = sequence
        self.last_signal_monotonic = (
            time.monotonic() if now_monotonic is None else float(now_monotonic)
        )
        self.last_signal_wall = time.time() if now_wall is None else float(now_wall)
        self.last_heartbeat_monotonic = self.last_signal_monotonic
        self.last_heartbeat_wall = self.last_signal_wall
        return True

    def deadline_reached(self, *, now_monotonic: float | None = None) -> bool:
        return self.seconds_since_signal(now_monotonic=now_monotonic) >= self.timeout_seconds


def syncer_watchdog_timeout_seconds(config: Config) -> float:
    configured = config.liveness.syncer_unresponsive_timeout_seconds
    return float(config.liveness.no_progress_timeout_seconds if configured is None else configured)


def confirm_syncer_unresponsive(
    watchdog: SyncerProgressWatchdog,
    paths: RunPaths,
    *,
    version_field: str,
    config: Config | None = None,
    now_monotonic: float | None = None,
    now_wall: float | None = None,
) -> bool:
    """Confirm a reached deadline against stop/latest before self-stopping."""

    now_monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
    reader = _epoch_control_reader(paths, run_id=None if config is None else config.run.run_id)
    if reader is not None:
        if config is None:
            raise ValueError("HA watchdog confirmation requires config")
        heartbeat_signal = (
            watchdog.last_heartbeat_monotonic
            if watchdog.last_heartbeat_monotonic is not None
            else watchdog.last_signal_monotonic
        )
        if max(0.0, now_monotonic - heartbeat_signal) < float(
            config.coordination.syncer_ha.heartbeat_stale_after_seconds
        ):
            return False
        heartbeat = reader.read_current_heartbeat(now_monotonic=now_monotonic)
        if heartbeat is not None:
            watchdog.observe_heartbeat(
                heartbeat,
                now_monotonic=now_monotonic,
                now_wall=now_wall,
            )
        if read_authoritative_terminal(paths) is not None:
            return False
        heartbeat_signal = (
            watchdog.last_heartbeat_monotonic
            if watchdog.last_heartbeat_monotonic is not None
            else watchdog.last_signal_monotonic
        )
        return max(0.0, now_monotonic - heartbeat_signal) >= float(
            config.coordination.syncer_ha.learner_recovery_wait_seconds
        )
    if not watchdog.deadline_reached(now_monotonic=now_monotonic):
        return False
    if paths.stop_json.exists():
        return False
    latest = safe_read_json(paths.latest_json)
    if latest is not None and version_field in latest:
        watchdog.observe(
            int(latest[version_field]),
            now_monotonic=now_monotonic,
            now_wall=now_wall,
        )
    return watchdog.deadline_reached(now_monotonic=now_monotonic)


def build_inner_optimizer_and_scheduler(
    model: torch.nn.Module,
    config: Config,
    *,
    completed_local_steps: int,
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
    completed_local_steps = int(completed_local_steps)
    if completed_local_steps < 0:
        raise ValueError("completed_local_steps must be >= 0")
    for group in optimizer.param_groups:
        group["initial_lr"] = config.inner_optimizer.lr
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: inner_lr_multiplier(config, step),
        # LambdaLR performs its initial step during construction. Starting from
        # N-1 therefore leaves it at the phase for N completed optimizer steps.
        last_epoch=completed_local_steps - 1,
    )
    return optimizer, scheduler


def inner_lr_multiplier(config: Config, completed_local_steps: int) -> float:
    """Return the LR multiplier for the next cumulative local optimizer step."""

    completed_local_steps = int(completed_local_steps)
    if completed_local_steps < 0:
        raise ValueError("completed_local_steps must be >= 0")
    if config.inner_optimizer.scheduler == "none":
        return 1.0
    if config.inner_optimizer.scheduler != "cosine":
        raise ValueError(
            f"unsupported inner optimizer scheduler: {config.inner_optimizer.scheduler}"
        )
    warmup_steps = int(config.inner_optimizer.warmup_steps)
    if warmup_steps > 0 and completed_local_steps < warmup_steps:
        return float(completed_local_steps + 1) / float(warmup_steps)
    total_steps = config.inner_optimizer.scheduler_total_steps
    if total_steps is None or int(total_steps) <= warmup_steps:
        raise ValueError(
            "cosine scheduler requires scheduler_total_steps greater than warmup_steps"
        )
    progress = min(
        1.0,
        max(
            0.0,
            float(completed_local_steps - warmup_steps) / float(int(total_steps) - warmup_steps),
        ),
    )
    cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
    return max(float(config.inner_optimizer.min_lr_ratio), cosine)


def current_inner_learning_rate(optimizer: torch.optim.Optimizer) -> float:
    """Return the first param-group LR used by the next optimizer step."""

    return float(optimizer.param_groups[0]["lr"])


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
        delta_norm = float(torch.linalg.vector_norm(local_delta, ord=2, dtype=torch.float32).item())
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
        int(tensor.numel()) != expected_numel for tensor in (local_flat, global_flat, outer_theta)
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

    load_result = load_or_refresh_latest(
        paths=paths,
        latest=cached_latest,
        version_field="version",
        load_fn=lambda candidate: predict_next_global_weight(
            model=model,
            latest=candidate,
            param_index=param_index,
            device=device,
            config=config,
            local_tokens=local_tokens,
        ),
        **_latest_load_retry_kwargs(config),
    )
    loaded_latest = load_result.latest
    if int(loaded_latest["version"]) != base_version:
        return (
            None,
            None,
            loaded_latest,
            {
                "reason": "cached_checkpoint_collected",
                "cached_version": base_version,
                "missing_path": load_result.missing_path,
                "waited_seconds": load_result.waited_seconds,
            },
        )

    reference_flat, stats = load_result.value

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
    paths: RunPaths | None = None,
    config: Config | None = None,
) -> tuple[int, dict[int, int]]:
    def load_snapshot(candidate: dict[str, Any]) -> tuple[torch.Tensor, dict[int, int], int]:
        fragments = candidate.get("fragments") or {}
        fragment_tensors = {
            int(fragment_id): load_fragment_weight(info["weight_path"])
            for fragment_id, info in fragments.items()
        }
        flat = materialize_full_from_fragments(
            fragment_tensors,
            fragment_index,
            int(param_index["total_numel"]),
        )
        versions = {
            int(fragment_id): int(info["version"]) for fragment_id, info in fragments.items()
        }
        event = int(candidate.get("global_merge_event", candidate.get("version", 0)))
        return flat, versions, event

    if paths is not None and config is not None:
        result = load_or_refresh_latest(
            paths=paths,
            latest=latest,
            version_field="global_merge_event",
            latest_kind="fragment",
            load_fn=load_snapshot,
            **_latest_load_retry_kwargs(config),
        )
        flat, versions, event = result.value
    else:
        flat, versions, event = load_snapshot(latest)
    load_flat_into_model(model, flat, param_index)
    model.to(device)
    return event, versions


def adopt_fragment_updates(
    *,
    model: torch.nn.Module,
    latest: dict[str, Any],
    param_index: dict[str, Any],
    fragment_index: dict[str, Any],
    last_loaded_fragment_versions: dict[int, int],
    device: torch.device,
    paths: RunPaths | None = None,
    config: Config | None = None,
) -> tuple[int, dict[int, int], list[int]]:
    initial_versions = dict(last_loaded_fragment_versions)

    def load_snapshot(
        candidate: dict[str, Any],
    ) -> tuple[torch.Tensor, dict[int, int], list[int], int]:
        fragments = candidate.get("fragments") or {}
        changed: list[int] = []
        versions = dict(initial_versions)
        flat = flatten_trainable_params(model, param_index, dtype=torch.float32)
        for fragment_id_text, info in sorted(fragments.items(), key=lambda item: int(item[0])):
            fragment_id = int(fragment_id_text)
            version = int(info["version"])
            if version <= int(initial_versions.get(fragment_id, -1)):
                continue
            fragment_tensor = load_fragment_weight(info["weight_path"])
            flat = scatter_fragment(flat, fragment_index, fragment_id, fragment_tensor)
            versions[fragment_id] = version
            changed.append(fragment_id)
        event = int(candidate.get("global_merge_event", candidate.get("version", 0)))
        return flat, versions, changed, event

    if paths is not None and config is not None:
        result = load_or_refresh_latest(
            paths=paths,
            latest=latest,
            version_field="global_merge_event",
            latest_kind="fragment",
            load_fn=load_snapshot,
            **_latest_load_retry_kwargs(config),
        )
        flat, versions, changed, event = result.value
    else:
        flat, versions, changed, event = load_snapshot(latest)
    if changed:
        load_flat_into_model(model, flat, param_index)
        model.to(device)
    last_loaded_fragment_versions.clear()
    last_loaded_fragment_versions.update(versions)
    return (
        event,
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
    completed_local_steps: int,
    paths: RunPaths | None = None,
    adopt_fn: Callable[..., tuple[int, dict[int, int], list[int]]] = adopt_fragment_updates,
    rebuild_inner_state_fn: Callable[..., tuple[Any, Any]] = (build_inner_optimizer_and_scheduler),
) -> FragmentAdoptionResult:
    """Apply one fragment latest snapshot with call-site-specific legacy semantics."""

    adoption_start = time.monotonic()
    global_merge_event, fragment_versions, changed = adopt_fn(
        model=model,
        latest=latest,
        param_index=param_index,
        fragment_index=fragment_index,
        last_loaded_fragment_versions=last_loaded_fragment_versions,
        device=device,
        paths=paths,
        config=config,
    )
    adoption_load_apply_seconds = time.monotonic() - adoption_start
    next_tokens = dict(tokens_since_fragment_load)
    next_count = fragment_adopt_count
    next_last_adopted = last_adopted_fragments
    next_optimizer = optimizer
    next_scheduler = scheduler
    adoption_optimizer_reset_seconds = 0.0
    if changed:
        next_count += len(changed)
        next_last_adopted = changed
        if reset_tokens:
            for fragment_id in changed:
                next_tokens[fragment_id] = 0
        if reset_optimizer and config.fragments.reset_inner_optimizer_on_fragment_adopt:
            reset_start = time.monotonic()
            next_optimizer, next_scheduler = rebuild_inner_state_fn(
                model,
                config,
                completed_local_steps=completed_local_steps,
            )
            adoption_optimizer_reset_seconds = time.monotonic() - reset_start
            logger.event(
                "inner_optimizer_reset",
                version=global_merge_event,
                fragments=changed,
                **inner_training_state_metrics(next_optimizer, next_scheduler),
            )
        event_fields: dict[str, Any] = {
            "global_merge_event": global_merge_event,
            "fragments": changed,
            "adoption_load_apply_seconds": adoption_load_apply_seconds,
            "adoption_optimizer_reset_seconds": adoption_optimizer_reset_seconds,
            "adoption_pause_seconds": (
                adoption_load_apply_seconds + adoption_optimizer_reset_seconds
            ),
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
    mid_cycle_adoption_count: int,
    base_switched_at_step: int | None,
    admission: Admission | None = None,
) -> tuple[str, Path, Path, dict[str, Any]]:
    mid_cycle_adoption_count = int(mid_cycle_adoption_count)
    if mid_cycle_adoption_count < 0:
        raise ValueError("mid_cycle_adoption_count must be >= 0")
    if mid_cycle_adoption_count == 0 and base_switched_at_step is not None:
        raise ValueError("base_switched_at_step requires a mid-cycle adoption")
    if mid_cycle_adoption_count > 0:
        if base_switched_at_step is None:
            raise ValueError("mid-cycle adoption requires base_switched_at_step")
        base_switched_at_step = int(base_switched_at_step)
        if not 1 <= base_switched_at_step <= int(inner_steps):
            raise ValueError("base_switched_at_step must be within the upload interval")
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
        "mid_cycle_adoption_count": mid_cycle_adoption_count,
        "base_switched_at_step": base_switched_at_step,
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
    if admission is not None:
        metadata.update(
            learner_instance_id=admission.instance_id,
            placement_id=admission.placement_id,
            placement_epoch=admission.placement_epoch,
            stream_id=admission.stream_id,
            stream_epoch=admission.stream_epoch,
            admission_generation=admission.admission_generation,
            admission_token_hash=hashlib.sha256(
                admission.admission_token.encode("utf-8")
            ).hexdigest(),
        )
    metadata.update(resource_metrics)
    atomic_write_json(meta_path, metadata)
    return update_id, tensor_path, meta_path, metadata


@dataclass
class MidCycleAdoptionTracker:
    """Describe successful replace adoptions within one upload interval."""

    adoption_count: int = 0
    base_switched_at_step: int | None = None

    def reset(self) -> None:
        self.adoption_count = 0
        self.base_switched_at_step = None

    def record(self, *, completed_interval_steps: int) -> None:
        completed_interval_steps = int(completed_interval_steps)
        if completed_interval_steps < 1:
            raise ValueError("completed_interval_steps must be >= 1")
        self.adoption_count += 1
        self.base_switched_at_step = completed_interval_steps

    def metadata(self) -> dict[str, int | None]:
        return {
            "mid_cycle_adoption_count": self.adoption_count,
            "base_switched_at_step": self.base_switched_at_step,
        }


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
    meta_path = paths.fragment_update_pointer_path(learner_id, fragment_id)
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
        paths=paths,
        config=config,
    )
    syncer_watchdog = SyncerProgressWatchdog.start(
        timeout_seconds=syncer_watchdog_timeout_seconds(config),
        initial_version=last_loaded_global_merge_event,
    )
    optimizer, scheduler = build_inner_optimizer_and_scheduler(
        model,
        config,
        completed_local_steps=0,
    )
    tokens_since_fragment_load = {fragment_id: 0 for fragment_id in last_loaded_fragment_versions}
    local_update_index = 0
    fragment_adopt_count = 0
    last_adopted_fragments: list[int] = []
    logger.event(
        "loaded_fragment_latest",
        global_merge_event=last_loaded_global_merge_event,
        fragment_versions=last_loaded_fragment_versions,
    )
    logger.event(
        "inner_optimizer_reset",
        version=last_loaded_global_merge_event,
        **inner_training_state_metrics(optimizer, scheduler),
    )
    logger.event(
        "syncer_watchdog_started",
        timeout_seconds=syncer_watchdog.timeout_seconds,
        global_merge_event=last_loaded_global_merge_event,
    )
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
        learning_rate=current_inner_learning_rate(optimizer),
        scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
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
    watchdog_stop_reason: str | None = None

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
            completed_local_steps=local_step,
            paths=paths,
        )
        syncer_watchdog.observe(result.global_merge_event)
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
            last_step_learning_rate: float | None = None

            for _ in range(config.training.inner_steps):
                if stop_requested(paths, local_step, config):
                    break
                last_step_learning_rate = current_inner_learning_rate(optimizer)
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
                        learning_rate=last_step_learning_rate,
                        scheduler_completed_steps=local_step - 1,
                        scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
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
                        learning_rate=current_inner_learning_rate(optimizer),
                        scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
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
                if confirm_syncer_unresponsive(
                    syncer_watchdog,
                    paths,
                    version_field="global_merge_event",
                ):
                    watchdog_stop_reason = "syncer_unresponsive"
                    logger.event(
                        "syncer_unresponsive",
                        timeout_seconds=syncer_watchdog.timeout_seconds,
                        seconds_since_signal=syncer_watchdog.seconds_since_signal(),
                        last_signal_at=syncer_watchdog.last_signal_wall,
                        last_observed_global_merge_event=(syncer_watchdog.last_observed_version),
                        local_step=local_step,
                    )
                    break

            if watchdog_stop_reason is not None:
                break

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
                local_cycle_elapsed_seconds=elapsed,
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
                    "local_cycle_elapsed_seconds": elapsed,
                    "param_norm": param_norm,
                    "fragment_norm": fragment_norm,
                    "last_loaded_fragment_versions_json": json.dumps(
                        {str(k): v for k, v in sorted(last_loaded_fragment_versions.items())},
                        sort_keys=True,
                    ),
                    "fragment_adopt_count": fragment_adopt_count,
                    "learning_rate": last_step_learning_rate,
                    "scheduler_total_steps": config.inner_optimizer.scheduler_total_steps,
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
                learning_rate=current_inner_learning_rate(optimizer),
                scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
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

        def attempt_final_fragment_adoption() -> None:
            target_event = config.sync.stop_after_outer_steps
            if not had_error and watchdog_stop_reason is None and target_event is not None:

                def write_final_wait_heartbeat() -> None:
                    write_heartbeat(
                        paths=paths,
                        config=config,
                        learner_id=learner_id,
                        status=LEARNER_STATUS_ACTIVE,
                        phase="final_fragment_wait",
                        last_loaded_global_version=last_loaded_global_merge_event,
                        last_loaded_global_merge_event=last_loaded_global_merge_event,
                        last_loaded_fragment_versions=last_loaded_fragment_versions,
                        last_adopted_fragments=last_adopted_fragments,
                        last_local_step=local_step,
                        last_update_id=last_update_id,
                        resource_metrics=training_resources,
                        learning_rate=current_inner_learning_rate(optimizer),
                        scheduler_total_steps=(config.inner_optimizer.scheduler_total_steps),
                    )
                    logger.event(
                        "heartbeat_written",
                        local_step=local_step,
                        phase="final_fragment_wait",
                        global_merge_event=last_loaded_global_merge_event,
                    )

                wait_for_final_fragment_progress(
                    paths=paths,
                    target_global_merge_event=int(target_event),
                    current_global_merge_event_fn=(lambda: last_loaded_global_merge_event),
                    no_progress_timeout_seconds=(config.liveness.no_progress_timeout_seconds),
                    heartbeat_interval_seconds=(config.liveness.heartbeat_interval_seconds),
                    poll_seconds=config.sync.stop_file_poll_seconds,
                    handle_latest_fn=lambda maybe_latest: handle_fragment_latest(
                        maybe_latest,
                        event_type="final_wait_fragments_adopted",
                        reset_tokens=True,
                        reset_optimizer=False,
                        include_fragment_versions=False,
                    ),
                    write_heartbeat_fn=write_final_wait_heartbeat,
                    logger=logger,
                )
            maybe_latest = (
                safe_read_json(paths.latest_json) if watchdog_stop_reason is None else None
            )
            if maybe_latest and maybe_latest.get("latest_kind") == "fragment":
                handle_fragment_latest(
                    maybe_latest,
                    event_type="final_fragments_adopted",
                    reset_tokens=False,
                    reset_optimizer=False,
                    include_fragment_versions=False,
                )

        def write_final_stopped_heartbeat() -> None:
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
                learning_rate=current_inner_learning_rate(optimizer),
                scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
                status_reason=watchdog_stop_reason,
            )

        finalize_fragment_adoption_and_heartbeat(
            final_adoption_fn=attempt_final_fragment_adoption,
            write_stopped_heartbeat_fn=write_final_stopped_heartbeat,
            logger=logger,
        )
        logger.event(
            "process_exit",
            local_step=local_step,
            global_version=last_loaded_global_merge_event,
            fragment_versions=last_loaded_fragment_versions,
            fragment_adopt_count=fragment_adopt_count,
            status_reason=watchdog_stop_reason,
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
        load_or_refresh_latest_fn=load_or_refresh_latest,
        terminal_published_fn=lambda candidate_paths: (
            read_authoritative_terminal(candidate_paths) is not None
        ),
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
    completed_local_steps: int,
) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler | None]:
    outcome = action.adoption
    if outcome is not None:
        optimizer_reset_seconds = 0.0
        if not outcome.preserve_inner_state:
            reset_start = time.monotonic()
            optimizer, scheduler = build_inner_optimizer_and_scheduler(
                model,
                config,
                completed_local_steps=completed_local_steps,
            )
            optimizer_reset_seconds = time.monotonic() - reset_start
        logger.event(
            "global_adopted",
            version=outcome.version,
            adoption_load_apply_seconds=outcome.load_apply_seconds,
            adoption_optimizer_reset_seconds=optimizer_reset_seconds,
            adoption_pause_seconds=outcome.load_apply_seconds + optimizer_reset_seconds,
        )
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
        logger.event(
            "inner_optimizer_reset",
            version=outcome.version,
            **inner_training_state_metrics(optimizer, scheduler),
        )
        return optimizer, scheduler
    if action.reset_optimizer_reason is not None:
        optimizer, scheduler = build_inner_optimizer_and_scheduler(
            model,
            config,
            completed_local_steps=completed_local_steps,
        )
        logger.event(
            "inner_optimizer_reset",
            version=current_version,
            reason=action.reset_optimizer_reason,
            **inner_training_state_metrics(optimizer, scheduler),
        )
    return optimizer, scheduler


def run_learner(
    config: Config,
    learner_id: str | None,
    *,
    bootstrap_slot: int | None = None,
    launch_request_id: str | None = None,
) -> None:
    dynamic = config.membership.mode == "dynamic"
    if dynamic:
        if learner_id is not None:
            raise ValueError("dynamic learner generates its own instance ID")
        learner_id = new_learner_instance_id()
    elif learner_id is None:
        raise ValueError("static learner requires --learner-id")
    assert learner_id is not None
    if config.fragments.enabled:
        run_fragment_learner(config, learner_id)
        return
    paths = RunPaths(Path(config.run.shared_root or "."))
    loaded_descriptor = None
    admission: Admission | None = None
    if config.coordination.syncer_ha.enabled:
        loaded = load_run_descriptor(
            paths.shared_root,
            expected_run_id=config.run.run_id,
            expected_git_commit=config.run.git_commit,
            expected_git_dirty=config.run.git_dirty,
            expected_source_fingerprint=config.run.source_fingerprint,
            expected_descriptor_sha256=os.environ.get("FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256"),
        )
        if config_to_dict(loaded.config) != config_to_dict(config):
            raise RuntimeError("learner config differs from the immutable resolved run descriptor")
        prepare_learner_instance_dir(paths, learner_id)
        loaded_descriptor = loaded
    else:
        prepare_run_dirs(paths, config.sync.num_learners)
    logger = JsonlLogger(paths.logs / f"{learner_id}.jsonl", learner_id)
    log_uncaught_exception(logger)
    if dynamic:
        assert loaded_descriptor is not None
        if (bootstrap_slot is None) == (launch_request_id is None):
            raise ValueError(
                "dynamic learner requires exactly one of --bootstrap-slot or --launch-request-id"
            )
        if bootstrap_slot is not None:
            if not 0 <= bootstrap_slot < int(loaded_descriptor.descriptor["bootstrap_slots"]):
                raise ValueError("bootstrap slot is outside the descriptor budget")
            expected_request_id = bootstrap_request_id(
                run_id=config.run.run_id or "",
                bootstrap_slot=bootstrap_slot,
                config_fingerprint=str(loaded_descriptor.descriptor["descriptor_sha256"]),
            )
            ready_deadline = time.monotonic() + float(
                config.membership.initial_membership_deadline_seconds
            )
            ready_request = None
            while time.monotonic() < ready_deadline:
                ready_request = read_bootstrap_ready(
                    paths,
                    run_id=config.run.run_id or "",
                    bootstrap_slot=bootstrap_slot,
                )
                if ready_request is not None:
                    break
                time.sleep(config.membership.registration_scan_interval_seconds)
            if ready_request is None:
                raise TimeoutError(f"bootstrap slot {bootstrap_slot} was not published")
            launch_request_id = str(ready_request["request_id"])
            if launch_request_id != expected_request_id:
                raise RuntimeError("bootstrap request ID differs from the immutable descriptor")
        assert launch_request_id is not None
        request = write_registration_request(
            paths,
            run_id=config.run.run_id or "",
            instance_id=learner_id,
            placement=placement_id(),
            launch_request_id=launch_request_id,
            source_fingerprint=str(config.run.source_fingerprint),
            config_sha256=str(loaded_descriptor.descriptor["resolved_config_sha256"]),
            ttl_seconds=config.membership.registration_request_ttl_seconds,
            pbs_job_id=os.environ.get("PBS_JOBID"),
            gpu_identity=os.environ.get("CUDA_VISIBLE_DEVICES") or "cpu",
        )
        logger.event(
            "registration_requested",
            instance_id=learner_id,
            launch_request_id=launch_request_id,
            placement_id=request["placement_id"],
            bootstrap_slot=bootstrap_slot,
        )
        admission_deadline = time.monotonic() + float(
            config.membership.initial_membership_deadline_seconds
            if bootstrap_slot is not None
            else config.membership.registration_request_ttl_seconds
        )
        while time.monotonic() < admission_deadline:
            decision = read_registration_decision(
                paths,
                run_id=config.run.run_id or "",
                instance_id=learner_id,
            )
            if decision is not None and decision.get("state") != "admitted":
                raise RuntimeError(
                    "dynamic learner registration rejected: "
                    f"{decision.get('rejection_reason') or decision.get('state')}"
                )
            admission = read_admission(
                paths,
                run_id=config.run.run_id or "",
                instance_id=learner_id,
            )
            if admission is not None:
                break
            time.sleep(config.membership.registration_scan_interval_seconds)
        if admission is None:
            raise TimeoutError(f"dynamic learner admission timed out: {learner_id}")
        logger.event(
            "registration_admitted",
            instance_id=learner_id,
            placement_id=admission.placement_id,
            placement_epoch=admission.placement_epoch,
            stream_id=admission.stream_id,
            stream_epoch=admission.stream_epoch,
            stream_restarted=admission.stream_restarted,
        )
        _publish_test_admission_signal(learner_id)
    control_reader = _epoch_control_reader(
        paths,
        run_id=config.run.run_id,
        canonical_repair_wait_seconds=(
            config.coordination.syncer_ha.canonical_repair_wait_seconds
            if config.coordination.syncer_ha.enabled
            else None
        ),
        scan_cache_seconds=(
            min(
                config.sync.stop_file_poll_seconds,
                config.coordination.syncer_ha.heartbeat_interval_seconds,
            )
            if config.coordination.syncer_ha.enabled
            else None
        ),
    )
    learner_index = (
        admission.stream_id if admission is not None else learner_index_from_id(learner_id)
    )
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
    try:
        initial_latest = wait_for_authoritative_latest(
            paths,
            timeout_seconds=(
                config.coordination.syncer_ha.learner_recovery_wait_seconds
                if config.coordination.syncer_ha.enabled
                else 1800.0
            ),
        )
    except Exception:
        if control_reader is not None:
            logger.event(
                "canonical_latest_wait_failed",
                **control_reader.observation_metrics(),
            )
        close_epoch_control_reader(paths)
        raise
    initial_load = load_or_refresh_latest(
        paths=paths,
        latest=initial_latest,
        version_field="version",
        load_fn=lambda candidate: adopt_global(
            model=model,
            latest=candidate,
            param_index=param_index,
            device=device,
        ),
        **_latest_load_retry_kwargs(config),
    )
    last_loaded_global_version = int(initial_load.value)
    latest = initial_load.latest
    syncer_watchdog = SyncerProgressWatchdog.start(
        timeout_seconds=syncer_watchdog_timeout_seconds(config),
        initial_version=last_loaded_global_version,
    )
    recovery_manager = None
    next_recovery_reconciliation = 0.0
    if config.coordination.recovery_submission.enabled:
        assert loaded_descriptor is not None
        recovery_manager = RecoveryClaimManager(
            paths=paths,
            config=config.coordination.recovery_submission,
            scheduler=PBSScheduler(),
            descriptor_sha256=str(loaded_descriptor.descriptor["descriptor_sha256"]),
        )
    last_loaded_latest = latest
    optimizer, scheduler = build_inner_optimizer_and_scheduler(
        model,
        config,
        completed_local_steps=0,
    )
    adoption_strategy = make_global_adoption_strategy(config)
    logger.event("loaded_global", version=last_loaded_global_version)
    logger.event(
        "inner_optimizer_reset",
        version=last_loaded_global_version,
        **inner_training_state_metrics(optimizer, scheduler),
    )
    logger.event(
        "syncer_watchdog_started",
        timeout_seconds=syncer_watchdog.timeout_seconds,
        global_version=last_loaded_global_version,
    )
    write_heartbeat(
        paths=paths,
        config=config,
        learner_id=learner_id,
        status=LEARNER_STATUS_ACTIVE,
        phase="loaded_global",
        last_loaded_global_version=last_loaded_global_version,
        last_local_step=0,
        last_update_id=None,
        learning_rate=current_inner_learning_rate(optimizer),
        scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
        admission=admission,
    )

    batch_iter = (
        build_stream_batch_iterator(
            config,
            tokenizer,
            stream_id=admission.stream_id,
            stream_pool_size=config.membership.stream_pool_size,
        )
        if admission is not None
        else build_batch_iterator(
            config,
            tokenizer,
            learner_index=learner_index,
            num_learners=config.sync.num_learners,
        )
    )
    resource_monitor = create_resource_monitor(device)
    resource_monitor.start()
    local_step = 0
    tokens_since_global_load = 0
    last_heartbeat = time.monotonic()
    last_update_id: str | None = None
    watchdog_stop_reason: str | None = None
    mid_cycle_adoptions = MidCycleAdoptionTracker()
    reported_cache_rejections = 0
    reported_canonical_repair_waits = 0
    reported_stale_scan_rejections = 0
    drain_payload: dict[str, Any] | None = None

    def log_control_observation_changes() -> None:
        nonlocal reported_cache_rejections
        nonlocal reported_canonical_repair_waits
        nonlocal reported_stale_scan_rejections
        if control_reader is None:
            return
        metrics = control_reader.observation_metrics()
        cache_rejections = int(metrics["cache_rejected_lower_epoch_count"])
        repair_waits = int(metrics["canonical_repair_wait_count"])
        stale_scan_rejections = int(metrics["stale_epoch_scan_rejected_count"])
        if cache_rejections > reported_cache_rejections:
            logger.event(
                "cache_rejected_lower_epoch",
                count=cache_rejections,
                delta=cache_rejections - reported_cache_rejections,
                **metrics,
            )
            reported_cache_rejections = cache_rejections
        if repair_waits > reported_canonical_repair_waits:
            logger.event(
                "canonical_repair_wait",
                count=repair_waits,
                delta=repair_waits - reported_canonical_repair_waits,
                **metrics,
            )
            reported_canonical_repair_waits = repair_waits
        if stale_scan_rejections > reported_stale_scan_rejections:
            logger.event(
                "stale_epoch_scan_rejected",
                count=stale_scan_rejections,
                delta=stale_scan_rejections - reported_stale_scan_rejections,
                **metrics,
            )
            reported_stale_scan_rejections = stale_scan_rejections

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
            completed_local_steps=local_step,
        )
        if action.adoption is not None:
            syncer_watchdog.observe(action.adoption.version)
            last_loaded_global_version = action.adoption.version
            last_loaded_latest = action.adoption.latest
            tokens_since_global_load = action.adoption.tokens_since_global_load
        elif action.tokens_since_global_load is not None:
            tokens_since_global_load = action.tokens_since_global_load

    try:
        while not stop_requested(paths, local_step, config):
            if dynamic:
                drain_payload = read_current_drain(
                    paths,
                    run_id=config.run.run_id or "",
                )
                if drain_payload is not None:
                    logger.event(
                        "drain_seen",
                        close_generation=int(drain_payload["close_generation"]),
                        max_terminal_version=int(drain_payload["max_terminal_version"]),
                    )
                    break
            mid_cycle_adoptions.reset()
            resource_monitor.begin_cycle()
            interval_start_time = time.monotonic()
            interval_start_step = local_step
            base_global_version = last_loaded_global_version
            losses: list[float] = []
            interval_tokens = 0
            interval_examples = 0
            grad_norm: float | None = None
            last_step_learning_rate: float | None = None

            for _ in range(config.training.inner_steps):
                if stop_requested(paths, local_step, config):
                    break
                last_step_learning_rate = current_inner_learning_rate(optimizer)
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
                        learning_rate=last_step_learning_rate,
                        scheduler_completed_steps=local_step - 1,
                        scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
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
                        learning_rate=current_inner_learning_rate(optimizer),
                        scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
                        admission=admission,
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
                        if (
                            action.adoption is not None
                            and config.learner.global_adoption_strategy == "replace"
                        ):
                            interval_step = local_step - interval_start_step
                            mid_cycle_adoptions.record(
                                completed_interval_steps=interval_step,
                            )
                            logger.event(
                                "mid_cycle_global_adopted",
                                version=action.adoption.version,
                                local_step=local_step,
                                interval_step=interval_step,
                                mid_cycle_adoption_count=(mid_cycle_adoptions.adoption_count),
                            )
                        base_global_version = last_loaded_global_version
                if (
                    recovery_manager is not None
                    and time.monotonic() >= next_recovery_reconciliation
                ):
                    control_reader = _epoch_control_reader(paths, run_id=config.run.run_id)
                    assert control_reader is not None
                    recovery_heartbeat = control_reader.read_current_heartbeat()
                    if recovery_heartbeat is not None:
                        syncer_watchdog.observe_heartbeat(recovery_heartbeat)
                    heartbeat_signal = (
                        syncer_watchdog.last_heartbeat_monotonic
                        if syncer_watchdog.last_heartbeat_monotonic is not None
                        else syncer_watchdog.last_signal_monotonic
                    )
                    heartbeat_stale = (
                        time.monotonic() - heartbeat_signal
                        >= config.coordination.syncer_ha.heartbeat_stale_after_seconds
                    )
                    if heartbeat_stale:
                        leader = control_reader.current_leader() or {}
                        highest_epoch = int((recovery_heartbeat or leader).get("epoch", 0))
                        heartbeat_seq = int((recovery_heartbeat or leader).get("heartbeat_seq", 0))
                        fingerprint = str(
                            (recovery_heartbeat or {}).get("payload_sha256", "missing-heartbeat")
                        )
                        observation_key = recovery_observation_key(
                            run_id=config.run.run_id or "",
                            highest_epoch=highest_epoch,
                            heartbeat_seq=heartbeat_seq,
                            heartbeat_fingerprint=fingerprint,
                        )
                        claim_result = recovery_manager.maybe_submit(
                            observation_key=observation_key,
                            claimant_id=(f"{learner_id}:{socket.gethostname()}:{os.getpid()}"),
                            terminal_published=(read_authoritative_terminal(paths) is not None),
                        )
                        logger.event(
                            "syncer_recovery_reconciled",
                            state=claim_result.state,
                            observation_key=observation_key,
                            attempt=claim_result.attempt,
                            job_id=claim_result.job_id,
                        )
                    next_recovery_reconciliation = time.monotonic() + float(
                        config.coordination.recovery_submission.reconciliation_interval_seconds
                    )
                syncer_unresponsive = confirm_syncer_unresponsive(
                    syncer_watchdog,
                    paths,
                    version_field="version",
                    config=config,
                )
                log_control_observation_changes()
                if syncer_unresponsive:
                    watchdog_stop_reason = (
                        "syncer_recovery_exhausted"
                        if config.coordination.syncer_ha.enabled
                        else "syncer_unresponsive"
                    )
                    logger.event(
                        watchdog_stop_reason,
                        timeout_seconds=(
                            config.coordination.syncer_ha.learner_recovery_wait_seconds
                            if config.coordination.syncer_ha.enabled
                            else syncer_watchdog.timeout_seconds
                        ),
                        seconds_since_signal=syncer_watchdog.seconds_since_signal(),
                        last_signal_at=syncer_watchdog.last_signal_wall,
                        last_observed_global_version=syncer_watchdog.last_observed_version,
                        local_step=local_step,
                    )
                    break

            if watchdog_stop_reason is not None:
                break

            adoption_ctx = current_adoption_context()
            if read_authoritative_terminal(paths) is not None and adoption_strategy.on_stop(
                adoption_ctx
            ):
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
                **mid_cycle_adoptions.metadata(),
                admission=admission,
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
                local_cycle_elapsed_seconds=elapsed,
                **mid_cycle_adoptions.metadata(),
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
                    "local_cycle_elapsed_seconds": elapsed,
                    "param_norm": param_norm,
                    "learning_rate": last_step_learning_rate,
                    "scheduler_total_steps": config.inner_optimizer.scheduler_total_steps,
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
                learning_rate=current_inner_learning_rate(optimizer),
                scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
                admission=admission,
                last_proposal_at=time.time(),
            )
            logger.event("heartbeat_written", local_step=local_step)
            del flat

            if dynamic:
                drain_payload = read_current_drain(
                    paths,
                    run_id=config.run.run_id or "",
                )
                if drain_payload is not None:
                    logger.event(
                        "drain_seen_after_final_proposal",
                        close_generation=int(drain_payload["close_generation"]),
                        final_update_id=last_update_id,
                    )
                    break

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
        stop_payload = read_authoritative_terminal(paths)
        if stop_payload is not None:
            logger.event("stop_seen", reason=stop_payload.get("reason"))
        final_status = "drained" if admission is not None and drain_payload is not None else (
            LEARNER_STATUS_STOPPED
        )
        write_heartbeat(
            paths=paths,
            config=config,
            learner_id=learner_id,
            status=final_status,
            phase="drain_ack" if final_status == "drained" else "process_exit",
            last_loaded_global_version=last_loaded_global_version,
            last_local_step=local_step,
            last_update_id=last_update_id,
            resource_metrics=training_resources,
            learning_rate=current_inner_learning_rate(optimizer),
            scheduler_total_steps=config.inner_optimizer.scheduler_total_steps,
            status_reason=watchdog_stop_reason,
            admission=admission,
            close_generation=(
                None if drain_payload is None else int(drain_payload["close_generation"])
            ),
            final_update_id=last_update_id if drain_payload is not None else None,
        )
        logger.event(
            "process_exit",
            local_step=local_step,
            global_version=last_loaded_global_version,
            status_reason=watchdog_stop_reason,
            **({} if control_reader is None else control_reader.observation_metrics()),
            **training_resources,
        )
        close_epoch_control_reader(paths)


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
    if config.membership.mode == "dynamic":
        if args.learner_id is not None:
            raise ValueError("dynamic learner rejects --learner-id")
        if args.num_learners is not None:
            raise ValueError("dynamic learner rejects --num-learners as membership authority")
        if (args.bootstrap_slot is None) == (args.launch_request_id is None):
            raise ValueError(
                "dynamic learner requires exactly one of --bootstrap-slot or --launch-request-id"
            )
    else:
        if args.learner_id is None:
            raise ValueError("static learner requires --learner-id")
        if args.bootstrap_slot is not None or args.launch_request_id is not None:
            raise ValueError("static learner rejects dynamic admission arguments")
    run_learner(
        config,
        args.learner_id,
        bootstrap_slot=args.bootstrap_slot,
        launch_request_id=args.launch_request_id,
    )


if __name__ == "__main__":
    main()
