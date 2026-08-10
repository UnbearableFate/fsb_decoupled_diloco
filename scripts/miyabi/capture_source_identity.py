#!/usr/bin/env python3
"""Publish the canonical source identity for a Full Protocol allocation."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from fs_diloco.core.source_identity import capture_source_identity


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-env", type=Path, required=True)
    args = parser.parse_args()

    identity = capture_source_identity(args.project_root)
    identity["captured_at"] = time.time()
    _atomic_write(
        args.output_json,
        json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        args.output_env,
        "\n".join(
            (
                f"FS_DILOCO_GIT_COMMIT={identity['git_commit']}",
                f"FS_DILOCO_GIT_DIRTY={int(identity['git_dirty'])}",
                f"FS_DILOCO_SOURCE_FINGERPRINT={identity['source_fingerprint']}",
                "FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1",
                "export FS_DILOCO_GIT_COMMIT FS_DILOCO_GIT_DIRTY",
                "export FS_DILOCO_SOURCE_FINGERPRINT FS_DILOCO_REQUIRE_SOURCE_IDENTITY",
                "",
            )
        ),
    )
    keys = ("git_commit", "git_dirty", "source_fingerprint")
    print(json.dumps({key: identity[key] for key in keys}, sort_keys=True))


if __name__ == "__main__":
    main()
