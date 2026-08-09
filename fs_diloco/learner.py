"""Public torch-free entry point for the mandatory fenced learner runtime."""

from __future__ import annotations

from .runtime.learner_entrypoint import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
