from __future__ import annotations

import os

from hypothesis import settings


settings.register_profile("plan03-development", max_examples=20, deadline=None)
settings.register_profile("plan03-phase", max_examples=75, deadline=None)
settings.load_profile(os.environ.get("PLAN03_HYPOTHESIS_PROFILE", "plan03-development"))
