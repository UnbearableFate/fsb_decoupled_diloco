"""Canonical source-scope identity for runnable and test harness artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any


SOURCE_SCOPES = (
    "fs_diloco",
    "configs",
    "do_experiments",
    "scripts/miyabi",
    "tests",
    "tools",
    "pyproject.toml",
    "README.md",
    "docs",
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


def capture_source_identity(project_root: str | Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
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
    relative_path_set.update(
        scope
        for scope in SOURCE_SCOPES
        if (project_root / scope).is_file() or (project_root / scope).is_symlink()
    )
    source_files = [
        _source_record(project_root, relative_path) for relative_path in sorted(relative_path_set)
    ]
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
    return {
        **fingerprint_payload,
        "git_commit": commit,
        "git_dirty": dirty,
        "source_fingerprint": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    }


def bind_source_identity(config: Any, project_root: str | Path) -> dict[str, Any]:
    """Bind a resolved config to the exact source tree used for initialization."""

    identity = capture_source_identity(project_root)
    config.run.git_commit = identity["git_commit"]
    config.run.git_dirty = identity["git_dirty"]
    config.run.source_fingerprint = identity["source_fingerprint"]
    return identity
