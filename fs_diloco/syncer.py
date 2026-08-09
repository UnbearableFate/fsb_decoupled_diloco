"""Public entry point for the mandatory fenced syncer candidate runtime."""

from __future__ import annotations

from .runtime.syncer_entrypoint import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
