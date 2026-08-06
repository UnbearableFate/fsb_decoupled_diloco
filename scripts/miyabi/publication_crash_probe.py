#!/usr/bin/env python3
"""Run the full-publication kill/restart matrix against the synthetic model."""

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
from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime.syncer import initialize_run, publish_global, resume_run
from fs_diloco.storage.atomic_io import atomic_write_json, safe_read_json
from fs_diloco.storage.maintenance import run_maintenance
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs
from fs_diloco.storage.sqlite_store import SQLiteStore
from fs_diloco.storage.tensor_codec import (
    load_global_weights_flat,
    load_outer_state,
    save_update_vector,
)


FAILPOINTS = (
    "weight_temp",
    "after_weight",
    "after_outer",
    "sqlite_transaction",
    "after_db_commit",
    "after_latest",
)


def config_for(shared_root: Path, run_id: str):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id=run_id,
        shared_root=str(shared_root),
        num_learners=1,
    )
    config.sync.max_staleness_versions = 0
    config.wandb.enabled = False
    return config


def add_proposal(
    store: SQLiteStore,
    paths: RunPaths,
    config: Any,
    *,
    update_id: str,
    base_version: int,
    vector: torch.Tensor,
) -> dict[str, Any]:
    payload_path = paths.update_payload_dir("learner_000") / f"{update_id}.params.safetensors"
    save_update_vector(payload_path, vector)
    now = time.time()
    metadata = {
        "format_version": 1,
        "run_id": config.run.run_id,
        "update_id": update_id,
        "learner_id": "learner_000",
        "hostname": "probe",
        "base_global_version": base_version,
        "local_step_start": base_version,
        "local_step_end": base_version + 1,
        "inner_steps": 1,
        "tokens_this_update": 1,
        "tokens_since_global_load": 1,
        "file_path": str(payload_path),
        "created_at": now,
        "committed_at": now,
    }
    atomic_write_json(paths.update_pointer_path("learner_000"), metadata)
    if not store.insert_update_metadata(
        metadata, pointer_path=paths.update_pointer_path("learner_000")
    ):
        raise RuntimeError(f"proposal {update_id} was not inserted")
    return metadata


def select(store: SQLiteStore, update_id: str, target_version: int) -> dict[str, Any]:
    store.mark_updates_selected([update_id], f"probe-v{target_version}")
    row = store.get_update(update_id)
    if row is None or row["status"] != "selected":
        raise RuntimeError(f"proposal {update_id} was not selected")
    return row


def publish_next(
    config: Any,
    paths: RunPaths,
    store: SQLiteStore,
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


def child_publish(shared_root: Path, run_id: str) -> None:
    config = config_for(shared_root, run_id)
    paths = RunPaths(shared_root)
    store = SQLiteStore(paths.sqlite_db)
    committed = store.latest_global_version()
    if committed is None or int(committed["version"]) != 0:
        raise RuntimeError("crash child expected committed v0")
    param_index = json.loads(paths.param_index_json.read_text(encoding="utf-8"))
    theta = load_global_weights_flat(committed["weight_path"], param_index)
    _outer_theta, outer_state = load_outer_state(committed["optim_path"])
    selected = store.get_update("crash-u0")
    if selected is None:
        raise RuntimeError("selected crash proposal is missing")
    publish_next(
        config,
        paths,
        store,
        predecessor=0,
        theta=theta,
        outer_state=outer_state,
        param_index=param_index,
        selected=selected,
    )
    raise RuntimeError("configured publication failpoint did not fire")


def cross_node_initialize(shared_root: Path, run_id: str) -> None:
    config = config_for(shared_root, run_id)
    paths = RunPaths(shared_root)
    prepare_run_dirs(paths, 1)
    logger = JsonlLogger(paths.logs / "cross-node.jsonl", "cross-node", mirror_stdout=False)
    store = SQLiteStore(paths.sqlite_db)
    _version, theta, outer_state, param_index, _tokens = initialize_run(
        config, paths, store, logger
    )
    add_proposal(
        store,
        paths,
        config,
        update_id="node-a-u0",
        base_version=0,
        vector=theta + 0.01,
    )
    selected = select(store, "node-a-u0", 1)
    theta = publish_next(
        config,
        paths,
        store,
        predecessor=0,
        theta=theta,
        outer_state=outer_state,
        param_index=param_index,
        selected=selected,
    )
    run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
        scan_interval_seconds=config.sync.scan_interval_seconds,
    )
    add_proposal(
        store,
        paths,
        config,
        update_id="carry-selected-u1",
        base_version=1,
        vector=theta + 0.01,
    )
    select(store, "carry-selected-u1", 2)
    store.integrity_check()
    summary = {
        "status": "PASS",
        "host": os.uname().nodename,
        "committed_version": store.latest_global_version()["version"],
        "carry_status": store.get_update("carry-selected-u1")["status"],
        "latest_version": int((safe_read_json(paths.latest_json) or {})["version"]),
    }
    store.close()
    print(json.dumps(summary, sort_keys=True))


def cross_node_resume(shared_root: Path, run_id: str) -> None:
    config = config_for(shared_root, run_id)
    config.init.resume = True
    paths = RunPaths(shared_root)
    paths.latest_json.unlink(missing_ok=True)
    logger = JsonlLogger(paths.logs / "cross-node.jsonl", "cross-node", mirror_stdout=False)
    store = SQLiteStore(paths.sqlite_db)
    current, theta, outer_state, param_index, _tokens = resume_run(
        config, paths, store, logger
    )
    carry = store.get_update("carry-selected-u1")
    if carry is None or carry["status"] != "pending":
        raise RuntimeError(f"resume did not reset carried selection: {carry}")
    selected = select(store, "carry-selected-u1", current + 1)
    publish_next(
        config,
        paths,
        store,
        predecessor=current,
        theta=theta,
        outer_state=outer_state,
        param_index=param_index,
        selected=selected,
    )
    run_maintenance(
        store,
        paths,
        heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
        scan_interval_seconds=config.sync.scan_interval_seconds,
    )
    store.integrity_check()
    ids = history_ids(paths.update_history_jsonl)
    summary = {
        "status": "PASS",
        "host": os.uname().nodename,
        "committed_version": store.latest_global_version()["version"],
        "latest_version": int((safe_read_json(paths.latest_json) or {})["version"]),
        "carry_archive_count": ids.count("carry-selected-u1"),
        "active_updates": store.conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0],
    }
    if summary["committed_version"] != 2 or summary["latest_version"] != 2:
        raise RuntimeError(f"cross-node resume did not reach v2: {summary}")
    if summary["carry_archive_count"] != 1 or summary["active_updates"] != 0:
        raise RuntimeError(f"cross-node proposal was not exactly-once: {summary}")
    store.close()
    print(json.dumps(summary, sort_keys=True))


def history_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        str(row["update_id"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line)).get("update_id")
    ]


def one_case(root: Path, failpoint: str, iteration: int) -> dict[str, Any]:
    run_id = f"crash-{failpoint}-{iteration:02d}"
    shared_root = root / run_id
    config = config_for(shared_root, run_id)
    paths = RunPaths(shared_root)
    prepare_run_dirs(paths, 1)
    logger = JsonlLogger(paths.logs / "probe.jsonl", "probe", mirror_stdout=False)
    store = SQLiteStore(paths.sqlite_db)
    _version, theta, outer_state, param_index, _tokens = initialize_run(
        config, paths, store, logger
    )
    add_proposal(
        store,
        paths,
        config,
        update_id="crash-u0",
        base_version=0,
        vector=theta + 0.01,
    )
    select(store, "crash-u0", 1)
    store.close()

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
            "--run-id",
            run_id,
        ],
        env=env,
        check=False,
    )
    if child.returncode == 0:
        raise RuntimeError(f"{failpoint} child unexpectedly succeeded")

    latest_before = safe_read_json(paths.latest_json)
    if not latest_before:
        raise RuntimeError("latest.json became unreadable after a publication crash")
    for key in ("weight_path", "optim_path"):
        if not Path(str(latest_before[key])).is_file():
            raise RuntimeError(f"latest.json references missing {key} after {failpoint}")

    config.init.resume = True
    store = SQLiteStore(paths.sqlite_db)
    current, theta, outer_state, param_index, _tokens = resume_run(
        config, paths, store, logger
    )
    while current < 2:
        update_id = "crash-u0" if current == 0 else f"recovery-u{current}"
        row = store.get_update(update_id)
        if row is None:
            add_proposal(
                store,
                paths,
                config,
                update_id=update_id,
                base_version=current,
                vector=theta + 0.01,
            )
        selected = select(store, update_id, current + 1)
        theta = publish_next(
            config,
            paths,
            store,
            predecessor=current,
            theta=theta,
            outer_state=outer_state,
            param_index=param_index,
            selected=selected,
        )
        run_maintenance(
            store,
            paths,
            heartbeat_interval_seconds=config.liveness.heartbeat_interval_seconds,
            scan_interval_seconds=config.sync.scan_interval_seconds,
        )
        current += 1

    store.integrity_check()
    latest = safe_read_json(paths.latest_json) or {}
    if int(latest.get("version", -1)) != 2:
        raise RuntimeError(f"recovery ended at latest {latest.get('version')}, expected 2")
    if store.latest_global_version()["version"] != 2:
        raise RuntimeError("SQLite did not reach recovery version 2")
    weight_files = (
        list(paths.iter_epoch_weights())
        if paths.bootstrap_complete_json.is_file()
        else list(paths.weights.glob("global_v*.safetensors"))
    )
    optim_files = (
        list(paths.iter_epoch_optim())
        if paths.bootstrap_complete_json.is_file()
        else list(paths.optim.glob("outer_v*.safetensors"))
    )
    if len(weight_files) != 1:
        raise RuntimeError("weight GC did not converge to current-only")
    if len(optim_files) != 1:
        raise RuntimeError("outer-state GC did not converge to current-only")
    if list(paths.iter_instance_payloads()):
        raise RuntimeError("terminal proposal payloads remain after recovery")
    ids = history_ids(paths.update_history_jsonl)
    if ids.count("crash-u0") != 1:
        raise RuntimeError(f"crash-u0 application/archive count is {ids.count('crash-u0')}")
    store.close()
    return {
        "failpoint": failpoint,
        "iteration": iteration,
        "child_returncode": child.returncode,
        "db_version_after_crash": 1 if failpoint in {"after_db_commit", "after_latest"} else 0,
        "final_version": 2,
        "run_root": str(shared_root),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode")
    child = sub.add_parser("_child")
    child.add_argument("--root", type=Path, required=True)
    child.add_argument("--run-id", required=True)
    for mode in ("cross-init", "cross-resume"):
        cross = sub.add_parser(mode)
        cross.add_argument("--root", type=Path, required=True)
        cross.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--iterations", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "_child":
        child_publish(args.root, args.run_id)
        return
    if args.mode == "cross-init":
        cross_node_initialize(args.root, args.run_id)
        return
    if args.mode == "cross-resume":
        cross_node_resume(args.root, args.run_id)
        return
    if args.root is None:
        raise SystemExit("--root is required")
    args.root.mkdir(parents=True, exist_ok=True)
    cases = [
        one_case(args.root, failpoint, iteration)
        for failpoint in FAILPOINTS
        for iteration in range(args.iterations)
    ]
    print(
        json.dumps(
            {
                "status": "PASS",
                "iterations_per_failpoint": args.iterations,
                "case_count": len(cases),
                "failpoints": list(FAILPOINTS),
                "cases": cases,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
