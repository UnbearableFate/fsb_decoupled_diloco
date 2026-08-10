from __future__ import annotations

import os

from hypothesis import settings


settings.register_profile("development", max_examples=20, deadline=None)
settings.register_profile("thorough", max_examples=75, deadline=None)
settings.load_profile(os.environ.get("FS_DILOCO_HYPOTHESIS_PROFILE", "development"))
