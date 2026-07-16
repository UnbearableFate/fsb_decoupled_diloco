"""Compatibility entry point for the syncer runtime."""

from __future__ import annotations

from .runtime.syncer import initialize_run, main, resume_run, run_syncer

__all__ = ["initialize_run", "main", "resume_run", "run_syncer"]


if __name__ == "__main__":
    main()
