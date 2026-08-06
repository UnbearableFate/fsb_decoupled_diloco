from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.core.constants import (
    CONTROL_EPOCH_FORMAT_VERSION,
    HA_SCHEMA_VERSION,
    SYNCER_HEARTBEAT_FORMAT_VERSION,
)
from fs_diloco.protocol.control_epoch import EpochControlPublisher, EpochControlReader
from fs_diloco.runtime import pbs_scheduler as pbs_scheduler_module
from fs_diloco.runtime import syncer as syncer_runtime
from fs_diloco.runtime.launch_outbox import RecoveryClaimManager, recovery_observation_key
from fs_diloco.runtime.learner import (
    close_epoch_control_reader,
    read_authoritative_terminal,
)
from fs_diloco.runtime.pbs_scheduler import PBSJobObservation, PBSScheduler
from fs_diloco.runtime.syncer_ha import acquire_candidate, open_leader_store
from fs_diloco.tools.launch_independent_run import _walltime_resource, launch
from fs_diloco.tools.init_run import initialize_run as initialize_ha_run
from fs_diloco.storage.fenced_store import FencedSQLiteStore, ReadOnlySQLiteStore
from fs_diloco.storage.leader_lease import (
    LeaderLeaseStore,
    LeaseSafetyTracker,
    LeaseUnavailableError,
    StaleLeaderTokenError,
)
from fs_diloco.storage.paths import (
    RunPaths,
    prepare_authority_dirs,
    prepare_learner_instance_dir,
)
from fs_diloco.storage import maintenance as maintenance_module
from fs_diloco.storage.maintenance import archive_ha_history, collect_runtime_artifacts
from fs_diloco.storage.schema_bootstrap import (
    BootstrapIdentity,
    initialize_new_run,
    open_existing,
    open_readonly,
)
from fs_diloco.storage.sqlite_store import SQLiteStore


def identity() -> BootstrapIdentity:
    return BootstrapIdentity(
        run_id="ha-test",
        source_fingerprint="sha256:source",
        config_sha256="config-digest",
        mode="full",
    )


def bootstrapped(tmp_path: Path) -> RunPaths:
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    initialize_new_run(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
    )
    return paths


def acquire(paths: RunPaths, owner: str, *, wall_clock=lambda: 100.0):
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        wall_clock=wall_clock,
    )
    token = lease.acquire(owner_id=owner, hostname="host", pid=1)
    return lease, token


def fenced(
    paths: RunPaths,
    token,
    *,
    wall_clock=lambda: 100.0,
    lease_duration_seconds: float = 90.0,
    max_clock_skew_seconds: float = 2.0,
) -> FencedSQLiteStore:
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=lease_duration_seconds,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    return FencedSQLiteStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=max_clock_skew_seconds,
        wall_clock=wall_clock,
        lease_safety_check=tracker.assert_safe,
    )


def test_ha_config_defaults_and_artifact_versions(tmp_path: Path) -> None:
    config = resolve_config(project_root=tmp_path)
    assert not config.coordination.syncer_ha.enabled
    assert not config.coordination.recovery_submission.enabled
    assert config.io.checkpoint_digest_mode == "off"
    assert HA_SCHEMA_VERSION == 2
    assert SYNCER_HEARTBEAT_FORMAT_VERSION == 1
    assert CONTROL_EPOCH_FORMAT_VERSION == 1


def test_independent_launcher_requires_explicit_short_walltime_for_submit() -> None:
    assert _walltime_resource("00:02:00", required=True) == [
        "-l",
        "walltime=00:02:00",
    ]
    with pytest.raises(ValueError, match="requires an estimated"):
        _walltime_resource(None, required=True)
    with pytest.raises(ValueError, match="invalid PBS walltime"):
        _walltime_resource("24 hours", required=True)


def test_independent_launcher_validates_walltime_before_creating_run(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires an estimated"):
        launch(
            config_path=tmp_path / "missing.yaml",
            run_id="must-not-exist",
            shared_root=str(tmp_path / "must-not-exist"),
            project_root=tmp_path,
            submit=True,
            allow_dirty_snapshot=False,
        )
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            "coordination:\n  syncer_ha:\n    renew_interval_seconds: 0\n",
            "renew_interval_seconds",
        ),
        (
            "coordination:\n  syncer_ha:\n    enabled: true\nfragments:\n  enabled: true\n",
            "not supported with fragments",
        ),
        (
            "coordination:\n  recovery_submission:\n    enabled: true\n",
            "requires coordination.syncer_ha.enabled",
        ),
        (
            "coordination:\n  syncer_ha:\n    enabled: true\n  recovery_submission:\n    enabled: true\n",
            "candidate_walltime",
        ),
        ("io:\n  checkpoint_digest_mode: invalid\n", "checkpoint_digest_mode"),
    ],
)
def test_ha_config_rejects_invalid_combinations(tmp_path: Path, payload: str, match: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        resolve_config(path, project_root=tmp_path)


def test_schema_bootstrap_has_double_version_identity_and_readonly_open(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    marker = json.loads(paths.bootstrap_complete_json.read_text(encoding="utf-8"))
    assert marker["schema_version"] == HA_SCHEMA_VERSION
    conn = open_existing(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == HA_SCHEMA_VERSION
    assert (
        conn.execute("SELECT schema_version FROM schema_meta WHERE singleton = 1").fetchone()[0]
        == HA_SCHEMA_VERSION
    )
    assert (
        json.loads(
            conn.execute("SELECT value FROM run_state WHERE key='schema_version'").fetchone()[0]
        )
        == HA_SCHEMA_VERSION
    )
    conn.close()
    before = paths.sqlite_db.stat().st_mtime_ns
    readonly = open_readonly(paths.sqlite_db)
    assert readonly.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        readonly.execute("INSERT INTO run_state VALUES ('bad', 'bad', 0)")
    readonly.close()
    assert paths.sqlite_db.stat().st_mtime_ns == before


def test_ha_initializer_writes_identical_root_and_control_config(
    tmp_path: Path,
) -> None:
    shared_root = tmp_path / "initialized-run"
    config = resolve_config(
        project_root=tmp_path,
        run_id="initialized-run",
        shared_root=str(shared_root),
    )
    config.coordination.syncer_ha.enabled = True
    config.run.git_commit = "a" * 40
    config.run.git_dirty = False
    config.run.source_fingerprint = "sha256:source"
    initialize_ha_run(config, project_root=tmp_path)
    paths = RunPaths(shared_root)
    assert paths.resolved_config_yaml.read_bytes() == paths.run_root_config_yaml.read_bytes()
    with pytest.raises(FileExistsError, match="run root already exists"):
        initialize_ha_run(config, project_root=tmp_path)


def test_incomplete_or_pre_ha_database_fails_closed(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "run")
    paths.control.mkdir(parents=True)
    SQLiteStore(paths.sqlite_db).close()
    with pytest.raises(FileNotFoundError):
        open_existing(
            paths.sqlite_db,
            identity(),
            marker_path=paths.bootstrap_complete_json,
        )
    paths.bootstrap_complete_json.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="marker identity mismatch"):
        open_existing(
            paths.sqlite_db,
            identity(),
            marker_path=paths.bootstrap_complete_json,
        )


def test_lease_epoch_is_monotonic_and_stale_owner_cannot_renew(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    now = [100.0]
    first, token1 = acquire(paths, "owner-1", wall_clock=lambda: now[0])
    contender = LeaderLeaseStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: now[0],
    )
    with pytest.raises(LeaseUnavailableError):
        contender.acquire(owner_id="owner-2", hostname="host", pid=2)
    now[0] = 193.0
    token2 = contender.acquire(owner_id="owner-2", hostname="host", pid=2)
    assert token2.epoch == token1.epoch + 1
    with pytest.raises(StaleLeaderTokenError):
        first.renew(token1)
    contender.release(token2)
    token3 = first.acquire(owner_id="owner-3", hostname="host", pid=3)
    assert token3.epoch == token2.epoch + 1
    first.release(token3)
    first.close()
    contender.close()


def test_concurrent_first_acquire_has_exactly_one_winner(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    barrier = threading.Barrier(8)
    results: list[tuple[str, int | None]] = []
    lock = threading.Lock()

    def candidate(index: int) -> None:
        store = LeaderLeaseStore(
            paths.sqlite_db,
            identity(),
            marker_path=paths.bootstrap_complete_json,
            lease_duration_seconds=90.0,
            max_clock_skew_seconds=2.0,
            wall_clock=lambda: 100.0,
        )
        barrier.wait()
        try:
            token = store.acquire(owner_id=f"owner-{index}", hostname="host", pid=index)
            outcome = ("winner", token.epoch)
        except LeaseUnavailableError:
            outcome = ("loser", None)
        finally:
            store.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=candidate, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(("winner", 1)) == 1
    assert sum(state == "loser" for state, _epoch in results) == 7


def test_all_fenced_public_mutators_require_token() -> None:
    names = {
        "set_run_state",
        "upsert_global_version",
        "initialize_full_run",
        "commit_full_merge",
        "upsert_learner",
        "update_learner_status",
        "insert_update_metadata",
        "mark_updates_selected",
        "mark_updates_applied",
        "reset_selected_to_pending",
        "reset_all_selected_to_pending",
        "prepare_full_resume",
        "drop_updates",
        "drop_obsolete_updates",
        "drop_ineligible_updates",
        "finalize_unconsumed_updates",
        "drop_superseded_updates",
        "delete_archived_rows",
        "clear_gc_pending_paths",
        "record_control_publication",
        "set_controller_state",
        "finalize_terminal_state",
    }
    for name in names:
        parameters = list(inspect.signature(getattr(FencedSQLiteStore, name)).parameters)
        assert parameters[:2] == ["self", "token"], name
    for fragment_mutator in (
        "upsert_fragment_definition",
        "upsert_fragment_version",
        "insert_fragment_update_metadata",
    ):
        assert not hasattr(FencedSQLiteStore, fragment_mutator)


def test_fenced_store_rejects_raw_and_superseded_writes(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    first, token1 = acquire(paths, "owner-1")
    store1 = fenced(paths, token1)
    store1.set_run_state(token1, "first", 1)
    with pytest.raises(AttributeError):
        store1.execute("UPDATE run_state SET value='bad'")
    with pytest.raises(AttributeError):
        _ = store1.conn
    first.release(token1)
    second, token2 = acquire(paths, "owner-2")
    with pytest.raises(StaleLeaderTokenError):
        store1.set_run_state(token1, "stale", True)
    store2 = fenced(paths, token2)
    store2.set_run_state(token2, "second", 2)
    assert store2.get_run_state("second") == 2
    store1.close()
    store2.close()
    second.release(token2)
    first.close()
    second.close()


def test_fenced_store_enforces_local_monotonic_lease_boundary(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    monotonic_now = [100.0]
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        monotonic_clock=lambda: monotonic_now[0],
    )
    store = FencedSQLiteStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
        lease_safety_check=tracker.assert_safe,
    )
    store.set_run_state(token, "before-boundary", True)
    monotonic_now[0] = 189.0
    with pytest.raises(StaleLeaderTokenError, match="monotonic safety boundary"):
        store.set_run_state(token, "after-boundary", True)
    assert store.get_run_state("after-boundary") is None
    tracker.mark_renewed(token)
    store.set_run_state(token, "after-renew", True)
    store.close()
    lease.release(token)
    lease.close()


def test_fenced_named_parameter_mutation_preserves_metadata_values(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token)
    metadata = {
        "update_id": "update-1",
        "learner_id": "learner_000",
        "hostname": "host",
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": 2,
        "inner_steps": 2,
        "tokens_this_update": 32,
        "tokens_since_global_load": 32,
        "num_examples_this_update": 2,
        "train_loss": 1.25,
        "grad_norm": 0.5,
        "param_norm": 2.0,
        "delta_norm": 0.25,
        "training_cpu_utilization_peak_percent": 12.5,
        "training_gpu_utilization_peak_percent": 34.5,
        "file_path": str(paths.shared_root / "update.safetensors"),
        "file_size_bytes": 10,
        "sha256": None,
        "created_at": 100.0,
        "committed_at": 101.0,
    }
    assert store.insert_update_metadata(token, metadata)
    row = store.get_update("update-1")
    assert row is not None
    assert row["learner_id"] == "learner_000"
    assert row["training_cpu_utilization_peak_percent"] == 12.5
    assert row["training_gpu_utilization_peak_percent"] == 34.5
    store.close()
    lease.release(token)
    lease.close()


def test_epoch_control_ignores_fixed_cache_pollution_and_repairs_takeover(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    first, token1 = acquire(paths, "owner-1")
    store1 = fenced(paths, token1)
    weight1 = paths.epoch_weight_path(1, token1.owner_id, 0, "pub-one")
    optim1 = paths.epoch_outer_optim_path(1, token1.owner_id, 0, "pub-one")
    weight1.parent.mkdir(parents=True)
    optim1.parent.mkdir(parents=True)
    weight1.write_bytes(b"weight")
    optim1.write_bytes(b"optim")
    row = store1.initialize_full_run(
        token1,
        publication_id="pub-one",
        weight_size_bytes=weight1.stat().st_size,
        optim_size_bytes=optim1.stat().st_size,
        weight_path=paths.relative(weight1),
        optim_path=paths.relative(optim1),
        outer_optimizer="nesterov",
        identity={"run_id": "ha-test"},
        config_snapshot={},
    )
    publisher1 = EpochControlPublisher(paths, store1, token1)
    publisher1.publish_latest(row)
    publisher1.publish_heartbeat(first.observe())
    paths.latest_json.write_text('{"epoch": 999, "version": 999}\n', encoding="utf-8")
    reader = EpochControlReader(paths, run_id="ha-test")
    assert not hasattr(reader, "store")
    assert reader.read_current_latest()["version"] == 0
    first.release(token1)
    second, token2 = acquire(paths, "owner-2")
    store2 = fenced(paths, token2)
    publisher2 = EpochControlPublisher(paths, store2, token2)
    publisher2.publish_heartbeat(second.observe())
    assert reader.read_current_latest() is None
    repaired = publisher2.repair_latest_from_db()
    assert repaired is not None and repaired["source_commit_epoch"] == 1
    assert publisher2.repair_latest_from_db() == repaired
    publisher2.publish_heartbeat(second.observe())
    latest = reader.read_current_latest()
    assert latest is not None and latest["epoch"] == 2 and latest["version"] == 0
    error_terminal = store2.finalize_terminal_state(
        token2,
        generation=1,
        stop_reason="error",
        final_version=0,
        total_seen_tokens=0,
        finalized_at=150.0,
    )
    publisher2.publish_terminal(error_terminal)
    assert reader.read_current_terminal()["stop_reason"] == "error"
    assert read_authoritative_terminal(paths, run_id="ha-test") is None

    terminal = store2.finalize_terminal_state(
        token2,
        generation=2,
        stop_reason="completed",
        final_version=0,
        total_seen_tokens=0,
        finalized_at=200.0,
    )
    publisher2.publish_terminal(
        terminal,
        summary={"final_version": 0, "stop_reason": "completed"},
    )
    canonical_summary = paths.epoch_summary_path(2, token2.owner_id, 2).read_bytes()
    paths.stop_json.write_text(
        '{"epoch": 1, "owner_id": "owner-1", "reason": "polluted"}\n',
        encoding="utf-8",
    )
    paths.summary_json.write_text(
        '{"epoch": 1, "owner_id": "owner-1", "stop_reason": "polluted"}\n',
        encoding="utf-8",
    )
    authoritative_terminal = reader.read_current_terminal()
    assert authoritative_terminal is not None
    assert authoritative_terminal["epoch"] == 2
    assert authoritative_terminal["stop_reason"] == "completed"
    assert read_authoritative_terminal(paths, run_id="ha-test")["stop_reason"] == "completed"
    canonical_stop_path = paths.epoch_stop_path(2, token2.owner_id, 2)
    canonical_stop = canonical_stop_path.read_bytes()
    corrupted_stop = json.loads(canonical_stop)
    corrupted_stop["stop_reason"] = "polluted"
    canonical_stop_path.write_text(json.dumps(corrupted_stop) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="terminal checksum mismatch"):
        reader.read_current_terminal()
    canonical_stop_path.write_bytes(canonical_stop)
    assert paths.epoch_summary_path(2, token2.owner_id, 2).read_bytes() == canonical_summary
    close_epoch_control_reader(paths)
    reader.close()
    store1.close()
    store2.close()
    second.release(token2)
    first.close()
    second.close()


def test_incomplete_completed_terminal_is_repaired_before_future_rejection(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    config = resolve_config(project_root=tmp_path)
    config.run.run_id = "ha-test"
    config.run.shared_root = str(paths.shared_root)
    config.coordination.syncer_ha.enabled = True

    first, token1 = acquire(paths, "owner-1")
    store1 = fenced(paths, token1)
    weight = paths.epoch_weight_path(1, token1.owner_id, 0, "initial")
    optim = paths.epoch_outer_optim_path(1, token1.owner_id, 0, "initial")
    weight.parent.mkdir(parents=True)
    optim.parent.mkdir(parents=True)
    weight.write_bytes(b"weight")
    optim.write_bytes(b"optim")
    version = store1.initialize_full_run(
        token1,
        publication_id="initial",
        weight_size_bytes=weight.stat().st_size,
        optim_size_bytes=optim.stat().st_size,
        weight_path=paths.relative(weight),
        optim_path=paths.relative(optim),
        outer_optimizer="nesterov",
        identity={"run_id": "ha-test"},
        config_snapshot={},
    )
    publisher1 = EpochControlPublisher(paths, store1, token1)
    publisher1.publish_latest(version)
    publisher1.publish_heartbeat(first.observe())
    interrupted = store1.finalize_terminal_state(
        token1,
        generation=1,
        stop_reason="completed",
        final_version=0,
        total_seen_tokens=0,
    )
    publisher1.publish_terminal(interrupted)
    store1.close()
    first.release(token1)
    first.close()

    lease2, token2, safety2, logger2 = acquire_candidate(
        paths=paths,
        identity=identity(),
        config=config,
        owner_id="owner-2",
    )
    store2 = open_leader_store(
        paths=paths,
        identity=identity(),
        config=config,
        token=token2,
        safety_tracker=safety2,
    )
    repaired = syncer_runtime.repair_completed_ha_terminal(
        paths=paths,
        store=store2,
        lease_store=lease2,
        config=config,
        logger=logger2,
    )
    assert repaired["generation"] == 2
    assert repaired["finalized_by_epoch"] == 2
    terminal = EpochControlReader(paths, run_id="ha-test").read_current_terminal()
    assert terminal is not None
    assert terminal["generation"] == 2
    assert paths.epoch_summary_path(2, token2.owner_id, 2).is_file()
    store2.close()
    lease2.release(token2)
    lease2.close()

    with pytest.raises(RuntimeError, match="candidate cannot acquire a terminal run"):
        acquire_candidate(
            paths=paths,
            identity=identity(),
            config=config,
            owner_id="owner-3",
        )


def test_learner_directory_creation_does_not_create_authority(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "run")
    with pytest.raises(RuntimeError, match="authority directories"):
        prepare_learner_instance_dir(paths, "learner_000")
    prepare_authority_dirs(paths)
    instance = prepare_learner_instance_dir(paths, "learner_000")
    assert instance.is_dir()


class FakeScheduler:
    def __init__(self) -> None:
        self.submissions = 0

    def submit_candidate(self, **_kwargs):
        self.submissions += 1
        return {
            "returncode": 0,
            "job_id_raw": f"{self.submissions}.server",
            "job_id_normalized": str(self.submissions),
        }

    def query(self, job_id: str) -> PBSJobObservation:
        return PBSJobObservation(job_id, "queued", {}, 0, "")


class FinishedScheduler(FakeScheduler):
    def query(self, job_id: str) -> PBSJobObservation:
        return PBSJobObservation(job_id, "finished", {"Exit_status": "0"}, 0, "")


def test_recovery_claim_has_one_mkdir_winner_and_queued_job_stays_outstanding(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    config = resolve_config(project_root=tmp_path).coordination.recovery_submission
    config.enabled = True
    scheduler = FakeScheduler()
    manager = RecoveryClaimManager(
        paths=paths,
        config=config,
        scheduler=scheduler,  # type: ignore[arg-type]
        descriptor_sha256="descriptor",
        wall_clock=lambda: 100.0,
    )
    key = recovery_observation_key(
        run_id="run", highest_epoch=1, heartbeat_seq=2, heartbeat_fingerprint="hb"
    )
    first = manager.maybe_submit(
        observation_key=key, claimant_id="learner-1", terminal_published=False
    )
    second = manager.maybe_submit(
        observation_key=key, claimant_id="learner-2", terminal_published=False
    )
    assert first.state == "submitted"
    assert second.state == "outstanding"
    assert scheduler.submissions == 1


def test_eight_recovery_claimants_create_only_one_attempt_and_submission(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    config = resolve_config(project_root=tmp_path).coordination.recovery_submission
    config.enabled = True
    scheduler = FakeScheduler()
    manager = RecoveryClaimManager(
        paths=paths,
        config=config,
        scheduler=scheduler,  # type: ignore[arg-type]
        descriptor_sha256="descriptor",
    )
    key = recovery_observation_key(
        run_id="run", highest_epoch=1, heartbeat_seq=2, heartbeat_fingerprint="hb"
    )
    barrier = threading.Barrier(8)
    results = []
    result_lock = threading.Lock()

    def claimant(index: int) -> None:
        barrier.wait()
        result = manager.maybe_submit(
            observation_key=key,
            claimant_id=f"learner-{index}",
            terminal_published=False,
        )
        with result_lock:
            results.append(result)

    threads = [threading.Thread(target=claimant, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result.state == "submitted" for result in results) == 1
    assert scheduler.submissions == 1
    attempts = list(paths.syncer_launch_claims.glob("*/attempt_*.lock"))
    assert len(attempts) == 1
    assert (attempts[0] / "claim.json").is_file()
    assert (attempts[0] / "submission.json").is_file()
    other_key = recovery_observation_key(
        run_id="run", highest_epoch=1, heartbeat_seq=3, heartbeat_fingerprint="hb-2"
    )
    other = manager.maybe_submit(
        observation_key=other_key,
        claimant_id="learner-3",
        terminal_published=False,
    )
    assert other.state == "outstanding"
    assert scheduler.submissions == 1


class HistoricalScheduler(FakeScheduler):
    def __init__(self, historical_classification: str) -> None:
        super().__init__()
        self.historical_classification = historical_classification

    def query(self, job_id: str, *, historical: bool = False) -> PBSJobObservation:
        classification = self.historical_classification if historical else "query_failed"
        return PBSJobObservation(job_id, classification, {}, 0, "")


def test_recovery_reconciles_historical_scheduler_state_before_retry(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    config = resolve_config(project_root=tmp_path).coordination.recovery_submission
    config.enabled = True
    config.claim_timeout_seconds = 1.0
    config.backoff_initial_seconds = 1.0
    config.backoff_max_seconds = 1.0
    config.uncertainty_timeout_seconds = 1.0
    now = [100.0]
    running_scheduler = HistoricalScheduler("suspended")
    manager = RecoveryClaimManager(
        paths=paths,
        config=config,
        scheduler=running_scheduler,  # type: ignore[arg-type]
        descriptor_sha256="descriptor",
        wall_clock=lambda: now[0],
    )
    key = recovery_observation_key(
        run_id="run", highest_epoch=1, heartbeat_seq=2, heartbeat_fingerprint="hb"
    )
    assert (
        manager.maybe_submit(
            observation_key=key, claimant_id="learner-1", terminal_published=False
        ).state
        == "submitted"
    )
    now[0] = 1000.0
    assert (
        manager.maybe_submit(
            observation_key=key, claimant_id="learner-2", terminal_published=False
        ).state
        == "outstanding"
    )
    assert running_scheduler.submissions == 1


def test_recovery_archival_does_not_reset_current_observation_budget(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    config = resolve_config(project_root=tmp_path).coordination.recovery_submission
    config.enabled = True
    config.claim_timeout_seconds = 0.25
    config.backoff_initial_seconds = 0.25
    config.backoff_max_seconds = 0.25
    config.uncertainty_timeout_seconds = 0.25
    config.claim_retention_seconds = 0.5
    config.max_attempts_per_observation = 2
    now = [100.0]
    scheduler = FinishedScheduler()
    manager = RecoveryClaimManager(
        paths=paths,
        config=config,
        scheduler=scheduler,  # type: ignore[arg-type]
        descriptor_sha256="descriptor",
        wall_clock=lambda: now[0],
    )
    key = recovery_observation_key(
        run_id="run", highest_epoch=1, heartbeat_seq=2, heartbeat_fingerprint="hb"
    )
    assert (
        manager.maybe_submit(
            observation_key=key, claimant_id="learner-1", terminal_published=False
        ).state
        == "submitted"
    )
    now[0] += 1.0
    assert (
        manager.maybe_submit(
            observation_key=key, claimant_id="learner-2", terminal_published=False
        ).state
        == "submitted"
    )
    now[0] += 1.0
    assert (
        manager.maybe_submit(
            observation_key=key, claimant_id="learner-3", terminal_published=False
        ).state
        == "budget_exhausted"
    )
    assert scheduler.submissions == 2
    assert len(list(paths.syncer_launch_claims.glob("*/attempt_*.lock"))) == 2


def test_pbs_scheduler_failures_are_nonfatal_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("qstat-or-qsub", 0.01)

    monkeypatch.setattr(pbs_scheduler_module.subprocess, "run", timeout)
    scheduler = PBSScheduler(timeout_seconds=0.01)
    observation = scheduler.query("123.server")
    assert observation.classification == "query_failed"
    assert observation.returncode == -1
    submission = scheduler.submit_candidate(
        script="candidate.pbs",
        request_fingerprint="request-1",
        shared_root="/work/example/run",
        descriptor_sha256="descriptor",
        walltime="00:02:00",
    )
    assert submission["returncode"] == -1
    assert "job_id_raw" not in submission
    assert scheduler.find_by_request_fingerprint("request-1") is None


def test_readonly_store_rejects_mutating_surface(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    store = ReadOnlySQLiteStore(paths.sqlite_db)
    assert store.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        store.execute("DELETE FROM run_state")
    assert not hasattr(store, "set_run_state")
    store.close()


def test_ha_gc_registers_then_rechecks_and_deletes_only_archived_publication(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token)
    store.gc_grace_seconds = 2.0
    old_weight = paths.epoch_weight_path(1, token.owner_id, 0, "old")
    old_optim = paths.epoch_outer_optim_path(1, token.owner_id, 0, "old")
    current_weight = paths.epoch_weight_path(1, token.owner_id, 1, "current")
    current_optim = paths.epoch_outer_optim_path(1, token.owner_id, 1, "current")
    for artifact in (old_weight, old_optim, current_weight, current_optim):
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(artifact.name.encode())
    store.initialize_full_run(
        token,
        publication_id="old",
        weight_size_bytes=old_weight.stat().st_size,
        optim_size_bytes=old_optim.stat().st_size,
        weight_path=paths.relative(old_weight),
        optim_path=paths.relative(old_optim),
        outer_optimizer="nesterov",
        identity={"run_id": "ha-test"},
        config_snapshot={},
    )
    store.upsert_global_version(
        token,
        1,
        paths.relative(current_weight),
        paths.relative(current_optim),
        publication_id="current",
        weight_size_bytes=current_weight.stat().st_size,
        optim_size_bytes=current_optim.stat().st_size,
        outer_optimizer="nesterov",
    )
    old_row = store.historical_version_rows()
    assert [row["version"] for row in old_row] == [0]
    store.delete_archived_rows(token, update_rows=[], version_rows=old_row)
    assert old_weight.exists() and old_optim.exists()
    bound = store.bind(token)
    deleted = collect_runtime_artifacts(
        bound,
        paths,
        orphan_grace_seconds=0.0,
        now=time.time() + 1.0,
    )
    assert deleted == 2
    assert not old_weight.exists() and not old_optim.exists()
    assert current_weight.exists() and current_optim.exists()
    bound.close()
    lease.release(token)
    lease.close()


def test_stale_gc_after_takeover_deletes_only_frozen_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = bootstrapped(tmp_path)
    first, token1 = acquire(paths, "owner-1")
    store1 = fenced(paths, token1)
    store1.gc_grace_seconds = 0.0
    old_weight = paths.epoch_weight_path(1, token1.owner_id, 0, "old")
    old_optim = paths.epoch_outer_optim_path(1, token1.owner_id, 0, "old")
    current_weight = paths.epoch_weight_path(1, token1.owner_id, 1, "current")
    current_optim = paths.epoch_outer_optim_path(1, token1.owner_id, 1, "current")
    for artifact in (old_weight, old_optim, current_weight, current_optim):
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(artifact.name.encode())
    store1.initialize_full_run(
        token1,
        publication_id="old",
        weight_size_bytes=old_weight.stat().st_size,
        optim_size_bytes=old_optim.stat().st_size,
        weight_path=paths.relative(old_weight),
        optim_path=paths.relative(old_optim),
        outer_optimizer="nesterov",
        identity={"run_id": "ha-test"},
        config_snapshot={},
    )
    store1.upsert_global_version(
        token1,
        1,
        paths.relative(current_weight),
        paths.relative(current_optim),
        publication_id="current",
        weight_size_bytes=current_weight.stat().st_size,
        optim_size_bytes=current_optim.stat().st_size,
        outer_optimizer="nesterov",
    )
    store1.delete_archived_rows(
        token1,
        update_rows=[],
        version_rows=store1.historical_version_rows(),
    )
    bound1 = store1.bind(token1)
    takeover: dict[str, object] = {}
    original_unlink = maintenance_module._unlink

    def takeover_before_first_unlink(path: Path) -> bool:
        if not takeover:
            first.release(token1)
            second, token2 = acquire(paths, "owner-2")
            store2 = fenced(paths, token2)
            takeover.update(second=second, token=token2, store=store2)
        return original_unlink(path)

    monkeypatch.setattr(maintenance_module, "_unlink", takeover_before_first_unlink)
    with pytest.raises(StaleLeaderTokenError):
        collect_runtime_artifacts(
            bound1,
            paths,
            orphan_grace_seconds=0.0,
            now=time.time() + 1.0,
        )
    assert sum(path.exists() for path in (old_weight, old_optim)) == 1
    assert current_weight.exists() and current_optim.exists()

    second = takeover["second"]
    token2 = takeover["token"]
    store2 = takeover["store"]
    assert isinstance(second, LeaderLeaseStore)
    assert isinstance(store2, FencedSQLiteStore)
    bound2 = store2.bind(token2)  # type: ignore[arg-type]
    collect_runtime_artifacts(
        bound2,
        paths,
        orphan_grace_seconds=0.0,
        now=time.time() + 2.0,
    )
    assert not old_weight.exists() and not old_optim.exists()
    assert current_weight.exists() and current_optim.exists()
    assert not store2.ha_gc_candidate_paths()
    bound1.close()
    bound2.close()
    second.release(token2)  # type: ignore[arg-type]
    first.close()
    second.close()


@pytest.mark.parametrize("digest_mode", ["off", "checker", "always"])
def test_ha_checkpoint_digest_modes_preserve_publication_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    digest_mode: str,
) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token).bind(token)
    config = resolve_config(project_root=tmp_path)
    config.run.run_id = "ha-test"
    config.run.shared_root = str(paths.shared_root)
    config.io.checkpoint_digest_mode = digest_mode
    theta = torch.tensor([1.0, 2.0, 3.0])
    payloads = {"weight": b"weight-bytes", "outer": b"outer-bytes"}

    def save_weight(path: Path, *_args, **_kwargs) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads["weight"])

    def save_outer(path: Path, *_args, **_kwargs) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads["outer"])

    monkeypatch.setattr(syncer_runtime, "save_global_weights", save_weight)
    monkeypatch.setattr(syncer_runtime, "save_outer_state", save_outer)
    row = syncer_runtime.publish_global(
        config=config,
        paths=paths,
        store=store,
        version=0,
        theta=theta,
        outer_state={"momentum": theta.clone()},
        param_index={"total_numel": theta.numel()},
        num_updates=0,
        total_update_tokens=0,
        total_seen_tokens=0,
    )
    if digest_mode == "always":
        assert row["weight_sha256"] == hashlib.sha256(payloads["weight"]).hexdigest()
        assert row["optim_sha256"] == hashlib.sha256(payloads["outer"]).hexdigest()
    else:
        assert row["weight_sha256"] is None
        assert row["optim_sha256"] is None
    canonical = EpochControlReader(paths, run_id="ha-test").read_current_latest()
    assert canonical is not None
    assert canonical["weight_sha256"] == row["weight_sha256"]
    assert canonical["optim_sha256"] == row["optim_sha256"]
    store.close()
    lease.release(token)
    lease.close()


def test_ha_gc_removes_only_unreferenced_superseded_epoch_orphans(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    first, token1 = acquire(paths, "owner-1")
    store1 = fenced(paths, token1)
    current_weight = paths.epoch_weight_path(1, token1.owner_id, 0, "current")
    current_optim = paths.epoch_outer_optim_path(1, token1.owner_id, 0, "current")
    old_orphan = paths.epoch_weight_path(1, token1.owner_id, 1, "orphan")
    for artifact in (current_weight, current_optim, old_orphan):
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(artifact.name.encode())
    store1.initialize_full_run(
        token1,
        publication_id="current",
        weight_size_bytes=current_weight.stat().st_size,
        optim_size_bytes=current_optim.stat().st_size,
        weight_path=paths.relative(current_weight),
        optim_path=paths.relative(current_optim),
        outer_optimizer="nesterov",
        identity={"run_id": "ha-test"},
        config_snapshot={},
    )
    first.release(token1)
    second, token2 = acquire(paths, "owner-2")
    store2 = fenced(paths, token2)
    current_epoch_orphan = paths.epoch_weight_path(2, token2.owner_id, 1, "staging")
    current_epoch_orphan.parent.mkdir(parents=True, exist_ok=True)
    current_epoch_orphan.write_bytes(b"staging")
    bound = store2.bind(token2)
    deleted = collect_runtime_artifacts(
        bound,
        paths,
        orphan_grace_seconds=0.0,
        now=time.time() + 1.0,
    )
    assert deleted == 1
    assert not old_orphan.exists()
    assert current_weight.exists() and current_optim.exists()
    assert current_epoch_orphan.exists()
    bound.close()
    store1.close()
    second.release(token2)
    first.close()
    second.close()


def test_epoch_history_compaction_keeps_active_rows_bounded(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
    )
    token = lease.acquire(owner_id="owner-1", hostname="host", pid=1)
    for epoch in range(2, 21):
        lease.release(token)
        token = lease.acquire(owner_id=f"owner-{epoch}", hostname="host", pid=epoch)
    store = FencedSQLiteStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=2.0,
        max_retained_epoch_dirs=4,
        wall_clock=lambda: 100.0,
        lease_safety_check=LeaseSafetyTracker(
            token,
            lease_duration_seconds=90.0,
            max_clock_skew_seconds=2.0,
        ).assert_safe,
    )
    bound = store.bind(token)
    assert archive_ha_history(bound, paths) == 16
    active = store.archivable_ha_history(before_epoch=10_000)["epochs"]
    assert [row["epoch"] for row in active] == [17, 18, 19, 20]
    history = [
        json.loads(line) for line in paths.syncer_epoch_history_jsonl.read_text().splitlines()
    ]
    assert [row["epoch"] for row in history] == list(range(1, 17))
    bound.close()
    lease.release(token)
    lease.close()


def test_1000_takeover_and_claim_cycles_keep_active_surfaces_bounded(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    now = [100.0]
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=10.0,
        max_clock_skew_seconds=0.0,
        wall_clock=lambda: now[0],
    )
    token = lease.acquire(owner_id="owner-1", hostname="host", pid=1)
    used_pages: dict[int, int] = {}
    for epoch in range(2, 1001):
        lease.release(token)
        token = lease.acquire(owner_id=f"owner-{epoch}", hostname="host", pid=epoch)
        if epoch % 8 == 0:
            store = FencedSQLiteStore(
                paths.sqlite_db,
                identity(),
                marker_path=paths.bootstrap_complete_json,
                max_clock_skew_seconds=0.0,
                max_retained_epoch_dirs=8,
                wall_clock=lambda: now[0],
                lease_safety_check=LeaseSafetyTracker(
                    token,
                    lease_duration_seconds=10.0,
                    max_clock_skew_seconds=0.0,
                ).assert_safe,
            )
            bound = store.bind(token)
            archive_ha_history(bound, paths)
            raw = open_readonly(paths.sqlite_db)
            page_count = int(raw.execute("PRAGMA page_count").fetchone()[0])
            freelist = int(raw.execute("PRAGMA freelist_count").fetchone()[0])
            raw.close()
            if epoch in {504, 1000}:
                used_pages[epoch] = page_count - freelist
            bound.close()
    assert token.epoch == 1000
    active = open_readonly(paths.sqlite_db)
    assert active.execute("SELECT COUNT(*) FROM syncer_epochs").fetchone()[0] <= 8
    active.close()
    assert used_pages[1000] <= used_pages[504] + 2
    lease.release(token)
    lease.close()

    claim_now = [100.0]
    claim_config = resolve_config(project_root=tmp_path).coordination.recovery_submission
    claim_config.enabled = True
    claim_config.claim_retention_seconds = 0.5
    claim_config.uncertainty_timeout_seconds = 0.25
    claim_config.claim_timeout_seconds = 0.25
    claim_config.backoff_initial_seconds = 0.25
    claim_config.backoff_max_seconds = 0.25
    manager = RecoveryClaimManager(
        paths=paths,
        config=claim_config,
        scheduler=FinishedScheduler(),  # type: ignore[arg-type]
        descriptor_sha256="descriptor",
        wall_clock=lambda: claim_now[0],
    )
    for attempt in range(1000):
        key = recovery_observation_key(
            run_id="ha-test",
            highest_epoch=1000,
            heartbeat_seq=attempt,
            heartbeat_fingerprint=f"hb-{attempt}",
        )
        result = manager.maybe_submit(
            observation_key=key,
            claimant_id="learner-1",
            terminal_published=False,
        )
        assert result.state == "submitted"
        claim_now[0] += 1.0
    # Trigger archival for the final completed claim.
    claim_now[0] += 1.0
    final_key = recovery_observation_key(
        run_id="ha-test",
        highest_epoch=1000,
        heartbeat_seq=1000,
        heartbeat_fingerprint="hb-final",
    )
    manager.maybe_submit(
        observation_key=final_key,
        claimant_id="learner-1",
        terminal_published=False,
    )
    assert len(list(paths.syncer_launch_claims.glob("*/attempt_*.lock"))) == 1
    history = paths.recovery_submission_history_jsonl.read_text().splitlines()
    assert len(history) == 1000
