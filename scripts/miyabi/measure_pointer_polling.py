#!/usr/bin/env python3
"""Measure the process cost of polling a fixed set of shared-FS pointers."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def _proc_io() -> dict[str, int]:
    path = Path("/proc/self/io")
    if not path.is_file():
        return {}
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip())
    return values


def _prepare_pointers(root: Path, count: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(count):
        path = root / f"learner_{index:03d}.json"
        temporary = root / f".{path.name}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps({"learner_id": f"learner_{index:03d}", "sequence": 1}) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        paths.append(path)
    return paths


def measure(*, root: Path, pointer_count: int, interval_seconds: float, duration: float) -> dict[str, Any]:
    if pointer_count <= 0:
        raise ValueError("pointer_count must be positive")
    if interval_seconds <= 0.0 or duration <= 0.0:
        raise ValueError("interval_seconds and duration must be positive")
    pointers = _prepare_pointers(root, pointer_count)
    io_before = _proc_io()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    stat_seconds = 0.0
    iterations = 0
    deadline = wall_start + duration
    next_poll = wall_start
    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        stat_start = now
        for pointer in pointers:
            pointer.stat()
        stat_seconds += time.perf_counter() - stat_start
        iterations += 1
        next_poll += interval_seconds
        time.sleep(max(0.0, min(next_poll, deadline) - time.perf_counter()))
    cpu_seconds = time.process_time() - cpu_start
    wall_seconds = time.perf_counter() - wall_start
    io_after = _proc_io()
    stat_calls = iterations * len(pointers)
    return {
        "root": str(root.resolve()),
        "pointer_count": len(pointers),
        "interval_seconds": interval_seconds,
        "requested_duration_seconds": duration,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "process_cpu_percent": 100.0 * cpu_seconds / wall_seconds,
        "poll_iterations": iterations,
        "stat_calls": stat_calls,
        "stat_seconds": stat_seconds,
        "mean_stat_microseconds": (
            1e6 * stat_seconds / stat_calls if stat_calls else 0.0
        ),
        "stat_wall_percent": 100.0 * stat_seconds / wall_seconds,
        "proc_io_delta": {
            key: io_after.get(key, 0) - io_before.get(key, 0)
            for key in sorted(set(io_before) | set(io_after))
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pointer-count", type=int, default=8)
    parser.add_argument("--interval-seconds", type=float, default=0.2)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    args = parser.parse_args()
    result = measure(
        root=args.root,
        pointer_count=args.pointer_count,
        interval_seconds=args.interval_seconds,
        duration=args.duration_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
