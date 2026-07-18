"""Run-directory path helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .atomic_io import ensure_dir
from ..core.constants import GLOBAL_WEIGHT_TEMPLATE, OUTER_OPTIM_TEMPLATE


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
    def optim(self) -> Path:
        return self.shared_root / "optim"

    @property
    def updates_latest(self) -> Path:
        return self.shared_root / "updates" / "latest"

    @property
    def updates_payloads(self) -> Path:
        return self.shared_root / "updates" / "payloads"

    @property
    def updates_pending(self) -> Path:
        """Compatibility alias for fragment proposal payload storage."""
        return self.updates_payloads

    @property
    def fragments(self) -> Path:
        return self.shared_root / "fragments"

    @property
    def fragment_weights(self) -> Path:
        return self.fragments / "weights"

    @property
    def fragment_optim(self) -> Path:
        return self.fragments / "optim"

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
    def eval_checkpoints(self) -> Path:
        """Non-authoritative checkpoints retained only for offline evaluation."""
        return self.shared_root / "eval_checkpoints"

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
    def fragment_index_json(self) -> Path:
        return self.fragments / "fragment_index.json"

    @property
    def resolved_config_yaml(self) -> Path:
        return self.control / "run_config.resolved.yaml"

    @property
    def run_root_config_yaml(self) -> Path:
        return self.shared_root / "run_config.resolved.yaml"

    @property
    def sqlite_db(self) -> Path:
        return self.control / "syncer_metadata.sqlite3"

    @property
    def update_history_jsonl(self) -> Path:
        return self.metrics / "update_history.jsonl"

    @property
    def global_version_history_jsonl(self) -> Path:
        return self.metrics / "global_version_history.jsonl"

    def update_pointer_path(self, learner_id: str) -> Path:
        return self.updates_latest / f"{learner_id}.json"

    def fragment_update_pointer_path(self, learner_id: str, fragment_id: int) -> Path:
        return self.updates_latest / f"{learner_id}_f{int(fragment_id):03d}.json"

    def update_payload_dir(self, learner_id: str) -> Path:
        return self.updates_payloads / learner_id

    def global_weight_path(self, version: int) -> Path:
        return self.weights / GLOBAL_WEIGHT_TEMPLATE.format(version=version)

    def outer_optim_path(self, version: int) -> Path:
        return self.optim / OUTER_OPTIM_TEMPLATE.format(version=version)

    def fragment_weight_path(self, fragment_id: int, version: int) -> Path:
        return self.fragment_weights / f"fragment_{fragment_id:03d}" / f"v{version:06d}.safetensors"

    def fragment_outer_optim_path(self, fragment_id: int, version: int) -> Path:
        return self.fragment_optim / f"fragment_{fragment_id:03d}" / f"v{version:06d}.safetensors"

def prepare_run_dirs(paths: RunPaths, num_learners: int) -> None:
    for directory in [
        paths.control,
        paths.weights,
        paths.optim,
        paths.updates_latest,
        paths.updates_payloads,
        paths.fragments,
        paths.fragment_weights,
        paths.fragment_optim,
        paths.heartbeats,
        paths.logs,
        paths.metrics,
    ]:
        ensure_dir(directory)
    for index in range(num_learners):
        learner_id = f"learner_{index:03d}"
        ensure_dir(paths.update_payload_dir(learner_id))
