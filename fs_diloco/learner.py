"""Compatibility entry point for the learner runtime."""

from __future__ import annotations

from .runtime.learner import main, run_learner

__all__ = ["main", "run_learner"]


if __name__ == "__main__":
    main()
