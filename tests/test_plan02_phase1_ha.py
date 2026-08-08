from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.core.run_descriptor import load_run_descriptor
from fs_diloco.core.constants import (
    CONTROL_EPOCH_FORMAT_VERSION,
    HA_SCHEMA_VERSION,
    SYNCER_HEARTBEAT_FORMAT_VERSION,
)
from fs_diloco.observability.phase1_performance import (
    BUSINESS_TRANSACTION_BATCH_SIZE,
    BUSINESS_TRANSACTION_MAX_P99_RATIO,
    BUSINESS_TRANSACTION_MIN_SAMPLES,
    BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
    CHECKPOINT_PUBLISH_MAX_P99_RATIO,
    CHECKPOINT_PUBLISH_MIN_SAMPLES,
    CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
    MATCHED_PERFORMANCE_FORMAT_VERSION,
    matched_p99_limit,
)
from fs_diloco.protocol.control_epoch import EpochControlPublisher, EpochControlReader
from fs_diloco.runtime import pbs_scheduler as pbs_scheduler_module
from fs_diloco.runtime import syncer as syncer_runtime
from fs_diloco.tools import launch_independent_run as independent_launcher
from fs_diloco.runtime.launch_outbox import RecoveryClaimManager, recovery_observation_key
from fs_diloco.runtime.learner import (
    SyncerProgressWatchdog,
    close_epoch_control_reader,
    confirm_syncer_unresponsive,
    read_authoritative_terminal,
)
from fs_diloco.runtime.pbs_scheduler import PBSJobObservation, PBSScheduler
from fs_diloco.runtime.syncer_ha import (
    LeaseRenewalThread,
    acquire_candidate,
    open_leader_store,
)
from fs_diloco.tools.launch_independent_run import _walltime_resource, launch
from fs_diloco.tools.launch_phase1_acceptance import submit_acceptance_jobs
from fs_diloco.tools.phase1_matched_performance import (
    _business_batch_schedule,
    _is_writer_transaction_statement,
)
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
from scripts.miyabi.check_plan02_phase1 import (
    _blocking_failure_events,
    _canonical_adoption_violations,
    _matched_performance_errors,
    _stale_business_commit_violations,
)


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
    assert config.coordination.syncer_ha.business_busy_timeout_ms == 60_000
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


def test_independent_launcher_preserves_syncer_receipt_when_learner_qsub_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = resolve_config(project_root=tmp_path)
    config.sync.num_learners = 2
    shared_root = tmp_path / "run"
    monkeypatch.setattr(independent_launcher, "resolve_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        independent_launcher,
        "initialize_run",
        lambda *args, **kwargs: {
            "descriptor": {
                "shared_root": str(shared_root),
                "descriptor_sha256": "descriptor-digest",
            }
        },
    )
    submissions = iter(
        (
            subprocess.CompletedProcess([], 0, "12345.opbs\n", ""),
            subprocess.CompletedProcess([], 1, "", "learner array rejected"),
        )
    )
    monkeypatch.setattr(
        independent_launcher.subprocess,
        "run",
        lambda *args, **kwargs: next(submissions),
    )

    result = launch(
        config_path=tmp_path / "config.yaml",
        run_id="partial-submit",
        shared_root=str(shared_root),
        project_root=tmp_path,
        submit=True,
        allow_dirty_snapshot=False,
        syncer_walltime="00:00:20",
        learner_walltime="00:00:45",
    )

    assert result["submission_status"] == "partial"
    assert result["syncer_job_id"] == "12345.opbs"
    assert result["syncer_submission"]["status"] == "submitted"
    assert result["learner_submission"]["status"] == "failed"
    assert "learner array rejected" in result["learner_submission"]["stderr"]


@pytest.mark.parametrize("fail_role", ["successor_syncer", "learner_array"])
def test_acceptance_launcher_persists_every_accepted_job_before_later_qsub_failure(
    tmp_path: Path,
    fail_role: str,
) -> None:
    roles = iter(("crash_syncer", "successor_syncer", "learner_array"))

    def qsub(command: list[str]) -> dict[str, object]:
        role = next(roles)
        if role == fail_role:
            return {
                "status": "failed",
                "returncode": 1,
                "stdout": "",
                "stderr": f"{role} rejected",
                "command": command,
            }
        return {
            "status": "submitted",
            "returncode": 0,
            "stdout": f"{role}.opbs",
            "stderr": "",
            "command": command,
            "job_id": f"{role}.opbs",
        }

    pending = tmp_path / "acceptance_review.json"
    passed = tmp_path / "acceptance_pass.json"
    result = submit_acceptance_jobs(
        project_root=tmp_path,
        run_id="run",
        shared_root=tmp_path / "run",
        descriptor_sha256="descriptor",
        launcher_job_id="launcher.opbs",
        crash_walltime="00:00:15",
        successor_walltime="00:00:30",
        learner_walltime="00:00:25",
        pending_artifact=pending,
        pass_artifact=passed,
        qsub_fn=qsub,
    )

    persisted = json.loads(pending.read_text(encoding="utf-8"))
    assert result["status"] == "partial"
    assert persisted == result
    assert persisted["crash_syncer_job_id"] == "crash_syncer.opbs"
    if fail_role == "learner_array":
        assert persisted["successor_syncer_job_id"] == "successor_syncer.opbs"
    else:
        assert "successor_syncer_job_id" not in persisted
    assert persisted["submission_receipts"][-1]["status"] == "failed"
    assert not passed.exists()


def test_syncer_releases_acquired_lease_when_leader_store_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = resolve_config(
        "configs/fs_diloco_tiny_ha_static.yaml",
        run_id="startup-open-failure",
        shared_root=str(tmp_path / "run"),
    )
    token = SimpleNamespace(epoch=1, owner_id="owner")

    class FakeLease:
        def __init__(self) -> None:
            self.released: list[object] = []
            self.closed = False

        def release(self, released_token: object) -> None:
            self.released.append(released_token)

        def close(self) -> None:
            self.closed = True

    lease = FakeLease()
    monkeypatch.setattr(
        syncer_runtime,
        "load_run_descriptor",
        lambda *args, **kwargs: SimpleNamespace(config=config, identity=identity()),
    )
    monkeypatch.setattr(
        syncer_runtime,
        "acquire_candidate",
        lambda **kwargs: (lease, token, object(), object()),
    )
    monkeypatch.setattr(
        syncer_runtime,
        "open_leader_store",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("store open failed")),
    )

    with pytest.raises(RuntimeError, match="store open failed"):
        syncer_runtime.run_syncer(config)

    assert lease.released == [token]
    assert lease.closed


def test_syncer_cleans_all_acquired_resources_when_renewer_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = resolve_config(
        "configs/fs_diloco_tiny_ha_static.yaml",
        run_id="startup-renewer-failure",
        shared_root=str(tmp_path / "run"),
    )
    token = SimpleNamespace(epoch=1, owner_id="owner")

    class FakeLease:
        def __init__(self) -> None:
            self.released: list[object] = []
            self.closed = False

        def release(self, released_token: object) -> None:
            self.released.append(released_token)

        def close(self) -> None:
            self.closed = True

    class FakeStore:
        fenced_store = object()

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FailingRenewer:
        instance: "FailingRenewer | None" = None

        def __init__(self, **kwargs: object) -> None:
            self.stopped = False
            FailingRenewer.instance = self

        def start(self) -> None:
            raise RuntimeError("renewer start failed")

        def stop(self) -> None:
            self.stopped = True

    lease = FakeLease()
    store = FakeStore()
    monkeypatch.setattr(
        syncer_runtime,
        "load_run_descriptor",
        lambda *args, **kwargs: SimpleNamespace(config=config, identity=identity()),
    )
    monkeypatch.setattr(
        syncer_runtime,
        "acquire_candidate",
        lambda **kwargs: (lease, token, object(), object()),
    )
    monkeypatch.setattr(syncer_runtime, "open_leader_store", lambda **kwargs: store)
    monkeypatch.setattr(syncer_runtime, "LeaseRenewalThread", FailingRenewer)

    with pytest.raises(RuntimeError, match="renewer start failed"):
        syncer_runtime.run_syncer(config)

    assert FailingRenewer.instance is not None and FailingRenewer.instance.stopped
    assert store.closed
    assert lease.released == [token]
    assert lease.closed


def test_syncer_cleans_acquired_resources_when_post_renewer_startup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = resolve_config(
        "configs/fs_diloco_tiny_ha_static.yaml",
        run_id="post-renewer-startup-failure",
        shared_root=str(tmp_path / "run"),
    )
    token = SimpleNamespace(epoch=1, owner_id="owner")

    class FakeLease:
        def __init__(self) -> None:
            self.released: list[object] = []
            self.closed = False

        def release(self, released_token: object) -> None:
            self.released.append(released_token)

        def close(self) -> None:
            self.closed = True

    class FakeStore:
        fenced_store = object()
        path = tmp_path / "state.sqlite"

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def terminal_state(self) -> None:
            raise RuntimeError("terminal state startup failed")

    class RunningRenewer:
        instance: "RunningRenewer | None" = None

        def __init__(self, **kwargs: object) -> None:
            self.started = False
            self.stopped = False
            RunningRenewer.instance = self

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    lease = FakeLease()
    store = FakeStore()
    monkeypatch.setattr(
        syncer_runtime,
        "load_run_descriptor",
        lambda *args, **kwargs: SimpleNamespace(config=config, identity=identity()),
    )
    monkeypatch.setattr(
        syncer_runtime,
        "acquire_candidate",
        lambda **kwargs: (lease, token, object(), object()),
    )
    monkeypatch.setattr(syncer_runtime, "open_leader_store", lambda **kwargs: store)
    monkeypatch.setattr(syncer_runtime, "LeaseRenewalThread", RunningRenewer)

    with pytest.raises(RuntimeError, match="terminal state startup failed"):
        syncer_runtime.run_syncer(config)

    assert RunningRenewer.instance is not None
    assert RunningRenewer.instance.started and RunningRenewer.instance.stopped
    assert store.closed
    assert lease.released == [token]
    assert lease.closed


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            "coordination:\n  syncer_ha:\n    renew_interval_seconds: 0\n",
            "renew_interval_seconds",
        ),
        (
            "coordination:\n  syncer_ha:\n    business_busy_timeout_ms: 0\n",
            "business_busy_timeout_ms",
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
    replay = initialize_ha_run(config, project_root=tmp_path)
    assert replay["recovered"] is True


def test_run_descriptor_rejects_all_identity_tampering_without_lease_writes(
    tmp_path: Path,
) -> None:
    def initialized(name: str) -> tuple[RunPaths, object]:
        shared_root = tmp_path / name
        config = resolve_config(
            project_root=tmp_path,
            run_id=name,
            shared_root=str(shared_root),
        )
        config.coordination.syncer_ha.enabled = True
        config.run.git_commit = "a" * 40
        config.run.git_dirty = False
        config.run.source_fingerprint = "sha256:source"
        initialize_ha_run(config, project_root=tmp_path)
        return RunPaths(shared_root), config

    def assert_no_leadership_rows(paths: RunPaths) -> None:
        conn = open_readonly(paths.sqlite_db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM syncer_leader").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM syncer_epochs").fetchone()[0] == 0
        finally:
            conn.close()

    paths, config = initialized("expected-dirty")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        load_run_descriptor(
            paths.shared_root,
            expected_run_id=config.run.run_id,
            expected_git_commit=config.run.git_commit,
            expected_git_dirty=True,
            expected_source_fingerprint=config.run.source_fingerprint,
        )
    assert_no_leadership_rows(paths)

    paths, _config = initialized("descriptor-checksum")
    assert paths.run_descriptor_json.stat().st_mode & 0o222 == 0
    descriptor = json.loads(paths.run_descriptor_json.read_text(encoding="utf-8"))
    descriptor["run_id"] = "tampered"
    paths.run_descriptor_json.chmod(0o644)
    paths.run_descriptor_json.write_text(json.dumps(descriptor), encoding="utf-8")
    with pytest.raises(RuntimeError, match="self-checksum"):
        load_run_descriptor(paths.shared_root)
    assert_no_leadership_rows(paths)

    paths, _config = initialized("config-checksum")
    assert paths.resolved_config_yaml.stat().st_mode & 0o222 == 0
    paths.resolved_config_yaml.chmod(0o644)
    paths.resolved_config_yaml.write_text(
        paths.resolved_config_yaml.read_text(encoding="utf-8") + "# tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="resolved config checksum"):
        load_run_descriptor(paths.shared_root)
    assert_no_leadership_rows(paths)

    paths, _config = initialized("source-checksum")
    assert paths.run_source_manifest_json.stat().st_mode & 0o222 == 0
    paths.run_source_manifest_json.chmod(0o644)
    paths.run_source_manifest_json.write_text(
        paths.run_source_manifest_json.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="source manifest checksum"):
        load_run_descriptor(paths.shared_root)
    assert_no_leadership_rows(paths)


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


def test_candidate_retries_writer_lock_until_release_and_times_out_cleanly(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    config = resolve_config(project_root=tmp_path)
    config.coordination.syncer_ha.enabled = True
    config.coordination.syncer_ha.lease_busy_timeout_ms = 10
    config.coordination.syncer_ha.candidate_acquire_poll_seconds = 0.01
    config.coordination.syncer_ha.candidate_wait_seconds = 1.0
    blocker = sqlite3.connect(paths.sqlite_db, timeout=0.0)
    blocker.execute("BEGIN IMMEDIATE")
    outcome: dict[str, object] = {}
    acquired_event = threading.Event()
    cleanup_event = threading.Event()

    def run_candidate() -> None:
        try:
            lease, token, _tracker, _logger = acquire_candidate(
                paths=paths,
                identity=identity(),
                config=config,
                owner_id="writer-lock-retry",
            )
            outcome["epoch"] = token.epoch
            acquired_event.set()
            if not cleanup_event.wait(timeout=2.0):
                raise TimeoutError("test did not authorize candidate cleanup")
            lease.release(token)
            lease.close()
            outcome["cleaned"] = True
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run_candidate)
    thread.start()
    deadline = time.monotonic() + 1.0
    candidate_log = paths.logs / "candidates" / f"{RunPaths.owner_short('writer-lock-retry')}.jsonl"
    while time.monotonic() < deadline:
        if candidate_log.is_file() and "writer_lock_blocked" in candidate_log.read_text(
            encoding="utf-8"
        ):
            break
        time.sleep(0.01)
    blocker.rollback()
    blocker.close()
    assert acquired_event.wait(timeout=2.0)
    cleanup_event.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert "error" not in outcome
    assert outcome == {"epoch": 1, "cleaned": True}

    blocker = sqlite3.connect(paths.sqlite_db, timeout=0.0)
    blocker.execute("BEGIN IMMEDIATE")
    config.coordination.syncer_ha.candidate_wait_seconds = 0.08
    with pytest.raises(TimeoutError, match="wait deadline"):
        acquire_candidate(
            paths=paths,
            identity=identity(),
            config=config,
            owner_id="writer-lock-timeout",
        )
    blocker.rollback()
    blocker.close()


def test_lease_renewer_retries_transient_sqlite_busy(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    config = resolve_config(project_root=tmp_path)
    ha = config.coordination.syncer_ha
    ha.enabled = True
    ha.lease_duration_seconds = 2.0
    ha.renew_interval_seconds = 0.05
    ha.max_clock_skew_seconds = 0.1
    ha.heartbeat_interval_seconds = 0.05
    ha.lease_busy_timeout_ms = 10
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity(),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=ha.lease_duration_seconds,
        max_clock_skew_seconds=ha.max_clock_skew_seconds,
        busy_timeout_ms=ha.lease_busy_timeout_ms,
    )
    token = lease.acquire(owner_id="renew-busy", hostname="host", pid=1)
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=ha.lease_duration_seconds,
        max_clock_skew_seconds=ha.max_clock_skew_seconds,
    )
    blocker = sqlite3.connect(paths.sqlite_db, timeout=0.0)
    blocker.execute("BEGIN IMMEDIATE")
    renewer = LeaseRenewalThread(
        paths=paths,
        identity=identity(),
        config=config,
        token=token,
        fenced_store=object(),  # heartbeat publication stays disabled in this test
        safety_tracker=tracker,
    )
    renewer.start()
    deadline = time.monotonic() + 1.0
    while renewer.busy_retry_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    blocker.rollback()
    blocker.close()
    deadline = time.monotonic() + 1.0
    while renewer.observation_metrics()["lease_renew_count"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    renewer.stop()

    metrics = renewer.observation_metrics()
    assert metrics["lease_renew_busy_retry_count"] > 0
    assert metrics["lease_renew_count"] > 0
    assert metrics["lease_renew_failure_count"] == 0
    lease.release(token)
    lease.close()


def test_final_lease_metrics_are_observed_only_after_renewer_stop() -> None:
    class FinalFailureRenewer:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

        def observation_metrics(self) -> dict[str, object]:
            return {
                "lease_renew_count": 1,
                "lease_renew_failure_count": int(self.stopped),
            }

    renewer = FinalFailureRenewer()
    metrics = syncer_runtime.stop_lease_renewer_for_final_metrics(renewer)  # type: ignore[arg-type]
    assert renewer.stopped
    assert metrics["lease_renew_failure_count"] == 1


def test_final_lease_metrics_propagate_stop_failure() -> None:
    class StopFailureRenewer:
        def stop(self) -> None:
            raise RuntimeError("late renewal failure")

        def observation_metrics(self) -> dict[str, object]:
            raise AssertionError("metrics must not be sampled after a failed stop")

    with pytest.raises(RuntimeError, match="late renewal failure"):
        syncer_runtime.stop_lease_renewer_for_final_metrics(  # type: ignore[arg-type]
            StopFailureRenewer()
        )


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
        "register_orphan_gc_candidate",
        "claim_gc_candidate",
        "expedite_terminal_gc_candidates",
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


@pytest.mark.parametrize(
    "statement",
    [
        "WITH target AS (SELECT 1) UPDATE run_state SET value='bad' WHERE key='missing'",
        "/* hidden mutation */ UPDATE run_state SET value='bad' WHERE key='missing'",
        "PRAGMA user_version=999",
        "PRAGMA journal_mode(WAL)",
        "PRAGMA synchronous(OFF)",
        "PRAGMA query_only(OFF)",
        "PRAGMA busy_timeout(1)",
    ],
)
def test_fenced_connection_rejects_unrecognized_or_disguised_mutations(
    tmp_path: Path,
    statement: str,
) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token)
    store._connection.activate(token)
    try:
        with pytest.raises(RuntimeError, match="forbidden"):
            store._connection.execute(statement)
    finally:
        if store._connection.in_transaction:
            store._connection.rollback()
        store._connection.deactivate()
    assert store.get_run_state("missing") is None
    store.close()
    lease.release(token)
    lease.close()


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
    reader.configure_canonical_repair_wait(10.0)
    assert not hasattr(reader, "store")
    assert reader.read_current_latest(now_monotonic=0.0)["version"] == 0
    first.release(token1)
    second, token2 = acquire(paths, "owner-2")
    store2 = fenced(paths, token2)
    publisher2 = EpochControlPublisher(paths, store2, token2)
    publisher2.publish_heartbeat(second.observe())
    assert reader.read_current_latest(now_monotonic=100.0) is None
    assert reader.read_current_latest(now_monotonic=109.999) is None
    assert reader.observation_metrics()["canonical_repair_wait_count"] == 0
    assert reader.read_current_latest(now_monotonic=110.0) is None
    repair_metrics = reader.observation_metrics()
    assert repair_metrics["canonical_gap_epoch"] == 2
    assert repair_metrics["canonical_repair_wait_count"] == 1
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
    heartbeat_path = paths.syncer_heartbeat_path(2, token2.owner_id)
    head_path = paths.epoch_head_path(2, token2.owner_id)
    heartbeat_bytes = heartbeat_path.read_bytes()
    head_bytes = head_path.read_bytes()
    heartbeat_path.unlink()
    head_path.unlink()
    assert reader.read_current_terminal() is None
    assert reader.observation_metrics()["cache_rejected_lower_epoch_count"] >= 1
    heartbeat_path.write_bytes(heartbeat_bytes)
    head_path.write_bytes(head_bytes)
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


def test_epoch_reader_ignores_torn_lower_epoch_but_fails_on_current_torn_epoch(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    first, token1 = acquire(paths, "owner-1")
    store1 = fenced(paths, token1)
    publisher1 = EpochControlPublisher(paths, store1, token1)
    publisher1.publish_heartbeat(first.observe())
    first.release(token1)
    second, token2 = acquire(paths, "owner-2")
    store2 = fenced(paths, token2)
    publisher2 = EpochControlPublisher(paths, store2, token2)
    publisher2.publish_heartbeat(second.observe())
    paths.syncer_heartbeat_path(1, token1.owner_id).write_text("{torn", encoding="utf-8")

    reader = EpochControlReader(paths, run_id="ha-test")
    current = reader.current_leader()
    assert current is not None and current["epoch"] == 2
    assert reader.observation_metrics()["stale_epoch_scan_rejected_count"] == 1

    paths.syncer_heartbeat_path(2, token2.owner_id).write_text("{torn", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid epoch control"):
        reader.current_leader()
    store1.close()
    store2.close()
    second.release(token2)
    first.close()
    second.close()


def test_epoch_reader_scan_cache_avoids_repeated_recursive_scans(tmp_path: Path) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token)
    publisher = EpochControlPublisher(paths, store, token)
    publisher.publish_heartbeat(lease.observe())
    reader = EpochControlReader(paths, run_id="ha-test")
    reader.configure_scan_cache(1.0)

    assert reader.current_leader(now_monotonic=0.0) is not None
    paths.syncer_heartbeat_path(token.epoch, token.owner_id).unlink()
    assert reader.current_leader(now_monotonic=0.5) is not None
    assert reader.current_leader(now_monotonic=1.0) is None
    metrics = reader.observation_metrics()
    assert metrics["control_scan_count"] == 2
    assert metrics["control_scan_cache_hit_count"] == 1
    assert metrics["control_scan_wall_seconds"] >= 0.0
    assert metrics["control_scan_cpu_seconds"] >= 0.0
    store.close()
    lease.release(token)
    lease.close()


def test_ha_watchdog_uses_heartbeat_progress_and_recovery_budget_not_model_merges(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    config = resolve_config(project_root=tmp_path)
    config.run.run_id = "ha-test"
    config.run.shared_root = str(paths.shared_root)
    config.coordination.syncer_ha.enabled = True
    config.coordination.syncer_ha.learner_recovery_wait_seconds = 1800.0
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token)
    publisher = EpochControlPublisher(paths, store, token)
    publisher.publish_heartbeat(lease.observe())
    watchdog = SyncerProgressWatchdog.start(
        timeout_seconds=30.0,
        initial_version=0,
        now_monotonic=0.0,
        now_wall=100.0,
    )

    assert not confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field="version",
        config=config,
        now_monotonic=0.0,
        now_wall=100.0,
    )
    # A queued recovery candidate may exceed the legacy 600-second watchdog;
    # the frozen HA recovery budget remains authoritative.
    assert not confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field="version",
        config=config,
        now_monotonic=700.0,
        now_wall=800.0,
    )

    renewed = lease.renew(token)
    publisher.publish_heartbeat(renewed)
    assert not confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field="version",
        config=config,
        now_monotonic=1000.0,
        now_wall=1100.0,
    )
    assert watchdog.last_observed_version == 0
    assert watchdog.last_heartbeat_seq == int(renewed["heartbeat_seq"])
    assert not confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field="version",
        config=config,
        now_monotonic=2799.999,
        now_wall=2899.999,
    )
    assert confirm_syncer_unresponsive(
        watchdog,
        paths,
        version_field="version",
        config=config,
        now_monotonic=2800.0,
        now_wall=2900.0,
    )

    close_epoch_control_reader(paths)
    store.close()
    lease.release(token)
    lease.close()


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


def test_run_paths_recursively_discovers_ha_runtime_surfaces(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "run")
    syncer_log = paths.logs / "syncers" / "e000001_owner.jsonl"
    learner_log = paths.logs / "instances" / "learner_li_example" / "events.jsonl"
    heartbeat = paths.heartbeats / "instances" / "learner_li_example" / "heartbeat.json"
    pointer = paths.updates_latest / "instances" / "learner_li_example" / "latest.json"
    payload = paths.updates_payloads / "instances" / "learner_li_example" / "u1.safetensors"
    for path in (syncer_log, learner_log, heartbeat, pointer, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")

    assert list(paths.iter_syncer_logs()) == [syncer_log]
    assert list(paths.iter_learner_logs()) == [learner_log]
    assert list(paths.iter_learner_heartbeats()) == [heartbeat]
    assert list(paths.iter_instance_pointers()) == [pointer]
    assert list(paths.iter_instance_payloads()) == [payload]


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


def test_recovery_global_budget_is_atomic_across_distinct_observations(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    config = resolve_config(project_root=tmp_path).coordination.recovery_submission
    config.enabled = True
    config.max_outstanding_candidates = 1
    scheduler = FakeScheduler()
    first_entered = threading.Event()
    release_first = threading.Event()

    class SlowFirstManager(RecoveryClaimManager):
        calls = 0
        calls_lock = threading.Lock()

        def _archive_expired_claims(self, **kwargs: object) -> int:
            with self.calls_lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                first_entered.set()
                assert release_first.wait(timeout=2.0)
            return super()._archive_expired_claims(**kwargs)

    manager = SlowFirstManager(
        paths=paths,
        config=config,
        scheduler=scheduler,  # type: ignore[arg-type]
        descriptor_sha256="descriptor",
    )
    keys = [
        recovery_observation_key(
            run_id="run",
            highest_epoch=1,
            heartbeat_seq=index,
            heartbeat_fingerprint=f"hb-{index}",
        )
        for index in (1, 2)
    ]
    first_result: list[object] = []
    thread = threading.Thread(
        target=lambda: first_result.append(
            manager.maybe_submit(
                observation_key=keys[0],
                claimant_id="learner-1",
                terminal_published=False,
            )
        )
    )
    thread.start()
    assert first_entered.wait(timeout=2.0)
    second = manager.maybe_submit(
        observation_key=keys[1],
        claimant_id="learner-2",
        terminal_published=False,
    )
    release_first.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert second.state == "reservation_busy"
    assert len(first_result) == 1 and first_result[0].state == "submitted"
    assert scheduler.submissions == 1
    assert len(list(paths.syncer_launch_claims.glob("*/attempt_*.lock"))) == 1


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


def test_pbs_scheduler_matches_exact_request_variable_not_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = """Job Id: 10.server
    job_state = Q
    Variable_List = OTHER=x,FS_DILOCO_RECOVERY_REQUEST=request-10
Job Id: 1.server
    job_state = R
    substate = 42
    Variable_List = FS_DILOCO_RECOVERY_REQUEST=request-1,OTHER=y
"""
    monkeypatch.setattr(
        pbs_scheduler_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )

    observation = PBSScheduler().find_by_request_fingerprint("request-1")

    assert observation is not None
    assert observation.job_id == "1"
    assert observation.classification == "running"


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


def test_ha_maintenance_does_not_delete_current_authority_writer_temp(
    tmp_path: Path,
) -> None:
    paths = bootstrapped(tmp_path)
    lease, token = acquire(paths, "owner")
    store = fenced(paths, token)
    store.gc_grace_seconds = 30.0
    bound = store.bind(token)
    epoch_dir = paths.syncer_epoch_dir(token.epoch, token.owner_id)
    epoch_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_tmp = epoch_dir / ".heartbeat.json.inflight.tmp"
    heartbeat_tmp.write_text("in flight", encoding="utf-8")
    modified_at = heartbeat_tmp.stat().st_mtime

    assert (
        collect_runtime_artifacts(
            bound,
            paths,
            orphan_grace_seconds=0.0,
            now=modified_at + 29.999,
        )
        == 0
    )
    assert heartbeat_tmp.is_file()
    assert (
        collect_runtime_artifacts(
            bound,
            paths,
            orphan_grace_seconds=0.0,
            now=modified_at + 30.0,
        )
        == 1
    )
    assert not heartbeat_tmp.exists()

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
    store2.gc_grace_seconds = 0.0
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


def test_phase1_checker_blocks_late_runtime_failures() -> None:
    events = [
        {"event_type": "process_exit"},
        {"event_type": "lease_renewer_stop_failed", "actor": "syncer"},
        {"event_type": "canonical_latest_wait_failed", "actor": "learner_000"},
    ]
    assert [event["event_type"] for event in _blocking_failure_events(events)] == [
        "lease_renewer_stop_failed",
        "canonical_latest_wait_failed",
    ]


def test_phase1_checker_derives_stale_writer_violations_from_persisted_rows() -> None:
    violations = _stale_business_commit_violations(
        versions=[
            {"version": 0, "commit_epoch": 1, "commit_owner_id": "owner-1"},
            {"version": 1, "commit_epoch": 1, "commit_owner_id": "stale-owner"},
        ],
        epochs=[{"epoch": 1, "owner_id": "owner-1"}],
        updates=[
            {
                "update_id": "u1",
                "status": "applied",
                "applied_version": 1,
                "applied_by_epoch": 2,
            }
        ],
        controller={"updated_by_epoch": 1, "updated_by_owner_id": "owner-1"},
        terminal={"finalized_by_epoch": 2, "finalized_by_owner_id": "owner-2"},
        publications=[
            {
                "kind": "latest",
                "logical_generation": 1,
                "published_by_epoch": 1,
                "published_by_owner_id": "stale-owner",
            }
        ],
    )
    assert any("version 1 writer" in violation for violation in violations)
    assert any("update u1 has unknown applied_by_epoch=2" in violation for violation in violations)
    assert any("terminal writer" in violation for violation in violations)
    assert any("control publication latest/1" in violation for violation in violations)


def test_phase1_checker_derives_canonical_adoption_errors_per_expected_learner() -> None:
    violations = _canonical_adoption_violations(
        learner_events=[
            {
                "event_type": "process_exit",
                "actor": "learner_000",
                "global_version": 9,
                "status_reason": "syncer_unresponsive",
            },
            {
                "event_type": "final_fragment_adoption_failed",
                "actor": "learner_000",
            },
        ],
        terminal={"final_version": 10},
        expected_learner_ids={"learner_000", "learner_001"},
    )
    assert any("emitted final_fragment_adoption_failed" in item for item in violations)
    assert any("exit identities mismatch" in item for item in violations)
    assert any("status_reason=syncer_unresponsive" in item for item in violations)
    assert any("does not match terminal 10" in item for item in violations)


def _matched_performance_payload() -> tuple[dict[str, object], dict[str, object]]:
    identity_payload: dict[str, object] = {
        "run_id": "run",
        "descriptor_sha256": "descriptor",
        "git_commit": "commit",
        "git_dirty": False,
        "source_fingerprint": "sha256:source",
        "config_sha256": "config",
    }
    business_baseline = 0.004
    checkpoint_baseline = 0.003
    schedule = _business_batch_schedule(samples_per_mode=BUSINESS_TRANSACTION_MIN_SAMPLES)
    blocks = [
        {
            "batch": batch,
            "mode": "observer" if with_observer else "baseline",
            "sample_count": BUSINESS_TRANSACTION_BATCH_SIZE,
            "candidate_observation_count": 1 if with_observer else 0,
        }
        for batch, with_observer in enumerate(schedule)
    ]
    payload: dict[str, object] = {
        "checker": "plan02_phase1_matched_performance",
        "format_version": MATCHED_PERFORMANCE_FORMAT_VERSION,
        "status": "PASS",
        "identity": dict(identity_payload),
        "business_candidate_observer": {
            "baseline_sample_count": BUSINESS_TRANSACTION_MIN_SAMPLES,
            "observer_sample_count": BUSINESS_TRANSACTION_MIN_SAMPLES,
            "baseline_p99_seconds": business_baseline,
            "observer_p99_seconds": 0.0045,
            "max_p99_ratio": BUSINESS_TRANSACTION_MAX_P99_RATIO,
            "jitter_seconds": BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
            "allowed_observer_p99_seconds": matched_p99_limit(
                business_baseline,
                max_ratio=BUSINESS_TRANSACTION_MAX_P99_RATIO,
                jitter_seconds=BUSINESS_TRANSACTION_P99_JITTER_SECONDS,
            ),
            "candidate_observation_count": sum(schedule),
            "candidate_writer_transaction_attempt_count": 0,
            "candidate_writer_transaction_instrumentation": (
                "sqlite trace count of BEGIN IMMEDIATE/EXCLUSIVE"
            ),
            "batch_size": BUSINESS_TRANSACTION_BATCH_SIZE,
            "blocks": blocks,
        },
        "checkpoint_publish": {
            "baseline_contract": "Plan 01 legacy SQLiteStore publication",
            "matched_fields": "source/config/model/seed/tensor/dtype/filesystem",
            "tensor_numel": 2048,
            "publish_dtype": "float32",
            "baseline_sample_count": CHECKPOINT_PUBLISH_MIN_SAMPLES,
            "ha_sample_count": CHECKPOINT_PUBLISH_MIN_SAMPLES,
            "baseline_p99_seconds": checkpoint_baseline,
            "ha_p99_seconds": 0.0035,
            "max_p99_ratio": CHECKPOINT_PUBLISH_MAX_P99_RATIO,
            "jitter_seconds": CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
            "allowed_ha_p99_seconds": matched_p99_limit(
                checkpoint_baseline,
                max_ratio=CHECKPOINT_PUBLISH_MAX_P99_RATIO,
                jitter_seconds=CHECKPOINT_PUBLISH_P99_JITTER_SECONDS,
            ),
            "digest_mode": "off",
        },
    }
    return payload, identity_payload


def test_phase1_checker_accepts_only_identity_bound_frozen_matched_gates() -> None:
    payload, expected_identity = _matched_performance_payload()
    assert not _matched_performance_errors(payload, expected_identity=expected_identity)

    payload["identity"]["git_commit"] = "other"  # type: ignore[index]
    payload["business_candidate_observer"][  # type: ignore[index]
        "candidate_writer_transaction_attempt_count"
    ] = 1
    payload["checkpoint_publish"]["max_p99_ratio"] = 99.0  # type: ignore[index]
    errors = _matched_performance_errors(payload, expected_identity=expected_identity)
    assert any("git_commit mismatch" in error for error in errors)
    assert any("attempted a writer transaction" in error for error in errors)
    assert any("checkpoint threshold definition changed" in error for error in errors)


def test_phase1_checker_blocks_missing_or_regressed_matched_evidence() -> None:
    _payload, expected_identity = _matched_performance_payload()
    assert _matched_performance_errors(None, expected_identity=expected_identity) == [
        "completed gate requires a matched performance artifact"
    ]

    payload, _expected_identity = _matched_performance_payload()
    business = payload["business_candidate_observer"]  # type: ignore[assignment]
    business["observer_p99_seconds"] = business["allowed_observer_p99_seconds"] + 0.001
    checkpoint = payload["checkpoint_publish"]  # type: ignore[assignment]
    checkpoint["ha_p99_seconds"] = checkpoint["allowed_ha_p99_seconds"] + 0.001
    errors = _matched_performance_errors(payload, expected_identity=expected_identity)
    assert any("candidate observer p99 regression" in error for error in errors)
    assert any("HA checkpoint p99 regression" in error for error in errors)


def test_matched_business_schedule_is_fine_grained_balanced_ab_ba() -> None:
    schedule = _business_batch_schedule(samples_per_mode=BUSINESS_TRANSACTION_MIN_SAMPLES)
    assert len(schedule) == 2 * BUSINESS_TRANSACTION_MIN_SAMPLES // BUSINESS_TRANSACTION_BATCH_SIZE
    assert sum(schedule) * BUSINESS_TRANSACTION_BATCH_SIZE == BUSINESS_TRANSACTION_MIN_SAMPLES
    assert (len(schedule) - sum(schedule)) * BUSINESS_TRANSACTION_BATCH_SIZE == (
        BUSINESS_TRANSACTION_MIN_SAMPLES
    )
    for pair_index in range(0, len(schedule), 2):
        expected = (False, True) if (pair_index // 2) % 2 == 0 else (True, False)
        assert schedule[pair_index : pair_index + 2] == expected

    assert _is_writer_transaction_statement("BEGIN IMMEDIATE")
    assert _is_writer_transaction_statement("  begin   exclusive transaction")
    assert not _is_writer_transaction_statement("BEGIN")
    assert not _is_writer_transaction_statement("SELECT * FROM syncer_leader")


def test_phase1_checker_requires_matched_block_and_writer_instrumentation() -> None:
    payload, expected_identity = _matched_performance_payload()
    business = payload["business_candidate_observer"]  # type: ignore[assignment]
    business["candidate_writer_transaction_instrumentation"] = "hardcoded"
    business["blocks"][0]["candidate_observation_count"] = 1
    errors = _matched_performance_errors(payload, expected_identity=expected_identity)
    assert any("writer-attempt instrumentation" in error for error in errors)
    assert any("AB/BA block evidence" in error for error in errors)


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
