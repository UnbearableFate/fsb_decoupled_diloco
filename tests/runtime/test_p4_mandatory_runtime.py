from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fs_diloco.core.config import resolve_config
from fs_diloco.core.config_v4 import (
    ConfigProfile,
    ConfigV4,
    LeaderSection,
    MaintenanceSection,
    load_config_v4,
)
from fs_diloco.modeling.hf_data import build_indexed_batch_iterator
from fs_diloco.protocol._validation import identity as validate_identity
from fs_diloco.protocol.admission_v4 import (
    dynamic_placement_id,
    publish_admission_response,
    read_admission_response,
)
from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.protocol.control_v4 import (
    V4ControlPublisher,
    read_current_control,
    wait_for_receipt_barrier,
)
from fs_diloco.protocol.data_cursor import ContributorResumeState, IndexedBlockCursor
from fs_diloco.protocol.cycle_receipt import canonical_receipt_relative_path
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.authority import AuthorityIdentity, CommittedVersion, LeaderAuthority
from fs_diloco.storage.leader_lease import LeaderToken
from fs_diloco.storage.paths import RunPaths
from fs_diloco.tools import launch_independent_run
from fs_diloco.tools.init_run import initialize_run
from fs_diloco.tools.launch_independent_run import _walltime_resource
from fs_diloco.tools.migrate_config_v3_to_v4 import migrate
from fs_diloco.runtime.syncer_v4 import _raise_injected_candidate_failure
from tests.support.v4_protocol import receipt


PLAN03_REQUIREMENTS = frozenset(
    {"AUTH-02", "AUTH-03", "AUTH-04", "AUTH-05", "AUTH-07", "AUTH-09", "AUTH-10", "P4-MIGRATE"}
)


def test_receipt_object_identity_is_isolated_by_contributor_fence() -> None:
    first = StaticContributorFence("static", "learner_000", "launch-0", "attempt-1", 1)
    replacement = StaticContributorFence("static", "learner_000", "launch-0", "attempt-2", 2)

    assert canonical_receipt_relative_path(first, 1) != canonical_receipt_relative_path(
        replacement, 1
    )


RETAINED_FULL_CONFIGS = tuple(
    path
    for path in sorted(Path("configs").rglob("fs_diloco_*.yaml"))
    if "fragment" not in path.name and "no_fragment_50x10" not in path.name
)
TORCH_BASELINE_CONFIGS = tuple(sorted(Path("configs").glob("torch_baseline_*.yaml")))
RETAINED_FULL_PBS = (
    Path("scripts/miyabi/run_1node_debug.pbs"),
    Path("scripts/miyabi/run_2node_debug.pbs"),
    Path("scripts/miyabi/run_2node_resume_regression.pbs"),
    Path("scripts/miyabi/run_8node_colocated_gpt2_wikitext2_5000steps.pbs"),
    Path("scripts/miyabi/run_9node_gpt2_wikitext2.pbs"),
    Path("scripts/miyabi/run_9node_gpt2_wikitext2_5000steps.pbs"),
    Path("scripts/miyabi/run_plan01_regression.pbs"),
)


@pytest.mark.parametrize("path", RETAINED_FULL_CONFIGS)
def test_retained_full_repository_configs_are_strict_v4(path: Path) -> None:
    config = load_config_v4(path, profile=ConfigProfile.FULL_V4)
    assert config.config_schema_version == 1
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "init" not in payload
    assert "fragments" not in payload
    assert "syncer_ha" not in payload.get("coordination", {})
    assert "stop_after_global_tokens" not in payload.get("sync", {})


@pytest.mark.parametrize("path", TORCH_BASELINE_CONFIGS)
def test_torch_baseline_configs_keep_explicit_shared_schema(path: Path) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert payload["config_schema_version"] == 1
    config = resolve_config(path, profile="torch_baseline")
    assert config.config_schema_version == 1


def test_strict_v4_configs_have_a_temporary_validated_classic_oracle_projection() -> None:
    local = resolve_config("configs/fs_diloco_tiny_local.yaml")
    static_ha = resolve_config("configs/fs_diloco_tiny_ha_static.yaml")

    assert local.coordination.syncer_ha.enabled is False
    assert static_ha.coordination.syncer_ha.enabled is True
    assert static_ha.coordination.syncer_ha.lease_duration_seconds == 30.0


def test_retained_full_pbs_use_initializer_and_only_v4_runtime_shape() -> None:
    helper = Path("scripts/miyabi/run_v4_allocation.sh").read_text(encoding="utf-8")
    local = Path("scripts/local/run_tiny_2proc_smoke.sh").read_text(encoding="utf-8")
    for source in (helper, local):
        assert "fs_diloco.tools.init_run" in source
        assert '--config "$RESOLVED_CONFIG"' in source
        assert "fs_diloco.syncer" in source
        assert "fs_diloco.learner" in source
    for path in RETAINED_FULL_PBS:
        source = path.read_text(encoding="utf-8")
        assert "#PBS -W group_list=xg24i002" in source
        if path.name in {
            "run_1node_debug.pbs",
            "run_2node_debug.pbs",
            "run_8node_colocated_gpt2_wikitext2_5000steps.pbs",
            "run_9node_gpt2_wikitext2.pbs",
            "run_9node_gpt2_wikitext2_5000steps.pbs",
        }:
            assert "run_v4_allocation.sh" in source
        elif path.name == "run_2node_resume_regression.pbs":
            assert "fs_diloco.tools.init_run" in source
            assert '--config "$CONFIG" --shared-root "$SHARED_ROOT"' in source
        else:
            assert "run_tiny_2proc_smoke.sh" in source


def test_production_v4_entrypoint_closure_has_no_classic_authority_or_shared_csv() -> None:
    runtime_paths = (
        Path("fs_diloco/syncer.py"),
        Path("fs_diloco/learner.py"),
        Path("fs_diloco/runtime/syncer_entrypoint.py"),
        Path("fs_diloco/runtime/syncer_v4.py"),
        Path("fs_diloco/runtime/learner_entrypoint.py"),
        Path("fs_diloco/runtime/learner_v4.py"),
        Path("fs_diloco/protocol/control_v4.py"),
        Path("fs_diloco/protocol/admission_v4.py"),
    )
    forbidden = (
        "ha_mode",
        "syncer_ha.enabled",
        "SQLiteStore(",
        "prepare_run_dirs",
        "config.init.resume",
        "syncer_metrics.csv",
        "learner_metrics.csv",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_paths)
    for needle in forbidden:
        assert needle not in combined


def test_migration_is_dry_run_by_default_and_refuses_fragment_or_ambiguous_stop(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v3.yaml"
    source.write_text(
        "sync:\n  stop_after_outer_steps: 2\n  stop_after_global_tokens: null\n",
        encoding="utf-8",
    )
    before = source.read_bytes()
    report = migrate(source)
    assert report["write_mode"] == "dry_run"
    assert source.read_bytes() == before

    fragment = tmp_path / "fragment.yaml"
    fragment.write_text("fragments:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fragment config is unsupported"):
        migrate(fragment)

    ambiguous = tmp_path / "ambiguous.yaml"
    ambiguous.write_text("sync:\n  stop_after_global_tokens: 100\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        migrate(ambiguous)


def test_migration_output_no_clobber_and_hash_fenced_in_place(tmp_path: Path) -> None:
    source = tmp_path / "v3.yaml"
    source.write_text("run:\n  name: migrate\n", encoding="utf-8")
    output = tmp_path / "v4.yaml"
    migrate(source, output_path=output)
    load_config_v4(output, profile=ConfigProfile.FULL_V4)
    with pytest.raises(FileExistsError):
        migrate(source, output_path=output)
    with pytest.raises(ValueError, match="provided together"):
        migrate(source, in_place=True)
    with pytest.raises(RuntimeError, match="input changed"):
        migrate(source, in_place=True, expected_sha256="0" * 64)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    migrate(source, in_place=True, expected_sha256=digest)
    load_config_v4(source, profile=ConfigProfile.FULL_V4)


def test_independent_walltimes_have_repository_minimum() -> None:
    assert _walltime_resource("00:10:00", required=True) == [
        "-l",
        "walltime=00:10:00",
    ]
    with pytest.raises(ValueError, match="at least"):
        _walltime_resource("00:09:59", required=True)
    with pytest.raises(ValueError, match="requires an estimated"):
        _walltime_resource(None, required=True)


def test_dynamic_placement_identity_is_safe_for_authority() -> None:
    placement_id = dynamic_placement_id(
        hostname="mg0005.example",
        accelerator="GPU-0:1,cpu",
    )
    assert validate_identity(placement_id, name="placement_id") == placement_id


def test_admission_response_replay_is_byte_idempotent(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    request = {
        "mode": "static",
        "run_id": "run-1",
        "descriptor_sha256": "d" * 64,
        "learner_id": "learner_000",
        "attempt_id": "attempt-1",
    }
    fence = StaticContributorFence(
        kind="static",
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        binding_generation=1,
    )
    resume = ContributorResumeState(
        cursor=0,
        last_receipt_id=None,
        last_receipt_sha256=None,
        next_cycle_seq=1,
    )
    first = publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        fence=fence,
        resume=resume,
    )
    first_bytes = first.read_bytes()
    second = publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        fence=fence,
        resume=resume,
    )
    assert second == first
    assert second.read_bytes() == first_bytes


def test_stale_epoch_admission_response_cannot_open_torch_gate(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    old = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=1, owner_id="owner-old"),
        lease_duration_seconds=30.0,
    )
    current = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=2, owner_id="owner-current"),
        lease_duration_seconds=30.0,
    )
    old.publish_heartbeat()
    current.publish_heartbeat()
    request = {
        "mode": "static",
        "run_id": "run-1",
        "descriptor_sha256": "d" * 64,
        "learner_id": "learner_000",
        "attempt_id": "attempt-1",
    }
    fence = StaticContributorFence(
        kind="static",
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        binding_generation=1,
    )
    resume = ContributorResumeState(0, None, None, 1)
    publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-old",
        request=request,
        fence=fence,
        resume=resume,
    )
    assert (
        read_admission_response(
            paths,
            run_id="run-1",
            descriptor_sha256="d" * 64,
            actor_id="learner_000",
            attempt_id="attempt-1",
            max_clock_skew_seconds=0.0,
        )
        is None
    )
    publish_admission_response(
        paths,
        epoch=2,
        owner_id="owner-current",
        request=request,
        fence=fence,
        resume=resume,
    )
    admitted = read_admission_response(
        paths,
        run_id="run-1",
        descriptor_sha256="d" * 64,
        actor_id="learner_000",
        attempt_id="attempt-1",
        max_clock_skew_seconds=0.0,
    )
    assert admitted is not None and admitted.fence == fence


def test_receipt_ack_is_current_epoch_fenced_and_byte_idempotent(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    cycle_receipt = receipt()
    old = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-v4", epoch=1, owner_id="owner-old"),
        lease_duration_seconds=30.0,
    )
    current = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-v4", epoch=2, owner_id="owner-current"),
        lease_duration_seconds=30.0,
    )
    old.publish_heartbeat()
    old.publish_receipt_ack(cycle_receipt, descriptor_sha256="d" * 64)
    current.publish_heartbeat()
    with pytest.raises(TimeoutError, match="receipt acknowledgement"):
        wait_for_receipt_barrier(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            receipt=cycle_receipt,
            timeout_seconds=0.02,
            poll_seconds=0.005,
            max_clock_skew_seconds=0.0,
        )

    first = current.publish_receipt_ack(cycle_receipt, descriptor_sha256="d" * 64)
    first_bytes = first.read_bytes()
    second = current.publish_receipt_ack(cycle_receipt, descriptor_sha256="d" * 64)
    assert second == first
    assert second.read_bytes() == first_bytes
    barrier = wait_for_receipt_barrier(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        receipt=cycle_receipt,
        timeout_seconds=0.1,
        poll_seconds=0.005,
        max_clock_skew_seconds=0.0,
    )
    assert barrier.kind == "ack"
    assert barrier.payload["epoch"] == 2


def test_epoch_control_ignores_polluted_fixed_cache_and_repairs_it(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    publisher = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-v4", epoch=2, owner_id="owner-current"),
        lease_duration_seconds=30.0,
    )
    publisher.publish_heartbeat()
    version = CommittedVersion(
        version=3,
        predecessor_version=2,
        publication_id="publication-3",
        weight_relative_path="weights/epochs/e2/owner/global-v3.safetensors",
        weight_size=10,
        weight_sha256="a" * 64,
        optim_relative_path="optim/epochs/e2/owner/outer-v3.safetensors",
        optim_size=11,
        optim_sha256="b" * 64,
        theta_sha256="c" * 64,
        committed_by_epoch=2,
        committed_by_owner_id="owner-current",
        committed_at=10.0,
        direct_weight_tokens_applied=32,
    )
    expected = publisher.publish_latest(version)
    atomic_write_json(
        paths.latest_json,
        {"run_id": "run-v4", "epoch": 999, "owner_id": "stale", "version": 999},
    )
    current = read_current_control(paths, run_id="run-v4")
    assert current is not None and current.latest == expected
    publisher.publish_latest(version)
    assert yaml.safe_load(paths.latest_json.read_text(encoding="utf-8")) == expected


def test_authority_missing_fails_closed_even_when_fixed_cache_exists(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    atomic_write_json(
        paths.latest_json,
        {"run_id": "run-v4", "version": 7, "weight_path": "weights/global-v7"},
    )
    identity = AuthorityIdentity("run-v4", "source", "d" * 64)
    with pytest.raises((FileNotFoundError, RuntimeError, OSError)):
        LeaderAuthority(
            paths.sqlite_db,
            identity,
            StaticMembershipScope(("learner-0",)),
            marker_path=paths.bootstrap_complete_json,
        )


def test_candidate_error_fences_epoch_and_manual_successor_can_acquire(tmp_path: Path) -> None:
    identity = AuthorityIdentity("run-v4", "source", "d" * 64)
    scope = StaticMembershipScope(("learner-0",))
    from fs_diloco.storage.authority import initialize_authority_v4
    from fs_diloco.storage.leader_lease import StaleLeaderTokenError

    initialize_authority_v4(tmp_path / "authority.sqlite3", identity, scope)
    with LeaderAuthority(tmp_path / "authority.sqlite3", identity, scope) as authority:
        failed = authority.acquire_leader(owner_id="failed", hostname="host", pid=1)
        authority.fail_leader(failed)
        with pytest.raises(StaleLeaderTokenError):
            authority.renew_leader(failed)
        successor = authority.acquire_leader(owner_id="manual", hostname="host", pid=2)
        rows = authority.read.syncer_epochs()
        assert [(row["epoch"], row["final_state"]) for row in rows] == [
            (1, "error"),
            (2, None),
        ]
        authority.release_leader(successor)


def test_candidate_failure_hook_is_exact_and_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raise_injected_candidate_failure(100)
    monkeypatch.setenv("FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION", "2")
    _raise_injected_candidate_failure(1)
    with pytest.raises(RuntimeError, match="committed version 2"):
        _raise_injected_candidate_failure(2)


def test_epoch_publication_paths_separate_same_version_and_owner() -> None:
    paths = RunPaths(Path("/run"))
    first = paths.epoch_weight_path(1, "owner-a", 3, "publication-a")
    successor = paths.epoch_weight_path(2, "owner-b", 3, "publication-a")
    collision_peer = paths.epoch_weight_path(1, "owner-a", 3, "publication-b")
    assert len({first, successor, collision_peer}) == 3


def test_indexed_runtime_data_resumes_without_replaying_prefix() -> None:
    config = load_config_v4(
        "configs/fs_diloco_tiny_ha_static.yaml",
        profile=ConfigProfile.FULL_V4,
    ).shared

    class Tokenizer:
        vocab_size = 64

    identity = "a" * 64
    first_cursor = IndexedBlockCursor(
        stable_contributor_key="learner_000",
        dataset_identity_sha256=identity,
        seed=config.training.seed,
        block_index=0,
        shard_index=0,
        shard_count=2,
    )
    first_stream = build_indexed_batch_iterator(config, Tokenizer(), cursor=first_cursor)
    first = next(first_stream).input_ids
    second = next(first_stream).input_ids
    resumed = next(
        build_indexed_batch_iterator(
            config,
            Tokenizer(),
            cursor=first_cursor.advance(),
        )
    ).input_ids
    peer = next(
        build_indexed_batch_iterator(
            config,
            Tokenizer(),
            cursor=IndexedBlockCursor(
                stable_contributor_key="learner_001",
                dataset_identity_sha256=identity,
                seed=config.training.seed,
                block_index=0,
                shard_index=1,
                shard_count=2,
            ),
        )
    ).input_ids
    assert first.tolist() != second.tolist()
    assert resumed.tolist() == second.tolist()
    assert peer.tolist() != first.tolist()


def test_dynamic_launcher_preserves_each_partial_submission_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = resolve_config(project_root=tmp_path)
    shared.membership.mode = "dynamic"
    shared.membership.stream_pool_size = 2
    shared.membership.bootstrap_instances = 2
    shared.sync.num_learners = 2
    shared.sync.quorum_min = 1
    shared.sync.quorum_max = 2
    shared.scaling.desired_contributors = 1
    config = ConfigV4(shared=shared)
    monkeypatch.setattr(launch_independent_run, "resolve_config_v4", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        launch_independent_run,
        "initialize_run",
        lambda *args, **kwargs: {
            "descriptor": {
                "shared_root": str(tmp_path / "run"),
                "descriptor_sha256": "d" * 64,
            }
        },
    )
    submissions = iter(
        (
            subprocess.CompletedProcess([], 0, "100.opbs\n", ""),
            subprocess.CompletedProcess([], 0, "101.opbs\n", ""),
            subprocess.CompletedProcess([], 1, "", "rejected"),
        )
    )
    monkeypatch.setattr(
        launch_independent_run.subprocess,
        "run",
        lambda *args, **kwargs: next(submissions),
    )
    result = launch_independent_run.launch(
        config_path=tmp_path / "config.yaml",
        run_id="dynamic",
        shared_root=str(tmp_path / "run"),
        project_root=tmp_path,
        submit=True,
        allow_dirty_snapshot=False,
        syncer_walltime="00:10:00",
        learner_walltime="00:10:00",
    )
    assert result["submission_status"] == "partial"
    assert result["syncer_job_id"] == "100.opbs"
    assert result["accepted_learner_job_ids"] == ["101.opbs"]
    assert [item["bootstrap_slot"] for item in result["learner_submissions"]] == [0, 1]


def test_learner_timeout_path_does_not_import_torch_before_admission(
    tmp_path: Path,
) -> None:
    shared = resolve_config(
        project_root=tmp_path,
        run_id="pre-torch",
        shared_root=str(tmp_path / "run"),
    )
    shared.run.git_commit = "a" * 40
    shared.run.git_dirty = False
    shared.run.source_fingerprint = "source-pre-torch"
    shared.sync.num_learners = 1
    shared.sync.quorum_min = 1
    shared.sync.quorum_max = 1
    config = ConfigV4(
        shared=shared,
        leader=LeaderSection(
            lease_duration_seconds=0.5,
            renew_interval_seconds=0.05,
            max_clock_skew_seconds=0.01,
            heartbeat_interval_seconds=0.05,
            heartbeat_stale_after_seconds=0.16,
            lease_busy_timeout_ms=50,
            business_busy_timeout_ms=1000,
            candidate_acquire_poll_seconds=0.05,
            candidate_wait_seconds=0.6,
            learner_recovery_wait_seconds=0.6,
            canonical_repair_wait_seconds=0.1,
            max_retained_epoch_dirs=2,
        ),
        maintenance=MaintenanceSection(publication_orphan_grace_seconds=0.6),
    )
    initialized = initialize_run(config, project_root=tmp_path)
    descriptor = initialized["descriptor"]
    code = """
import sys
from fs_diloco.runtime.learner_entrypoint import main
try:
    main(sys.argv[1:])
except TimeoutError:
    print('TORCH_IMPORTED=' + str('torch' in sys.modules))
else:
    raise SystemExit('expected admission timeout')
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "--config",
            descriptor["resolved_config_path"],
            "--shared-root",
            descriptor["shared_root"],
            "--learner-id",
            "learner_000",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    assert "TORCH_IMPORTED=False" in completed.stdout
