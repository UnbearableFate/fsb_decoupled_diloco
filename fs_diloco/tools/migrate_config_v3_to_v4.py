"""Safely migrate one full v1-v3 configuration to strict Full Protocol v4."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from ..core.config_v4 import migrate_v3_bytes_to_v4


def _write_create_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_replace(path: Path, data: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(
    input_path: str | Path,
    *,
    output_path: str | Path | None = None,
    in_place: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    source = Path(input_path)
    if in_place and output_path is not None:
        raise ValueError("--in-place and --output are mutually exclusive")
    if in_place != (expected_sha256 is not None):
        raise ValueError("--in-place and --expected-sha256 must be provided together")
    original = source.read_bytes()
    migrated, report = migrate_v3_bytes_to_v4(original)
    if expected_sha256 is not None and report["input_sha256"] != expected_sha256:
        raise RuntimeError("input changed: --expected-sha256 does not match")
    destination: Path | None = None
    if in_place:
        destination = source
        _atomic_replace(destination, migrated)
    elif output_path is not None:
        destination = Path(output_path)
        _write_create_new(destination, migrated)
    return {
        "input": str(source),
        "output": None if destination is None else str(destination),
        "write_mode": "in_place" if in_place else ("output" if destination else "dry_run"),
        **report,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output")
    destination.add_argument("--in-place", action="store_true")
    parser.add_argument("--expected-sha256")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    report = migrate(
        args.config,
        output_path=args.output,
        in_place=args.in_place,
        expected_sha256=args.expected_sha256,
    )
    json.dump(report, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
