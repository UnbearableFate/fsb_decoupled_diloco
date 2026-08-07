#!/usr/bin/env python3
"""Capture a reproducible manifest for sources used by a formal run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


SOURCE_SCOPES = (
    "fs_diloco",
    "configs",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)


def _git(project_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
    ).stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_record(project_root: Path, relative_path: str) -> dict[str, Any]:
    path = project_root / relative_path
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": relative_path, "kind": "missing"}

    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path)
        return {
            "path": relative_path,
            "kind": "symlink",
            "mode": f"{mode:04o}",
            "target": target,
            "sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        }
    if stat.S_ISREG(metadata.st_mode):
        return {
            "path": relative_path,
            "kind": "file",
            "mode": f"{mode:04o}",
            "size_bytes": metadata.st_size,
            "sha256": _sha256_file(path),
        }
    return {
        "path": relative_path,
        "kind": "unsupported",
        "mode": f"{mode:04o}",
    }


def capture(project_root: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    commit = _git(project_root, "rev-parse", "HEAD").decode("ascii").strip()
    dirty = bool(
        _git(
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *SOURCE_SCOPES,
        ).strip()
    )
    listed = _git(
        project_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *SOURCE_SCOPES,
    )
    relative_path_set = {item.decode("utf-8") for item in listed.split(b"\0") if item}
    # Explicit file scopes such as the environment-specific (and therefore
    # gitignored) uv.lock still define the executable environment. Include
    # them when present even though `git ls-files --exclude-standard` omits
    # ignored files.
    relative_path_set.update(
        scope
        for scope in SOURCE_SCOPES
        if (project_root / scope).is_file() or (project_root / scope).is_symlink()
    )
    relative_paths = sorted(relative_path_set)
    source_files = [_source_record(project_root, path) for path in relative_paths]
    fingerprint_payload = {
        "fingerprint_format": 1,
        "source_scopes": list(SOURCE_SCOPES),
        "source_files": source_files,
    }
    canonical = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return {
        **fingerprint_payload,
        "git_commit": commit,
        "git_dirty": dirty,
        "source_fingerprint": fingerprint,
        "captured_at": time.time(),
    }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
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

    identity = capture(args.project_root)
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
