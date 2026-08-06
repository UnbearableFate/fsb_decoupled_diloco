"""Run the Phase 1 matched candidate-observer and checkpoint-publish gates."""

from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import torch

from ..core.config import Config
from ..core.run_descriptor import LoadedRunDescriptor, load_run_descriptor
from ..modeling.hf_model import load_causal_lm_and_tokenizer
from ..modeling.outer_optim import init_outer_state
from ..modeling.param_index import build_param_index, flatten_trainable_params
from ..observability.phase1_performance import (
    BUSINESS_TRANSACTION_BATCH_SIZE,
    BUSINESS_TRANSACTION_MAX_P99_RATIO,
    BUSINESS_TRANSACTION_MIN_SAMPLES,
    BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
    CHECKPOINT_PUBLISH_MAX_P99_RATIO,
    CHECKPOINT_PUBLISH_MIN_SAMPLES,
    CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
    MATCHED_PERFORMANCE_FORMAT_VERSION,
    matched_p99_limit,
    nearest_rank_percentile,
)
from ..runtime.syncer import (
    align_state_to_publication_dtype,
    publish_global,
    syncer_compute_dtype,
)
from ..storage.atomic_io import atomic_write_json
from ..storage.fenced_store import FencedSQLiteStore
from ..storage.leader_lease import LeaderLeaseStore, LeaseSafetyTracker
from ..storage.paths import RunPaths, prepare_authority_dirs, prepare_run_dirs
from ..storage.schema_bootstrap import BootstrapIdentity, initialize_new_run
from ..storage.sqlite_store import SQLiteStore


def _business_batch_schedule(
    *,
    samples_per_mode: int,
    batch_size: int = BUSINESS_TRANSACTION_BATCH_SIZE,
) -> tuple[bool, ...]:
    """Return fine-grained AB/BA pairs without favoring either temporal position."""

    if samples_per_mode <= 0 or batch_size <= 0:
        raise ValueError("matched business sample and batch sizes must be positive")
    if samples_per_mode % batch_size != 0:
        raise ValueError("matched business samples must divide evenly into batches")
    schedule: list[bool] = []
    for pair in range(samples_per_mode // batch_size):
        schedule.extend((False, True) if pair % 2 == 0 else (True, False))
    return tuple(schedule)


def _is_writer_transaction_statement(statement: str) -> bool:
    normalized = " ".join(str(statement).strip().upper().split())
    return normalized.startswith(("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"))


def _benchmark_identity(loaded: LoadedRunDescriptor, run_id: str) -> BootstrapIdentity:
    return BootstrapIdentity(
        run_id=run_id,
        source_fingerprint=loaded.identity.source_fingerprint,
        config_sha256=loaded.identity.config_sha256,
        mode="full",
    )


def _business_transaction_samples(
    root: Path,
    *,
    loaded: LoadedRunDescriptor,
) -> tuple[list[float], list[float], int, int, list[dict[str, Any]]]:
    paths = RunPaths(root / "business")
    prepare_authority_dirs(paths)
    identity = _benchmark_identity(loaded, "phase1-matched-business")
    initialize_new_run(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
    )
    ha = loaded.config.coordination.syncer_ha
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=max(120.0, float(ha.lease_duration_seconds)),
        max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
        busy_timeout_ms=int(ha.lease_busy_timeout_ms),
    )
    token = lease.acquire(owner_id="matched-leader", hostname="matched-host", pid=1)
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=max(120.0, float(ha.lease_duration_seconds)),
        max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
    )
    fenced = FencedSQLiteStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
        busy_timeout_ms=int(ha.business_busy_timeout_ms),
        lease_safety_check=tracker.assert_safe,
    )
    store = fenced.bind(token)
    baseline: list[float] = []
    observer: list[float] = []
    observation_count = 0
    observer_active = threading.Event()
    observer_idle = threading.Event()
    observer_idle.set()
    observer_started = threading.Event()
    observer_stop = threading.Event()
    observation_condition = threading.Condition()
    observation_errors: list[BaseException] = []
    writer_transaction_statements: list[str] = []
    block_evidence: list[dict[str, Any]] = []

    def observe() -> None:
        nonlocal observation_count
        candidate = LeaderLeaseStore(
            paths.sqlite_db,
            identity,
            marker_path=paths.bootstrap_complete_json,
            lease_duration_seconds=max(120.0, float(ha.lease_duration_seconds)),
            max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
            busy_timeout_ms=int(ha.lease_busy_timeout_ms),
        )

        def trace(statement: str) -> None:
            if _is_writer_transaction_statement(statement):
                writer_transaction_statements.append(statement)

        candidate.conn.set_trace_callback(trace)
        observer_started.set()
        try:
            while not observer_stop.is_set():
                if not observer_active.wait(timeout=0.01):
                    observer_idle.set()
                    continue
                if observer_stop.is_set():
                    break
                observer_idle.clear()
                candidate.terminal_state()
                observed = candidate.observe()
                if observed is None or observed["state"] != "active":
                    raise RuntimeError("matched observer lost the healthy active leader")
                with observation_condition:
                    observation_count += 1
                    observation_condition.notify_all()
                observer_stop.wait(max(0.001, float(ha.candidate_acquire_poll_seconds)))
        except BaseException as exc:
            observation_errors.append(exc)
            with observation_condition:
                observation_condition.notify_all()
        finally:
            observer_idle.set()
            candidate.close()

    def assert_observer_healthy() -> None:
        if observation_errors:
            raise RuntimeError("matched candidate observer failed") from observation_errors[0]

    def pause_observer() -> None:
        observer_active.clear()
        if not observer_idle.wait(timeout=5.0):
            raise TimeoutError("matched candidate observer did not become idle")
        assert_observer_healthy()

    def activate_observer() -> int:
        before = observation_count
        observer_idle.clear()
        observer_active.set()
        deadline = time.monotonic() + 5.0
        with observation_condition:
            while observation_count <= before and not observation_errors:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise TimeoutError("matched candidate observer did not complete an observation")
                observation_condition.wait(timeout=remaining)
        assert_observer_healthy()
        return before

    def run_batch(*, with_observer: bool, batch: int, samples: int) -> list[float]:
        if with_observer:
            observations_before = activate_observer()
        else:
            pause_observer()
            observations_before = observation_count
        before = store.business_transaction_metrics()["business_transaction_captured_count"]
        for index in range(samples):
            store.set_run_state("matched-business", {"batch": batch, "index": index})
        recorded = store.business_transaction_metrics()["business_transaction_seconds"]
        measured = [float(value) for value in recorded[int(before) :]]
        pause_observer()
        observations_after = observation_count
        block_observations = observations_after - observations_before
        if with_observer and block_observations <= 0:
            raise RuntimeError("matched observer block completed without an observation")
        if not with_observer and block_observations != 0:
            raise RuntimeError("matched baseline block overlapped a candidate observation")
        if len(measured) != samples:
            raise RuntimeError(f"business sample mismatch: {len(measured)} != {samples}")
        block_evidence.append(
            {
                "batch": batch,
                "mode": "observer" if with_observer else "baseline",
                "sample_count": len(measured),
                "candidate_observation_count": block_observations,
            }
        )
        return measured

    observer_thread = threading.Thread(target=observe, name="phase1-matched-observer")
    observer_thread.start()
    try:
        if not observer_started.wait(timeout=5.0):
            raise TimeoutError("matched candidate observer did not start")
        assert_observer_healthy()
        for index in range(20):
            store.set_run_state("matched-warmup", index)
        schedule = _business_batch_schedule(samples_per_mode=BUSINESS_TRANSACTION_MIN_SAMPLES)
        for batch, with_observer in enumerate(schedule):
            measured = run_batch(
                with_observer=with_observer,
                batch=batch,
                samples=BUSINESS_TRANSACTION_BATCH_SIZE,
            )
            (observer if with_observer else baseline).extend(measured)
    finally:
        observer_stop.set()
        observer_active.set()
        observer_thread.join(timeout=5.0)
        if observer_thread.is_alive():
            raise TimeoutError("matched candidate observer did not stop")
        store.close()
        lease.release(token)
        lease.close()
    assert_observer_healthy()
    return (
        baseline,
        observer,
        observation_count,
        len(writer_transaction_statements),
        block_evidence,
    )


def _publication_config(
    loaded: LoadedRunDescriptor,
    *,
    run_id: str,
    shared_root: Path,
    ha_enabled: bool,
) -> Config:
    config = copy.deepcopy(loaded.config)
    config.run.run_id = run_id
    config.run.shared_root = str(shared_root)
    config.coordination.syncer_ha.enabled = ha_enabled
    config.io.checkpoint_digest_mode = "off"
    return config


def _publish_sample(
    root: Path,
    *,
    loaded: LoadedRunDescriptor,
    sample: int,
    ha_enabled: bool,
    theta: torch.Tensor,
    outer_state: dict[str, torch.Tensor],
    param_index: dict[str, Any],
) -> float:
    label = "ha" if ha_enabled else "plan01_legacy"
    paths = RunPaths(root / f"publish_{sample:03d}_{label}")
    config = _publication_config(
        loaded,
        run_id=f"phase1-matched-{label}-{sample}",
        shared_root=paths.shared_root,
        ha_enabled=ha_enabled,
    )
    store: Any
    lease: LeaderLeaseStore | None = None
    if ha_enabled:
        prepare_authority_dirs(paths)
        identity = _benchmark_identity(loaded, str(config.run.run_id))
        initialize_new_run(
            paths.sqlite_db,
            identity,
            marker_path=paths.bootstrap_complete_json,
        )
        ha = config.coordination.syncer_ha
        lease = LeaderLeaseStore(
            paths.sqlite_db,
            identity,
            marker_path=paths.bootstrap_complete_json,
            lease_duration_seconds=max(30.0, float(ha.lease_duration_seconds)),
            max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
        )
        token = lease.acquire(owner_id=f"matched-{sample}", hostname="matched-host", pid=1)
        tracker = LeaseSafetyTracker(
            token,
            lease_duration_seconds=max(30.0, float(ha.lease_duration_seconds)),
            max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
        )
        fenced = FencedSQLiteStore(
            paths.sqlite_db,
            identity,
            marker_path=paths.bootstrap_complete_json,
            max_clock_skew_seconds=float(ha.max_clock_skew_seconds),
            lease_safety_check=tracker.assert_safe,
        )
        store = fenced.bind(token)
    else:
        prepare_run_dirs(paths, 1)
        store = SQLiteStore(paths.sqlite_db)
        token = None
    try:
        publication = publish_global(
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
        return float(publication["publish_checkpoint_seconds"])
    finally:
        store.close()
        if lease is not None and token is not None:
            lease.release(token)
            lease.close()


def _checkpoint_publish_samples(
    root: Path,
    *,
    loaded: LoadedRunDescriptor,
) -> tuple[list[float], list[float], int]:
    torch.manual_seed(int(loaded.config.training.seed))
    model, _tokenizer = load_causal_lm_and_tokenizer(loaded.config.model)
    model.to("cpu")
    param_index = build_param_index(
        model,
        model_name_or_path=loaded.config.model.name_or_path,
    )
    theta = flatten_trainable_params(
        model,
        param_index,
        dtype=syncer_compute_dtype(loaded.config),
        device="cpu",
    )
    outer_state = init_outer_state(theta, loaded.config.outer_optimizer)
    theta, outer_state = align_state_to_publication_dtype(
        loaded.config,
        theta,
        outer_state,
    )
    baseline: list[float] = []
    ha_samples: list[float] = []
    for sample in range(CHECKPOINT_PUBLISH_MIN_SAMPLES):
        order = (False, True) if sample % 2 == 0 else (True, False)
        for ha_enabled in order:
            measured = _publish_sample(
                root,
                loaded=loaded,
                sample=sample,
                ha_enabled=ha_enabled,
                theta=theta,
                outer_state=outer_state,
                param_index=param_index,
            )
            (ha_samples if ha_enabled else baseline).append(measured)
    return baseline, ha_samples, int(theta.numel())


def run_matched_performance(run_root: Path) -> dict[str, Any]:
    loaded = load_run_descriptor(run_root.resolve())
    if loaded.descriptor.get("git_dirty") is not False:
        raise RuntimeError("formal matched performance requires a clean target descriptor")
    torch.set_num_threads(1)
    with TemporaryDirectory(
        prefix="plan02-phase1-matched-",
        dir=run_root.resolve().parent,
    ) as directory:
        benchmark_root = Path(directory)
        (
            business_baseline,
            business_observer,
            observation_count,
            candidate_writer_transaction_attempt_count,
            business_blocks,
        ) = _business_transaction_samples(benchmark_root, loaded=loaded)
        checkpoint_baseline, checkpoint_ha, checkpoint_tensor_numel = _checkpoint_publish_samples(
            benchmark_root,
            loaded=loaded,
        )

    business_baseline_p99 = nearest_rank_percentile(business_baseline, 0.99)
    business_observer_p99 = nearest_rank_percentile(business_observer, 0.99)
    business_limit = matched_p99_limit(
        business_baseline_p99,
        max_ratio=BUSINESS_TRANSACTION_MAX_P99_RATIO,
        jitter_seconds=BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
    )
    checkpoint_baseline_p99 = nearest_rank_percentile(checkpoint_baseline, 0.99)
    checkpoint_ha_p99 = nearest_rank_percentile(checkpoint_ha, 0.99)
    checkpoint_limit = matched_p99_limit(
        checkpoint_baseline_p99,
        max_ratio=CHECKPOINT_PUBLISH_MAX_P99_RATIO,
        jitter_seconds=CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
    )
    passed = (
        len(business_baseline) >= BUSINESS_TRANSACTION_MIN_SAMPLES
        and len(business_observer) >= BUSINESS_TRANSACTION_MIN_SAMPLES
        and observation_count > 0
        and candidate_writer_transaction_attempt_count == 0
        and business_observer_p99 <= business_limit
        and len(checkpoint_baseline) >= CHECKPOINT_PUBLISH_MIN_SAMPLES
        and len(checkpoint_ha) >= CHECKPOINT_PUBLISH_MIN_SAMPLES
        and checkpoint_ha_p99 <= checkpoint_limit
    )
    return {
        "checker": "plan02_phase1_matched_performance",
        "format_version": MATCHED_PERFORMANCE_FORMAT_VERSION,
        "status": "PASS" if passed else "BLOCKED",
        "checked_at": time.time(),
        "identity": {
            "run_id": loaded.identity.run_id,
            "descriptor_sha256": loaded.descriptor["descriptor_sha256"],
            "git_commit": loaded.descriptor["git_commit"],
            "git_dirty": loaded.descriptor["git_dirty"],
            "source_fingerprint": loaded.identity.source_fingerprint,
            "config_sha256": loaded.identity.config_sha256,
        },
        "business_candidate_observer": {
            "metric": "fenced business transaction seconds (sqlite commit boundary)",
            "aggregation": "nearest-rank p99 over interleaved matched batches",
            "baseline_sample_count": len(business_baseline),
            "observer_sample_count": len(business_observer),
            "baseline_p99_seconds": business_baseline_p99,
            "observer_p99_seconds": business_observer_p99,
            "max_p99_ratio": BUSINESS_TRANSACTION_MAX_P99_RATIO,
            "jitter_seconds": BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
            "allowed_observer_p99_seconds": business_limit,
            "candidate_observation_count": observation_count,
            "candidate_writer_transaction_attempt_count": (
                candidate_writer_transaction_attempt_count
            ),
            "candidate_writer_transaction_instrumentation": (
                "sqlite trace count of BEGIN IMMEDIATE/EXCLUSIVE"
            ),
            "batch_size": BUSINESS_TRANSACTION_BATCH_SIZE,
            "blocks": business_blocks,
        },
        "checkpoint_publish": {
            "baseline_contract": "Plan 01 legacy SQLiteStore publication",
            "matched_fields": "source/config/model/seed/tensor/dtype/filesystem",
            "tensor_numel": checkpoint_tensor_numel,
            "publish_dtype": loaded.config.syncer.publish_dtype,
            "aggregation": "nearest-rank p99 over alternating matched samples",
            "baseline_sample_count": len(checkpoint_baseline),
            "ha_sample_count": len(checkpoint_ha),
            "baseline_p99_seconds": checkpoint_baseline_p99,
            "ha_p99_seconds": checkpoint_ha_p99,
            "max_p99_ratio": CHECKPOINT_PUBLISH_MAX_P99_RATIO,
            "jitter_seconds": CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
            "allowed_ha_p99_seconds": checkpoint_limit,
            "digest_mode": "off",
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_matched_performance(args.run_root)
    atomic_write_json(args.output, report)
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
