"""Run-directory path helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .atomic_io import ensure_dir
from .constants import DB_DUMP_TEMPLATE, GLOBAL_WEIGHT_TEMPLATE, OUTER_OPTIM_TEMPLATE


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
    def updates_pending(self) -> Path:
        return self.shared_root / "updates" / "pending"

    @property
    def updates_processed(self) -> Path:
        return self.shared_root / "updates" / "processed"

    @property
    def updates_dropped(self) -> Path:
        return self.shared_root / "updates" / "dropped"

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
    def db_dumps(self) -> Path:
        return self.shared_root / "db_dumps"

    @property
    def logs(self) -> Path:
        return self.shared_root / "logs"

    @property
    def metrics(self) -> Path:
        return self.shared_root / "metrics"

    @property
    def latest_json(self) -> Path:
        return self.control / "latest.json"

    @property
    def stop_json(self) -> Path:
        return self.control / "stop.json"

    @property
    def param_index_json(self) -> Path:
        return self.control / "param_index.json"

    @property
    def fragment_index_json(self) -> Path:
        return self.fragments / "fragment_index.json"

    @property
    def resolved_config_yaml(self) -> Path:
        return self.control / "run_config.resolved.yaml"

    def global_weight_path(self, version: int) -> Path:
        return self.weights / GLOBAL_WEIGHT_TEMPLATE.format(version=version)

    def outer_optim_path(self, version: int) -> Path:
        return self.optim / OUTER_OPTIM_TEMPLATE.format(version=version)

    def fragment_weight_path(self, fragment_id: int, version: int) -> Path:
        return self.fragment_weights / f"fragment_{fragment_id:03d}" / f"v{version:06d}.safetensors"

    def fragment_outer_optim_path(self, fragment_id: int, version: int) -> Path:
        return self.fragment_optim / f"fragment_{fragment_id:03d}" / f"v{version:06d}.safetensors"

    def db_dump_path(self, timestamp: str, version: int) -> Path:
        return self.db_dumps / DB_DUMP_TEMPLATE.format(timestamp=timestamp, version=version)


def prepare_run_dirs(paths: RunPaths, num_learners: int) -> None:
    for directory in [
        paths.control,
        paths.weights,
        paths.optim,
        paths.updates_pending,
        paths.updates_processed,
        paths.updates_dropped,
        paths.fragments,
        paths.fragment_weights,
        paths.fragment_optim,
        paths.heartbeats,
        paths.db_dumps,
        paths.logs,
        paths.metrics,
    ]:
        ensure_dir(directory)
    for index in range(num_learners):
        learner_dir = f"learner_{index:03d}"
        ensure_dir(paths.updates_pending / learner_dir)
        ensure_dir(paths.updates_processed / learner_dir)
        ensure_dir(paths.updates_dropped / learner_dir)
