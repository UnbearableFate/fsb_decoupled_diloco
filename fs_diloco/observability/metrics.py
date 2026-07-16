"""CSV metrics helpers."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..storage.atomic_io import ensure_dir


def append_csv_row(
    path: str | Path, row: dict[str, Any], fieldnames: list[str] | None = None
) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    if fieldnames is None:
        fieldnames = list(row.keys())
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


SYNCER_METRIC_FIELDS = [
    "timestamp",
    "version",
    "global_merge_event",
    "fragment_id",
    "fragment_version",
    "selected_count",
    "total_update_tokens",
    "read_seconds",
    "fragment_read_seconds",
    "aggregation_seconds",
    "fragment_aggregation_seconds",
    "outer_step_seconds",
    "publish_seconds",
    "materialize_full_seconds",
    "fragment_staleness_min",
    "fragment_staleness_mean",
    "fragment_staleness_max",
    "stale_updates_dropped",
    "global_interval_seconds",
    "learner_training_cpu_utilization_peak_percent_mean",
    "learner_training_gpu_utilization_peak_percent_mean",
    "learner_local_cycle_cpu_utilization_peak_percent_mean",
    "learner_local_cycle_gpu_utilization_peak_percent_mean",
    "learner_local_cycle_step_time_seconds_mean",
]

LEARNER_METRIC_FIELDS = [
    "timestamp",
    "learner_id",
    "local_step",
    "global_version",
    "global_merge_event",
    "fragment_id",
    "base_fragment_version",
    "train_loss",
    "tokens",
    "tokens_per_sec",
    "update_write_seconds",
    "param_norm",
    "fragment_norm",
    "last_loaded_fragment_versions_json",
    "fragment_adopt_count",
    "phase",
    "training_cpu_utilization_peak_percent",
    "training_gpu_utilization_peak_percent",
    "local_cycle_cpu_utilization_peak_percent",
    "local_cycle_gpu_utilization_peak_percent",
    "local_cycle_step_time_seconds_mean",
    "local_cycle_step_count",
    "local_cycle_resource_sample_count",
]

UPDATE_MANIFEST_FIELDS = [
    "timestamp",
    "update_id",
    "learner_id",
    "update_kind",
    "fragment_id",
    "base_fragment_version",
    "base_global_merge_event",
    "base_global_version",
    "local_step_start",
    "local_step_end",
    "tokens_this_update",
    "tensor_dtype",
    "file_path",
    "file_size_bytes",
    "sha256",
]
