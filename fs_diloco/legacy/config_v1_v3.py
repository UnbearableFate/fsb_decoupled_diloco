"""Read-only decoder for historical unversioned v1-v3 resolved config files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


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
