"""Compatibility entry point for run inspection."""

from __future__ import annotations

from .tools.analysis import assert_fragment_run, main, summarize_run

__all__ = ["assert_fragment_run", "main", "summarize_run"]


if __name__ == "__main__":
    main()
