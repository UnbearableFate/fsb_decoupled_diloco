"""Filesystem-backed Decoupled DiLoCo syncer."""

from __future__ import annotations

import argparse
import os
import socket
import time
from pathlib import Path
from typing import Any

import torch

from ..core.config import Config, config_to_dict, resolve_config, write_resolved_config
from ..core.constants import FORMAT_VERSION, GLOBAL_STATUS_COMMITTED, learner_id_from_index
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
from ..storage.atomic_io import atomic_write_json, read_json, safe_read_json
from ..storage.paths import RunPaths, prepare_run_dirs
from ..storage.retention import cleanup_global_artifacts
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
    parser.add_argument("--sqlite-local-dir")
    parser.add_argument("--num-learners", type=int)
    return parser.parse_args(argv)


def sqlite_path(config: Config) -> Path:
    run_id = config.run.run_id or "unknown_run"
    local_dir = config.io.sqlite_local_dir
    if local_dir is None:
        local_dir = str(Path(os.environ.get("TMPDIR", "/tmp")) / "fs_diloco" / run_id)
    return Path(local_dir) / "syncer_metadata.sqlite3"


def latest_payload(
    *,
    config: Config,
    paths: RunPaths,
    version: int,
    weight_path: Path,
    optim_path: Path,
    total_seen_tokens: int,
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "version": version,
        "weight_path": str(weight_path),
        "optim_path": str(optim_path),
        "param_index_path": str(paths.param_index_json),
        "created_at": time.time(),
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
) -> None:
    weight_path = paths.global_weight_path(version)
    optim_path = paths.outer_optim_path(version)
    save_global_weights(weight_path, theta, param_index)
    save_outer_state(optim_path, theta, outer_state)
    store.upsert_global_version(
        version,
        str(weight_path),
        str(optim_path),
        num_updates=num_updates,
        total_update_tokens=total_update_tokens,
        total_seen_tokens=total_seen_tokens,
        outer_optimizer=config.outer_optimizer.name,
        status=GLOBAL_STATUS_COMMITTED,
    )
    atomic_write_json(
        paths.latest_json,
        latest_payload(
            config=config,
            paths=paths,
            version=version,
            weight_path=weight_path,
            optim_path=optim_path,
            total_seen_tokens=total_seen_tokens,
        ),
    )


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
    if materialized_weight_path is None or should_materialize_fragment_full(config, global_merge_event):
        materialize_start = time.monotonic()
        full = materialize_full_from_fragments(fragment_thetas, fragment_index, int(param_index["total_numel"]))
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
    if paths.latest_json.exists() and not config.init.allow_overwrite_existing_run:
        raise FileExistsError(f"{paths.latest_json} exists; set init.resume or allow overwrite")
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    param_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    theta = flatten_trainable_params(model, param_index, device=device).float()
    outer_state = init_outer_state(theta, config.outer_optimizer)
    atomic_write_json(paths.param_index_json, param_index)
    write_resolved_config(config, paths.resolved_config_yaml)
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
    store.set_run_state("config", config_to_dict(config))
    store.insert_event("syncer", "run_initialized", global_version=0)
    logger.event("run_initialized", version=0, total_numel=int(theta.numel()))
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
    if paths.latest_json.exists() and not config.init.allow_overwrite_existing_run:
        raise FileExistsError(f"{paths.latest_json} exists; set init.resume or allow overwrite")
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

    fragment_thetas: dict[int, torch.Tensor] = {}
    outer_states: dict[int, dict[str, torch.Tensor]] = {}
    fragment_versions: dict[int, int] = {}
    fragment_updated_events: dict[int, int] = {}
    for fragment in fragment_index["fragments"]:
        fragment_id = int(fragment["fragment_id"])
        theta_f = extract_fragment(theta, fragment_index, fragment_id).to(device=device, dtype=torch.float32)
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
    store.insert_event("syncer", "fragment_run_initialized", global_version=0)
    logger.event(
        "fragment_run_initialized",
        global_merge_event=0,
        total_numel=int(theta.numel()),
        num_fragments=int(fragment_index["num_fragments"]),
        strategy=fragment_index["strategy"],
    )
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


def _resume_latest_payload(config: Config, paths: RunPaths) -> dict[str, Any]:
    if config.init.resume_version == "latest":
        return read_json(paths.latest_json)
    version = int(config.init.resume_version)
    return {
        "format_version": FORMAT_VERSION,
        "run_id": config.run.run_id,
        "version": version,
        "weight_path": str(paths.global_weight_path(version)),
        "optim_path": str(paths.outer_optim_path(version)),
        "param_index_path": str(paths.param_index_json),
        "total_seen_tokens": 0,
    }


def _newest_db_dump(paths: RunPaths, version: int) -> Path | None:
    dumps = sorted(paths.db_dumps.glob(f"metadata_*_v{version:06d}.db"))
    return dumps[-1] if dumps else None


def resume_run(
    config: Config,
    paths: RunPaths,
    store: SQLiteStore,
    logger: JsonlLogger,
    *,
    device: torch.device | str = "cpu",
) -> tuple[int, torch.Tensor, dict[str, torch.Tensor], dict[str, Any], int]:
    latest = _resume_latest_payload(config, paths)
    param_index = load_param_index(latest["param_index_path"])
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    current_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_compatible_index(current_index, param_index)
    theta = load_global_weights_flat(latest["weight_path"], param_index, device=device)
    optim_theta, outer_state = load_outer_state(latest["optim_path"], device=device)
    if optim_theta.numel() == theta.numel():
        theta = optim_theta.float()

    requested_dump = Path(config.init.resume_db_dump) if config.init.resume_db_dump else None
    dump = requested_dump or _newest_db_dump(paths, int(latest["version"]))
    if dump and dump.exists():
        row = store.conn.execute("SELECT COUNT(*) AS n FROM global_versions").fetchone()
        if row["n"] == 0:
            store.restore_from_dump(dump)
    total_seen_tokens = int(latest.get("total_seen_tokens") or 0)
    store.upsert_global_version(
        int(latest["version"]),
        latest["weight_path"],
        latest["optim_path"],
        num_updates=0,
        total_update_tokens=0,
        total_seen_tokens=total_seen_tokens,
        outer_optimizer=config.outer_optimizer.name,
        status=GLOBAL_STATUS_COMMITTED,
        notes="resumed",
    )
    logger.event("run_resumed", version=int(latest["version"]), db_dump=str(dump) if dump else None)
    return int(latest["version"]), theta.float().to(device), outer_state, param_index, total_seen_tokens


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
        required = ["base_fragment_version", "base_global_merge_event", "tokens_since_fragment_load"]
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
        file_path.relative_to(paths.shared_root)
    except ValueError:
        pass
    return True


def ingest_update_metadata(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
) -> int:
    inserted = 0
    for path in sorted(paths.updates_pending.glob("learner_*/update_*.meta.json")):
        payload = safe_read_json(path)
        if payload is None or not validate_update_metadata(payload, config=config, paths=paths):
            continue
        inserted_payload = (
            store.insert_fragment_update_metadata(payload)
            if config.fragments.enabled
            else store.insert_update_metadata(payload)
        )
        if inserted_payload:
            inserted += 1
            store.insert_event(
                "syncer",
                "metadata_ingested",
                learner_id=payload["learner_id"],
                update_id=payload["update_id"],
                payload={"path": str(path)},
            )
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


def collect_with_grace_window(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    current_version: int,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + min(
        config.sync.grace_window.fixed_seconds,
        config.sync.grace_window.max_seconds,
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
        if len(selected) >= config.sync.quorum_max:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(config.sync.scan_interval_seconds, max(0.0, deadline - time.monotonic())))
        sync_liveness_and_metadata(store, paths, config, logger)
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
        logger.event("fragment_updates_dropped_missing_files", count=len(missing), update_ids=missing)
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
    deadline = time.monotonic() + min(
        config.sync.grace_window.fixed_seconds,
        config.sync.grace_window.max_seconds,
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
        if len(selected) >= config.sync.quorum_max:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(config.sync.scan_interval_seconds, max(0.0, deadline - time.monotonic())))
        sync_liveness_and_metadata(store, paths, config, logger)
    return selected


def finite_local_training_complete(store: SQLiteStore, config: Config) -> bool:
    if config.training.max_local_steps is None:
        return False
    learners = store.list_learners()
    if len(learners) < config.sync.num_learners:
        return False
    max_local_steps = int(config.training.max_local_steps)
    complete = 0
    for learner in learners:
        last_local_step = int(learner.get("last_local_step") or 0)
        if last_local_step >= max_local_steps:
            complete += 1
    return complete >= config.sync.num_learners


def select_terminal_drain_updates(
    store: SQLiteStore,
    paths: RunPaths,
    config: Config,
    logger: JsonlLogger,
    *,
    current_version: int,
) -> list[dict[str, Any]]:
    target = config.sync.stop_after_outer_steps
    if target is None or current_version >= target:
        return []
    if not finite_local_training_complete(store, config):
        return []
    pending = drop_missing_update_files(store, store.pending_updates(), logger)
    selected = select_one_per_learner(
        pending,
        policy="oldest_pending",
        quorum_max=config.sync.quorum_max,
    )
    if selected:
        logger.event(
            "terminal_drain_selected",
            version=current_version,
            selected_count=len(selected),
            remaining_outer_steps=int(target) - current_version,
            learners=[row["learner_id"] for row in selected],
        )
    elif paths.stop_json.exists():
        logger.event("terminal_drain_no_pending_updates", version=current_version)
    return selected


def dump_db(store: SQLiteStore, paths: RunPaths, version: int, logger: JsonlLogger) -> None:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = paths.db_dump_path(timestamp, version)
    store.backup_to(path, global_version=version)
    logger.event("db_dumped", version=version, path=str(path))


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


def _fragment_staleness_stats(selected: list[dict[str, Any]], current_fragment_version: int) -> dict[str, float | int]:
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
            if (
                config.sync.stop_after_outer_steps is not None
                and global_merge_event >= int(config.sync.stop_after_outer_steps)
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
            store.mark_fragment_updates_selected([row["update_id"] for row in selected], run_selection_id)
            logger.event(
                "fragment_updates_selected",
                global_merge_event=global_merge_event,
                fragment_id=target_fragment,
                fragment_version=current_fragment_version,
                update_ids=[row["update_id"] for row in selected],
                learners=[row["learner_id"] for row in selected],
            )

            read_start = time.monotonic()
            missing_after_select = [row["update_id"] for row in selected if not Path(row["file_path"]).exists()]
            if missing_after_select:
                missing_ids = set(missing_after_select)
                store.drop_fragment_updates(missing_after_select, "missing_file")
                store.reset_fragment_selected_to_pending(
                    [row["update_id"] for row in selected if row["update_id"] not in missing_ids]
                )
                logger.event("selected_fragment_updates_missing_files", count=len(missing_after_select))
                continue
            try:
                vectors = [load_fragment_update(row["file_path"], device=device) for row in selected]
            except FileNotFoundError:
                missing = [row["update_id"] for row in selected if not Path(row["file_path"]).exists()]
                if missing:
                    missing_ids = set(missing)
                    store.drop_fragment_updates(missing, "missing_file")
                    store.reset_fragment_selected_to_pending(
                        [row["update_id"] for row in selected if row["update_id"] not in missing_ids]
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
            cleanup_global_artifacts(paths, keep_last=config.io.keep_last_global_versions, logger=logger)
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
            if config.sync.db_dump_every_versions and new_global_merge_event % config.sync.db_dump_every_versions == 0:
                dump_db(store, paths, new_global_merge_event, logger)

            stale_stats = _fragment_staleness_stats(selected, current_fragment_version)
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
            dump_db(store, paths, global_merge_event, logger)
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
    paths = RunPaths(Path(config.run.shared_root or "."))
    prepare_run_dirs(paths, config.sync.num_learners)
    store = SQLiteStore(sqlite_path(config))
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
    try:
        while True:
            if config.sync.stop_after_outer_steps is not None and version >= config.sync.stop_after_outer_steps:
                stop_reason = "stop_after_outer_steps"
                break
            if (
                config.sync.stop_after_global_tokens is not None
                and total_seen_tokens >= config.sync.stop_after_global_tokens
            ):
                stop_reason = "stop_after_global_tokens"
                break

            sync_liveness_and_metadata(store, paths, config, logger)
            eligible = store.eligible_updates(version, config.sync.max_staleness_versions)
            eligible = drop_missing_update_files(store, eligible, logger)
            one_per_learner = select_one_per_learner(
                eligible,
                policy=config.sync.selection_policy,
                quorum_max=config.sync.quorum_max,
            )
            selected: list[dict[str, Any]]
            terminal_drain = False
            if len(one_per_learner) < config.sync.quorum_min:
                terminal_selected = select_terminal_drain_updates(
                    store,
                    paths,
                    config,
                    logger,
                    current_version=version,
                )
                if terminal_selected:
                    selected = terminal_selected
                    terminal_drain = True
                else:
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
                    terminal_selected = select_terminal_drain_updates(
                        store,
                        paths,
                        config,
                        logger,
                        current_version=version,
                    )
                    if not terminal_selected:
                        continue
                    selected = terminal_selected
                    terminal_drain = True

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
            missing_after_select = [row["update_id"] for row in selected if not Path(row["file_path"]).exists()]
            if missing_after_select:
                store.drop_updates(missing_after_select, "missing_file")
                store.reset_selected_to_pending(
                    [row["update_id"] for row in selected if row["update_id"] not in set(missing_after_select)]
                )
                logger.event("selected_updates_missing_files", count=len(missing_after_select))
                continue
            try:
                vectors = [load_update_vector(row["file_path"], device=device) for row in selected]
            except FileNotFoundError:
                missing = [row["update_id"] for row in selected if not Path(row["file_path"]).exists()]
                if missing:
                    store.drop_updates(missing, "missing_file")
                    store.reset_selected_to_pending(
                        [row["update_id"] for row in selected if row["update_id"] not in set(missing)]
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
            theta, outer_state = outer_optimizer_step(theta, grad, outer_state, config.outer_optimizer)
            outer_seconds = time.monotonic() - outer_start

            new_version = version + 1
            total_update_tokens = sum(int(row["tokens_this_update"]) for row in selected)
            total_seen_tokens += total_update_tokens
            publish_start = time.monotonic()
            publish_global(
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
            )
            cleanup_global_artifacts(paths, keep_last=config.io.keep_last_global_versions, logger=logger)
            publish_seconds = time.monotonic() - publish_start

            store.mark_updates_applied(
                selected,
                applied_version=new_version,
                effective_weights=weights_by_update,
            )
            dropped = store.drop_superseded_updates(selected)
            if not terminal_drain:
                dropped += store.drop_obsolete_updates(new_version, config.sync.max_staleness_versions)
            if config.sync.db_dump_every_versions and new_version % config.sync.db_dump_every_versions == 0:
                dump_db(store, paths, new_version, logger)
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
                    "stale_updates_dropped": dropped,
                    "global_interval_seconds": time.time() - last_global_time,
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
                        "syncer/stale_updates_dropped": dropped,
                        "syncer/global_interval_seconds": time.time() - last_global_time,
                        **selected_update_summary(selected, current_version=version),
                    },
                    step=new_version,
                )
            logger.event(
                "outer_step_applied",
                version=new_version,
                selected_count=len(selected),
                total_update_tokens=total_update_tokens,
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
            dump_db(store, paths, version, logger)
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
        sqlite_local_dir=args.sqlite_local_dir,
        num_learners=args.num_learners,
    )
    run_syncer(config)


if __name__ == "__main__":
    main()
