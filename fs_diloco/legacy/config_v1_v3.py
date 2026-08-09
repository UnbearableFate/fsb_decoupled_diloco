"""Read-only config projection for completed v1-v3 runs.

The production loader deliberately rejects these keys.  Evaluation and export
tools opt into this module so historical runs remain inspectable without making
their runtime modes constructible or resumable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..core.config import Config, _from_dict, load_resolved_config_snapshot


_LEGACY_TOP_LEVEL_RUNTIME_KEYS = frozenset({"init", "fragments", "failure_sim"})
_LEGACY_NESTED_RUNTIME_KEYS = {
    "coordination": frozenset({"syncer_ha", "recovery_submission"}),
    "sync": frozenset(
        {
            "stop_after_global_tokens",
            "stop_after_direct_weight_tokens_applied",
            "capture_terminal_predecessor_for_eval",
            "upload_mode",
        }
    ),
    "liveness": frozenset({"quorum_policy"}),
    "inner_optimizer": frozenset({"reset_on_global_update"}),
}


@dataclass(frozen=True)
class LegacyConfigV1V3:
    payload: Mapping[str, Any]
    semantic_version: str = "legacy-config-v1-v3"


def load_legacy_config(path: str | Path) -> LegacyConfigV1V3:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("legacy config must contain a mapping")
    return LegacyConfigV1V3(payload=payload)


def _has_legacy_runtime_keys(payload: Mapping[str, Any]) -> bool:
    if _LEGACY_TOP_LEVEL_RUNTIME_KEYS & payload.keys():
        return True
    for section_name, removed_keys in _LEGACY_NESTED_RUNTIME_KEYS.items():
        section = payload.get(section_name)
        if isinstance(section, Mapping) and removed_keys & section.keys():
            return True
    learner = payload.get("learner")
    return isinstance(learner, Mapping) and "prediction_reconcile_timeout_seconds" in learner


def _project_legacy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    projected = copy.deepcopy(dict(payload))
    for key in _LEGACY_TOP_LEVEL_RUNTIME_KEYS:
        projected.pop(key, None)
    for section_name, removed_keys in _LEGACY_NESTED_RUNTIME_KEYS.items():
        section = projected.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in removed_keys:
            section.pop(key, None)
        if section_name == "coordination" and not section:
            projected.pop(section_name)

    learner = projected.get("learner")
    if isinstance(learner, dict) and "prediction_reconcile_timeout_seconds" in learner:
        legacy_timeout = learner.pop("prediction_reconcile_timeout_seconds")
        prediction = learner.setdefault("prediction", {})
        if not isinstance(prediction, dict):
            raise ValueError("legacy learner.prediction must be a mapping")
        current_timeout = prediction.get("reconcile_timeout_seconds")
        if current_timeout is not None and current_timeout != legacy_timeout:
            raise ValueError("conflicting legacy learner prediction reconcile timeouts")
        prediction["reconcile_timeout_seconds"] = legacy_timeout
    return projected


def load_query_config_snapshot(path: str | Path) -> Config:
    """Load current config strictly or explicitly project a historical snapshot.

    Unknown keys do not trigger a compatibility downgrade.  Only a snapshot
    containing a known v1-v3 runtime key is eligible for the legacy projection.
    """

    try:
        return load_resolved_config_snapshot(path)
    except ValueError as strict_error:
        legacy = load_legacy_config(path)
        if not _has_legacy_runtime_keys(legacy.payload):
            raise strict_error
        config = _from_dict(Config, _project_legacy_payload(legacy.payload))
        config.validate(
            profile="torch_baseline" if config.torch_baseline.enabled else "full_v4_shared"
        )
        return config
