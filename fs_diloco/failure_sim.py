"""Optional learner failure simulation controls."""

from __future__ import annotations

import random
import sys
import time
from typing import Any


def maybe_sleep_jitter(config: Any) -> float:
    if not getattr(config, "enabled", False):
        return 0.0
    max_sleep = float(getattr(config, "sleep_jitter_seconds", 0.0))
    if max_sleep <= 0.0:
        return 0.0
    duration = random.uniform(0.0, max_sleep)
    time.sleep(duration)
    return duration


def should_skip_upload(config: Any) -> bool:
    if not getattr(config, "enabled", False):
        return False
    probability = float(getattr(config, "upload_skip_probability", 0.0))
    return probability > 0.0 and random.random() < probability


def maybe_crash(config: Any) -> None:
    if not getattr(config, "enabled", False):
        return
    probability = float(getattr(config, "crash_probability", 0.0))
    if probability > 0.0 and random.random() < probability:
        sys.exit(97)
