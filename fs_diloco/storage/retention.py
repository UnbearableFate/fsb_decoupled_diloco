"""Local checkpoint/update retention helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .atomic_io import safe_read_json
from .paths import RunPaths

_GLOBAL_WEIGHT_RE = re.compile(r"^global_v(\d{6})\.safetensors$")
_OUTER_OPTIM_RE = re.compile(r"^outer_v(\d{6})\.safetensors$")


def _safe_unlink(path: Path, logger: Any | None = None) -> bool:
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        if logger is not None:
            logger.event("retention_delete_failed", path=str(path), error=repr(exc))
        return False
    return True


def _versioned_files(directory: Path, pattern: str, regex: re.Pattern[str]) -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    for path in directory.glob(pattern):
        match = regex.match(path.name)
        if match is None:
            continue
        files.append((int(match.group(1)), path))
    return files


def cleanup_global_artifacts(paths: RunPaths, *, keep_last: int | None, logger: Any | None = None) -> int:
    """Keep only the newest global weight/outer-optimizer versions produced by the syncer."""
    if keep_last is None:
        return 0
    keep_last = max(0, int(keep_last))
    artifacts = [
        *_versioned_files(paths.weights, "global_v*.safetensors", _GLOBAL_WEIGHT_RE),
        *_versioned_files(paths.optim, "outer_v*.safetensors", _OUTER_OPTIM_RE),
    ]
    versions = sorted({version for version, _path in artifacts})
    keep_versions = set(versions[-keep_last:]) if keep_last else set()
    deleted = 0
    for version, path in artifacts:
        if version in keep_versions:
            continue
        if _safe_unlink(path, logger):
            deleted += 1
    if deleted and logger is not None:
        logger.event("retention_cleanup", role="syncer", deleted_files=deleted, keep_last=keep_last)
    return deleted


def _inside_directory(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except ValueError:
        return False
    return True


def cleanup_learner_update_artifacts(
    update_dir: Path,
    *,
    keep_last: int | None,
    logger: Any | None = None,
) -> int:
    """Keep only the newest update metadata/tensor pairs produced by one learner."""
    if keep_last is None:
        return 0
    keep_last = max(0, int(keep_last))
    entries: list[tuple[int, float, str, Path, Path | None]] = []
    for meta_path in update_dir.glob("update_*.meta.json"):
        payload = safe_read_json(meta_path)
        if payload is None:
            continue
        try:
            local_step_end = int(payload.get("local_step_end", -1))
        except (TypeError, ValueError):
            local_step_end = -1
        try:
            committed_at = float(payload.get("committed_at", meta_path.stat().st_mtime))
        except (OSError, TypeError, ValueError):
            committed_at = 0.0
        tensor_path: Path | None = None
        raw_file_path = payload.get("file_path")
        if raw_file_path:
            candidate = Path(raw_file_path)
            if _inside_directory(candidate, update_dir):
                tensor_path = candidate
        entries.append((local_step_end, committed_at, meta_path.name, meta_path, tensor_path))

    entries.sort(key=lambda item: (item[0], item[1], item[2]))
    keep_entries = entries[-keep_last:] if keep_last else []
    keep_meta_paths = {entry[3].resolve(strict=False) for entry in keep_entries}
    keep_tensor_paths = {
        entry[4].resolve(strict=False)
        for entry in keep_entries
        if entry[4] is not None
    }

    deleted = 0
    for _step, _committed_at, _name, meta_path, tensor_path in entries:
        if meta_path.resolve(strict=False) in keep_meta_paths:
            continue
        if tensor_path is not None and _safe_unlink(tensor_path, logger):
            deleted += 1
        if _safe_unlink(meta_path, logger):
            deleted += 1

    for tensor_path in update_dir.glob("update_*.params.safetensors"):
        if tensor_path.resolve(strict=False) in keep_tensor_paths:
            continue
        if _safe_unlink(tensor_path, logger):
            deleted += 1

    if deleted and logger is not None:
        logger.event(
            "retention_cleanup",
            role="learner",
            update_dir=str(update_dir),
            deleted_files=deleted,
            keep_last=keep_last,
        )
    return deleted
