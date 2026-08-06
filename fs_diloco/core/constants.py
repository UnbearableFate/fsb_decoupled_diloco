"""Shared constants for the filesystem DiLoCo prototype."""

from __future__ import annotations

FORMAT_VERSION = 1
PROTOCOL_VERSION = 3
HA_SCHEMA_VERSION = 2
SYNCER_HEARTBEAT_FORMAT_VERSION = 1
CONTROL_EPOCH_FORMAT_VERSION = 1
DEFAULT_RUNS_DIR = "runs/fs_diloco"
LEARNER_ID_PREFIX = "learner_"

UPDATE_STATUS_PENDING = "pending"
UPDATE_STATUS_SELECTED = "selected"
UPDATE_STATUS_APPLIED = "applied"
UPDATE_STATUS_DROPPED = "dropped"
UPDATE_STATUS_FAILED = "failed"

LEARNER_STATUS_UNKNOWN = "unknown"
LEARNER_STATUS_ACTIVE = "active"
LEARNER_STATUS_STALE = "stale"
LEARNER_STATUS_DEAD = "dead"
LEARNER_STATUS_STOPPED = "stopped"

GLOBAL_STATUS_WRITING = "writing"
GLOBAL_STATUS_COMMITTED = "committed"
GLOBAL_STATUS_ABANDONED = "abandoned"

GLOBAL_WEIGHT_TEMPLATE = "global_v{version:06d}.safetensors"
OUTER_OPTIM_TEMPLATE = "outer_v{version:06d}.safetensors"


def learner_id_from_index(index: int) -> str:
    return f"{LEARNER_ID_PREFIX}{index:03d}"


def learner_index_from_id(learner_id: str) -> int:
    if not learner_id.startswith(LEARNER_ID_PREFIX):
        raise ValueError(f"invalid learner id: {learner_id}")
    return int(learner_id.removeprefix(LEARNER_ID_PREFIX))
