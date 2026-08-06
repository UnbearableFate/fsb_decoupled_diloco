#!/usr/bin/env python3
"""Exercise HA publication crashes and DB-first takeover recovery."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.core.run_descriptor import load_run_descriptor
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.protocol.control_epoch import EpochControlPublisher, EpochControlReader
from fs_diloco.runtime.syncer import initialize_run, publish_global, resume_run
from fs_diloco.runtime.syncer_ha import acquire_candidate, open_leader_store
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.leader_lease import StaleLeaderTokenError
from fs_diloco.storage.maintenance import run_maintenance
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.tensor_codec import save_update_vector
from fs_diloco.tools.init_run import initialize_run as initialize_ha_root


FAILPOINTS = (
    "weight_temp",
    "after_weight",
    "after_outer",
    "sqlite_transaction",
    "after_db_commit",
    "after_latest",
)


def _fast_lease(config: Any) -> None:
    ha = config.coordination.syncer_ha
    ha.lease_duration_seconds = 0.50
    ha.renew_interval_seconds = 0.10
    ha.max_clock_skew_seconds = 0.0
    ha.heartbeat_interval_seconds = 0.02
    ha.heartbeat_stale_after_seconds = 0.08
    ha.candidate_acquire_poll_seconds = 0.01
    ha.candidate_wait_seconds = 1.0
    ha.learner_recovery_wait_seconds = 1.0
    ha.canonical_repair_wait_seconds = 0.05


def _add_pending_update(
    store: Any,
    paths: RunPaths,
    config: Any,
    *,
    update_id: str,
    base_version: int,
    theta: torch.Tensor,
) -> dict[str, Any]:
    payload = paths.update_payload_dir("learner_000") / f"{update_id}.params.safetensors"
    save_update_vector(payload, theta + 0.01)
    now = time.time()
    metadata = {
        "format_version": 1,
        "run_id": config.run.run_id,
        "update_id": update_id,
        "learner_id": "learner_000",
        "hostname": "phase1-probe",
        "base_global_version": base_version,
        "local_step_start": base_version,
        "local_step_end": base_version + 1,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_global_load": 1,
        "file_path": str(payload),
        "file_size_bytes": payload.stat().st_size,
        "created_at": now,
        "committed_at": now,
    }
    atomic_write_json(paths.update_pointer_path("learner_000"), metadata)
    if not store.insert_update_metadata(
        metadata, pointer_path=paths.update_pointer_path("learner_000")
    ):
        raise RuntimeError(f"failed to insert {update_id}")
    return metadata


def _select(store: Any, update_id: str, target_version: int) -> dict[str, Any]:
    store.mark_updates_selected([update_id], f"phase1-probe-v{target_version}")
    selected = store.get_update(update_id)
    if selected is None or selected["status"] != "selected":
        raise RuntimeError(f"update was not selected: {update_id}")
    return selected


def _publish_next(
    config: Any,
    paths: RunPaths,
    store: Any,
    *,
    predecessor: int,
    theta: torch.Tensor,
    outer_state: dict[str, torch.Tensor],
    param_index: dict[str, Any],
    selected: dict[str, Any],
) -> torch.Tensor:
    next_theta = theta + 0.001
    publish_global(
        config=config,
        paths=paths,
        store=store,
        version=predecessor + 1,
        theta=next_theta,
        outer_state=outer_state,
        param_index=param_index,
        num_updates=1,
        total_update_tokens=1,
        total_seen_tokens=predecessor + 1,
        selected_updates=[selected],
        effective_weights={str(selected["update_id"]): 1.0},
        predecessor_version=predecessor,
    )
    return next_theta


def _child_publish(shared_root: Path, update_id: str) -> None:
    loaded = load_run_descriptor(shared_root)
    config = loaded.config
    _fast_lease(config)
    paths = RunPaths(shared_root)
    lease, token, safety, _logger = acquire_candidate(
        paths=paths,
        identity=loaded.identity,
        config=config,
        owner_id=f"crash-child:{os.getpid()}",
    )
    store = open_leader_store(
        paths=paths,
        identity=loaded.identity,
        config=config,
        token=token,
        safety_tracker=safety,
    )
    logger = JsonlLogger(
        paths.logs / "fault-probe" / f"child_e{token.epoch:06d}.jsonl",
        "phase1_fault_child",
        mirror_stdout=False,
    )
    current, theta, outer_state, param_index, _tokens = resume_run(
        config, paths, store, logger, device=torch.device("cpu")
    )
    selected = _select(store, update_id, current + 1)
    _publish_next(
        config,
        paths,
        store,
        predecessor=current,
        theta=theta,
        outer_state=outer_state,
        param_index=param_index,
        selected=selected,
    )
    store.close()
    lease.close()
    raise RuntimeError("configured failpoint did not terminate the crash child")


def _one_case(root: Path, failpoint: str, iteration: int) -> dict[str, Any]:
    run_id = f"ha-{failpoint}-{iteration:02d}"
    shared_root = root / run_id
    config = resolve_config(
        "configs/fs_diloco_tiny_ha_static.yaml",
        run_id=run_id,
        shared_root=str(shared_root),
    )
    config.sync.num_learners = 1
    config.sync.quorum_min = 1
    config.sync.quorum_max = 1
    config.wandb.enabled = False
    initialize_ha_root(config, project_root=Path.cwd(), allow_dirty_snapshot=True)
    loaded = load_run_descriptor(shared_root)
    config = loaded.config
    config.wandb.enabled = False
    _fast_lease(config)
    paths = RunPaths(shared_root)

    lease1, token1, safety1, _candidate = acquire_candidate(
        paths=paths,
        identity=loaded.identity,
        config=config,
        owner_id=f"initializer:{iteration}",
    )
    store1 = open_leader_store(
        paths=paths,
        identity=loaded.identity,
        config=config,
        token=token1,
        safety_tracker=safety1,
    )
    logger = JsonlLogger(
        paths.logs / "fault-probe" / "parent.jsonl",
        "phase1_fault_parent",
        mirror_stdout=False,
    )
    _version, theta, outer_state, param_index, _tokens = initialize_run(
        config, paths, store1, logger, device=torch.device("cpu")
    )
    update_id = "crash-u0"
    _add_pending_update(
        store1,
        paths,
        config,
        update_id=update_id,
        base_version=0,
        theta=theta,
    )
    lease1.release(token1)

    env = dict(os.environ)
    env["FS_DILOCO_PUBLICATION_FAILPOINT"] = failpoint
    env["FS_DILOCO_FAILPOINT_ACTION"] = "kill"
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_child",
            "--root",
            str(shared_root),
            "--update-id",
            update_id,
        ],
        env=env,
        check=False,
    )
    if child.returncode != -9:
        raise RuntimeError(
            f"{failpoint} child did not terminate at the SIGKILL failpoint: "
            f"returncode={child.returncode}"
        )
    after_crash = store1.latest_global_version()
    after_crash_version = int(after_crash["version"])
    expected_after_crash = 1 if failpoint in {"after_db_commit", "after_latest"} else 0
    if after_crash_version != expected_after_crash:
        raise RuntimeError(
            f"{failpoint} committed v{after_crash_version}, expected v{expected_after_crash}"
        )

    time.sleep(config.coordination.syncer_ha.lease_duration_seconds + 0.05)
    lease3, token3, safety3, _candidate = acquire_candidate(
        paths=paths,
        identity=loaded.identity,
        config=config,
        owner_id=f"successor:{iteration}",
    )
    if token3.epoch != 3:
        raise RuntimeError(f"successor epoch was {token3.epoch}, expected 3")
    store3 = open_leader_store(
        paths=paths,
        identity=loaded.identity,
        config=config,
        token=token3,
        safety_tracker=safety3,
    )
    store3.fenced_store.gc_grace_seconds = 0.0
    current, theta, outer_state, param_index, _tokens = resume_run(
        config, paths, store3, logger, device=torch.device("cpu")
    )
    while current < 2:
        next_id = update_id if current == 0 else f"recovery-u{current}"
        row = store3.get_update(next_id)
        if row is None:
            _add_pending_update(
                store3,
                paths,
                config,
                update_id=next_id,
                base_version=current,
                theta=theta,
            )
        selected = _select(store3, next_id, current + 1)
        theta = _publish_next(
            config,
            paths,
            store3,
            predecessor=current,
            theta=theta,
            outer_state=outer_state,
            param_index=param_index,
            selected=selected,
        )
        current += 1

    old_latest = next(paths.syncer_epoch_dir(1, token1.owner_id).glob("latest/v*.json"))
    paths.latest_json.write_bytes(old_latest.read_bytes())
    reader = EpochControlReader(paths, run_id=config.run.run_id or "")
    authoritative = reader.read_current_latest()
    reader.close()
    if authoritative is None or int(authoritative["version"]) != 2:
        raise RuntimeError("fixed-cache pollution changed the authoritative version")
    EpochControlPublisher(paths, store3.fenced_store, token3).repair_latest_from_db()
    try:
        store1.set_run_state("stale-write", True)
    except StaleLeaderTokenError:
        stale_rejected = True
    else:
        stale_rejected = False
    if not stale_rejected:
        raise RuntimeError("released epoch committed through a stale bound store")

    maintenance = run_maintenance(
        store3,
        paths,
        heartbeat_interval_seconds=0.01,
        scan_interval_seconds=0.01,
    )
    active = store3.latest_global_version()
    if active is None or int(active["version"]) != 2:
        raise RuntimeError("maintenance changed the current global version")
    store1.close()
    store3.close()
    lease3.release(token3)
    lease1.close()
    lease3.close()
    return {
        "failpoint": failpoint,
        "iteration": iteration,
        "child_returncode": child.returncode,
        "db_version_after_crash": after_crash_version,
        "successor_epoch": token3.epoch,
        "final_version": 2,
        "stale_write_rejected": stale_rejected,
        "maintenance": maintenance,
        "run_root": str(shared_root),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode")
    child = subparsers.add_parser("_child")
    child.add_argument("--root", type=Path, required=True)
    child.add_argument("--update-id", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "_child":
        _child_publish(args.root, args.update_id)
        return
    if args.root is None or args.output is None:
        raise SystemExit("--root and --output are required")
    args.root.mkdir(parents=True, exist_ok=True)
    cases = [
        _one_case(args.root, failpoint, iteration)
        for failpoint in FAILPOINTS
        for iteration in range(args.iterations)
    ]
    result = {
        "status": "PASS",
        "iterations_per_failpoint": args.iterations,
        "case_count": len(cases),
        "failpoints": list(FAILPOINTS),
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print("PASS")


if __name__ == "__main__":
    main()
