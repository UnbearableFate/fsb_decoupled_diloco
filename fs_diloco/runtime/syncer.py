"""Filesystem-backed Decoupled DiLoCo syncer."""

from __future__ import annotations

import argparse
import math
import os
import signal
import socket
import time
from pathlib import Path
from typing import Any

import torch

from ..core.config import Config, config_to_dict, resolve_config, write_resolved_config
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
from ..protocol.liveness import ingest_heartbeats, no_progress_timed_out, update_liveness_statuses
from ..protocol.merge import (
    normalized_fragment_update_weights,
    normalized_update_weights,
    select_one_per_learner,
    weighted_average_tensors,
)
from ..storage.atomic_io import atomic_write_json, safe_read_json
from ..storage.maintenance import run_maintenance
from ..storage.paths import RunPaths, prepare_run_dirs
from ..storage.sqlite_store import SQLiteStore
from ..storage.tensor_codec import (
    load_global_weights_flat,
    load_outer_state,
    load_update_vector,
    save_global_weights,
    save_outer_state,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--shared-root")
    parser.add_argument("--num-learners", type=int)
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
) -> dict[str, Any]:
    weight_path = paths.global_weight_path(version)
    optim_path = paths.outer_optim_path(version)
    if os.environ.get("FS_DILOCO_PUBLICATION_FAILPOINT") == "weight_temp":
        temp_path = weight_path.parent / f".{weight_path.name}.injected.tmp"
        with temp_path.open("wb") as handle:
            handle.write(b"incomplete-weight")
            handle.flush()
            os.fsync(handle.fileno())
        publication_failpoint("weight_temp")
    save_global_weights(weight_path, theta, param_index)
    publication_failpoint("after_weight")
    save_outer_state(optim_path, theta, outer_state)
    publication_failpoint("after_outer")
    sqlite_start = time.monotonic()
    if version == 0:
        row = store.initialize_full_run(
            weight_path=str(weight_path),
            optim_path=str(optim_path),
            outer_optimizer=config.outer_optimizer.name,
            identity=run_identity(config),
            config_snapshot=config_to_dict(config),
        )
    else:
        if predecessor_version is None or selected_updates is None or effective_weights is None:
            raise ValueError("full merge publication requires predecessor and selected updates")
        row = store.commit_full_merge(
            predecessor_version=predecessor_version,
            target_version=version,
            weight_path=str(weight_path),
            optim_path=str(optim_path),
            selected_updates=selected_updates,
            effective_weights=effective_weights,
            total_update_tokens=total_update_tokens,
            total_seen_tokens=total_seen_tokens,
            outer_optimizer=config.outer_optimizer.name,
            max_staleness_versions=config.sync.max_staleness_versions,
            before_commit=lambda: publication_failpoint("sqlite_transaction"),
        )
    sqlite_commit_seconds = time.monotonic() - sqlite_start
    publication_failpoint("after_db_commit")
    atomic_write_json(
        paths.latest_json,
        latest_payload(
            config=config,
            paths=paths,
            version=version,
            weight_path=weight_path,
            optim_path=optim_path,
            total_seen_tokens=total_seen_tokens,
            created_at=float(row["created_at"]),
        ),
    )
    publication_failpoint("after_latest")
    return {**row, "sqlite_commit_seconds": sqlite_commit_seconds}


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
        return True
    return global_merge_event % int(interval) == 0


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
) -> tuple[Path, float]:
    materialize_seconds = 0.0
    materialized_weight_path = previous_materialized_weight_path
    if materialized_weight_path is None or should_materialize_fragment_full(
        config, global_merge_event
    ):
        materialize_start = time.monotonic()
        full = materialize_full_from_fragments(
            fragment_thetas, fragment_index, int(param_index["total_numel"])
        )
        materialized_weight_path = paths.global_weight_path(global_merge_event)
        save_global_weights(materialized_weight_path, full, param_index)
        materialize_seconds = time.monotonic() - materialize_start
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
    return materialized_weight_path, materialize_seconds


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
    theta = flatten_trainable_params(model, param_index, device=device).float()
    outer_state = init_outer_state(theta, config.outer_optimizer)
    atomic_write_json(paths.param_index_json, param_index)
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
    theta = flatten_trainable_params(model, param_index, device=device).float()
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
            device=device, dtype=torch.float32
        )
        state_f = init_outer_state(theta_f, config.outer_optimizer)
        weight_path = paths.fragment_weight_path(fragment_id, 0)
        optim_path = paths.fragment_outer_optim_path(fragment_id, 0)
        save_fragment_weight(weight_path, theta_f)
        save_outer_state(optim_path, theta_f, state_f)
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

    materialized_weight_path, _materialize_seconds = publish_fragment_latest(
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
    param_index = load_param_index(paths.param_index_json)
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    current_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_compatible_index(current_index, param_index)
    weight_path = Path(str(committed["weight_path"]))
    optim_path = Path(str(committed["optim_path"]))
    if not weight_path.is_file() or not optim_path.is_file():
        raise FileNotFoundError(
            f"committed checkpoint is incomplete: weight={weight_path}, outer={optim_path}"
        )
    theta = load_global_weights_flat(weight_path, param_index, device=device).float()
    optim_theta, outer_state = load_outer_state(optim_path, device=device)
    optim_theta = optim_theta.float()
    if theta.shape != optim_theta.shape or not torch.equal(theta, optim_theta):
        raise RuntimeError("committed weight and outer checkpoint theta do not match")

    reset_selected = store.reset_all_selected_to_pending()
    total_seen_tokens = int(committed["total_seen_tokens"])
    repaired_latest = latest_payload(
        config=config,
        paths=paths,
        version=int(committed["version"]),
        weight_path=weight_path,
        optim_path=optim_path,
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
        reset_selected=reset_selected,
    )
    logger.event("state_maintenance_completed", **maintenance)
    return (
        int(committed["version"]),
        theta.float().to(device),
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


def ingest_update_metadata(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
) -> int:
    inserted = 0
    if config.fragments.enabled:
        metadata_paths = sorted(paths.updates_payloads.glob("learner_*/update_*.meta.json"))
    else:
        metadata_paths = [
            paths.update_pointer_path(learner_id_from_index(index))
            for index in range(config.sync.num_learners)
        ]
    for path in metadata_paths:
        payload = safe_read_json(path)
        if payload is None or not validate_update_metadata(payload, config=config, paths=paths):
            continue
        inserted_payload = (
            store.insert_fragment_update_metadata(payload)
            if config.fragments.enabled
            else store.insert_update_metadata(payload, pointer_path=path)
        )
        if inserted_payload:
            inserted += 1
    if inserted:
        logger.event("metadata_ingested", count=inserted)
    return inserted


def sync_liveness_and_metadata(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
) -> None:
    heartbeat_count = ingest_heartbeats(
        store,
        paths.heartbeats,
        run_id=config.run.run_id or "",
        num_learners=config.sync.num_learners,
    )
    counts = update_liveness_statuses(
        store,
        stale_after_seconds=config.liveness.stale_after_seconds,
        dead_after_seconds=config.liveness.dead_after_seconds,
    )
    if heartbeat_count:
        logger.event("heartbeats_ingested", count=heartbeat_count)
        logger.event("learner_liveness_updated", **counts)
    ingest_update_metadata(store, paths, config, logger)


def configured_grace_seconds(config: Config) -> float:
    grace = config.sync.grace_window
    requested = (
        grace.initial_seconds
        if grace.mode == "adaptive_fastest_upload_eta"
        else grace.fixed_seconds
    )
    return max(0.0, min(float(requested), float(grace.max_seconds)))


def fastest_next_upload_eta_seconds(
    updates: list[dict[str, Any]],
    *,
    inner_steps: int,
    now: float | None = None,
) -> float | None:
    """Estimate time until the earliest selected learner's next upload.

    The estimate intentionally uses measured compute time only. Serialization and
    adoption overhead therefore act as a conservative safety margin before the
    actual next pointer publication.
    """
    now = time.time() if now is None else float(now)
    estimates: list[float] = []
    for update in updates:
        step_seconds = update.get("local_cycle_step_time_seconds_mean")
        committed_at = update.get("committed_at")
        if step_seconds is None or committed_at is None:
            continue
        try:
            cycle_seconds = float(step_seconds) * max(1, int(inner_steps))
            eta_seconds = float(committed_at) + cycle_seconds - now
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
    now_monotonic: float,
    now_wall: float,
) -> tuple[float, float | None]:
    if config.sync.grace_window.mode != "adaptive_fastest_upload_eta":
        return deadline, None
    eta_seconds = fastest_next_upload_eta_seconds(
        selected,
        inner_steps=config.training.inner_steps,
        now=now_wall,
    )
    if eta_seconds is None:
        return deadline, None
    return min(deadline, now_monotonic + eta_seconds), eta_seconds


def collect_with_grace_window(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    current_version: int,
) -> list[dict[str, Any]]:
    started_at = time.monotonic()
    initial_seconds = configured_grace_seconds(config)
    deadline = started_at + initial_seconds
    deadline_source = "initial"
    logger.event(
        "grace_window_started",
        mode=config.sync.grace_window.mode,
        initial_seconds=initial_seconds,
        version=current_version,
    )
    selected: list[dict[str, Any]] = []
    while True:
        eligible = store.eligible_updates(current_version, config.sync.max_staleness_versions)
        eligible = drop_missing_update_files(store, eligible, logger)
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
            now_monotonic=now_monotonic,
            now_wall=time.time(),
        )
        if shortened_deadline < deadline:
            deadline = shortened_deadline
            deadline_source = "fastest_upload_eta"
            logger.event(
                "grace_window_shortened",
                version=current_version,
                selected=len(selected),
                fastest_next_upload_eta_seconds=eta_seconds,
                remaining_seconds=max(0.0, deadline - now_monotonic),
            )
        if len(selected) >= config.sync.quorum_max:
            deadline_source = "quorum_max"
            break
        if now_monotonic >= deadline:
            break
        time.sleep(min(config.sync.scan_interval_seconds, max(0.0, deadline - time.monotonic())))
        sync_liveness_and_metadata(store, paths, config, logger)
    logger.event(
        "grace_window_completed",
        version=current_version,
        selected=len(selected),
        elapsed_seconds=time.monotonic() - started_at,
        deadline_source=deadline_source,
    )
    return selected


def drop_missing_update_files(
    store: SQLiteStore,
    updates: list[dict[str, Any]],
    logger: JsonlLogger,
) -> list[dict[str, Any]]:
    missing = [row["update_id"] for row in updates if not Path(row["file_path"]).exists()]
    if missing:
        store.drop_updates(missing, "missing_file")
        logger.event("updates_dropped_missing_files", count=len(missing), update_ids=missing)
    missing_ids = set(missing)
    return [row for row in updates if row["update_id"] not in missing_ids]


def drop_missing_fragment_update_files(
    store: SQLiteStore,
    updates: list[dict[str, Any]],
    logger: JsonlLogger,
) -> list[dict[str, Any]]:
    missing = [row["update_id"] for row in updates if not Path(row["file_path"]).exists()]
    if missing:
        store.drop_fragment_updates(missing, "missing_file")
        logger.event(
            "fragment_updates_dropped_missing_files", count=len(missing), update_ids=missing
        )
    missing_ids = set(missing)
    return [row for row in updates if row["update_id"] not in missing_ids]


def collect_fragment_with_grace_window(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    fragment_id: int,
    current_fragment_version: int,
) -> list[dict[str, Any]]:
    started_at = time.monotonic()
    initial_seconds = configured_grace_seconds(config)
    deadline = started_at + initial_seconds
    deadline_source = "initial"
    logger.event(
        "grace_window_started",
        mode=config.sync.grace_window.mode,
        initial_seconds=initial_seconds,
        fragment_id=fragment_id,
        fragment_version=current_fragment_version,
    )
    selected: list[dict[str, Any]] = []
    while True:
        eligible = store.eligible_fragment_updates(
            fragment_id=fragment_id,
            current_fragment_version=current_fragment_version,
            max_staleness_versions=config.sync.max_staleness_versions,
        )
        eligible = drop_missing_fragment_update_files(store, eligible, logger)
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
            now_monotonic=now_monotonic,
            now_wall=time.time(),
        )
        if shortened_deadline < deadline:
            deadline = shortened_deadline
            deadline_source = "fastest_upload_eta"
            logger.event(
                "grace_window_shortened",
                fragment_id=fragment_id,
                fragment_version=current_fragment_version,
                selected=len(selected),
                fastest_next_upload_eta_seconds=eta_seconds,
                remaining_seconds=max(0.0, deadline - now_monotonic),
            )
        if len(selected) >= config.sync.quorum_max:
            deadline_source = "quorum_max"
            break
        if now_monotonic >= deadline:
            break
        time.sleep(min(config.sync.scan_interval_seconds, max(0.0, deadline - time.monotonic())))
        sync_liveness_and_metadata(store, paths, config, logger)
    logger.event(
        "grace_window_completed",
        fragment_id=fragment_id,
        fragment_version=current_fragment_version,
        selected=len(selected),
        elapsed_seconds=time.monotonic() - started_at,
        deadline_source=deadline_source,
    )
    return selected


def all_expected_learners_stopped(store: SQLiteStore, config: Config) -> bool:
    expected = {learner_id_from_index(index) for index in range(config.sync.num_learners)}
    stopped = {
        str(row["learner_id"])
        for row in store.list_learners()
        if row.get("status") == LEARNER_STATUS_STOPPED
    }
    return stopped == expected


def select_terminal_drain_updates(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    current_version: int,
) -> list[dict[str, Any]]:
    if not all_expected_learners_stopped(store, config):
        return []
    store.drop_ineligible_updates(current_version, config.sync.max_staleness_versions)
    pending = store.eligible_updates(current_version, config.sync.max_staleness_versions)
    pending = drop_missing_update_files(store, pending, logger)
    selected = select_one_per_learner(
        pending,
        policy=config.sync.selection_policy,
        quorum_max=config.sync.quorum_max,
    )
    if selected:
        logger.event(
            "terminal_drain_selected",
            version=current_version,
            selected_count=len(selected),
            remaining_outer_steps=(
                int(config.sync.stop_after_outer_steps) - current_version
                if config.sync.stop_after_outer_steps is not None
                else None
            ),
            learners=[row["learner_id"] for row in selected],
        )
    else:
        logger.event("terminal_drain_no_pending_updates", version=current_version)
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


def wait_for_learner_shutdown(
    *,
    paths: RunPaths,
    store: SQLiteStore,
    config: Config,
    logger: JsonlLogger,
    stop_reason: str,
) -> bool:
    if stop_reason == "error":
        return False
    expected = {learner_id_from_index(index) for index in range(config.sync.num_learners)}
    timeout_seconds = max(
        30.0,
        min(120.0, 2.0 * config.liveness.heartbeat_interval_seconds),
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        ingest_update_metadata(store, paths, config, logger)
        stopped: set[str] = set()
        for learner_id in expected:
            heartbeat = safe_read_json(paths.heartbeats / f"{learner_id}.json") or {}
            if heartbeat.get("status") == LEARNER_STATUS_STOPPED:
                stopped.add(learner_id)
        if stopped == expected:
            logger.event("all_learners_stopped", count=len(stopped))
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
    logger.event(
        "learner_shutdown_timeout",
        timeout_seconds=timeout_seconds,
        expected_count=len(expected),
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

    last_progress_time = time.time()
    last_global_time = last_progress_time
    stop_reason = "completed"
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
            sync_liveness_and_metadata(store, paths, config, logger)
            eligible = store.eligible_fragment_updates(
                fragment_id=target_fragment,
                current_fragment_version=current_fragment_version,
                max_staleness_versions=config.sync.max_staleness_versions,
            )
            eligible = drop_missing_fragment_update_files(store, eligible, logger)
            one_per_learner = select_one_per_learner(
                eligible,
                policy=config.sync.selection_policy,
                quorum_max=config.sync.quorum_max,
            )
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
                time.sleep(config.sync.scan_interval_seconds)
                continue

            selected = collect_fragment_with_grace_window(
                store,
                paths,
                config,
                logger,
                fragment_id=target_fragment,
                current_fragment_version=current_fragment_version,
            )
            if len(selected) < config.sync.quorum_min:
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
                store.reset_fragment_selected_to_pending(
                    [row["update_id"] for row in selected if row["update_id"] not in missing_ids]
                )
                logger.event(
                    "selected_fragment_updates_missing_files", count=len(missing_after_select)
                )
                continue
            try:
                vectors = [
                    load_fragment_update(row["file_path"], device=device) for row in selected
                ]
            except FileNotFoundError:
                missing = [
                    row["update_id"] for row in selected if not Path(row["file_path"]).exists()
                ]
                if missing:
                    missing_ids = set(missing)
                    store.drop_fragment_updates(missing, "missing_file")
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
            save_fragment_weight(weight_path, theta_f)
            save_outer_state(optim_path, theta_f, outer_state_f)
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
            materialized_weight_path, materialize_seconds = publish_fragment_latest(
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
            publish_seconds = time.monotonic() - publish_start

            store.mark_fragment_updates_applied(
                selected,
                applied_fragment_version=new_fragment_version,
                applied_global_merge_event=new_global_merge_event,
                effective_weights=weights_by_update,
            )
            dropped = store.drop_superseded_fragment_updates(selected)
            dropped += store.drop_obsolete_fragment_updates(
                fragment_id=target_fragment,
                current_fragment_version=new_fragment_version,
                max_staleness_versions=config.sync.max_staleness_versions,
            )
            maintenance = run_maintenance(
                store,
                paths,
                heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
                scan_interval_seconds=config.sync.scan_interval_seconds,
            )
            logger.event("state_maintenance_completed", **maintenance)

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
                    "materialize_full_seconds": materialize_seconds,
                    "fragment_staleness_min": stale_stats["min"],
                    "fragment_staleness_mean": stale_stats["mean"],
                    "fragment_staleness_max": stale_stats["max"],
                    "stale_updates_dropped": dropped,
                    "global_interval_seconds": time.time() - last_global_time,
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
                        "syncer/stale_updates_dropped": dropped,
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
            last_global_time = last_progress_time
    except Exception:
        stop_reason = "error"
        logger.exception("error", global_merge_event=global_merge_event)
        raise
    finally:
        try:
            if stop_reason != "error":
                materialized_weight_path, _seconds = publish_fragment_latest(
                    config=config,
                    paths=paths,
                    param_index=param_index,
                    fragment_index=fragment_index,
                    fragment_thetas=fragment_thetas,
                    fragment_versions=fragment_versions,
                    fragment_updated_events=fragment_updated_events,
                    total_seen_tokens=total_seen_tokens,
                    global_merge_event=global_merge_event,
                    previous_materialized_weight_path=None,
                )
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
            )
            ingest_update_metadata(store, paths, config, logger)
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
    paths = RunPaths(Path(config.run.shared_root or "."))
    prepare_run_dirs(paths, config.sync.num_learners)
    database_path = sqlite_path(config)
    if config.init.resume and not database_path.is_file():
        raise FileNotFoundError(
            f"resume requires persistent SQLite at {database_path}; latest.json is not authoritative"
        )
    store = SQLiteStore(database_path)
    logger = JsonlLogger(paths.logs / "syncer.jsonl", "syncer")
    log_uncaught_exception(logger)
    device = choose_device()
    hostname = socket.gethostname()
    logger.event(
        "process_start",
        run_id=config.run.run_id,
        shared_root=str(paths.shared_root),
        sqlite_path=str(store.path),
        hostname=hostname,
        device=str(device),
        cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"),
    )
    wandb_run = init_wandb_run(
        config=config,
        paths=paths,
        logger=logger,
        device=device,
        hostname=hostname,
    )
    if config.fragments.enabled:
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
    if config.init.resume:
        version, theta, outer_state, param_index, total_seen_tokens = resume_run(
            config,
            paths,
            store,
            logger,
            device=device,
        )
    else:
        version, theta, outer_state, param_index, total_seen_tokens = initialize_run(
            config,
            paths,
            store,
            logger,
            device=device,
        )

    last_progress_time = time.time()
    last_global_time = last_progress_time
    stop_reason = "completed"
    terminal_grace_complete = False
    try:
        while True:
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

            sync_liveness_and_metadata(store, paths, config, logger)
            input_closed = all_expected_learners_stopped(store, config)
            if input_closed and not terminal_grace_complete:
                logger.event("terminal_input_closed", version=version)
                grace_seconds = configured_grace_seconds(config)
                if grace_seconds > 0:
                    time.sleep(grace_seconds)
                sync_liveness_and_metadata(store, paths, config, logger)
                terminal_grace_complete = True

            if input_closed:
                selected = select_terminal_drain_updates(
                    store,
                    paths,
                    config,
                    logger,
                    current_version=version,
                )
                if not selected:
                    stop_reason = "input_exhausted"
                    logger.event("input_exhausted", version=version)
                    break
                terminal_drain = len(selected) < config.sync.quorum_min
            else:
                terminal_drain = False
            eligible = store.eligible_updates(version, config.sync.max_staleness_versions)
            eligible = drop_missing_update_files(store, eligible, logger)
            one_per_learner = select_one_per_learner(
                eligible,
                policy=config.sync.selection_policy,
                quorum_max=config.sync.quorum_max,
            )
            if not input_closed:
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
                    time.sleep(config.sync.scan_interval_seconds)
                    continue
                else:
                    selected = collect_with_grace_window(
                        store,
                        paths,
                        config,
                        logger,
                        current_version=version,
                    )
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
                vectors = [load_update_vector(row["file_path"], device=device) for row in selected]
            except FileNotFoundError:
                missing = [
                    row["update_id"] for row in selected if not Path(row["file_path"]).exists()
                ]
                if missing:
                    store.drop_updates(missing, "missing_file")
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
            aggregation_start = time.monotonic()
            p_bar = weighted_average_tensors(vectors, weights)
            grad = theta - p_bar
            aggregation_seconds = time.monotonic() - aggregation_start

            outer_start = time.monotonic()
            theta, outer_state = outer_optimizer_step(
                theta, grad, outer_state, config.outer_optimizer
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
            )
            publish_seconds = time.monotonic() - publish_start
            sqlite_commit_seconds = float(publication["sqlite_commit_seconds"])

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
                    "maintenance_seconds": maintenance_seconds,
                    "stale_updates_dropped": dropped,
                    "global_interval_seconds": time.time() - last_global_time,
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
                        "syncer/maintenance_seconds": maintenance_seconds,
                        "syncer/stale_updates_dropped": dropped,
                        "syncer/global_interval_seconds": time.time() - last_global_time,
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
            )
            logger.event("global_published", version=new_version)
            logger.event("updates_marked_applied", version=new_version)
            if dropped:
                logger.event("updates_dropped", version=new_version, count=dropped)
            version = new_version
            last_progress_time = time.time()
            last_global_time = last_progress_time
    except Exception:
        stop_reason = "error"
        logger.exception("error", version=version)
        raise
    finally:
        try:
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
            )
            ingest_update_metadata(store, paths, config, logger)
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
            write_training_summary(
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
            if wandb_run is not None:
                wandb_run.summary["stop_reason"] = stop_reason
                wandb_run.summary["final_version"] = version
                wandb_run.summary["total_seen_tokens"] = total_seen_tokens
            logger.event("process_exit", reason=stop_reason, version=version)
        finally:
            try:
                if wandb_run is not None:
                    wandb_run.finish(exit_code=1 if stop_reason == "error" else 0)
            finally:
                store.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = resolve_config(
        args.config,
        run_id=args.run_id,
        shared_root=args.shared_root,
        num_learners=args.num_learners,
    )
    run_syncer(config)


if __name__ == "__main__":
    main()
