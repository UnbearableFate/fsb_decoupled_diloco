from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import signal
from types import SimpleNamespace

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
from fs_diloco.storage.admission import (
    AdmissionAuthorizationError,
    AdmissionRejectedError,
    admission_request_sha256,
    archive_disposed_admission_request,
    dynamic_placement_id,
    publish_admission_disposition,
    publish_admission_rejection,
    publish_admission_response,
    publish_static_replacement_authorization,
    publish_static_request,
    read_admission_response,
    read_static_replacement_authorization,
)
from fs_diloco.storage import admission as admission_protocol
from fs_diloco.protocol.contributor import (
    DynamicContributorFence,
    StaticContributorFence,
    StaticMembershipScope,
)
from fs_diloco.storage.control import (
    V4ControlPublisher,
    read_current_control,
    wait_for_receipt_barrier,
)
from fs_diloco.protocol.data_cursor import ContributorResumeState, IndexedBlockCursor
from fs_diloco.protocol.cycle_receipt import (
    canonical_receipt_relative_path,
    contributor_fence_namespace,
)
from fs_diloco.storage.atomic_io import atomic_write_json, sha256_file
from fs_diloco.storage.authority import (
    AuthoritySchemaError,
    AuthorityIdentity,
    CommittedVersion,
    LeaderAuthority,
    initialize_authority_v4,
)
from fs_diloco.storage.leader_lease import (
    CommittedLeaderLease,
    LeaderToken,
    StaleLeaderTokenError,
)
from fs_diloco.storage.paths import RunPaths
from fs_diloco.tools import launch_independent_run
from fs_diloco.tools.init_run import initialize_run
from fs_diloco.tools.launch_independent_run import _walltime_resource
from fs_diloco.tools import migrate_config_v3_to_v4 as migration_tool
from fs_diloco.tools.migrate_config_v3_to_v4 import migrate
from fs_diloco.runtime.syncer_v4 import (
    _admit_requests,
    _pause_candidate_outside_transaction,
    _raise_injected_candidate_failure,
)
from fs_diloco.runtime import syncer_v4 as syncer_runtime
from tests.support.v4_protocol import receipt


PLAN03_REQUIREMENTS = frozenset(
    {
        "AUTH-02",
        "AUTH-03",
        "AUTH-04",
        "AUTH-05",
        "AUTH-07",
        "AUTH-09",
        "AUTH-10",
        "AUTH-11",
        "MODE-02",
        "P4-MIGRATE",
    }
)


class _TelemetryProbe:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


def _static_admission_runtime(tmp_path: Path):
    paths = RunPaths(tmp_path)
    identity = AuthorityIdentity("run-v4", "source-fingerprint", "d" * 64)
    scope = StaticMembershipScope(("learner_000",))
    initialize_authority_v4(paths.sqlite_db, identity, scope, wall_clock=lambda: 100.0)
    authority = LeaderAuthority(paths.sqlite_db, identity, scope, wall_clock=lambda: 100.0)
    token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
    loaded = SimpleNamespace(
        paths=paths,
        descriptor={"run_id": "run-v4", "descriptor_sha256": "d" * 64},
    )
    return paths, authority, authority.open_leader(token), loaded


def _publish_synthetic_heartbeat(publisher: V4ControlPublisher, *, sequence: int = 1) -> None:
    renewed_at = time.time()
    publisher.publish_heartbeat(
        CommittedLeaderLease(
            token=publisher.token,
            renewed_at=renewed_at,
            lease_expires_at=renewed_at + 30.0,
            heartbeat_seq=sequence,
        )
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


def test_strict_v4_configs_have_no_classic_oracle_projection() -> None:
    for path in (
        "configs/fs_diloco_tiny_local.yaml",
        "configs/fs_diloco_tiny_ha_static.yaml",
    ):
        shared = resolve_config(path)
        assert not hasattr(shared, "init")
        assert not hasattr(shared, "fragments")
        assert not hasattr(shared, "coordination")


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
        Path("fs_diloco/storage/control.py"),
        Path("fs_diloco/storage/admission.py"),
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


def test_in_place_migration_revalidates_source_at_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "v3.yaml"
    source.write_text("run:\n  name: original\n", encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    concurrent_edit = b"run:\n  name: concurrent-edit\n"
    original_migrate = migration_tool.migrate_v3_bytes_to_v4

    def edit_after_initial_read(data: bytes):
        source.write_bytes(concurrent_edit)
        return original_migrate(data)

    monkeypatch.setattr(migration_tool, "migrate_v3_bytes_to_v4", edit_after_initial_read)
    with pytest.raises(RuntimeError, match="changed"):
        migrate(source, in_place=True, expected_sha256=expected)
    assert source.read_bytes() == concurrent_edit


def test_migration_output_failure_never_exposes_partial_final_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "v3.yaml"
    source.write_text("run:\n  name: migrate\n", encoding="utf-8")
    output = tmp_path / "v4.yaml"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(migration_tool.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        migrate(source, output_path=output)
    assert not output.exists()


def test_concurrent_migration_output_publishers_never_overwrite_winner(tmp_path: Path) -> None:
    source = tmp_path / "v3.yaml"
    source.write_text("run:\n  name: migrate\n", encoding="utf-8")
    output = tmp_path / "v4.yaml"
    barrier = threading.Barrier(2)

    def publish() -> str:
        barrier.wait()
        try:
            migrate(source, output_path=output)
        except FileExistsError:
            return "collision"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        outcomes = sorted(future.result() for future in futures)

    assert outcomes == ["collision", "published"]
    load_config_v4(output, profile=ConfigProfile.FULL_V4)


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


def test_default_static_duplicate_cannot_replace_an_active_binding(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        first = authority.read.static_binding("learner_000")
        assert first is not None and first.attempt_id == "attempt-1"

        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-2",
            expected_generation=first.binding_generation,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        current = authority.read.static_binding("learner_000")
        assert current is not None
        assert current.attempt_id == "attempt-1"
        assert current.binding_generation == 1
        assert [name for name, _fields in telemetry.events].count("learner_admitted") == 1
        assert [name for name, _fields in telemetry.events].count("admission_rejected") == 1
    finally:
        authority.close()


def test_exact_operator_authorization_allows_active_static_replacement(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        first = authority.read.static_binding("learner_000")
        assert first is not None
        old_fence = StaticContributorFence(
            "static",
            first.learner_id,
            first.logical_launch_id,
            first.attempt_id,
            first.binding_generation,
        )
        publish_static_replacement_authorization(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            old_fence=old_fence,
            new_logical_launch_id="logical-1",
            new_attempt_id="attempt-2",
            reason="operator observed exact old process termination",
        )
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-2",
            expected_generation=1,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        current = authority.read.static_binding("learner_000")
        assert current is not None
        assert current.attempt_id == "attempt-2"
        assert current.binding_generation == 2
    finally:
        authority.close()


def test_static_replacement_authorization_rejects_nonfinite_timestamp(
    tmp_path: Path,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        first = authority.read.static_binding("learner_000")
        assert first is not None
        old_fence = StaticContributorFence(
            "static",
            first.learner_id,
            first.logical_launch_id,
            first.attempt_id,
            first.binding_generation,
        )
        request_path = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-2",
            expected_generation=1,
        )
        authorization_path = publish_static_replacement_authorization(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            old_fence=old_fence,
            new_logical_launch_id="logical-1",
            new_attempt_id="attempt-2",
            reason="operator observed exact old process termination",
        )
        authorization = json.loads(authorization_path.read_text())
        authorization["created_at"] = float("nan")
        authorization_path.chmod(0o600)
        authorization_path.write_text(json.dumps(authorization), encoding="utf-8")

        with pytest.raises(
            AdmissionAuthorizationError,
            match="authorization payload is invalid",
        ):
            read_static_replacement_authorization(
                paths,
                request=json.loads(request_path.read_text()),
                current_fence=old_fence,
            )
    finally:
        authority.close()


def test_disposed_admission_request_leaves_hot_scan_and_emits_once(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        _admit_requests(loaded, authority, leader, telemetry)

        assert not tuple(paths.registration_requests.rglob("*.json"))
        assert [name for name, _fields in telemetry.events] == ["learner_admitted"]
        dispositions = tuple((paths.control / "registration_dispositions_v4").rglob("*.json"))
        assert len(dispositions) == 1
        payload = json.loads(dispositions[0].read_text(encoding="utf-8"))
        assert payload["outcome"] == "admitted"
    finally:
        authority.close()


def test_malformed_and_foreign_admission_requests_leave_hot_scan_once(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    malformed = paths.registration_requests / "static" / "learner_000" / "broken.json"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_bytes(b'{"format_version":1')
    foreign = paths.registration_requests / "dynamic" / "foreign.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(
        json.dumps(
            {
                "format_version": 1,
                "mode": "dynamic",
                "run_id": "another-run",
                "descriptor_sha256": "e" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        _admit_requests(loaded, authority, leader, telemetry)
        _admit_requests(loaded, authority, leader, telemetry)

        assert not tuple(paths.registration_requests.rglob("*.json"))
        assert [name for name, _fields in telemetry.events] == [
            "admission_request_discarded",
            "admission_request_discarded",
        ]
        dispositions = tuple(paths.registration_dispositions_v4.rglob("*.json"))
        history = tuple(paths.registration_history_v4.rglob("*.json"))
        assert len(dispositions) == 2
        assert len(history) == 2
        assert {
            json.loads(path.read_text(encoding="utf-8"))["error_type"] for path in dispositions
        } == {"ForeignAdmissionRequest", "MalformedAdmissionRequest"}
    finally:
        authority.close()


def test_identical_malformed_requests_share_archive_without_hot_path_collision(
    tmp_path: Path,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    request_bytes = b'{"format_version":1'
    for learner_id in ("learner_000", "learner_001"):
        path = paths.registration_requests / "static" / learner_id / "broken.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(request_bytes)
    try:
        _admit_requests(loaded, authority, leader, telemetry)

        assert not tuple(paths.registration_requests.rglob("*.json"))
        assert len(tuple(paths.registration_dispositions_v4.rglob("*.json"))) == 1
        assert len(tuple(paths.registration_history_v4.rglob("*.json"))) == 1
        assert [name for name, _fields in telemetry.events] == [
            "admission_request_discarded",
            "admission_request_discarded",
        ]
    finally:
        authority.close()


def test_unreadable_hot_entry_does_not_block_other_admissions(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    poison = paths.registration_requests / "static" / "learner_000" / "000-poison.json"
    poison.mkdir(parents=True)
    publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    try:
        _admit_requests(loaded, authority, leader, telemetry)

        binding = authority.read.static_binding("learner_000")
        assert binding is not None and binding.attempt_id == "attempt-1"
        assert poison.is_dir()
    finally:
        authority.close()


def test_invalid_utf8_hot_request_is_disposed_once_without_candidate_failure(
    tmp_path: Path,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    invalid = paths.registration_requests / "static" / "learner_000" / "invalid-utf8.json"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(b'{"format_version":"\xff\xfe\xfa"}')
    try:
        _admit_requests(loaded, authority, leader, telemetry)
        _admit_requests(loaded, authority, leader, telemetry)

        assert not invalid.exists()
        dispositions = tuple(paths.registration_dispositions_v4.rglob("*.json"))
        assert len(dispositions) == 1
        assert json.loads(dispositions[0].read_bytes())["error_type"] == (
            "MalformedAdmissionRequest"
        )
    finally:
        authority.close()


def test_one_shot_hot_read_error_retries_without_discarding_valid_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    from fs_diloco.storage import admission

    original_read = admission._read_hot_request
    injected = False

    def fail_once(path: Path):
        nonlocal injected
        if path == request_path and not injected:
            injected = True
            raise OSError(5, "injected shared-filesystem read failure")
        return original_read(path)

    monkeypatch.setattr(admission, "_read_hot_request", fail_once)
    try:
        _admit_requests(loaded, authority, leader, telemetry)
        assert request_path.is_file()
        assert authority.read.static_binding("learner_000") is None
        assert not tuple(paths.registration_dispositions_v4.rglob("*.json"))

        _admit_requests(loaded, authority, leader, telemetry)
        binding = authority.read.static_binding("learner_000")
        assert binding is not None and binding.attempt_id == "attempt-1"
        assert not request_path.exists()
    finally:
        authority.close()


def test_semantically_identical_valid_request_archives_canonical_bytes(
    tmp_path: Path,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    request = json.loads(request_path.read_bytes())
    try:
        _admit_requests(loaded, authority, leader, telemetry)
        history = paths.registration_history_path(admission_request_sha256(request))
        first_bytes = history.read_bytes()

        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        _admit_requests(loaded, authority, leader, telemetry)

        assert not request_path.exists()
        assert history.read_bytes() == first_bytes
        assert json.loads(first_bytes) == request
        assert hashlib.sha256(first_bytes).hexdigest() == admission_request_sha256(request)
    finally:
        authority.close()


def test_request_specific_rejections_do_not_collide_when_attempt_id_is_reused(
    tmp_path: Path,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-0",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        for expected_generation in (99, 1):
            publish_static_request(
                paths,
                run_id="run-v4",
                descriptor_sha256="d" * 64,
                learner_id="learner_000",
                logical_launch_id="logical-1",
                attempt_id="attempt-1",
                expected_generation=expected_generation,
            )
            _admit_requests(loaded, authority, leader, telemetry)

        rejection_root = (
            paths.epoch_membership_dir(leader.token.epoch, leader.token.owner_id)
            / "admissions_v4"
            / "rejections"
        )
        assert len(tuple(rejection_root.rglob("*.json"))) == 2
        assert not tuple(paths.registration_requests.rglob("*.json"))
    finally:
        authority.close()


def test_authorized_replacement_can_reuse_an_old_attempt_id(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        for old_attempt, new_attempt, generation in (
            ("attempt-1", "attempt-2", 1),
            ("attempt-2", "attempt-1", 2),
        ):
            binding = authority.read.static_binding("learner_000")
            assert binding is not None and binding.attempt_id == old_attempt
            old_fence = StaticContributorFence(
                "static",
                binding.learner_id,
                binding.logical_launch_id,
                binding.attempt_id,
                binding.binding_generation,
            )
            publish_static_replacement_authorization(
                paths,
                run_id="run-v4",
                descriptor_sha256="d" * 64,
                old_fence=old_fence,
                new_logical_launch_id="logical-1",
                new_attempt_id=new_attempt,
                reason=f"operator replacement generation {generation}",
            )
            publish_static_request(
                paths,
                run_id="run-v4",
                descriptor_sha256="d" * 64,
                learner_id="learner_000",
                logical_launch_id="logical-1",
                attempt_id=new_attempt,
                expected_generation=generation,
            )
            _admit_requests(loaded, authority, leader, telemetry)

        current = authority.read.static_binding("learner_000")
        assert current is not None
        assert (current.attempt_id, current.binding_generation) == ("attempt-1", 3)
        pointer = json.loads(
            paths.epoch_current_admission_path(
                leader.token.epoch, leader.token.owner_id, "learner_000"
            ).read_bytes()
        )
        assert pointer["fence"]["binding_generation"] == 3
        assert not tuple(paths.registration_requests.rglob("*.json"))
    finally:
        authority.close()


def test_incomplete_admission_disposition_cannot_remove_hot_request(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    request = json.loads(request_path.read_bytes())
    request_sha = admission_request_sha256(request)
    atomic_write_json(
        paths.registration_disposition_path(request_sha),
        {"request_sha256": request_sha},
    )
    try:
        _admit_requests(loaded, authority, leader, telemetry)

        assert request_path.is_file()
        assert authority.read.static_binding("learner_000") is None
        assert [name for name, _fields in telemetry.events] == ["admission_request_deferred"]
    finally:
        authority.close()


def test_admission_control_publication_failure_is_not_persisted_as_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )

    def fail_response(*_args: object, **_kwargs: object) -> Path:
        raise OSError("injected admission response publication failure")

    monkeypatch.setattr(syncer_runtime, "publish_admission_response", fail_response)
    try:
        _admit_requests(loaded, authority, leader, telemetry)

        binding = authority.read.static_binding("learner_000")
        assert binding is not None and binding.attempt_id == "attempt-1"
        assert request_path.is_file()
        assert not tuple(paths.registration_dispositions_v4.rglob("*.json"))
        rejection_root = (
            paths.epoch_membership_dir(leader.token.epoch, leader.token.owner_id)
            / "admissions_v4"
            / "rejections"
        )
        assert not tuple(rejection_root.rglob("*.json"))
        assert [name for name, _fields in telemetry.events] == ["admission_request_deferred"]
    finally:
        authority.close()


def test_admission_disposition_retry_reuses_published_resume_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    original_disposition = syncer_runtime.publish_admission_disposition

    def fail_disposition(*_args: object, **_kwargs: object) -> Path:
        raise OSError("injected disposition publication failure")

    monkeypatch.setattr(syncer_runtime, "publish_admission_disposition", fail_disposition)
    try:
        _admit_requests(loaded, authority, leader, telemetry)
        binding = authority.read.static_binding("learner_000")
        assert binding is not None
        fence = StaticContributorFence(
            "static",
            binding.learner_id,
            binding.logical_launch_id,
            binding.attempt_id,
            binding.binding_generation,
        )
        response_path = paths.epoch_admission_response_path(
            leader.token.epoch,
            leader.token.owner_id,
            fence.learner_id,
            fence.attempt_id,
            contributor_fence_namespace(fence),
        )
        response_before = response_path.read_bytes()
        leader.ingest_cycle_receipt(
            command_id="receipt-after-partial-admission-publication",
            receipt=receipt(
                run_id="run-v4",
                stable_contributor_key="learner_000",
                fence=fence.as_dict(),
            ),
        )

        monkeypatch.setattr(
            syncer_runtime,
            "publish_admission_disposition",
            original_disposition,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        assert response_path.read_bytes() == response_before
        assert not request_path.exists()
        assert len(tuple(paths.registration_dispositions_v4.rglob("*.json"))) == 1
    finally:
        authority.close()


def test_disposition_replay_requires_consumer_valid_resume_and_current_pointer(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path)
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    request = json.loads(request_path.read_bytes())
    fence = StaticContributorFence("static", "learner_000", "logical-1", "attempt-1", 1)
    response = publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        fence=fence,
        resume=ContributorResumeState(0, None, None, 1),
    )
    payload = json.loads(response.read_bytes())
    payload["resume"]["cursor"] = -1
    response.chmod(0o600)
    atomic_write_json(response, payload)
    pointer_path = paths.epoch_current_admission_path(1, "owner-1", "learner_000")
    pointer = json.loads(pointer_path.read_bytes())
    pointer["response_sha256"] = sha256_file(response)
    atomic_write_json(pointer_path, pointer)
    publish_admission_disposition(
        paths,
        request=request,
        epoch=1,
        owner_id="owner-1",
        outcome="admitted",
        control_path=response,
        fence=fence,
    )

    with pytest.raises(RuntimeError, match="resume"):
        archive_disposed_admission_request(
            paths,
            request_path=request_path,
            request=request,
        )
    assert request_path.is_file()


def test_disposition_replay_requires_exact_current_admission_pointer(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    request = json.loads(request_path.read_bytes())
    fence = StaticContributorFence("static", "learner_000", "logical-1", "attempt-1", 1)
    response = publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        fence=fence,
        resume=ContributorResumeState(0, None, None, 1),
    )
    paths.epoch_current_admission_path(1, "owner-1", "learner_000").unlink()
    publish_admission_disposition(
        paths,
        request=request,
        epoch=1,
        owner_id="owner-1",
        outcome="admitted",
        control_path=response,
        fence=fence,
    )

    with pytest.raises(RuntimeError, match="current admission"):
        archive_disposed_admission_request(
            paths,
            request_path=request_path,
            request=request,
        )
    assert request_path.is_file()


def test_disposition_replay_requires_consumer_valid_rejection_message(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    request = json.loads(request_path.read_bytes())
    rejection = publish_admission_rejection(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        error_type="MembershipFenceError",
        message="rejected",
    )
    payload = json.loads(rejection.read_bytes())
    payload["message"] = 7
    rejection.chmod(0o600)
    atomic_write_json(rejection, payload)
    publish_admission_disposition(
        paths,
        request=request,
        epoch=1,
        owner_id="owner-1",
        outcome="rejected",
        control_path=rejection,
        error_type="MembershipFenceError",
    )

    with pytest.raises(RuntimeError, match="rejected disposition"):
        archive_disposed_admission_request(
            paths,
            request_path=request_path,
            request=request,
        )
    assert request_path.is_file()


def test_cross_epoch_admission_replay_preserves_committed_admitted_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, first_leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    original_disposition = syncer_runtime.publish_admission_disposition
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, first_leader, telemetry)
        first = authority.read.static_binding("learner_000")
        assert first is not None
        old_fence = StaticContributorFence(
            "static",
            first.learner_id,
            first.logical_launch_id,
            first.attempt_id,
            first.binding_generation,
        )
        publish_static_replacement_authorization(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            old_fence=old_fence,
            new_logical_launch_id="logical-1",
            new_attempt_id="attempt-2",
            reason="operator replacement before injected crash",
        )
        request_path = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-2",
            expected_generation=1,
        )
        request = json.loads(request_path.read_bytes())

        def fail_disposition(*_args: object, **_kwargs: object) -> Path:
            raise OSError("injected post-admission disposition failure")

        monkeypatch.setattr(syncer_runtime, "publish_admission_disposition", fail_disposition)
        _admit_requests(loaded, authority, first_leader, telemetry)
        committed = authority.read.static_binding("learner_000")
        assert committed is not None and committed.attempt_id == "attempt-2"
        assert request_path.is_file()

        authority.fail_leader(first_leader.token)
        successor_token = authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        successor = authority.open_leader(successor_token)
        monkeypatch.setattr(
            syncer_runtime,
            "publish_admission_disposition",
            original_disposition,
        )
        _admit_requests(loaded, authority, successor, telemetry)

        disposition = json.loads(
            paths.registration_disposition_path(admission_request_sha256(request)).read_bytes()
        )
        assert disposition["outcome"] == "admitted"
        rejection_root = (
            paths.epoch_membership_dir(successor.token.epoch, successor.token.owner_id)
            / "admissions_v4"
            / "rejections"
        )
        assert not tuple(rejection_root.rglob("*.json"))
        assert not request_path.exists()
    finally:
        authority.close()


def test_same_epoch_admission_repair_reuses_immutable_resume_snapshot(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        binding = authority.read.static_binding("learner_000")
        assert binding is not None
        fence = StaticContributorFence(
            "static",
            binding.learner_id,
            binding.logical_launch_id,
            binding.attempt_id,
            binding.binding_generation,
        )
        response = paths.epoch_admission_response_path(
            leader.token.epoch,
            leader.token.owner_id,
            fence.learner_id,
            fence.attempt_id,
            contributor_fence_namespace(fence),
        )
        initial_bytes = response.read_bytes()
        leader.ingest_cycle_receipt(
            command_id="receipt-after-admission",
            receipt=receipt(
                run_id="run-v4",
                stable_contributor_key="learner_000",
                fence=fence.as_dict(),
            ),
        )

        _admit_requests(loaded, authority, leader, telemetry)
        assert response.read_bytes() == initial_bytes
        assert [name for name, _fields in telemetry.events] == ["learner_admitted"]
    finally:
        authority.close()


def test_nonmatching_current_fence_keeps_unprocessed_request_pending(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    publisher = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=1, owner_id="owner-1"),
    )
    _publish_synthetic_heartbeat(publisher)
    resume = ContributorResumeState(0, None, None, 1)
    first_request = {
        "mode": "static",
        "run_id": "run-1",
        "descriptor_sha256": "d" * 64,
        "learner_id": "learner_000",
        "attempt_id": "attempt-1",
    }
    second_request = {**first_request, "attempt_id": "attempt-2"}
    first_fence = StaticContributorFence("static", "learner_000", "logical-1", "attempt-1", 1)
    second_fence = StaticContributorFence("static", "learner_000", "logical-1", "attempt-2", 2)
    publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=first_request,
        fence=first_fence,
        resume=resume,
    )
    paths.epoch_current_admission_path(1, "owner-1", "learner_000").unlink()
    assert (
        read_admission_response(
            paths,
            run_id="run-1",
            descriptor_sha256="d" * 64,
            actor_id="learner_000",
            attempt_id="attempt-1",
            stable_contributor_key="learner_000",
            request_sha256=admission_request_sha256(first_request),
            max_clock_skew_seconds=0.0,
        )
        is None
    )
    publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=first_request,
        fence=first_fence,
        resume=resume,
    )
    publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=second_request,
        fence=second_fence,
        resume=resume,
    )

    assert (
        read_admission_response(
            paths,
            run_id="run-1",
            descriptor_sha256="d" * 64,
            actor_id="learner_000",
            attempt_id="attempt-1",
            stable_contributor_key="learner_000",
            request_sha256=admission_request_sha256(first_request),
            max_clock_skew_seconds=0.0,
        )
        is None
    )


def test_dynamic_admission_reader_uses_stable_stream_pointer_key(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    publisher = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=1, owner_id="owner-1"),
    )
    _publish_synthetic_heartbeat(publisher)
    request = {
        "mode": "dynamic",
        "run_id": "run-1",
        "descriptor_sha256": "d" * 64,
        "instance_id": "learner_li_1",
    }
    fence = DynamicContributorFence(
        kind="dynamic",
        instance_id="learner_li_1",
        placement_id="placement-1",
        placement_epoch=1,
        stream_id=0,
        stream_epoch=1,
        admission_generation=1,
        admission_token_sha256="a" * 64,
    )
    publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        fence=fence,
        resume=ContributorResumeState(0, None, None, 1, stream_epoch=1),
    )

    admission = read_admission_response(
        paths,
        run_id="run-1",
        descriptor_sha256="d" * 64,
        actor_id="learner_li_1",
        attempt_id="learner_li_1",
        stable_contributor_key="0",
        request_sha256=admission_request_sha256(request),
        max_clock_skew_seconds=0.0,
    )
    assert admission is not None and admission.fence == fence


def test_admission_response_rejects_extra_fields_even_with_matching_pointer_hash(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path)
    publisher = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=1, owner_id="owner-1"),
    )
    _publish_synthetic_heartbeat(publisher)
    request = {
        "mode": "static",
        "run_id": "run-1",
        "descriptor_sha256": "d" * 64,
        "learner_id": "learner_000",
        "attempt_id": "attempt-1",
    }
    fence = StaticContributorFence("static", "learner_000", "logical-1", "attempt-1", 1)
    response = publish_admission_response(
        paths,
        epoch=1,
        owner_id="owner-1",
        request=request,
        fence=fence,
        resume=ContributorResumeState(0, None, None, 1),
    )
    payload = json.loads(response.read_bytes())
    payload["unexpected"] = "field"
    atomic_write_json(response, payload)
    pointer_path = paths.epoch_current_admission_path(1, "owner-1", "learner_000")
    pointer = json.loads(pointer_path.read_bytes())
    pointer["response_sha256"] = sha256_file(response)
    atomic_write_json(pointer_path, pointer)

    with pytest.raises(RuntimeError, match="fields"):
        read_admission_response(
            paths,
            run_id="run-1",
            descriptor_sha256="d" * 64,
            actor_id="learner_000",
            attempt_id="attempt-1",
            stable_contributor_key="learner_000",
            request_sha256=admission_request_sha256(request),
            max_clock_skew_seconds=0.0,
        )


def test_stale_epoch_admission_response_cannot_open_torch_gate(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    old = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=1, owner_id="owner-old"),
    )
    current = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-1", epoch=2, owner_id="owner-current"),
    )
    _publish_synthetic_heartbeat(old)
    _publish_synthetic_heartbeat(current)
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
            stable_contributor_key="learner_000",
            request_sha256=admission_request_sha256(request),
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
        stable_contributor_key="learner_000",
        request_sha256=admission_request_sha256(request),
        max_clock_skew_seconds=0.0,
    )
    assert admitted is not None and admitted.fence == fence


def test_receipt_ack_is_current_epoch_fenced_and_byte_idempotent(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    cycle_receipt = receipt()
    old = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-v4", epoch=1, owner_id="owner-old"),
    )
    current = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-v4", epoch=2, owner_id="owner-current"),
    )
    _publish_synthetic_heartbeat(old)
    old.publish_receipt_ack(cycle_receipt, descriptor_sha256="d" * 64)
    _publish_synthetic_heartbeat(current)
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
    )
    _publish_synthetic_heartbeat(publisher)
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


def test_heartbeat_publication_uses_exact_committed_lease_and_rejects_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    paths = RunPaths(tmp_path)
    identity = AuthorityIdentity("run-v4", "source-fingerprint", "d" * 64)
    scope = StaticMembershipScope(("learner_000",))
    initialize_authority_v4(paths.sqlite_db, identity, scope, wall_clock=lambda: now[0])
    with LeaderAuthority(
        paths.sqlite_db,
        identity,
        scope,
        wall_clock=lambda: now[0],
        lease_duration_seconds=20.0,
    ) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        acquired = authority.committed_leader_lease(token)
        assert acquired.renewed_at == 100.0
        assert acquired.lease_expires_at == 120.0
        assert acquired.heartbeat_seq == 1

        now[0] = 105.0
        renewed = authority.renew_leader(token)
        assert renewed.renewed_at == 105.0
        assert renewed.lease_expires_at == 125.0
        assert renewed.heartbeat_seq == 2

        publisher = V4ControlPublisher(paths, token)
        monkeypatch.setattr("fs_diloco.storage.control.time.time", lambda: 124.0)
        heartbeat = publisher.publish_heartbeat(renewed)
        assert heartbeat["renewed_at"] == 105.0
        assert heartbeat["lease_expires_at"] == 125.0
        assert heartbeat["heartbeat_seq"] == 2

        monkeypatch.setattr("fs_diloco.storage.control.time.time", lambda: 126.0)
        with pytest.raises(RuntimeError, match="expired"):
            publisher.publish_heartbeat(renewed)


def test_latest_head_rejects_path_escape_and_payload_identity_mismatch(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "run")
    publisher = V4ControlPublisher(
        paths,
        LeaderToken(run_id="run-v4", epoch=1, owner_id="owner-1"),
    )
    _publish_synthetic_heartbeat(publisher)
    head_path = paths.epoch_head_path(1, "owner-1")
    outside = tmp_path / "outside.json"
    outside.write_text('{"publication_id":"outside"}\n', encoding="utf-8")
    atomic_write_json(
        head_path,
        {
            "format_version": 2,
            "kind": "latest_head",
            "run_id": "run-v4",
            "epoch": 1,
            "owner_id": "owner-1",
            "version": 7,
            "pointer_path": "../outside.json",
            "pointer_sha256": sha256_file(outside),
        },
    )
    current = read_current_control(paths, run_id="run-v4")
    assert current is not None and current.latest is None

    pointer = paths.epoch_version_pointer_path(1, "owner-1", 7)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "format_version": 2,
                "kind": "latest",
                "run_id": "run-v4",
                "epoch": 1,
                "owner_id": "wrong-owner",
                "version": 7,
                "publication_id": "publication-7",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    atomic_write_json(
        head_path,
        {
            "format_version": 2,
            "kind": "latest_head",
            "run_id": "run-v4",
            "epoch": 1,
            "owner_id": "owner-1",
            "version": 7,
            "pointer_path": paths.relative(pointer),
            "pointer_sha256": sha256_file(pointer),
        },
    )
    current = read_current_control(paths, run_id="run-v4")
    assert current is not None and current.latest is None


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


def test_candidate_pause_hook_quiesces_renewer_and_proves_transaction_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Authority:
        def __init__(self) -> None:
            self.checked = 0

        def assert_outside_transaction(self) -> None:
            self.checked += 1

    class Renewer:
        def __init__(self) -> None:
            self.quiesced = 0

        @contextmanager
        def quiesce_for_test_pause(self):
            self.quiesced += 1
            yield

    authority = Authority()
    renewer = Renewer()
    leader = SimpleNamespace(token=SimpleNamespace(epoch=3, owner_id="owner-3"))
    marker = tmp_path / "pause.json"
    observed_signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setenv("FS_DILOCO_TEST_PAUSE_AFTER_COMMITTED_VERSION", "4")
    monkeypatch.setenv("FS_DILOCO_TEST_PAUSE_MARKER_PATH", str(marker))
    monkeypatch.setattr(os, "kill", lambda pid, value: observed_signals.append((pid, value)))

    _pause_candidate_outside_transaction(authority, leader, renewer, version=3)
    assert not marker.exists()
    _pause_candidate_outside_transaction(authority, leader, renewer, version=4)

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["committed_version"] == 4
    assert payload["epoch"] == 3
    assert payload["sqlite_transaction_active"] is False
    assert payload["lease_renewer_quiesced"] is True
    assert renewer.quiesced == 1
    assert authority.checked == 2
    assert observed_signals == [(os.getpid(), signal.SIGSTOP)]

    _pause_candidate_outside_transaction(authority, leader, renewer, version=5)
    assert renewer.quiesced == 1


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


def test_fresh_request_cannot_reuse_the_current_static_attempt_id(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        first = authority.read.static_binding("learner_000")
        assert first is not None and first.binding_generation == 1

        duplicate_path = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=1,
        )
        duplicate = json.loads(duplicate_path.read_bytes())
        _admit_requests(loaded, authority, leader, telemetry)

        current = authority.read.static_binding("learner_000")
        assert current == first
        rejection = paths.epoch_admission_rejection_path(
            leader.token.epoch,
            leader.token.owner_id,
            "learner_000",
            "attempt-1",
            admission_request_sha256(duplicate),
        )
        assert rejection.is_file()
        assert json.loads(rejection.read_bytes())["error_type"] == "MembershipFenceError"
        assert not duplicate_path.exists()
    finally:
        authority.close()


def test_static_replay_requires_exact_committed_command_request(tmp_path: Path) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        first = authority.read.static_binding("learner_000")
        assert first is not None and first.binding_generation == 1

        duplicate_path = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=1,
        )
        duplicate = json.loads(duplicate_path.read_bytes())
        request_sha = admission_request_sha256(duplicate)
        collision = leader.bind_or_replace_static_attempt(
            command_id=f"admit-{request_sha}",
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        assert collision == first

        _admit_requests(loaded, authority, leader, telemetry)

        current = authority.read.static_binding("learner_000")
        assert current == first
        rejection = paths.epoch_admission_rejection_path(
            leader.token.epoch,
            leader.token.owner_id,
            "learner_000",
            "attempt-1",
            request_sha,
        )
        assert rejection.is_file()
        assert json.loads(rejection.read_bytes())["error_type"] == "CommandConflictError"
        assert not duplicate_path.exists()
    finally:
        authority.close()


def test_static_replay_keeps_malformed_committed_result_as_authority_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    original_disposition = syncer_runtime.publish_admission_disposition
    request_path = publish_static_request(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    request = json.loads(request_path.read_bytes())

    def fail_disposition(*_args: object, **_kwargs: object) -> Path:
        raise OSError("injected post-command disposition failure")

    monkeypatch.setattr(syncer_runtime, "publish_admission_disposition", fail_disposition)
    try:
        _admit_requests(loaded, authority, leader, telemetry)
        assert authority.read.static_binding("learner_000") is not None
        assert request_path.is_file()
        request_sha = admission_request_sha256(request)
        with sqlite3.connect(paths.sqlite_db) as connection:
            connection.execute(
                "UPDATE command_records SET result_json='{' WHERE command_id=?",
                (f"admit-{request_sha}",),
            )
        monkeypatch.setattr(
            syncer_runtime,
            "publish_admission_disposition",
            original_disposition,
        )

        with pytest.raises(AuthoritySchemaError, match="committed static binding result"):
            _admit_requests(loaded, authority, leader, telemetry)

        assert request_path.is_file()
        assert not paths.registration_disposition_path(request_sha).exists()
        rejection_root = (
            paths.epoch_membership_dir(leader.token.epoch, leader.token.owner_id)
            / "admissions_v4"
            / "rejections"
        )
        assert not tuple(rejection_root.rglob("*.json"))
    finally:
        authority.close()


def test_stale_leader_cannot_use_exact_binding_shortcut_to_remove_hot_request(
    tmp_path: Path,
) -> None:
    paths, authority, first_leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, first_leader, telemetry)
        hot = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=1,
        )
        authority.fail_leader(first_leader.token)
        authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)

        with pytest.raises(StaleLeaderTokenError):
            _admit_requests(loaded, authority, first_leader, telemetry)
        assert hot.is_file()
    finally:
        authority.close()


def test_identical_invalid_request_replay_is_byte_idempotent_across_epochs(
    tmp_path: Path,
) -> None:
    paths, authority, first_leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    poison = paths.registration_requests / "static" / "learner_000" / "poison.json"
    poison.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"format_version":"\xff"}'
    try:
        poison.write_bytes(raw)
        _admit_requests(loaded, authority, first_leader, telemetry)
        digest = hashlib.sha256(raw).hexdigest()
        history = paths.registration_history_path(digest)
        disposition = paths.registration_disposition_path(digest)
        before = (history.read_bytes(), disposition.read_bytes())

        authority.fail_leader(first_leader.token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        )
        poison.write_bytes(raw)
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-valid",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, successor, telemetry)

        assert not poison.exists()
        assert (history.read_bytes(), disposition.read_bytes()) == before
        binding = authority.read.static_binding("learner_000")
        assert binding is not None and binding.attempt_id == "attempt-valid"
    finally:
        authority.close()


def test_cross_epoch_rejected_disposition_is_visible_to_waiting_learner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, first_leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    original_archive = syncer_runtime.archive_disposed_admission_request
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, first_leader, telemetry)
        rejected_path = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-2",
            expected_generation=1,
        )
        rejected = json.loads(rejected_path.read_bytes())

        def fail_archive(*_args: object, **_kwargs: object) -> Path:
            raise OSError("injected crash after rejected disposition")

        monkeypatch.setattr(syncer_runtime, "archive_disposed_admission_request", fail_archive)
        _admit_requests(loaded, authority, first_leader, telemetry)
        assert rejected_path.is_file()

        authority.fail_leader(first_leader.token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        )
        _publish_synthetic_heartbeat(V4ControlPublisher(paths, successor.token))
        monkeypatch.setattr(
            syncer_runtime,
            "archive_disposed_admission_request",
            original_archive,
        )
        _admit_requests(loaded, authority, successor, telemetry)

        with pytest.raises(AdmissionRejectedError, match="active static replacement"):
            read_admission_response(
                paths,
                run_id="run-v4",
                descriptor_sha256="d" * 64,
                actor_id="learner_000",
                attempt_id="attempt-2",
                stable_contributor_key="learner_000",
                request_sha256=admission_request_sha256(rejected),
                max_clock_skew_seconds=0.0,
            )
        assert not rejected_path.exists()
    finally:
        authority.close()


def test_rejected_disposition_survives_takeover_during_successor_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, authority, first_leader, loaded = _static_admission_runtime(tmp_path)
    telemetry = _TelemetryProbe()
    original_archive = syncer_runtime.archive_disposed_admission_request
    original_repair = syncer_runtime.repair_rejected_admission_control
    try:
        publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, first_leader, telemetry)
        rejected_path = publish_static_request(
            paths,
            run_id="run-v4",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-2",
            expected_generation=1,
        )
        rejected = json.loads(rejected_path.read_bytes())

        def fail_archive(*_args: object, **_kwargs: object) -> Path:
            raise OSError("injected crash after rejected disposition")

        monkeypatch.setattr(syncer_runtime, "archive_disposed_admission_request", fail_archive)
        _admit_requests(loaded, authority, first_leader, telemetry)
        authority.fail_leader(first_leader.token)
        successor = authority.open_leader(
            authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        )
        monkeypatch.setattr(
            syncer_runtime,
            "archive_disposed_admission_request",
            original_archive,
        )

        def fence_successor_during_repair(*args: object, **kwargs: object) -> Path | None:
            repaired = original_repair(*args, **kwargs)
            authority.fail_leader(successor.token)
            third = authority.open_leader(
                authority.acquire_leader(owner_id="owner-3", hostname="host", pid=3)
            )
            _publish_synthetic_heartbeat(V4ControlPublisher(paths, third.token))
            return repaired

        monkeypatch.setattr(
            syncer_runtime,
            "repair_rejected_admission_control",
            fence_successor_during_repair,
        )
        _admit_requests(loaded, authority, successor, telemetry)

        assert not rejected_path.exists()
        with pytest.raises(AdmissionRejectedError, match="active static replacement"):
            read_admission_response(
                paths,
                run_id="run-v4",
                descriptor_sha256="d" * 64,
                actor_id="learner_000",
                attempt_id="attempt-2",
                stable_contributor_key="learner_000",
                request_sha256=admission_request_sha256(rejected),
                max_clock_skew_seconds=0.0,
            )
    finally:
        authority.close()


def test_request_publish_api_returns_digest_without_hot_file_reread(tmp_path: Path) -> None:
    publisher = getattr(admission_protocol, "publish_static_request_with_sha256", None)
    assert callable(publisher), "learner request publication must return its in-memory digest"
    paths = RunPaths(tmp_path)
    request_path, request_sha = publisher(
        paths,
        run_id="run-v4",
        descriptor_sha256="d" * 64,
        learner_id="learner_000",
        logical_launch_id="logical-1",
        attempt_id="attempt-1",
        expected_generation=None,
    )
    payload = json.loads(request_path.read_bytes())
    request_path.unlink()
    assert request_sha == admission_request_sha256(payload)
    entrypoint_source = Path("fs_diloco/runtime/learner_entrypoint.py").read_text(encoding="utf-8")
    assert "request_path.read_bytes()" not in entrypoint_source
