from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.core.run_descriptor import load_run_descriptor, write_actor_attestation
from fs_diloco.storage import run_initializer as initializer_module
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.run_initializer import (
    claim_identity_reservation,
    create_staging_root,
    find_reserved_staging,
    repair_identity_reservation,
    reservation_path,
    validate_completed_run,
)
from fs_diloco.tools.init_run import initialize_run


PLAN03_REQUIREMENTS = frozenset({"ENV-01", "INIT-01"})


class InjectedCrash(RuntimeError):
    pass


def _config(tmp_path: Path, name: str = "run"):
    root = tmp_path / name
    config = resolve_config(
        project_root=tmp_path,
        run_id=name,
        shared_root=str(root),
    )
    config.coordination.syncer_ha.enabled = True
    config.run.git_commit = "a" * 40
    config.run.git_dirty = False
    config.run.source_fingerprint = "sha256:source"
    config.model.revision = "model-revision"
    config.model.tokenizer_revision = "tokenizer-revision"
    config.data.revision = "dataset-revision"
    return config


@pytest.mark.parametrize(
    "fault_point",
    [
        "after_identity_reservation",
        "after_final_mkdir",
        "after_final_identity",
        "after_object_link:0",
        "before_complete_marker",
    ],
)
def test_each_precomplete_crash_prefix_is_invisible_and_same_staging_retry_recovers(
    tmp_path: Path, fault_point: str
) -> None:
    config = _config(tmp_path, fault_point.replace(":", "-"))

    def fault(point: str) -> None:
        if point == fault_point:
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash, match=fault_point):
        initialize_run(config, project_root=tmp_path, fault_hook=fault)
    with pytest.raises((FileNotFoundError, RuntimeError)):
        load_run_descriptor(config.run.shared_root)

    initialize_run(config, project_root=tmp_path)
    loaded = load_run_descriptor(config.run.shared_root)
    assert loaded.descriptor["shared_root"] == str(Path(config.run.shared_root).resolve())
    assert loaded.descriptor["model_identity"]["revision"] == "model-revision"
    assert loaded.descriptor["dataset_identity"]["revision"] == "dataset-revision"
    assert (
        find_reserved_staging(Path(config.run.shared_root).resolve(), allow_missing_owner=True)
        is None
    )


def test_crash_after_complete_is_visible_and_retry_removes_staging_alias(tmp_path: Path) -> None:
    config = _config(tmp_path, "post-complete")

    def fault(point: str) -> None:
        if point == "after_complete_marker":
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash, match="after_complete_marker"):
        initialize_run(config, project_root=tmp_path, fault_hook=fault)
    final_root = Path(config.run.shared_root).resolve()
    load_run_descriptor(final_root)
    assert find_reserved_staging(final_root) is not None

    assert initialize_run(config, project_root=tmp_path)["recovered"] is True
    assert find_reserved_staging(final_root, allow_missing_owner=True) is None


def test_fresh_initialization_is_not_reported_as_recovery(tmp_path: Path) -> None:
    config = _config(tmp_path, "fresh-result")

    assert initialize_run(config, project_root=tmp_path)["recovered"] is False


def test_retry_and_completed_replay_bind_the_entire_resolved_config_identity(
    tmp_path: Path,
) -> None:
    staged = _config(tmp_path, "staged-config-identity")

    def crash(point: str) -> None:
        if point == "after_identity_reservation":
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        initialize_run(staged, project_root=tmp_path, fault_hook=crash)
    staged.data.revision = "different-dataset-revision"
    with pytest.raises(FileExistsError, match="full config identity"):
        initialize_run(staged, project_root=tmp_path)

    completed = _config(tmp_path, "completed-config-identity")
    initialize_run(completed, project_root=tmp_path)
    completed.training.inner_steps += 1
    with pytest.raises(FileExistsError, match="full config identity"):
        initialize_run(completed, project_root=tmp_path)


def test_descriptor_validation_is_bounded_and_accepts_runtime_control_publications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, "runtime-publications")
    initialize_run(config, project_root=tmp_path)
    paths = RunPaths(Path(config.run.shared_root))
    for path in (
        paths.latest_json,
        paths.param_index_json,
        paths.stop_json,
        paths.summary_json,
        paths.dynamic_close_request_json,
        paths.bootstrap_scheduler_jobs_json,
        paths.sqlite_db.with_name(paths.sqlite_db.name + "-journal"),
    ):
        path.write_text("{}\n", encoding="utf-8")
    (paths.audit_batches / "history").mkdir(parents=True)
    for index in range(20):
        (paths.audit_batches / "history" / f"batch-{index}.json").write_text(
            "{}\n", encoding="utf-8"
        )

    def forbidden_rglob(_self: Path, _pattern: str):
        raise AssertionError("actor startup must not recursively scan the run root")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)
    loaded = load_run_descriptor(paths.shared_root)
    assert loaded.descriptor["run_id"] == config.run.run_id


def test_completed_descriptor_rejects_identity_mode_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path, "identity-mode-mismatch")
    initialize_run(config, project_root=tmp_path)
    identity_path = RunPaths(Path(config.run.shared_root)).run_identity_file
    identity = read_json(identity_path)
    identity["mode"] = "full_dynamic"
    content = {key: value for key, value in identity.items() if key != "identity_sha256"}
    identity["identity_sha256"] = hashlib.sha256(
        initializer_module.canonical_json_bytes(content)
    ).hexdigest()
    identity_path.chmod(0o644)
    identity_path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
    identity_path.chmod(0o444)

    with pytest.raises(RuntimeError, match="run identity does not match descriptor.*mode"):
        load_run_descriptor(config.run.shared_root)


def test_every_manifest_object_link_fault_is_invisible_and_retryable(tmp_path: Path) -> None:
    observed: list[str] = []
    probe = _config(tmp_path, "object-link-probe")
    initialize_run(probe, project_root=tmp_path, fault_hook=observed.append)
    points = [point for point in observed if point.startswith("after_object_link:")]
    assert points == [f"after_object_link:{index}" for index in range(len(points))]
    assert len(points) >= 5

    for index, fault_point in enumerate(points):
        config = _config(tmp_path, f"object-link-{index}")

        def fault(point: str, *, expected: str = fault_point) -> None:
            if point == expected:
                raise InjectedCrash(point)

        with pytest.raises(InjectedCrash, match=fault_point):
            initialize_run(config, project_root=tmp_path, fault_hook=fault)
        with pytest.raises((FileNotFoundError, RuntimeError)):
            load_run_descriptor(config.run.shared_root)
        initialize_run(config, project_root=tmp_path)
        load_run_descriptor(config.run.shared_root)


def test_every_initializer_directory_fsync_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = initializer_module.fsync_directory
    observed: list[Path] = []

    def observe(path: Path) -> None:
        observed.append(path)
        real_fsync(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(initializer_module, "fsync_directory", observe)
        initialize_run(_config(tmp_path, "fsync-probe"), project_root=tmp_path)
    assert len(observed) >= 5

    for failure_index in range(len(observed)):
        config = _config(tmp_path, f"fsync-{failure_index}")
        call_count = 0

        def fail_once(path: Path) -> None:
            nonlocal call_count
            current = call_count
            call_count += 1
            if current == failure_index:
                raise InjectedCrash(f"fsync:{failure_index}")
            real_fsync(path)

        with monkeypatch.context() as patcher:
            patcher.setattr(initializer_module, "fsync_directory", fail_once)
            with pytest.raises(InjectedCrash, match=f"fsync:{failure_index}"):
                initialize_run(config, project_root=tmp_path)
        final_root = Path(config.run.shared_root)
        if (final_root / ".complete").is_file():
            load_run_descriptor(final_root)
        else:
            with pytest.raises((FileNotFoundError, RuntimeError)):
                load_run_descriptor(final_root)
        initialize_run(config, project_root=tmp_path)
        load_run_descriptor(final_root)


def test_different_inode_staging_cannot_take_an_existing_identity_reservation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "collision")

    def crash_after_reservation(point: str) -> None:
        if point == "after_identity_reservation":
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        initialize_run(config, project_root=tmp_path, fault_hook=crash_after_reservation)
    final_root = Path(config.run.shared_root).resolve()
    owner = find_reserved_staging(final_root)
    assert owner is not None
    impostor = create_staging_root(final_root)
    shutil.copyfile(owner / ".identity", impostor / ".identity")
    os.chmod(impostor / ".identity", 0o444)

    with pytest.raises(FileExistsError, match="owned by another"):
        claim_identity_reservation(final_root=final_root, staging_root=impostor)


def test_completed_run_reservation_repair_requires_explicit_full_self_check(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "repair")
    initialize_run(config, project_root=tmp_path)
    final_root = Path(config.run.shared_root).resolve()
    reservation = reservation_path(final_root)
    reservation.unlink()

    with pytest.raises(RuntimeError, match="reservation"):
        validate_completed_run(final_root)
    repaired = repair_identity_reservation(final_root)
    assert repaired == reservation
    validate_completed_run(final_root)


def test_reservation_repair_rejects_protocol_external_entry(tmp_path: Path) -> None:
    config = _config(tmp_path, "repair-external")
    initialize_run(config, project_root=tmp_path)
    final_root = Path(config.run.shared_root).resolve()
    reservation_path(final_root).unlink()
    (final_root / "foreign.txt").write_text("not protocol-owned", encoding="utf-8")

    with pytest.raises(RuntimeError, match="protocol-external"):
        repair_identity_reservation(final_root)
    assert not reservation_path(final_root).exists()


def test_existing_final_race_releases_new_reservation_before_fault_hook(tmp_path: Path) -> None:
    final_root = tmp_path / "race"
    staging = create_staging_root(final_root)
    (staging / ".identity").write_text("identity", encoding="utf-8")
    (staging / ".identity").chmod(0o444)
    final_root.mkdir()
    invoked: list[str] = []

    with pytest.raises(FileExistsError, match="appeared"):
        claim_identity_reservation(
            final_root=final_root,
            staging_root=staging,
            fault_hook=invoked.append,
        )
    assert invoked == []
    assert not reservation_path(final_root).exists()


def test_broken_symlink_final_collision_releases_new_reservation(tmp_path: Path) -> None:
    final_root = tmp_path / "broken-link-race"
    staging = create_staging_root(final_root)
    (staging / ".identity").write_text("identity", encoding="utf-8")
    (staging / ".identity").chmod(0o444)
    final_root.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    with pytest.raises(FileExistsError, match="appeared"):
        claim_identity_reservation(final_root=final_root, staging_root=staging)
    assert not reservation_path(final_root).exists()


def test_retry_rejects_symlinked_manifest_directory_without_writing_outside(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "symlink-directory")

    def fault(point: str) -> None:
        if point == "after_final_identity":
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        initialize_run(config, project_root=tmp_path, fault_hook=fault)
    final_root = Path(config.run.shared_root).resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    (final_root / "audit").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileExistsError, match="directory collision"):
        initialize_run(config, project_root=tmp_path)
    assert list(outside.iterdir()) == []
    assert not (final_root / ".complete").exists()


def test_retry_rejects_changed_staging_object_before_complete(tmp_path: Path) -> None:
    config = _config(tmp_path, "changed-staging")

    def fault(point: str) -> None:
        if point == "after_identity_reservation":
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        initialize_run(config, project_root=tmp_path, fault_hook=fault)
    final_root = Path(config.run.shared_root).resolve()
    staging = find_reserved_staging(final_root)
    assert staging is not None
    config_path = staging / "control/run_config.resolved.yaml"
    config_path.chmod(0o644)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8"
    )
    config_path.chmod(0o444)

    with pytest.raises(RuntimeError, match="staging object changed"):
        initialize_run(config, project_root=tmp_path)
    assert not (final_root / ".complete").exists()


def test_reservation_repair_reopens_and_integrity_checks_mutable_authority_db(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "repair-corrupt-db")
    initialize_run(config, project_root=tmp_path)
    final_root = Path(config.run.shared_root).resolve()
    reservation_path(final_root).unlink()
    database = RunPaths(final_root).sqlite_db
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(
        (sqlite3.DatabaseError, RuntimeError), match="database|SQLite|file is not a database"
    ):
        repair_identity_reservation(final_root)
    assert not reservation_path(final_root).exists()


def test_actor_attestation_is_immutable_and_attempt_scoped(tmp_path: Path) -> None:
    config = _config(tmp_path, "attestation")
    initialize_run(config, project_root=tmp_path)
    loaded = load_run_descriptor(config.run.shared_root)
    path = write_actor_attestation(
        loaded,
        actor_kind="learner",
        actor_id="learner-0",
        attempt_id="attempt-1",
        runtime_evidence={
            "torch_version": "2.13.0",
            "cuda_runtime_version": "13.2",
            "gpu_driver_version": "580.0",
            "module_environment": ["nvidia/25.9", "nv-hpcx/25.9"],
            "resource_allocation": {"pbs_job_id": "123.opbs", "gpu_count": 8},
        },
        scheduler_job_id="123.opbs",
        observed_at=123.0,
    )
    assert path == RunPaths(Path(config.run.shared_root)).actor_attestation_path(
        "learner", "learner-0", "attempt-1"
    )
    assert path.stat().st_mode & 0o222 == 0
    assert (
        write_actor_attestation(
            loaded,
            actor_kind="learner",
            actor_id="learner-0",
            attempt_id="attempt-1",
            runtime_evidence={
                "torch_version": "2.13.0",
                "cuda_runtime_version": "13.2",
                "gpu_driver_version": "580.0",
                "module_environment": ["nvidia/25.9", "nv-hpcx/25.9"],
                "resource_allocation": {"pbs_job_id": "123.opbs", "gpu_count": 8},
            },
            scheduler_job_id="123.opbs",
            observed_at=123.0,
        )
        == path
    )
    payload = read_json(path)
    assert payload["runtime_evidence"]["torch_version"] == "2.13.0"
    assert payload["runtime_evidence"]["resource_allocation"]["gpu_count"] == 8


def test_actor_attestation_requires_explicit_runtime_resource_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, "attestation-required")
    initialize_run(config, project_root=tmp_path)
    loaded = load_run_descriptor(config.run.shared_root)

    with pytest.raises(ValueError, match="runtime_evidence fields"):
        write_actor_attestation(
            loaded,
            actor_kind="syncer",
            actor_id="syncer-0",
            attempt_id="attempt-1",
            runtime_evidence={},
        )
