from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fs_diloco.core.config import Config, resolve_config
from fs_diloco.modeling import hf_data
from fs_diloco.protocol.dynamic_terminal import (
    DynamicTerminalPublisher,
    read_current_drain,
    read_dynamic_close_request,
    write_dynamic_close_request,
)
from fs_diloco.protocol.membership import (
    Admission,
    MembershipPublisher,
    bootstrap_request_id,
    ingest_registration_requests,
    new_learner_instance_id,
    read_admission,
    read_bootstrap_ready,
    read_bootstrap_scheduler_jobs,
    read_registration_decision,
    validate_learner_instance_id,
    validate_registration_request,
    write_bootstrap_scheduler_jobs,
    write_registration_request,
)
from fs_diloco.protocol.merge import select_one_per_dynamic_member
from fs_diloco.runtime import learner as learner_runtime
from fs_diloco.runtime.syncer import dynamic_non_target_close_request
from fs_diloco.runtime.launch_outbox import LearnerLaunchOutbox
from fs_diloco.runtime.pbs_scheduler import PBSJobObservation, PBSScheduler
from fs_diloco.storage.atomic_io import safe_read_json
from fs_diloco.storage.fenced_store import FencedSQLiteStore, ReadOnlySQLiteStore
from fs_diloco.storage.leader_lease import LeaderLeaseStore, LeaseSafetyTracker
from fs_diloco.storage.maintenance import archive_dynamic_history
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from fs_diloco.storage.schema_bootstrap import BootstrapIdentity, initialize_new_run
from fs_diloco.storage.sqlite_store import DynamicMembershipFenceError
from fs_diloco.tools.launch_phase2_acceptance import submit_jobs as submit_acceptance_jobs
from fs_diloco.tools.launch_phase2_matched import submit_jobs as submit_matched_jobs


def dynamic_identity() -> BootstrapIdentity:
    return BootstrapIdentity(
        run_id="dynamic-test",
        source_fingerprint="sha256:dynamic-source",
        config_sha256="dynamic-config-digest",
        mode="full_dynamic",
    )


def dynamic_store(tmp_path: Path):
    paths = RunPaths(tmp_path / "run")
    prepare_authority_dirs(paths)
    initialize_new_run(
        paths.sqlite_db,
        dynamic_identity(),
        marker_path=paths.bootstrap_complete_json,
    )
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        dynamic_identity(),
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
    )
    token = lease.acquire(owner_id="dynamic-owner", hostname="host", pid=1)
    tracker = LeaseSafetyTracker(
        token,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
    )
    fenced = FencedSQLiteStore(
        paths.sqlite_db,
        dynamic_identity(),
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
        lease_safety_check=tracker.assert_safe,
    )
    return paths, lease, token, fenced, fenced.bind(token)


def request_payload(
    paths: RunPaths,
    *,
    instance_id: str,
    launch_request_id: str,
    placement_id: str,
    now: float = 100.0,
    ttl: float = 60.0,
    pbs_job_id: str | None = None,
) -> dict[str, Any]:
    return write_registration_request(
        paths,
        run_id=dynamic_identity().run_id,
        instance_id=instance_id,
        placement=placement_id,
        launch_request_id=launch_request_id,
        source_fingerprint=dynamic_identity().source_fingerprint,
        config_sha256=dynamic_identity().config_sha256,
        ttl_seconds=ttl,
        pbs_job_id=pbs_job_id,
        hostname=placement_id.split(":", 1)[0],
        pid=7,
        gpu_identity=placement_id.split(":", 1)[1],
        now=now,
    )


def admit(
    store: Any,
    paths: RunPaths,
    *,
    launch_request_id: str,
    placement_id: str,
    instance_id: str | None = None,
    now: float = 100.0,
    pool: int = 2,
    desired: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    instance_id = instance_id or new_learner_instance_id()
    request = request_payload(
        paths,
        instance_id=instance_id,
        launch_request_id=launch_request_id,
        placement_id=placement_id,
        now=now,
    )
    result = store.admit_registration(
        request,
        stream_pool_size=pool,
        desired_contributors=desired,
        allow_unsolicited_registration=False,
        allow_healthy_placement_replacement=False,
        reuse_stream_for_same_placement=True,
        now=now,
    )
    paths.registration_request_path(instance_id).unlink(missing_ok=True)
    return request, result


def initialize_membership(
    store: Any,
    *,
    pool: int = 2,
    bootstrap: int = 2,
) -> list[dict[str, Any]]:
    return store.initialize_dynamic_membership(
        stream_pool_size=pool,
        bootstrap_instances=bootstrap,
        config_fingerprint="descriptor-digest",
        created_at=100.0,
    )


def initialize_global_v0(store: Any) -> None:
    store.initialize_full_run(
        weight_path="weights/v0.safetensors",
        optim_path="optim/v0.safetensors",
        outer_optimizer="sgd",
        identity={"kind": "test"},
        config_snapshot={"kind": "test"},
        publication_id="publication-v0",
        weight_size_bytes=1,
        optim_size_bytes=1,
    )


def update_metadata(admission: dict[str, Any], *, update_id: str = "update-1") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "learner_id": admission["instance_id"],
        "learner_instance_id": admission["instance_id"],
        "hostname": "host",
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": 1,
        "inner_steps": 1,
        "tokens_this_update": 16,
        "tokens_since_global_load": 16,
        "file_path": f"updates/{update_id}.safetensors",
        "file_size_bytes": 1,
        "sha256": "digest",
        "created_at": 100.0,
        "committed_at": 100.0,
        "placement_id": admission["placement_id"],
        "placement_epoch": admission["placement_epoch"],
        "stream_id": admission["stream_id"],
        "stream_epoch": admission["stream_epoch"],
        "admission_generation": admission["admission_generation"],
        "admission_token_hash": hashlib.sha256(
            admission["admission_token"].encode("utf-8")
        ).hexdigest(),
    }


def heartbeat(admission: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = {
        "instance_id": admission["instance_id"],
        "learner_id": admission["instance_id"],
        "placement_id": admission["placement_id"],
        "placement_epoch": admission["placement_epoch"],
        "stream_id": admission["stream_id"],
        "stream_epoch": admission["stream_epoch"],
        "admission_generation": admission["admission_generation"],
        "admission_token": admission["admission_token"],
        "timestamp": 101.0,
        "status": "active",
    }
    payload.update(overrides)
    return payload


def capacity_parameters(
    *,
    key: str,
    kind: str,
    global_version: int,
    eligible: int,
    selected_instance_ids: list[str],
    now: float,
    max_total_launch_requests: int = 2,
) -> dict[str, Any]:
    return {
        "observation_key": key,
        "kind": kind,
        "global_version": global_version,
        "eligible_contributors": eligible,
        "selected_instance_ids": selected_instance_ids,
        "low_contributor_threshold": 1,
        "consecutive_low_windows": 2,
        "productive_window_count": 2,
        "startup_grace_seconds": 1.0,
        "heartbeat_stale_after_seconds": 10.0,
        "productive_upload_grace_factor": 2.0,
        "productive_upload_grace_min_seconds": 1.0,
        "productive_upload_grace_max_seconds": 10.0,
        "desired_contributors": 2,
        "stream_pool_size": 2,
        "scaling_enabled": True,
        "initial_membership_deadline_seconds": 10.0,
        "cooldown_seconds": 10.0,
        "max_pending_launch_requests": 1,
        "max_total_launch_requests": max_total_launch_requests,
        "launch_request_ttl_seconds": 20.0,
        "config_fingerprint": "descriptor",
        "now": now,
    }


def test_dynamic_identity_cli_and_fixed_stream_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identities = {new_learner_instance_id() for _ in range(1000)}
    assert len(identities) == 1000
    for instance_id in identities:
        validate_learner_instance_id(instance_id)
    for invalid in ("learner_000", "learner_li_not-a-uuid", "learner_li_" + "0" * 32):
        with pytest.raises(ValueError):
            validate_learner_instance_id(invalid)

    config = Config()
    config.membership.mode = "dynamic"
    calls: list[tuple[int, int]] = []
    sentinel = object()

    def fake_iterator(
        _config: Config, _tokenizer: object, *, learner_index: int, num_learners: int
    ):
        calls.append((learner_index, num_learners))
        return sentinel

    monkeypatch.setattr(hf_data, "build_batch_iterator", fake_iterator)
    assert (
        hf_data.build_stream_batch_iterator(
            config,
            object(),
            stream_id=3,
            stream_pool_size=8,
        )
        is sentinel
    )
    assert calls == [(3, 8)]
    with pytest.raises(ValueError, match="fixed stream pool"):
        hf_data.build_stream_batch_iterator(
            config,
            object(),
            stream_id=8,
            stream_pool_size=8,
        )

    dynamic_config = Config()
    dynamic_config.membership.mode = "dynamic"
    monkeypatch.setattr(learner_runtime, "resolve_config", lambda *args, **kwargs: dynamic_config)
    monkeypatch.setattr(learner_runtime, "run_learner", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="rejects --learner-id"):
        learner_runtime.main(["--config", "unused", "--learner-id", "learner_000"])
    with pytest.raises(ValueError, match="rejects --num-learners"):
        learner_runtime.main(["--config", "unused", "--num-learners", "8", "--bootstrap-slot", "0"])

    paths = RunPaths(tmp_path / "discovery")
    prepare_authority_dirs(paths)
    instance_id = next(iter(identities))
    paths.learner_heartbeat_path(instance_id).write_text("{}", encoding="utf-8")
    paths.update_pointer_path(instance_id).write_text("{}", encoding="utf-8")
    (paths.update_payload_dir(instance_id)).mkdir()
    (paths.update_payload_dir(instance_id) / "u.safetensors").write_bytes(b"x")
    paths.registration_request_path(instance_id).write_text("{}", encoding="utf-8")
    assert list(paths.iter_learner_heartbeats())
    assert list(paths.iter_instance_pointers())
    assert list(paths.iter_instance_payloads())
    assert list(paths.iter_registration_requests())


def test_dynamic_config_mode_matrix_and_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "dynamic.yaml"
    config_path.write_text(
        """
membership:
  mode: dynamic
  stream_pool_size: 2
  bootstrap_instances: 2
  max_active_instance_records: 4
scaling:
  desired_contributors: 2
  low_contributor_threshold: 1
coordination:
  syncer_ha:
    enabled: true
sync:
  quorum_min: 1
  quorum_max: 2
""",
        encoding="utf-8",
    )
    config = resolve_config(config_path, shared_root=str(tmp_path / "run"))
    assert config.membership.mode == "dynamic"
    assert config.membership.stream_pool_size == 2

    fragment = tmp_path / "fragment.yaml"
    fragment.write_text(
        config_path.read_text(encoding="utf-8")
        + "\nfragments:\n  enabled: true\n  materialize_full_every_events: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="dynamic membership requires full"):
        resolve_config(fragment)

    no_ha = tmp_path / "no-ha.yaml"
    no_ha.write_text(
        config_path.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="dynamic membership requires full"):
        resolve_config(no_ha)


def test_bootstrap_registration_replay_rejection_and_authorized_replacement(
    tmp_path: Path,
) -> None:
    paths, lease, token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=2, bootstrap=2)
        assert [row["bootstrap_slot"] for row in launches] == [0, 1]
        assert len(store.streams()) == 2
        with pytest.raises(RuntimeError, match="immutable"):
            initialize_membership(store, pool=3, bootstrap=2)

        publisher = MembershipPublisher(paths, fenced, token)
        publisher.publish_bootstrap_ready(launches)
        ready = read_bootstrap_ready(
            paths,
            run_id=dynamic_identity().run_id,
            bootstrap_slot=0,
        )
        assert ready is not None
        assert ready["request_id"] == bootstrap_request_id(
            run_id=dynamic_identity().run_id,
            bootstrap_slot=0,
            config_fingerprint="descriptor-digest",
        )

        request0, first = admit(
            store,
            paths,
            launch_request_id=str(launches[0]["request_id"]),
            placement_id="host:gpu0",
        )
        replay = store.admit_registration(
            request0,
            stream_pool_size=2,
            desired_contributors=2,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=101.0,
        )
        assert replay == first

        duplicate_request = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id=str(launches[1]["request_id"]),
            placement_id="host:gpu0",
        )
        with pytest.raises(RuntimeError, match="healthy placement"):
            store.admit_registration(
                duplicate_request,
                stream_pool_size=2,
                desired_contributors=2,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )
        assert store.current_instances()[0]["instance_id"] == first["instance_id"]

        authorization = store.authorize_placement_replacement(
            placement_id="host:gpu0",
            reason="test replacement",
            ttl_seconds=60.0,
            now=102.0,
        )
        _, replacement = admit(
            store,
            paths,
            launch_request_id=str(authorization["request_id"]),
            placement_id="host:gpu0",
            now=103.0,
        )
        assert replacement["placement_epoch"] == first["placement_epoch"] + 1
        assert replacement["stream_id"] == first["stream_id"]
        assert replacement["stream_epoch"] == first["stream_epoch"] + 1
        assert replacement["stream_restarted"] is True

        stale = heartbeat(first, timestamp=104.0)
        assert not store.update_instance_heartbeat(stale, heartbeat_path="stale.json")

        no_request = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id="missing-request",
            placement_id="host:gpu9",
        )
        with pytest.raises(RuntimeError, match="authorized launch"):
            store.admit_registration(
                no_request,
                stream_pool_size=2,
                desired_contributors=2,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )

        expired = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id=str(launches[1]["request_id"]),
            placement_id="host:gpu1",
            now=1.0,
            ttl=1.0,
        )
        with pytest.raises(RuntimeError, match="expired"):
            store.admit_registration(
                expired,
                stream_pool_size=2,
                desired_contributors=2,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )

        validation_path = paths.registration_request_path(expired["instance_id"])
        with pytest.raises(ValueError, match="identity mismatch"):
            validate_registration_request(
                {**expired, "source_fingerprint": "wrong"},
                path=validation_path,
                run_id=dynamic_identity().run_id,
                source_fingerprint=dynamic_identity().source_fingerprint,
                config_sha256=dynamic_identity().config_sha256,
                now=1.5,
            )

        publisher.publish_registration_result(first)
        decision = read_registration_decision(
            paths,
            run_id=dynamic_identity().run_id,
            instance_id=first["instance_id"],
        )
        admission = read_admission(
            paths,
            run_id=dynamic_identity().run_id,
            instance_id=first["instance_id"],
        )
        assert decision is not None and decision["state"] == "admitted"
        assert isinstance(admission, Admission)
    finally:
        fenced.close()
        lease.close()


def test_registration_waits_for_scheduler_identity_binding(tmp_path: Path) -> None:
    paths, lease, token, fenced, store = dynamic_store(tmp_path)
    try:
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        instance_id = new_learner_instance_id()
        request_payload(
            paths,
            instance_id=instance_id,
            launch_request_id=str(launch["request_id"]),
            placement_id="host:gpu0",
            pbs_job_id="123.opbs",
        )

        pending = ingest_registration_requests(
            store,
            paths,
            token=token,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            stream_pool_size=1,
            desired_contributors=1,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=100.0,
        )
        assert [row["state"] for row in pending] == ["pending_scheduler_authorization"], pending
        assert paths.registration_request_path(instance_id).is_file()
        assert store.current_instances() == []
        assert [row["state"] for row in store.registration_requests()] == ["pending"]

        assert (
            store.record_external_launch_jobs(
                [
                    {
                        "bootstrap_slot": 0,
                        "request_id": str(launch["request_id"]),
                        "pbs_job_id": "123.opbs",
                    }
                ],
                observed_at=101.0,
            )
            == 1
        )
        admitted = ingest_registration_requests(
            store,
            paths,
            token=token,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            stream_pool_size=1,
            desired_contributors=1,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=101.0,
        )
        assert [row["state"] for row in admitted] == ["admitted"]
        assert not paths.registration_request_path(instance_id).exists()
        assert [row["state"] for row in store.registration_requests()] == ["admitted"]
    finally:
        fenced.close()
        lease.close()


def test_registration_rejects_wrong_scheduler_identity_after_binding(
    tmp_path: Path,
) -> None:
    paths, lease, token, fenced, store = dynamic_store(tmp_path)
    try:
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        instance_id = new_learner_instance_id()
        request_payload(
            paths,
            instance_id=instance_id,
            launch_request_id=str(launch["request_id"]),
            placement_id="host:gpu0",
            pbs_job_id="999.opbs",
        )
        store.record_external_launch_jobs(
            [
                {
                    "bootstrap_slot": 0,
                    "request_id": str(launch["request_id"]),
                    "pbs_job_id": "123.opbs",
                }
            ],
            observed_at=100.0,
        )
        rejected = ingest_registration_requests(
            store,
            paths,
            token=token,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            stream_pool_size=1,
            desired_contributors=1,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=100.0,
        )
        assert [row["state"] for row in rejected] == ["rejected"]
        assert "does not match" in str(rejected[0]["rejection_reason"])
        assert store.current_instances() == []
        assert not paths.registration_request_path(instance_id).exists()
    finally:
        fenced.close()
        lease.close()


def test_pending_scheduler_registration_rejects_changed_request(
    tmp_path: Path,
) -> None:
    paths, lease, token, fenced, store = dynamic_store(tmp_path)
    try:
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        instance_id = new_learner_instance_id()
        request_payload(
            paths,
            instance_id=instance_id,
            launch_request_id=str(launch["request_id"]),
            placement_id="host:gpu0",
            pbs_job_id="123.opbs",
        )
        common = {
            "token": token,
            "run_id": dynamic_identity().run_id,
            "source_fingerprint": dynamic_identity().source_fingerprint,
            "config_sha256": dynamic_identity().config_sha256,
            "stream_pool_size": 1,
            "desired_contributors": 1,
            "allow_unsolicited_registration": False,
            "allow_healthy_placement_replacement": False,
            "reuse_stream_for_same_placement": True,
            "now": 100.0,
        }
        pending = ingest_registration_requests(store, paths, **common)
        assert [row["state"] for row in pending] == ["pending_scheduler_authorization"]

        request_payload(
            paths,
            instance_id=instance_id,
            launch_request_id=str(launch["request_id"]),
            placement_id="other:gpu0",
            pbs_job_id="123.opbs",
        )
        rejected = ingest_registration_requests(store, paths, **common)
        assert [row["state"] for row in rejected] == ["rejected"]
        assert "checksum" in str(rejected[0]["rejection_reason"])
        assert [row["state"] for row in store.registration_requests()] == ["rejected"]
        assert store.current_instances() == []
        assert not paths.registration_request_path(instance_id).exists()
    finally:
        fenced.close()
        lease.close()


def test_all_bootstrap_slots_admit_and_ninth_unsolicited_is_rejected(
    tmp_path: Path,
) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=8, bootstrap=8)
        admitted = [
            admit(
                store,
                paths,
                launch_request_id=str(launch["request_id"]),
                placement_id=f"host:gpu{slot}",
                pool=8,
                desired=8,
            )[1]
            for slot, launch in enumerate(launches)
        ]
        assert len(admitted) == 8
        assert {row["stream_id"] for row in admitted} == set(range(8))
        ninth = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id="unsolicited-ninth",
            placement_id="host:gpu8",
        )
        with pytest.raises(RuntimeError, match="authorized launch"):
            store.admit_registration(
                ninth,
                stream_pool_size=8,
                desired_contributors=8,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )
    finally:
        fenced.close()
        lease.close()


def test_bootstrap_scheduler_manifest_reconciles_external_jobs(
    tmp_path: Path,
) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=2, bootstrap=2)
        write_bootstrap_scheduler_jobs(
            paths,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            config_fingerprint="descriptor-digest",
            jobs_by_slot={0: "123.opbs", 1: "124.opbs"},
            now=101.0,
        )
        jobs = read_bootstrap_scheduler_jobs(
            paths,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            config_fingerprint="descriptor-digest",
        )
        assert len(jobs) == 2
        assert store.record_external_launch_jobs(jobs, observed_at=102.0) == 2
        rows = {row["bootstrap_slot"]: row for row in store.launch_requests()}
        assert rows[0]["pbs_job_id"] == "123.opbs"
        assert rows[1]["pbs_job_id"] == "124.opbs"
        reconciled = store.update_launch_request(
            request_id=str(launches[0]["request_id"]),
            expected_states={"external_submitted"},
            state="started",
            pbs_job_id="123",
            scheduler_state="running",
            observed_at=103.0,
        )
        assert reconciled["pbs_job_id"] == "123.opbs"

        wrong_job = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id=str(launches[0]["request_id"]),
            placement_id="host:gpu0",
            pbs_job_id="999.opbs",
        )
        with pytest.raises(RuntimeError, match="PBS job"):
            store.admit_registration(
                wrong_job,
                stream_pool_size=2,
                desired_contributors=2,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )
        accepted = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id=str(launches[0]["request_id"]),
            placement_id="host:gpu0",
            pbs_job_id="123",
        )
        result = store.admit_registration(
            accepted,
            stream_pool_size=2,
            desired_contributors=2,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=100.0,
        )
        assert result["state"] == "admitted"
    finally:
        fenced.close()
        lease.close()


def test_fault_injection_publishes_admission_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_path = tmp_path / "admitted.json"
    instance_id = new_learner_instance_id()
    monkeypatch.setenv("FS_DILOCO_TEST_ADMISSION_SIGNAL_PATH", str(signal_path))

    learner_runtime._publish_test_admission_signal(instance_id)

    assert safe_read_json(signal_path)["instance_id"] == instance_id
    assert safe_read_json(signal_path)["state"] == "admitted"


def test_acceptance_launcher_gates_victim_failure_on_admission(tmp_path: Path) -> None:
    shared_root = tmp_path / "run"
    (shared_root / "control").mkdir(parents=True)
    commands: list[list[str]] = []

    def qsub(command: list[str]) -> dict[str, Any]:
        commands.append(command)
        job_id = f"{1000 + len(commands)}.opbs"
        return {
            "command": command,
            "job_id": job_id,
            "returncode": 0,
            "status": "submitted",
            "stderr": "",
            "stdout": job_id,
        }

    payload = submit_acceptance_jobs(
        kind="g8",
        project_root=tmp_path,
        run_id="g8-run",
        shared_root=shared_root,
        descriptor_sha256="descriptor",
        source_fingerprint="sha256:source",
        config_sha256="config",
        launcher_job_id="launcher.opbs",
        crash_walltime="00:00:15",
        syncer_walltime="00:02:30",
        learner_walltime="00:02:00",
        checker_walltime="00:00:20",
        pending_artifact=tmp_path / "pending.json",
        pass_artifact=tmp_path / "pass.json",
        evidence_artifact=tmp_path / "evidence.json",
        qsub_fn=qsub,
    )

    assert payload["status"] == "PASS"
    victim = next(
        row for row in payload["submission_receipts"] if row["role"] == "victim_bootstrap_0"
    )
    variables = victim["command"][victim["command"].index("-v") + 1]
    assert "FS_DILOCO_TEST_TERMINATE_AFTER_ADMISSION_SECONDS=2" in variables
    assert "FS_DILOCO_TEST_TERMINATE_AFTER_SECONDS" not in variables


def test_logical_launch_request_admits_at_most_one_instance(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=2, bootstrap=1)
        launch_id = str(launches[0]["request_id"])
        _, first = admit(
            store,
            paths,
            launch_request_id=launch_id,
            placement_id="host:gpu0",
        )
        second_request = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id=launch_id,
            placement_id="host:gpu1",
        )
        with pytest.raises(RuntimeError, match="already admitted"):
            store.admit_registration(
                second_request,
                stream_pool_size=2,
                desired_contributors=2,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )
        launch = next(row for row in store.launch_requests() if row["request_id"] == launch_id)
        assert launch["admitted_instance_id"] == first["instance_id"]
        assert sum(row["launch_request_id"] == launch_id for row in store.current_instances()) == 1
    finally:
        fenced.close()
        lease.close()


def test_conflicting_registration_replay_preserves_canonical_admission(
    tmp_path: Path,
) -> None:
    paths, lease, token, fenced, store = dynamic_store(tmp_path)
    try:
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        original_request, admission = admit(
            store,
            paths,
            launch_request_id=str(launch["request_id"]),
            placement_id="host:gpu0",
            pool=1,
            desired=1,
        )
        publisher = MembershipPublisher(paths, fenced, token)
        decision_path = publisher.publish_registration_result(admission)
        original_bytes = decision_path.read_bytes()

        write_registration_request(
            paths,
            run_id=dynamic_identity().run_id,
            instance_id=str(original_request["instance_id"]),
            placement="attacker:gpu9",
            launch_request_id="changed-launch-request",
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            ttl_seconds=60.0,
            now=101.0,
        )
        results = ingest_registration_requests(
            store,
            paths,
            token=token,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            stream_pool_size=1,
            desired_contributors=1,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=True,
            now=102.0,
        )
        assert len(results) == 1
        assert results[0]["state"] == "admitted"
        assert results[0]["instance_id"] == admission["instance_id"]
        assert "admission_token" not in results[0]
        assert decision_path.read_bytes() == original_bytes
        assert (
            read_admission(
                paths,
                run_id=dynamic_identity().run_id,
                instance_id=str(original_request["instance_id"]),
            )
            is not None
        )

        invalid_path = paths.registration_requests / "not-an-instance.json"
        invalid_path.write_text("{}", encoding="utf-8")
        assert (
            ingest_registration_requests(
                store,
                paths,
                token=token,
                run_id=dynamic_identity().run_id,
                source_fingerprint=dynamic_identity().source_fingerprint,
                config_sha256=dynamic_identity().config_sha256,
                stream_pool_size=1,
                desired_contributors=1,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=103.0,
            )
            == []
        )
        assert not invalid_path.exists()
    finally:
        fenced.close()
        lease.close()


def test_dynamic_heartbeat_scan_uses_one_fenced_transaction(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=2, bootstrap=2)
        admissions = [
            admit(
                store,
                paths,
                launch_request_id=str(launches[index]["request_id"]),
                placement_id=f"host:gpu{index}",
            )[1]
            for index in range(2)
        ]
        before = int(fenced.business_transaction_metrics()["business_transaction_count"])
        accepted = store.update_instance_heartbeats(
            [
                (heartbeat(admission, timestamp=102.0), f"heartbeat-{index}.json")
                for index, admission in enumerate(admissions)
            ]
        )
        after = int(fenced.business_transaction_metrics()["business_transaction_count"])
        assert accepted == 2
        assert after - before == 1
        assert {row["last_seen"] for row in store.current_instances()} == {102.0}
    finally:
        fenced.close()
        lease.close()


def test_commit_revalidates_membership_and_selector_uniqueness(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        initialize_global_v0(store)
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        _, member = admit(
            store,
            paths,
            launch_request_id=str(launch["request_id"]),
            placement_id="host:gpu0",
            pool=1,
            desired=1,
        )
        metadata = update_metadata(member)
        assert store.insert_update_metadata(metadata)
        store.mark_updates_selected([metadata["update_id"]], "selection")
        revoked = store.revoke_dead_instances(
            heartbeat_dead_after_seconds=10.0,
            now=200.0,
        )
        assert [row["instance_id"] for row in revoked] == [member["instance_id"]]
        assert (
            store.insert_update_metadata(
                update_metadata(member, update_id="stale-pointer-after-revoke")
            )
            is False
        )
        with pytest.raises(DynamicMembershipFenceError, match="not current"):
            store.commit_full_merge(
                predecessor_version=0,
                target_version=1,
                weight_path="weights/v1.safetensors",
                optim_path="optim/v1.safetensors",
                selected_updates=[metadata],
                effective_weights={metadata["update_id"]: 1.0},
                total_update_tokens=16,
                total_seen_tokens=16,
                outer_optimizer="sgd",
                max_staleness_versions=0,
                publication_id="publication-v1",
                weight_size_bytes=1,
                optim_size_bytes=1,
                capacity_observation=capacity_parameters(
                    key="merge:1",
                    kind="merge",
                    global_version=1,
                    eligible=1,
                    selected_instance_ids=[member["instance_id"]],
                    now=100.0,
                ),
            )
        assert store.latest_global_version()["version"] == 0

        rows = [
            {
                "update_id": "u1",
                "learner_id": "i1",
                "stream_id": 0,
                "placement_id": "p1",
                "local_step_end": 2,
                "committed_at": 2.0,
            },
            {
                "update_id": "u2",
                "learner_id": "i2",
                "stream_id": 0,
                "placement_id": "p2",
                "local_step_end": 3,
                "committed_at": 3.0,
            },
            {
                "update_id": "u3",
                "learner_id": "i3",
                "stream_id": 1,
                "placement_id": "p2",
                "local_step_end": 1,
                "committed_at": 1.0,
            },
        ]
        selected = select_one_per_dynamic_member(rows)
        assert len({row["stream_id"] for row in selected}) == len(selected)
        assert len({row["placement_id"] for row in selected}) == len(selected)
    finally:
        fenced.close()
        lease.close()


def capacity_observation(
    store: Any,
    *,
    key: str,
    now: float,
    eligible: int,
    max_total_launch_requests: int = 2,
):
    return store.record_capacity_observation(
        **capacity_parameters(
            key=key,
            kind="synthetic",
            global_version=0,
            eligible=eligible,
            selected_instance_ids=[],
            max_total_launch_requests=max_total_launch_requests,
            now=now,
        )
    )


def test_capacity_observation_idempotency_hysteresis_and_limits(tmp_path: Path) -> None:
    _paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        initialize_membership(store, pool=2, bootstrap=0)
        first = capacity_observation(store, key="synthetic-low:1", now=100.0, eligible=0)
        assert first["consecutive_low_count"] == 1
        assert first["launch_request"] is None
        replay = capacity_observation(store, key="synthetic-low:1", now=101.0, eligible=0)
        assert replay["inserted"] is False
        second = capacity_observation(store, key="synthetic-low:2", now=102.0, eligible=0)
        assert second["consecutive_low_count"] == 2
        assert second["launch_request"] is not None
        assert len(store.capacity_observations()) == 2
        assert len(store.launch_requests(active_only=True)) == 1

        normal = capacity_observation(store, key="synthetic-healthy:1", now=103.0, eligible=2)
        assert normal["consecutive_low_count"] == 0
        third = capacity_observation(store, key="synthetic-low:3", now=104.0, eligible=0)
        assert third["launch_request"] is None
        assert len(store.launch_requests(active_only=True)) == 1

    finally:
        fenced.close()
        lease.close()


class MockScheduler:
    timeout_seconds = 0.1

    def __init__(self) -> None:
        self.found: PBSJobObservation | None = None
        self.queried: PBSJobObservation | None = None
        self.submissions = 0

    def find_by_launch_request(self, _request_id: str) -> PBSJobObservation | None:
        return self.found

    def query(self, job_id: str, *, historical: bool = False) -> PBSJobObservation:
        del historical
        return self.queried or PBSJobObservation(job_id, "unknown", {}, 0, "")

    def submit_learner(self, **_kwargs: Any) -> dict[str, Any]:
        self.submissions += 1
        return {"returncode": 0, "job_id_raw": "123.opbs"}


def test_pbs_learner_submission_can_override_acceptance_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="456.opbs\n", stderr="")

    monkeypatch.setattr("fs_diloco.runtime.pbs_scheduler.subprocess.run", run)
    result = PBSScheduler().submit_learner(
        script=tmp_path / "learner.pbs",
        launch_request_id="launch-request",
        shared_root=tmp_path / "run",
        descriptor_sha256="descriptor",
        walltime="00:02:00",
        queue="debug-g",
    )
    assert result["job_id_raw"] == "456.opbs"
    assert commands[0][commands[0].index("-q") + 1] == "debug-g"
    with pytest.raises(ValueError, match="unsafe PBS"):
        PBSScheduler().submit_learner(
            script=tmp_path / "learner.pbs",
            launch_request_id="launch-request",
            shared_root=tmp_path / "run",
            descriptor_sha256="descriptor",
            walltime="00:02:00",
            queue="debug-g,other",
        )


def test_launch_outbox_reconciles_qsub_windows_and_retains_scheduler_capacity(
    tmp_path: Path,
) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        initialize_membership(store, pool=2, bootstrap=0)
        capacity_observation(store, key="low:1", now=100.0, eligible=0)
        planned = capacity_observation(store, key="low:2", now=102.0, eligible=0)["launch_request"]
        assert planned is not None

        scheduler = MockScheduler()
        scheduler.found = PBSJobObservation("777", "queued", {"job_state": "Q"}, 0, "")
        outbox = LearnerLaunchOutbox(
            paths=paths,
            config=SimpleNamespace(
                scheduler_reconcile_interval_seconds=1.0,
                learner_pbs_script="learner.pbs",
                learner_walltime="00:01:00",
            ),
            scheduler=scheduler,
            descriptor_sha256="descriptor",
            wall_clock=lambda: 1000.0,
        )
        outbox.reconcile(store)
        row = next(
            item for item in store.launch_requests() if item["request_id"] == planned["request_id"]
        )
        assert row["state"] == "submitted"
        assert row["pbs_job_id"] == "777"
        assert row["reservation_released_at"] is None
        assert scheduler.submissions == 0

        scheduler.found = None
        scheduler.queried = PBSJobObservation("777", "unknown", {}, 0, "")
        outbox.reconcile(store)
        row = next(
            item for item in store.launch_requests() if item["request_id"] == planned["request_id"]
        )
        assert row["state"] == "submitted"
        assert row["reservation_released_at"] is None

        scheduler.queried = PBSJobObservation("777", "no_record", None, 1, "missing")
        outbox.reconcile(store)
        row = next(
            item for item in store.launch_requests() if item["request_id"] == planned["request_id"]
        )
        assert row["state"] == "failed"
        assert row["reservation_released_at"] == 1000.0
    finally:
        fenced.close()
        lease.close()


def test_bootstrap_job_remains_reserved_until_scheduler_terminal(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        jobs = [
            {
                "bootstrap_slot": 0,
                "request_id": str(launch["request_id"]),
                "pbs_job_id": "321.opbs",
            }
        ]
        store.record_external_launch_jobs(jobs, observed_at=100.0)
        scheduler = MockScheduler()
        scheduler.queried = PBSJobObservation("321.opbs", "queued", {"job_state": "Q"}, 0, "")
        outbox = LearnerLaunchOutbox(
            paths=paths,
            config=SimpleNamespace(
                scheduler_reconcile_interval_seconds=1.0,
                learner_pbs_script="learner.pbs",
                learner_walltime="00:01:00",
            ),
            scheduler=scheduler,
            descriptor_sha256="descriptor-digest",
            wall_clock=lambda: 10_000.0,
        )
        outbox.reconcile(store)
        row = next(
            item for item in store.launch_requests() if item["request_id"] == launch["request_id"]
        )
        assert row["state"] == "submitted"
        assert row["reservation_released_at"] is None

        scheduler.queried = PBSJobObservation("321.opbs", "no_record", None, 1, "missing")
        outbox.reconcile(store)
        row = next(
            item for item in store.launch_requests() if item["request_id"] == launch["request_id"]
        )
        assert row["state"] == "failed"
        assert row["reservation_released_at"] == 10_000.0
    finally:
        fenced.close()
        lease.close()


def test_dynamic_drain_ack_timeout_admission_fence_and_visibility(tmp_path: Path) -> None:
    paths, lease, token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=2, bootstrap=2)
        _, member = admit(
            store,
            paths,
            launch_request_id=str(launches[0]["request_id"]),
            placement_id="host:gpu0",
        )
        controller = store.begin_dynamic_drain(
            reason="manual",
            current_version=0,
            global_target=10,
            max_terminal_merges=1,
            requested_at=100.0,
        )
        assert controller["state"] == "draining"
        assert controller["max_terminal_version"] == 1
        directive_path = DynamicTerminalPublisher(paths, fenced, token).publish_drain(controller)
        assert directive_path.is_file()
        directive = read_current_drain(paths, run_id=dynamic_identity().run_id)
        assert directive is not None and directive["close_generation"] == 1

        late = request_payload(
            paths,
            instance_id=new_learner_instance_id(),
            launch_request_id=str(launches[1]["request_id"]),
            placement_id="host:gpu1",
            now=101.0,
        )
        with pytest.raises(RuntimeError, match="closed"):
            store.admit_registration(
                late,
                stream_pool_size=2,
                desired_contributors=2,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=101.0,
            )

        assert store.update_instance_heartbeat(
            heartbeat(
                member,
                status="drained",
                close_generation=1,
                final_update_id="final-update",
                timestamp=101.0,
            ),
            heartbeat_path="heartbeat.json",
        )
        started = store.advance_dynamic_drain(
            drain_ack_timeout_seconds=10.0,
            registration_visibility_grace_seconds=2.0,
            proposal_visibility_grace_seconds=3.0,
            now=101.0,
        )
        assert started["input_closed"] is False
        closed = store.advance_dynamic_drain(
            drain_ack_timeout_seconds=10.0,
            registration_visibility_grace_seconds=2.0,
            proposal_visibility_grace_seconds=3.0,
            now=104.0,
        )
        assert closed["input_closed"] is False
        assert closed["pending_final_pointers"] == 1
        assert store.insert_update_metadata(update_metadata(member, update_id="final-update"))
        visibility = store.advance_dynamic_drain(
            drain_ack_timeout_seconds=10.0,
            registration_visibility_grace_seconds=2.0,
            proposal_visibility_grace_seconds=3.0,
            now=104.0,
        )
        assert visibility["input_closed"] is False
        closed = store.advance_dynamic_drain(
            drain_ack_timeout_seconds=10.0,
            registration_visibility_grace_seconds=2.0,
            proposal_visibility_grace_seconds=3.0,
            now=107.0,
        )
        assert closed["input_closed"] is True
        assert store.dynamic_input_closed()

        controller_replay = store.begin_dynamic_drain(
            reason="manual",
            current_version=0,
            global_target=10,
            max_terminal_merges=1,
            requested_at=200.0,
        )
        assert controller_replay["generation"] == 1
    finally:
        fenced.close()
        lease.close()


def test_manual_deadline_and_launch_budget_close_requests(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        initialize_membership(store, pool=1, bootstrap=0)
        config = Config()
        config.membership.mode = "dynamic"
        config.run.run_id = dynamic_identity().run_id
        config.run.source_fingerprint = dynamic_identity().source_fingerprint

        config.terminal.admission_close_policy = "manual"
        assert (
            dynamic_non_target_close_request(
                config,
                store,
                paths,
                now=105.0,
            )
            is None
        )
        written = write_dynamic_close_request(
            paths,
            run_id=dynamic_identity().run_id,
            source_fingerprint=dynamic_identity().source_fingerprint,
            config_sha256=dynamic_identity().config_sha256,
            requested_at=104.0,
        )
        assert (
            read_dynamic_close_request(
                paths,
                run_id=dynamic_identity().run_id,
                source_fingerprint=dynamic_identity().source_fingerprint,
                config_sha256=dynamic_identity().config_sha256,
            )
            == written
        )
        assert dynamic_non_target_close_request(
            config,
            store,
            paths,
            now=105.0,
        ) == ("manual", 104.0)

        config.terminal.admission_close_policy = "deadline"
        config.terminal.deadline_seconds = 10.0
        assert (
            dynamic_non_target_close_request(
                config,
                store,
                paths,
                now=109.99,
            )
            is None
        )
        assert dynamic_non_target_close_request(
            config,
            store,
            paths,
            now=110.0,
        ) == ("deadline", 110.0)

        config.terminal.admission_close_policy = "global_target_or_launch_budget"
        config.scaling.enabled = True
        config.scaling.max_total_launch_requests = 1
        store.set_run_state("scale_launch_request_count", 1)
        assert dynamic_non_target_close_request(
            config,
            store,
            paths,
            now=111.0,
        ) == ("launch_budget_exhausted", 111.0)
    finally:
        fenced.close()
        lease.close()


def test_dynamic_state_is_bounded_after_1000_churn_cycles(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launch = initialize_membership(store, pool=1, bootstrap=1)[0]
        _, current = admit(
            store,
            paths,
            launch_request_id=str(launch["request_id"]),
            placement_id="host:gpu0",
            pool=1,
            desired=1,
        )
        page_count_after_warmup = 0
        for cycle in range(1, 1001):
            now = 100.0 + cycle * 100.0
            revoked = store.revoke_dead_instances(
                heartbeat_dead_after_seconds=1.0,
                now=now,
            )
            assert len(revoked) == 1
            authorization = store.authorize_placement_replacement(
                placement_id="host:gpu0",
                reason=f"churn-{cycle}",
                ttl_seconds=50.0,
                now=now + 1.0,
            )
            _, current = admit(
                store,
                paths,
                launch_request_id=str(authorization["request_id"]),
                placement_id="host:gpu0",
                now=now + 2.0,
                pool=1,
                desired=1,
            )
            store.record_capacity_observation(
                observation_key=f"churn:{cycle}",
                kind="synthetic",
                global_version=cycle,
                eligible_contributors=1,
                selected_instance_ids=[current["instance_id"]],
                low_contributor_threshold=0,
                consecutive_low_windows=2,
                productive_window_count=2,
                startup_grace_seconds=10.0,
                heartbeat_stale_after_seconds=10.0,
                productive_upload_grace_factor=2.0,
                productive_upload_grace_min_seconds=1.0,
                productive_upload_grace_max_seconds=10.0,
                desired_contributors=1,
                stream_pool_size=1,
                scaling_enabled=False,
                initial_membership_deadline_seconds=10.0,
                cooldown_seconds=10.0,
                max_pending_launch_requests=1,
                max_total_launch_requests=1,
                launch_request_ttl_seconds=20.0,
                config_fingerprint="descriptor",
                now=now + 3.0,
            )
            archive_dynamic_history(
                store,
                paths,
                expired_retention_seconds=1.0,
                max_active_instance_records=4,
                capacity_observation_retention_count=8,
            )
            if cycle == 100:
                page_count_after_warmup = int(
                    fenced._connection.execute("PRAGMA page_count").fetchone()[0]
                )

        counts = store.dynamic_state_counts()
        assert counts["current_instances"] == 1
        assert current["stream_epoch"] == 1000
        assert counts["registration_requests"] == 0
        assert counts["active_launch_requests"] == 0
        assert counts["capacity_observations"] <= 8
        assert len(store.registration_requests()) <= 8
        assert len(store.launch_requests()) <= 9
        assert sum(row["reason"] == "bootstrap" for row in store.launch_requests()) == 1
        assert len(list(paths.iter_registration_requests())) == 0
        final_page_count = int(fenced._connection.execute("PRAGMA page_count").fetchone()[0])
        # Frozen RED threshold: after 100-cycle warm-up, 900 more churn cycles
        # may consume at most 32 additional reusable SQLite pages.
        assert final_page_count - page_count_after_warmup <= 32
        assert paths.learner_instance_history_jsonl.is_file()
        assert paths.registration_history_jsonl.is_file()
        assert paths.launch_request_history_jsonl.is_file()
        assert paths.capacity_observation_history_jsonl.is_file()
        assert paths.membership_history_jsonl.is_file()

        readonly = ReadOnlySQLiteStore(paths.sqlite_db)
        try:
            assert readonly.dynamic_mode
            assert readonly.dynamic_state_counts()["current_instances"] == 1
            assert len(readonly.streams()) == 1
        finally:
            readonly.close()
    finally:
        fenced.close()
        lease.close()


def test_replacement_without_same_placement_stream_reuse_releases_old_stream(
    tmp_path: Path,
) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        launches = initialize_membership(store, pool=2, bootstrap=2)
        _, first = admit(
            store,
            paths,
            launch_request_id=str(launches[0]["request_id"]),
            placement_id="host:gpu0",
        )
        authorization = store.authorize_placement_replacement(
            placement_id="host:gpu0",
            reason="move to another virtual stream",
            ttl_seconds=60.0,
            now=102.0,
        )
        instance_id = new_learner_instance_id()
        request = request_payload(
            paths,
            instance_id=instance_id,
            launch_request_id=str(authorization["request_id"]),
            placement_id="host:gpu0",
            now=103.0,
        )
        replacement = store.admit_registration(
            request,
            stream_pool_size=2,
            desired_contributors=2,
            allow_unsolicited_registration=False,
            allow_healthy_placement_replacement=False,
            reuse_stream_for_same_placement=False,
            now=103.0,
        )
        assert replacement["stream_id"] != first["stream_id"]
        streams = {row["stream_id"]: row for row in store.streams()}
        assert streams[first["stream_id"]]["current_instance_id"] is None
        assert streams[first["stream_id"]]["state"] == "reusable"
        assert streams[replacement["stream_id"]]["current_instance_id"] == instance_id
    finally:
        fenced.close()
        lease.close()


def test_scale_budget_and_cooldown_survive_history_archival(tmp_path: Path) -> None:
    paths, lease, _token, fenced, store = dynamic_store(tmp_path)
    try:
        initialize_membership(store, pool=2, bootstrap=0)
        capacity_observation(
            store,
            key="budget:1",
            now=100.0,
            eligible=0,
            max_total_launch_requests=1,
        )
        launched = capacity_observation(
            store,
            key="budget:2",
            now=102.0,
            eligible=0,
            max_total_launch_requests=1,
        )["launch_request"]
        assert launched is not None
        store.update_launch_request(
            request_id=str(launched["request_id"]),
            expected_states={"planned"},
            state="failed",
            observed_at=103.0,
        )
        archive_dynamic_history(
            store,
            paths,
            expired_retention_seconds=1.0,
            max_active_instance_records=4,
            capacity_observation_retention_count=2,
        )
        assert not any(row["reason"] == "scale_out" for row in store.launch_requests())
        blocked = capacity_observation(
            store,
            key="budget:3",
            now=1000.0,
            eligible=0,
            max_total_launch_requests=1,
        )
        assert blocked["launch_request"] is None
        assert store.get_run_state("scale_launch_request_count") == 1
        assert store.get_run_state("last_scale_launch_at") == 102.0
    finally:
        fenced.close()
        lease.close()


def test_matched_launcher_uses_independent_learners_and_persists_receipts(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def qsub(command: list[str]) -> dict[str, Any]:
        commands.append(command)
        job_id = f"{1000 + len(commands)}.opbs"
        return {
            "status": "submitted",
            "returncode": 0,
            "stdout": job_id,
            "stderr": "",
            "job_id": job_id,
            "command": command,
        }

    project_root = tmp_path / "project"
    static_root = tmp_path / "static"
    dynamic_root = tmp_path / "dynamic"
    initialized_static = {
        "descriptor": {
            "shared_root": str(static_root),
            "descriptor_sha256": "static-descriptor",
            "run_id": "static-run",
            "source_fingerprint": "sha256:source",
            "resolved_config_sha256": "static-config",
        }
    }
    initialized_dynamic = {
        "descriptor": {
            "shared_root": str(dynamic_root),
            "descriptor_sha256": "dynamic-descriptor",
            "run_id": "dynamic-run",
            "source_fingerprint": "sha256:source",
            "resolved_config_sha256": "dynamic-config",
        }
    }
    receipts = tmp_path / "matched-receipts.json"
    payload = submit_matched_jobs(
        project_root=project_root,
        initialized_static=initialized_static,
        initialized_dynamic=initialized_dynamic,
        launcher_job_id="launcher.opbs",
        syncer_walltime="00:02:00",
        learner_walltime="00:01:30",
        checker_walltime="00:00:20",
        receipts_path=receipts,
        output_path=tmp_path / "matched.json",
        qsub_fn=qsub,
    )

    assert payload["status"] == "PASS"
    assert len(commands) == 19
    assert all("-J" not in command for command in commands)
    roles = [row["role"] for row in payload["submission_receipts"]]
    assert roles == [
        "static_syncer",
        *(f"static_learner_{index}" for index in range(8)),
        "dynamic_syncer",
        *(f"dynamic_learner_{index}" for index in range(8)),
        "matched_checker",
    ]
    for index, command in enumerate(commands[1:9]):
        variables = command[command.index("-v") + 1]
        assert f"LEARNER_INDEX={index}" in variables
        assert "depend=after:1001.opbs" in command
    for index, command in enumerate(commands[10:18]):
        variables = command[command.index("-v") + 1]
        assert f"BOOTSTRAP_SLOT={index}" in variables
        assert "depend=after:1010.opbs" in command
    checker = commands[-1]
    assert "depend=afterany:1010.opbs" in checker
    assert safe_read_json(receipts) == payload
    manifest = read_bootstrap_scheduler_jobs(
        RunPaths(dynamic_root),
        run_id="dynamic-run",
        source_fingerprint="sha256:source",
        config_sha256="dynamic-config",
        config_fingerprint="dynamic-descriptor",
    )
    assert [row["pbs_job_id"] for row in manifest] == [f"{1011 + index}.opbs" for index in range(8)]
