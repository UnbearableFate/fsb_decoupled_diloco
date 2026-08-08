"""Small real-SQLite dynamic authority fixture with deterministic lease time."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fs_diloco.protocol.membership import new_learner_instance_id, write_registration_request
from fs_diloco.storage.fenced_store import FencedSQLiteStore
from fs_diloco.storage.leader_lease import LeaderLeaseStore, LeaseSafetyTracker
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from fs_diloco.storage.schema_bootstrap import BootstrapIdentity, initialize_new_run


@dataclass
class DynamicAuthorityHarness:
    paths: RunPaths
    identity: BootstrapIdentity
    lease: LeaderLeaseStore
    fenced: FencedSQLiteStore
    store: Any

    @classmethod
    def create(cls, root: Path, *, run_id: str = "plan03-red") -> "DynamicAuthorityHarness":
        paths = RunPaths(root)
        prepare_authority_dirs(paths)
        identity = BootstrapIdentity(
            run_id=run_id,
            source_fingerprint="sha256:plan03-red-source",
            config_sha256="plan03-red-config",
            mode="full_dynamic",
        )
        initialize_new_run(paths.sqlite_db, identity, marker_path=paths.bootstrap_complete_json)
        lease = LeaderLeaseStore(
            paths.sqlite_db,
            identity,
            marker_path=paths.bootstrap_complete_json,
            lease_duration_seconds=90.0,
            max_clock_skew_seconds=2.0,
            wall_clock=lambda: 100.0,
        )
        token = lease.acquire(owner_id="plan03-owner", hostname="host", pid=1)
        tracker = LeaseSafetyTracker(
            token,
            lease_duration_seconds=90.0,
            max_clock_skew_seconds=2.0,
        )
        fenced = FencedSQLiteStore(
            paths.sqlite_db,
            identity,
            marker_path=paths.bootstrap_complete_json,
            max_clock_skew_seconds=2.0,
            wall_clock=lambda: 100.0,
            lease_safety_check=tracker.assert_safe,
        )
        return cls(paths, identity, lease, fenced, fenced.bind(token))

    def close(self) -> None:
        self.fenced.close()
        self.lease.close()

    def initialize(self, *, members: int = 2) -> list[dict[str, Any]]:
        self.store.initialize_full_run(
            weight_path="weights/v0.safetensors",
            optim_path="optim/v0.safetensors",
            outer_optimizer="sgd",
            identity={"kind": "test"},
            config_snapshot={"kind": "test"},
            publication_id="publication-v0",
            weight_size_bytes=1,
            optim_size_bytes=1,
        )
        launches = self.store.initialize_dynamic_membership(
            stream_pool_size=members,
            bootstrap_instances=members,
            config_fingerprint="descriptor-digest",
            created_at=100.0,
        )
        admissions = []
        for index, launch in enumerate(launches):
            instance_id = new_learner_instance_id()
            request = write_registration_request(
                self.paths,
                run_id=self.identity.run_id,
                instance_id=instance_id,
                placement=f"host:gpu{index}",
                launch_request_id=str(launch["request_id"]),
                source_fingerprint=self.identity.source_fingerprint,
                config_sha256=self.identity.config_sha256,
                ttl_seconds=60.0,
                hostname="host",
                pid=index + 10,
                gpu_identity=f"gpu{index}",
                now=100.0,
            )
            admission = self.store.admit_registration(
                request,
                stream_pool_size=members,
                desired_contributors=members,
                allow_unsolicited_registration=False,
                allow_healthy_placement_replacement=False,
                reuse_stream_for_same_placement=True,
                now=100.0,
            )
            self.paths.registration_request_path(instance_id).unlink(missing_ok=True)
            admissions.append(admission)
        return admissions

    @staticmethod
    def proposal(
        admission: dict[str, Any], update_id: str, *, committed_at: float
    ) -> dict[str, Any]:
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
            "created_at": committed_at,
            "committed_at": committed_at,
            "placement_id": admission["placement_id"],
            "placement_epoch": admission["placement_epoch"],
            "stream_id": admission["stream_id"],
            "stream_epoch": admission["stream_epoch"],
            "admission_generation": admission["admission_generation"],
            "admission_token_hash": hashlib.sha256(
                admission["admission_token"].encode("utf-8")
            ).hexdigest(),
        }
