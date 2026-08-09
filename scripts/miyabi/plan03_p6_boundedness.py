#!/usr/bin/env python3
"""Run the formal 10,000-cycle authority hot-set boundedness gate."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import random
import shutil
import sqlite3
import statistics
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from safetensors.torch import save as save_safetensors
import torch

from fs_diloco.core.config_v4 import MaintenanceSection
from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2, canonical_update_relative_path
from fs_diloco.runtime.services.maintenance import MaintenanceService
from fs_diloco.storage.atomic_io import publish_immutable_bytes
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority_v4
from fs_diloco.storage.object_store import tensor_schema_sha256
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from fs_diloco.storage.tensor_identity import tensor_content_sha256


PLAN_ID = "fsb_decoupled_diloco_plan_03_unified_ha"
REQUIREMENTS = ["P6-ACCEPTANCE"]


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Telemetry:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def event(self, name: str, **_fields: object) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1


def _capture_source(project_root: Path) -> dict[str, Any]:
    helper = project_root / "scripts/miyabi/capture_source_identity.py"
    specification = importlib.util.spec_from_file_location("plan03_capture_source", helper)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load source identity helper")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    value = module.capture(project_root)
    return {
        "git_commit": value["git_commit"],
        "git_dirty": value["git_dirty"],
        "source_fingerprint": value["source_fingerprint"],
    }


def _update_payload() -> tuple[bytes, str]:
    header = json.dumps(
        {"flat_update": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}},
        separators=(",", ":"),
    ).encode("utf-8")
    padded = header + b" " * ((8 - len(header) % 8) % 8)
    payload = len(padded).to_bytes(8, "little") + padded + struct.pack("<f", 1.0)
    return payload, tensor_schema_sha256([{"key": "flat_update", "dtype": "float32", "shape": [1]}])


def _checkpoint_payloads() -> tuple[bytes, bytes, str]:
    theta = torch.tensor([0.0], dtype=torch.float32)
    theta_sha256 = tensor_content_sha256(theta)
    metadata = {"fs_diloco_theta_sha256": theta_sha256}
    weight = save_safetensors(
        {"parameter": theta},
        metadata={**metadata, "fs_diloco_theta_order": '["parameter"]'},
    )
    outer = save_safetensors(
        {"theta": theta, "step": torch.tensor(0, dtype=torch.int64)},
        metadata=metadata,
    )
    return weight, outer, theta_sha256


def _checkpoint_metadata(
    *,
    version: int,
    weight_payload: bytes,
    outer_payload: bytes,
    theta_sha256: str,
) -> dict[str, Any]:
    weight_relative = f"weights/epochs/e1/v{version}.safetensors"
    outer_relative = f"optim/epochs/e1/v{version}.safetensors"
    return {
        "weight_relative_path": weight_relative,
        "weight_size": len(weight_payload),
        "weight_sha256": hashlib.sha256(weight_payload).hexdigest(),
        "optim_relative_path": outer_relative,
        "optim_size": len(outer_payload),
        "optim_sha256": hashlib.sha256(outer_payload).hexdigest(),
        "weight_theta_sha256": theta_sha256,
        "optim_theta_sha256": theta_sha256,
    }


def _publish_checkpoint_pair(
    root: Path, metadata: dict[str, Any], weight_payload: bytes, outer_payload: bytes
) -> None:
    publish_immutable_bytes(root / str(metadata["weight_relative_path"]), weight_payload)
    publish_immutable_bytes(root / str(metadata["optim_relative_path"]), outer_payload)


def _database_sample(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    finally:
        connection.close()
    return {
        "page_count": page_count,
        "freelist_count": freelist_count,
        "live_pages": page_count - freelist_count,
        "page_size": page_size,
    }


def _count_files(root: Path) -> tuple[int, int, int, int]:
    def count(path: Path) -> int:
        return sum(1 for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0

    checkpoints = count(root / "weights/epochs") + count(root / "optim/epochs")
    updates = count(root / "updates/payloads")
    audit = count(root / "audit")
    active = checkpoints + updates + count(root / "updates/latest") + count(root / "heartbeats")
    return active, audit, checkpoints, updates


def _ols_slope(points: list[tuple[int, float]]) -> float:
    mean_x = statistics.fmean(item[0] for item in points)
    mean_y = statistics.fmean(item[1] for item in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def _block_bootstrap_upper(
    points: list[tuple[int, float]], *, block_samples: int, seed: int = 20260809
) -> dict[str, Any]:
    if len(points) < block_samples * 4:
        raise RuntimeError("boundedness slope requires at least four complete sample blocks")
    slopes = [
        _ols_slope(points[start : start + block_samples])
        for start in range(0, len(points) - block_samples + 1, block_samples)
    ]
    generator = random.Random(seed)
    bootstrapped = sorted(
        statistics.fmean(generator.choice(slopes) for _ in slopes) for _ in range(4_000)
    )
    upper_index = math.ceil(0.95 * len(bootstrapped)) - 1
    return {
        "method": "fixed-contiguous-block slope bootstrap",
        "seed": seed,
        "repeats": len(bootstrapped),
        "block_samples": block_samples,
        "block_slopes": slopes,
        "slope": _ols_slope(points),
        "one_sided_95_upper": bootstrapped[upper_index],
    }


def _latency_probe(call: Callable[[], object], *, repeats: int = 200) -> dict[str, float]:
    values: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        values.append(time.perf_counter() - started)
    values.sort()
    return {
        "median_seconds": statistics.median(values),
        "p95_seconds": values[math.ceil(0.95 * len(values)) - 1],
    }


def _rows(connection: sqlite3.Connection, query: str) -> list[tuple[Any, ...]]:
    return connection.execute(query).fetchall()


def run_gate(
    *,
    project_root: Path,
    run_root: Path,
    cycles: int,
    sample_every: int,
    warmup: int,
    formal: bool,
) -> dict[str, Any]:
    if formal and cycles < 10_000:
        raise ValueError("formal boundedness gate requires at least 10,000 cycles")
    if not formal and cycles < 200:
        raise ValueError("boundedness calibration requires at least 200 cycles")
    minimum_warmup = 1_000 if formal else 50
    if warmup < minimum_warmup or warmup >= cycles or sample_every < 1:
        raise ValueError("invalid boundedness warm-up/sample interval")
    source = _capture_source(project_root)
    if source["git_dirty"]:
        raise RuntimeError("formal boundedness evidence requires a clean source tree")
    if run_root.exists():
        raise FileExistsError(f"boundedness run root already exists: {run_root}")
    run_root.mkdir(parents=True)
    paths = RunPaths(run_root)
    prepare_authority_dirs(paths)
    clock = _Clock()
    telemetry = _Telemetry()
    maintenance_config = MaintenanceSection()
    identity = AuthorityIdentity(
        "plan03-p6-boundedness",
        str(source["source_fingerprint"]),
        hashlib.sha256(b"plan03-p6-boundedness-v1").hexdigest(),
    )
    scope = StaticMembershipScope(("learner_000",))
    database = run_root / "control/syncer_metadata.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=clock)
    update_payload, update_schema = _update_payload()
    update_sha256 = hashlib.sha256(update_payload).hexdigest()
    weight_payload, outer_payload, theta_sha256 = _checkpoint_payloads()
    samples: list[dict[str, Any]] = []
    previous_receipt: CycleReceiptV1 | None = None
    warmup_latency: dict[str, dict[str, float]] | None = None
    started = time.monotonic()
    with LeaderAuthority(
        database,
        identity,
        scope,
        wall_clock=clock,
    ) as authority:
        token = authority.acquire_leader(owner_id="boundedness", hostname="compute", pid=1)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="bind-static-boundedness",
            learner_id="learner_000",
            logical_launch_id="boundedness-launch",
            attempt_id="boundedness-attempt",
        )
        fence = {
            "kind": "static",
            "learner_id": binding.learner_id,
            "logical_launch_id": binding.logical_launch_id,
            "attempt_id": binding.attempt_id,
            "binding_generation": binding.binding_generation,
        }
        initial_metadata = _checkpoint_metadata(
            version=0,
            weight_payload=weight_payload,
            outer_payload=outer_payload,
            theta_sha256=theta_sha256,
        )
        leader.prepare_publication(
            command_id="initialize-v0-boundedness-prepare",
            publication_id="publication-v0",
            target_version=0,
            selection_batch_id=None,
            **initial_metadata,
        )
        _publish_checkpoint_pair(run_root, initial_metadata, weight_payload, outer_payload)
        leader.commit_merge(
            command_id="initialize-v0-boundedness-commit",
            publication_id="publication-v0",
        )
        maintenance = MaintenanceService(
            authority=authority,
            leader=leader,
            paths=paths,
            config=maintenance_config,
            telemetry=telemetry,
        )
        for cycle in range(1, cycles + 1):
            clock.now += 1.0
            if cycle % 30 == 0:
                authority.renew_leader(token)
            update_id = str(uuid.UUID(int=cycle, version=4))
            cycle_id = str(uuid.UUID(int=cycles + cycle, version=4))
            receipt = CycleReceiptV1.from_dict(
                {
                    "cycle_receipt_format_version": 1,
                    "run_id": identity.run_id,
                    "stable_contributor_key": "learner_000",
                    "cycle_seq": cycle,
                    "cycle_id": cycle_id,
                    "receipt_id": f"receipt-learner_000-{cycle}",
                    "previous_receipt_id": (
                        None if previous_receipt is None else previous_receipt.receipt_id
                    ),
                    "previous_receipt_sha256": (
                        None if previous_receipt is None else previous_receipt.immutable_sha256()
                    ),
                    "processed_tokens_this_cycle": 8,
                    "effective_tokens_this_cycle": 6,
                    "local_discarded_tokens_this_cycle": 2,
                    "retained_tokens_since_base": 6,
                    "data_cursor_start": 8 * (cycle - 1),
                    "data_cursor_end": 8 * cycle,
                    "proposal_expected": True,
                    "planned_update_id": update_id,
                    "planned_payload_sha256": update_sha256,
                    "contributor_fence": fence,
                    "created_at": clock.now,
                }
            )
            leader.ingest_cycle_receipt(
                command_id=f"receipt-{receipt.immutable_sha256()}", receipt=receipt
            )
            relative_path = canonical_update_relative_path("learner_000", update_id)
            publication = publish_immutable_bytes(run_root / relative_path, update_payload)
            proposal = FullUpdateProposalV2.from_dict(
                {
                    "proposal_format_version": 2,
                    "run_id": identity.run_id,
                    "stable_contributor_key": "learner_000",
                    "cycle_seq": cycle,
                    "cycle_id": cycle_id,
                    "update_id": update_id,
                    "cycle_receipt_id": receipt.receipt_id,
                    "cycle_receipt_sha256": receipt.immutable_sha256(),
                    "base_global_version": cycle - 1,
                    "local_step_start": cycle - 1,
                    "local_step_end": cycle,
                    "inner_steps": 1,
                    "processed_tokens_this_cycle": 8,
                    "effective_tokens_this_update": 6,
                    "local_discarded_tokens_this_cycle": 2,
                    "retained_tokens_since_base": 6,
                    "data_cursor_start": 8 * (cycle - 1),
                    "data_cursor_end": 8 * cycle,
                    "contributor_fence": fence,
                    "payload_relative_path": relative_path,
                    "payload_size": publication.size_bytes,
                    "payload_sha256": publication.sha256,
                    "tensor_schema_sha256": update_schema,
                    "tensor_dtype": "float32",
                    "tensor_numel": 1,
                    "created_at": clock.now,
                }
            )
            leader.ingest_proposal(
                command_id=f"proposal-{proposal.immutable_sha256()}", proposal=proposal
            )
            selected = leader.try_select_batch(
                command_id=f"select-boundedness-v{cycle}", quorum_min=1, quorum_max=1
            )
            if selected.batch is None:
                raise RuntimeError(f"cycle {cycle} did not select its proposal")
            publication_id = f"publication-v{cycle}"
            checkpoint_metadata = _checkpoint_metadata(
                version=cycle,
                weight_payload=weight_payload,
                outer_payload=outer_payload,
                theta_sha256=theta_sha256,
            )
            leader.prepare_publication(
                command_id=f"prepare-{publication_id}",
                publication_id=publication_id,
                target_version=cycle,
                selection_batch_id=selected.batch.batch_id,
                **checkpoint_metadata,
            )
            _publish_checkpoint_pair(run_root, checkpoint_metadata, weight_payload, outer_payload)
            committed = leader.commit_merge(
                command_id=f"commit-{publication_id}", publication_id=publication_id
            )
            if committed.version != cycle:
                raise RuntimeError("global version sequence diverged from cycle sequence")
            maintenance.tick()
            previous_receipt = receipt
            if cycle == warmup:
                warmup_latency = {
                    "latest": _latency_probe(authority.read.latest_committed_version),
                    "progress": _latency_probe(
                        lambda: authority.read.contributor_progress("learner_000")
                    ),
                }
            if cycle >= warmup and cycle % sample_every == 0:
                database_sample = _database_sample(database)
                active, audit, checkpoints, updates = _count_files(run_root)
                samples.append(
                    {
                        "cycle": cycle,
                        **database_sample,
                        "active_recovery_files": active,
                        "audit_files": audit,
                        "checkpoint_files": checkpoints,
                        "update_payload_files": updates,
                    }
                )
        maintenance.tick(force=True)
        clock.now += 60.0
        authority.renew_leader(token)
        clock.now += maintenance_config.publication_orphan_grace_seconds - 59.0
        maintenance.tick()
        final_latency = {
            "latest": _latency_probe(authority.read.latest_committed_version),
            "progress": _latency_probe(lambda: authority.read.contributor_progress("learner_000")),
        }
        integrity = list(authority.read.integrity_check())
        latest = authority.read.latest_committed_version()
        progress = authority.read.contributor_progress("learner_000")
        ledger = authority.read.token_ledger_summary()
    if warmup_latency is None:
        raise RuntimeError("warm-up latency probe was not captured")
    connection = sqlite3.connect(database)
    try:
        logical = {
            "active_by_contributor_status": _rows(
                connection,
                "SELECT stable_contributor_key, status, COUNT(*) FROM updates "
                "WHERE status IN ('pending','selected') "
                "GROUP BY stable_contributor_key, status",
            ),
            "retired_active": int(
                connection.execute(
                    "SELECT COUNT(*) FROM updates WHERE status IN ('pending','selected') "
                    "AND stable_contributor_key NOT IN "
                    "(SELECT learner_id FROM static_contributor_bindings WHERE status='active')"
                ).fetchone()[0]
            ),
            "current_versions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM global_versions WHERE version=(SELECT MAX(version) "
                    "FROM global_versions)"
                ).fetchone()[0]
            ),
            "prepared_intents": int(
                connection.execute(
                    "SELECT COUNT(*) FROM publication_intents WHERE state='prepared'"
                ).fetchone()[0]
            ),
            "command_records": int(
                connection.execute("SELECT COUNT(*) FROM command_records").fetchone()[0]
            ),
            "hot_archive_batches": int(
                connection.execute("SELECT COUNT(*) FROM archive_batches").fetchone()[0]
            ),
            "gc_candidates": int(
                connection.execute("SELECT COUNT(*) FROM gc_candidates").fetchone()[0]
            ),
        }
    finally:
        connection.close()
    stable_samples = [item for item in samples if int(item["cycle"]) >= warmup]
    page_slope = _block_bootstrap_upper(
        [(int(item["cycle"]), float(item["live_pages"])) for item in stable_samples],
        block_samples=max(4, min(10, len(stable_samples) // 4)),
    )
    file_slope = _block_bootstrap_upper(
        [(int(item["cycle"]), float(item["active_recovery_files"])) for item in stable_samples],
        block_samples=max(4, min(10, len(stable_samples) // 4)),
    )
    errors: list[str] = []
    if integrity != ["ok"]:
        errors.append(f"integrity_check={integrity}")
    if latest is None or latest.version != cycles or progress.last_cycle_seq != cycles:
        errors.append("final version/contributor cursor did not reach the requested cycle count")
    if any(int(row[2]) > 1 for row in logical["active_by_contributor_status"]):
        errors.append("a contributor has more than one pending or selected proposal")
    if sum(int(row[2]) for row in logical["active_by_contributor_status"]) > 2:
        errors.append("active proposal total exceeds 2M")
    if logical["retired_active"] != 0 or logical["current_versions"] != 1:
        errors.append("retired/current authority logical bound failed")
    if logical["prepared_intents"] != 0 or logical["gc_candidates"] != 0:
        errors.append("quiescent maintenance left prepared intent or GC candidates")
    if ledger.balance != 0 or ledger.direct_outstanding != 0:
        errors.append("token ledger is not terminally balanced at the maintenance boundary")
    if formal and float(page_slope["one_sided_95_upper"]) >= 0.01:
        errors.append("live-page slope one-sided 95% upper is not below 0.01 page/cycle")
    if formal and abs(float(file_slope["one_sided_95_upper"])) >= 0.01:
        errors.append("active/recovery file slope is not approximately zero")
    for name in ("latest", "progress"):
        baseline = warmup_latency[name]["median_seconds"]
        observed = final_latency[name]["median_seconds"]
        if observed > max(baseline * 3.0, baseline + 0.001):
            errors.append(f"{name} recovery read latency grew materially with audit history")
    final_active, final_audit, final_checkpoints, final_updates = _count_files(run_root)
    return {
        "artifact_version": 1,
        "plan_id": PLAN_ID,
        "phase_id": "P6-acceptance-final-review",
        "gate": ("G6-10000-cycle-boundedness" if formal else "G6-boundedness-calibration"),
        "status": ("PASS" if not errors else "BLOCKED") if formal else "REVIEW",
        "requirements_covered": REQUIREMENTS if formal else [],
        "source_identity": source,
        "identity": {
            "git_dirty": source["git_dirty"],
            "authority_schema_version": 9,
            "protocol_version": 4,
            "run_id": identity.run_id,
        },
        "environment": {
            "pbs_job_id": __import__("os").environ.get("PBS_JOBID"),
            "run_root": str(run_root),
        },
        "metrics": {
            "cycles": cycles,
            "warmup_cycle": warmup,
            "sample_every": sample_every,
            "elapsed_seconds": time.monotonic() - started,
            "estimated_10000_cycle_seconds": ((time.monotonic() - started) * 10_000.0 / cycles),
            "live_page_slope": page_slope,
            "active_file_slope": file_slope,
            "latency_at_warmup": warmup_latency,
            "latency_at_end": final_latency,
            "logical_bounds": logical,
            "token_ledger": {
                "adjudicated_processed": ledger.adjudicated_processed,
                "direct_applied": ledger.direct_applied,
                "balance": ledger.balance,
            },
            "final_files": {
                "active_recovery": final_active,
                "audit": final_audit,
                "checkpoint": final_checkpoints,
                "update_payload": final_updates,
            },
            "telemetry_event_counts": telemetry.counts,
            "samples": samples,
        },
        "errors": errors,
        "evidence_paths": [str(database), str(run_root / "audit")],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=10_000)
    parser.add_argument("--sample-every", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=2_000)
    parser.add_argument("--delete-run-on-pass", action="store_true")
    parser.add_argument("--calibration", action="store_true")
    args = parser.parse_args()
    payload = run_gate(
        project_root=args.project_root.resolve(),
        run_root=args.run_root.resolve(),
        cycles=args.cycles,
        sample_every=args.sample_every,
        warmup=args.warmup,
        formal=not args.calibration,
    )
    _atomic_write_json(args.output.resolve(), payload)
    print(payload["status"])
    if payload["status"] not in {"PASS", "REVIEW"}:
        raise SystemExit(1)
    if args.delete_run_on_pass:
        shutil.rmtree(args.run_root.resolve())


if __name__ == "__main__":
    main()
