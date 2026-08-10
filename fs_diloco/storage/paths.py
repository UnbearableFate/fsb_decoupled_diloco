"""Run-directory path helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import ensure_dir


@dataclass(frozen=True)
class RunPaths:
    shared_root: Path

    @property
    def control(self) -> Path:
        return self.shared_root / "control"

    @property
    def weights(self) -> Path:
        return self.shared_root / "weights"

    @property
    def weight_epochs(self) -> Path:
        return self.weights / "epochs"

    @property
    def optim(self) -> Path:
        return self.shared_root / "optim"

    @property
    def optim_epochs(self) -> Path:
        return self.optim / "epochs"

    @property
    def updates_latest(self) -> Path:
        return self.shared_root / "updates" / "latest"

    @property
    def updates_payloads(self) -> Path:
        return self.shared_root / "updates" / "payloads"

    @property
    def heartbeats(self) -> Path:
        return self.shared_root / "heartbeats"

    @property
    def logs(self) -> Path:
        return self.shared_root / "logs"

    @property
    def metrics(self) -> Path:
        return self.shared_root / "metrics"

    @property
    def syncer_epochs(self) -> Path:
        return self.control / "syncer_epochs"

    @property
    def registration_requests(self) -> Path:
        return self.control / "registration_requests"

    @property
    def registration_history(self) -> Path:
        return self.control / "registration_history"

    @property
    def registration_dispositions(self) -> Path:
        return self.control / "registration_dispositions"

    @property
    def static_replacement_requests(self) -> Path:
        return self.control / "static_replacement_requests"

    @property
    def scheduler_operator_requests(self) -> Path:
        return self.control / "scheduler_operator_requests"

    @property
    def audit_batches(self) -> Path:
        return self.shared_root / "audit" / "batches"

    @property
    def audit_partitions(self) -> Path:
        return self.shared_root / "audit" / "partitions"

    @property
    def audit_command_receipts(self) -> Path:
        return self.shared_root / "audit" / "command_receipts"

    @property
    def terminal_close_request_json(self) -> Path:
        return self.control / "terminal_close_request.json"

    @property
    def bootstrap_complete_json(self) -> Path:
        return self.control / "bootstrap_complete.json"

    @property
    def run_identity_file(self) -> Path:
        return self.shared_root / ".identity"

    @property
    def run_complete_file(self) -> Path:
        return self.shared_root / ".complete"

    @property
    def artifact_policy_json(self) -> Path:
        return self.control / "artifact_policy.json"

    @property
    def run_descriptor_json(self) -> Path:
        return self.control / "run_descriptor.json"

    @property
    def run_source_manifest_json(self) -> Path:
        return self.control / "run_source_manifest.json"

    @property
    def latest_json(self) -> Path:
        return self.control / "latest.json"

    @property
    def stop_json(self) -> Path:
        return self.control / "stop.json"

    @property
    def summary_json(self) -> Path:
        return self.control / "summary.json"

    @property
    def param_index_json(self) -> Path:
        return self.control / "param_index.json"

    @property
    def resolved_config_yaml(self) -> Path:
        return self.control / "run_config.resolved.yaml"

    @property
    def run_root_config_yaml(self) -> Path:
        return self.shared_root / "run_config.resolved.yaml"

    @property
    def sqlite_db(self) -> Path:
        return self.control / "syncer_metadata.sqlite3"

    def update_pointer_path(self, learner_id: str) -> Path:
        return self.updates_latest / f"{learner_id}.json"

    def update_payload_dir(self, learner_id: str) -> Path:
        return self.updates_payloads / learner_id

    def actor_metrics_path(self, actor_kind: str, actor_id: str, attempt_id: str) -> Path:
        return self.metrics / actor_kind / actor_id / f"{attempt_id}.jsonl"

    def actor_attestation_path(self, actor_kind: str, actor_id: str, attempt_id: str) -> Path:
        return self.metrics / "attestations" / actor_kind / actor_id / f"{attempt_id}.json"

    @staticmethod
    def owner_short(owner_id: str) -> str:
        return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:12]

    def syncer_epoch_dir(self, epoch: int, owner_id: str) -> Path:
        return self.syncer_epochs / f"e{int(epoch):06d}_{self.owner_short(owner_id)}"

    def syncer_heartbeat_path(self, epoch: int, owner_id: str) -> Path:
        return self.syncer_epoch_dir(epoch, owner_id) / "heartbeat.json"

    def epoch_latest_dir(self, epoch: int, owner_id: str) -> Path:
        return self.syncer_epoch_dir(epoch, owner_id) / "latest"

    def epoch_head_path(self, epoch: int, owner_id: str) -> Path:
        return self.epoch_latest_dir(epoch, owner_id) / "head.json"

    def epoch_version_pointer_path(self, epoch: int, owner_id: str, version: int) -> Path:
        return self.epoch_latest_dir(epoch, owner_id) / f"v{int(version):06d}.json"

    def epoch_terminal_dir(self, epoch: int, owner_id: str) -> Path:
        return self.syncer_epoch_dir(epoch, owner_id) / "terminal"

    def epoch_membership_dir(self, epoch: int, owner_id: str) -> Path:
        return self.syncer_epoch_dir(epoch, owner_id) / "membership"

    def epoch_admission_response_path(
        self,
        epoch: int,
        owner_id: str,
        actor_id: str,
        attempt_id: str,
        fence_namespace: str,
    ) -> Path:
        return (
            self.epoch_membership_dir(epoch, owner_id)
            / "admissions"
            / "responses"
            / actor_id
            / attempt_id
            / f"{fence_namespace}.json"
        )

    def epoch_current_admission_path(
        self, epoch: int, owner_id: str, stable_contributor_key: str
    ) -> Path:
        return (
            self.epoch_membership_dir(epoch, owner_id)
            / "admissions"
            / "current"
            / f"{stable_contributor_key}.json"
        )

    def epoch_admission_rejection_path(
        self,
        epoch: int,
        owner_id: str,
        actor_id: str,
        attempt_id: str,
        request_sha256: str,
    ) -> Path:
        return (
            self.epoch_membership_dir(epoch, owner_id)
            / "admissions"
            / "rejections"
            / actor_id
            / attempt_id
            / f"{request_sha256}.json"
        )

    def registration_disposition_path(self, request_sha256: str) -> Path:
        return self.registration_dispositions / f"{request_sha256}.json"

    def registration_history_path(self, request_sha256: str) -> Path:
        return self.registration_history / f"{request_sha256}.json"

    def static_replacement_request_path(self, learner_id: str, attempt_id: str) -> Path:
        return self.static_replacement_requests / learner_id / f"{attempt_id}.json"

    def epoch_receipt_ack_path(
        self,
        epoch: int,
        owner_id: str,
        stable_contributor_key: str,
        cycle_seq: int,
    ) -> Path:
        return (
            self.syncer_epoch_dir(epoch, owner_id)
            / "receipt_acks"
            / stable_contributor_key
            / f"c{int(cycle_seq):09d}.json"
        )

    def epoch_stop_path(self, epoch: int, owner_id: str, generation: int) -> Path:
        return self.epoch_terminal_dir(epoch, owner_id) / f"stop_g{int(generation):06d}.json"

    def epoch_weight_path(
        self, epoch: int, owner_id: str, version: int, publication_id: str
    ) -> Path:
        publication_short = publication_id.replace("-", "")[:12]
        return (
            self.weight_epochs
            / f"e{int(epoch):06d}"
            / self.owner_short(owner_id)
            / f"global_v{int(version):06d}_p{publication_short}.safetensors"
        )

    def epoch_outer_optim_path(
        self, epoch: int, owner_id: str, version: int, publication_id: str
    ) -> Path:
        publication_short = publication_id.replace("-", "")[:12]
        return (
            self.optim_epochs
            / f"e{int(epoch):06d}"
            / self.owner_short(owner_id)
            / f"outer_v{int(version):06d}_p{publication_short}.safetensors"
        )

    def relative(self, path: str | Path) -> str:
        return str(Path(path).resolve().relative_to(self.shared_root.resolve()))

def prepare_authority_dirs(paths: RunPaths) -> None:
    """Create the fixed run authority directories during explicit initialization."""
    for directory in (
        paths.control,
        paths.weights,
        paths.weight_epochs,
        paths.optim,
        paths.optim_epochs,
        paths.updates_latest,
        paths.updates_payloads,
        paths.heartbeats,
        paths.logs,
        paths.metrics,
        paths.syncer_epochs,
        paths.registration_requests,
        paths.registration_history,
        paths.registration_dispositions,
        paths.static_replacement_requests,
        paths.scheduler_operator_requests,
        paths.audit_batches,
        paths.audit_partitions,
        paths.audit_command_receipts,
    ):
        ensure_dir(directory)


def prepare_learner_instance_dir(paths: RunPaths, learner_id: str) -> Path:
    """Create only a learner-owned payload directory under an initialized run."""
    required_parents = (paths.control, paths.updates_latest, paths.updates_payloads)
    missing = [str(path) for path in required_parents if not path.is_dir()]
    if missing:
        raise RuntimeError(f"run authority directories are missing: {missing}")
    return ensure_dir(paths.update_payload_dir(learner_id))
