"""Conservatively prune redundant output from one completed run.

The cleaner intentionally accepts only an exact run directory and a matching
PASS evidence artifact bound to the current terminal version.  It preserves
the authority database, fsync-before-prune histories, checkpoints,
configuration, control publications, syncer logs, and one representative
learner log.  Objects already owned by fenced authority GC are retained and
listed separately instead of blocking unrelated cleanup.  Deletion is opt-in
and always leaves an immutable report-side manifest of the resolved targets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..storage.artifact_policy import ArtifactClass, ArtifactPolicy


class CleanupRefusedError(RuntimeError):
    """Raised when a target cannot be proven to be a completed owned run."""


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    relative_path: str
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CleanupPlan:
    project_root: Path
    run_root: Path
    evidence_path: Path
    run_id: str
    evidence_sha256: str
    artifact_policy_sha256: str
    candidates: tuple[CleanupCandidate, ...]
    retained_representative_learner_log: str | None
    retained_authority_owned_gc_paths: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupRefusedError(f"cannot read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise CleanupRefusedError(f"{label} must contain a JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_existing_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise CleanupRefusedError(f"{label} must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CleanupRefusedError(f"{label} does not exist: {path}") from exc
    if not resolved.is_dir():
        raise CleanupRefusedError(f"{label} is not a directory: {resolved}")
    return resolved


def _resolve_existing_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise CleanupRefusedError(f"{label} must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CleanupRefusedError(f"{label} does not exist: {path}") from exc
    if not resolved.is_file():
        raise CleanupRefusedError(f"{label} is not a file: {resolved}")
    return resolved


def _files_below(directory: Path, *, run_root: Path) -> tuple[Path, ...]:
    try:
        relative_root = directory.relative_to(run_root)
        metadata = directory.lstat()
    except FileNotFoundError:
        return ()
    except ValueError as exc:
        raise CleanupRefusedError(f"cleanup scan escaped the run root: {directory}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CleanupRefusedError(f"cleanup scan root is not an owned directory: {directory}")
    _validate_owned_parents(run_root, relative_root)
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        owned_directories: list[str] = []
        for name in tuple(directory_names):
            child = current_path / name
            try:
                child_metadata = child.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(child_metadata.st_mode):
                owned_directories.append(name)
        directory_names[:] = owned_directories
        for name in file_names:
            child = current_path / name
            try:
                child_metadata = child.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(child_metadata.st_mode):
                continue
            files.append(child)
    return tuple(files)


def _validate_owned_parents(run_root: Path, relative: Path) -> None:
    current = run_root
    for component in relative.parts[:-1] if relative.parts else ():
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise CleanupRefusedError(f"cleanup parent disappeared: {current}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise CleanupRefusedError(f"cleanup parent must be a non-symlink directory: {current}")


def _terminal_identity(run_root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    summary = _load_json(run_root / "control" / "summary.json", label="terminal summary")
    stop = _load_json(run_root / "control" / "stop.json", label="terminal stop")
    run_id = str(summary.get("run_id") or "")
    if not run_id or run_id != str(stop.get("run_id") or "") or run_id != run_root.name:
        raise CleanupRefusedError("run directory, terminal summary, and stop run IDs do not match")
    if summary.get("all_learners_stopped") is not True:
        raise CleanupRefusedError("terminal summary does not confirm that all learners stopped")
    if int(summary.get("final_version", -1)) != int(stop.get("final_version", -2)):
        raise CleanupRefusedError("terminal summary and stop final versions do not match")
    database = run_root / "control" / "syncer_metadata.sqlite3"
    try:
        metadata = database.lstat()
    except FileNotFoundError as exc:
        raise CleanupRefusedError("authority database is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CleanupRefusedError("authority database must be a regular non-symlink file")
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        terminal = connection.execute("SELECT * FROM terminal_state WHERE singleton=1").fetchone()
        controller = connection.execute(
            "SELECT state FROM controller_state WHERE singleton=1"
        ).fetchone()
        identity = connection.execute(
            "SELECT run_id FROM run_identity WHERE singleton=1"
        ).fetchone()
        latest = connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
    except sqlite3.Error as exc:
        raise CleanupRefusedError("cannot validate terminal authority") from exc
    finally:
        connection.close()
    if integrity != ["ok"]:
        raise CleanupRefusedError("authority integrity check failed")
    if (
        terminal is None
        or terminal["state"] != "finalized"
        or controller is None
        or controller["state"] != "finalized"
        or identity is None
        or identity["run_id"] != run_id
        or latest is None
        or int(latest) != int(summary["final_version"])
        or int(terminal["final_version"]) != int(summary["final_version"])
    ):
        raise CleanupRefusedError("terminal filesystem controls do not match durable authority")
    owner_short = hashlib.sha256(
        str(terminal["finalized_by_owner_id"]).encode("utf-8")
    ).hexdigest()[:12]
    immutable_stop = (
        run_root
        / "control"
        / "syncer_epochs"
        / f"e{int(terminal['finalized_by_epoch']):06d}_{owner_short}"
        / "terminal"
        / f"stop_g{int(terminal['generation']):06d}.json"
    )
    try:
        immutable_metadata = immutable_stop.lstat()
    except FileNotFoundError as exc:
        raise CleanupRefusedError("immutable terminal control is missing") from exc
    if not stat.S_ISREG(immutable_metadata.st_mode) or immutable_metadata.st_mode & 0o222:
        raise CleanupRefusedError("immutable terminal control is not immutable")
    if _load_json(immutable_stop, label="immutable terminal control") != stop:
        raise CleanupRefusedError("fixed terminal stop differs from immutable authority output")
    return run_id, summary, stop


def _matching_pass_evidence(
    evidence_path: Path,
    run_root: Path,
    run_id: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    evidence = _load_json(evidence_path, label="completion evidence")
    if (
        evidence.get("artifact_version") != 1
        or evidence.get("status") != "PASS"
        or evidence.get("errors") != []
    ):
        raise CleanupRefusedError("completion evidence is not an error-free PASS")

    identity = evidence.get("identity")
    if not isinstance(identity, dict):
        raise CleanupRefusedError("completion evidence has no source identity")
    evidence_run_root = evidence.get("run_root")
    if not isinstance(evidence_run_root, str):
        raise CleanupRefusedError("completion evidence has no run_root")
    try:
        resolved_evidence_run = Path(evidence_run_root).resolve(strict=True)
    except OSError as exc:
        raise CleanupRefusedError("completion evidence run_root no longer exists") from exc
    if resolved_evidence_run != run_root:
        raise CleanupRefusedError("completion evidence belongs to a different run")
    if str(identity.get("run_id") or "") != run_id:
        raise CleanupRefusedError("completion evidence run identity does not match")
    authority = evidence.get("authority")
    if not isinstance(authority, dict):
        raise CleanupRefusedError("completion evidence has no authority result")
    try:
        evidence_final_version = int(authority["final_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CleanupRefusedError("completion evidence final version is invalid") from exc
    if evidence_final_version != int(summary.get("final_version", -1)):
        raise CleanupRefusedError("completion evidence final version does not match the run")
    if evidence.get("terminal_summary") != summary:
        raise CleanupRefusedError("completion evidence terminal summary no longer matches")
    cleanup = evidence.get("cleanup")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("eligible") is not True
        or cleanup.get("targets") != [str(run_root)]
    ):
        raise CleanupRefusedError("completion evidence does not authorize this cleanup target")

    descriptor = _load_json(run_root / "control" / "run_descriptor.json", label="run descriptor")
    if str(descriptor.get("run_id") or "") != run_id:
        raise CleanupRefusedError("run descriptor identity does not match")
    if str(identity.get("descriptor_sha256") or "") != str(
        descriptor.get("descriptor_sha256") or ""
    ):
        raise CleanupRefusedError("completion evidence descriptor identity does not match")
    if str(identity.get("source_fingerprint") or "") != str(
        descriptor.get("source_fingerprint") or ""
    ):
        raise CleanupRefusedError("completion evidence source identity does not match")
    return evidence


def _candidate(
    path: Path,
    *,
    run_root: Path,
    reason: str,
) -> CleanupCandidate:
    try:
        relative = path.relative_to(run_root).as_posix()
        _validate_owned_parents(run_root, Path(relative))
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise CleanupRefusedError(f"cleanup candidate escaped or disappeared: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CleanupRefusedError(f"cleanup candidate is not an owned regular file: {path}")
    return CleanupCandidate(
        path=path,
        relative_path=relative,
        size_bytes=int(metadata.st_size),
        mtime_ns=int(metadata.st_mtime_ns),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        reason=reason,
    )


def _load_artifact_policy(run_root: Path) -> ArtifactPolicy:
    path = run_root / "control" / "artifact_policy.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise CleanupRefusedError("artifact policy is required to prove generic cleanup safety")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o222:
        raise CleanupRefusedError("artifact policy must be an immutable regular file")
    try:
        return ArtifactPolicy.from_dict(_load_json(path, label="artifact policy"))
    except ValueError as exc:
        raise CleanupRefusedError(f"artifact policy is invalid: {exc}") from exc


def _authority_live_paths(run_root: Path) -> tuple[set[str], set[str]]:
    database = run_root / "control" / "syncer_metadata.sqlite3"
    try:
        metadata = database.lstat()
    except FileNotFoundError as exc:
        raise CleanupRefusedError("authority database is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CleanupRefusedError("authority database must be a regular non-symlink file")
    uri = f"file:{database.resolve()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise CleanupRefusedError("cannot open authority database read-only") from exc
    blocking: set[str] = set()
    authority_owned_gc: set[str] = set()
    try:
        rows = connection.execute(
            "SELECT payload_relative_path FROM updates WHERE status IN ('pending','selected')"
        ).fetchall()
        blocking.update(str(row[0]) for row in rows)
        rows = connection.execute(
            """
            SELECT relative_path FROM artifact_publications
            WHERE state IN ('prepared','committed','orphan')
            """
        ).fetchall()
        blocking.update(str(row[0]) for row in rows)
        rows = connection.execute(
            "SELECT relative_path FROM gc_candidates WHERE state IN ('pending','claimed')"
        ).fetchall()
        authority_owned_gc.update(str(row[0]) for row in rows)
    except sqlite3.Error as exc:
        raise CleanupRefusedError("cannot inspect authority live references") from exc
    finally:
        connection.close()
    return blocking, authority_owned_gc


def _validate_policy_candidates(
    *,
    run_root: Path,
    policy: ArtifactPolicy,
    candidates: tuple[CleanupCandidate, ...],
) -> None:
    blocking, authority_owned_gc = _authority_live_paths(run_root)
    for candidate in candidates:
        if candidate.relative_path in blocking or candidate.relative_path in authority_owned_gc:
            raise CleanupRefusedError(
                f"authority still references cleanup candidate: {candidate.relative_path}"
            )
        try:
            artifact_class = policy.classify(candidate.relative_path)
            allowed = policy.allows_generic_cleanup(candidate.relative_path)
        except ValueError as exc:
            raise CleanupRefusedError(
                f"cleanup candidate has ambiguous/invalid policy classification: "
                f"{candidate.relative_path}"
            ) from exc
        if artifact_class in {ArtifactClass.UNKNOWN, ArtifactClass.AUTHORITY, ArtifactClass.AUDIT}:
            raise CleanupRefusedError(
                f"generic cleanup cannot delete {artifact_class.value}: {candidate.relative_path}"
            )
        if not allowed:
            raise CleanupRefusedError(
                f"artifact policy forbids generic cleanup: {candidate.relative_path}"
            )


def build_cleanup_plan(
    project_root: str | Path,
    run_root: str | Path,
    evidence_path: str | Path,
) -> CleanupPlan:
    """Resolve and inventory safe cleanup candidates without deleting anything."""

    project = _resolve_existing_directory(Path(project_root), label="project root")
    runs_root = _resolve_existing_directory(project / "runs", label="project runs root")
    reports_root = _resolve_existing_directory(project / "reports", label="project reports root")
    run = _resolve_existing_directory(Path(run_root), label="run root")
    evidence = _resolve_existing_file(Path(evidence_path), label="completion evidence")
    if run == runs_root or not run.is_relative_to(runs_root):
        raise CleanupRefusedError("run root must be one exact run below the project runs directory")
    if not evidence.is_relative_to(reports_root):
        raise CleanupRefusedError(
            "completion evidence must be inside the project reports directory"
        )
    for suffix in ("-journal", "-shm", "-wal"):
        active_sidecar = run / "control" / f"syncer_metadata.sqlite3{suffix}"
        try:
            active_sidecar.lstat()
        except FileNotFoundError:
            pass
        else:
            raise CleanupRefusedError(f"authority database may still be active: {active_sidecar}")

    run_id, summary, _stop = _terminal_identity(run)
    _matching_pass_evidence(evidence, run, run_id, summary)
    artifact_policy = _load_artifact_policy(run)

    selected: dict[Path, str] = {}

    def select(paths: Iterable[Path], reason: str) -> None:
        for path in paths:
            selected[path] = reason

    learner_logs = sorted(
        (
            path
            for path in (run / "logs").glob("learner*.jsonl")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.name,
    )
    representative = learner_logs[0] if learner_logs else None
    select(learner_logs[1:], "repeated successful learner log")
    select(_files_below(run / "heartbeats", run_root=run), "terminal heartbeat cache")
    select(
        _files_below(run / "updates" / "latest", run_root=run),
        "terminal proposal pointer",
    )
    select(
        _files_below(run / "updates" / "payloads", run_root=run),
        "terminal update payload",
    )
    for path in _files_below(run, run_root=run):
        if path.name.endswith((".tmp", ".part", ".staging")) or path.name.startswith(".tmp-"):
            selected[path] = "temporary or staging file"

    blocking, authority_owned_gc = _authority_live_paths(run)
    selected_relative_paths = {path.relative_to(run).as_posix(): path for path in selected}
    blocking_selected = sorted(set(selected_relative_paths) & blocking)
    if blocking_selected:
        raise CleanupRefusedError(
            f"authority still references cleanup candidate: {blocking_selected[0]}"
        )
    retained_authority_owned_gc_paths = tuple(
        sorted(set(selected_relative_paths) & authority_owned_gc)
    )
    candidates = tuple(
        _candidate(path, run_root=run, reason=selected[path])
        for path in sorted(selected, key=lambda item: item.relative_to(run).as_posix())
        if path.relative_to(run).as_posix() not in authority_owned_gc
    )
    _validate_policy_candidates(
        run_root=run,
        policy=artifact_policy,
        candidates=candidates,
    )
    return CleanupPlan(
        project_root=project,
        run_root=run,
        evidence_path=evidence,
        run_id=run_id,
        evidence_sha256=_sha256(evidence),
        artifact_policy_sha256=artifact_policy.policy_sha256,
        candidates=candidates,
        retained_representative_learner_log=(
            None if representative is None else representative.relative_to(run).as_posix()
        ),
        retained_authority_owned_gc_paths=retained_authority_owned_gc_paths,
    )


def _manifest(plan: CleanupPlan, *, status: str) -> dict[str, Any]:
    return {
        "tool": "fs_diloco.tools.clean_run",
        "format_version": 1,
        "status": status,
        "run_id": plan.run_id,
        "run_root": str(plan.run_root),
        "completion_evidence": str(plan.evidence_path),
        "completion_evidence_sha256": plan.evidence_sha256,
        "artifact_policy_sha256": plan.artifact_policy_sha256,
        "candidate_count": len(plan.candidates),
        "candidate_bytes": plan.total_bytes,
        "retained_representative_learner_log": plan.retained_representative_learner_log,
        "retained_authority_owned_gc_count": len(plan.retained_authority_owned_gc_paths),
        "retained_authority_owned_gc_paths": list(plan.retained_authority_owned_gc_paths),
        "candidates": [candidate.as_dict() for candidate in plan.candidates],
    }


def _write_json_atomic(path: Path, payload: dict[str, Any], *, require_new: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if require_new:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise CleanupRefusedError(f"cleanup manifest already exists: {path}") from exc
            temporary.unlink()
        else:
            if path.is_symlink() or not path.is_file():
                raise CleanupRefusedError("cleanup manifest ownership changed")
            os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def execute_cleanup(plan: CleanupPlan, manifest_path: str | Path) -> dict[str, Any]:
    """Delete an already inventoried plan after persisting its exact manifest."""

    manifest = Path(manifest_path)
    reports_root = (plan.project_root / "reports").resolve(strict=True)
    manifest_parent = manifest.parent.resolve(strict=True)
    if not manifest_parent.is_relative_to(reports_root) or manifest.is_symlink():
        raise CleanupRefusedError("cleanup manifest must be a new file inside project reports")
    planned = _manifest(plan, status="planned")
    _write_json_atomic(manifest, planned, require_new=True)
    deleted_count = 0
    deleted_bytes = 0
    try:
        refreshed = build_cleanup_plan(
            plan.project_root,
            plan.run_root,
            plan.evidence_path,
        )
        if refreshed != plan:
            raise CleanupRefusedError(
                "run, completion evidence, or cleanup candidate changed after inventory"
            )
        for candidate in plan.candidates:
            _unlink_owned_candidate(plan.run_root, candidate)
            deleted_count += 1
            deleted_bytes += candidate.size_bytes
    except Exception as exc:
        failed = {
            **planned,
            "status": "failed",
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
            "error": str(exc),
        }
        _write_json_atomic(manifest, failed, require_new=False)
        raise
    completed = {
        **planned,
        "status": "complete",
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
    }
    _write_json_atomic(manifest, completed, require_new=False)
    return completed


def _unlink_owned_candidate(run_root: Path, candidate: CleanupCandidate) -> None:
    relative = Path(candidate.relative_path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(run_root, flags)
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or int(metadata.st_size) != candidate.size_bytes
            or int(metadata.st_mtime_ns) != candidate.mtime_ns
            or int(metadata.st_dev) != candidate.device
            or int(metadata.st_ino) != candidate.inode
        ):
            raise CleanupRefusedError(
                f"cleanup candidate changed after inventory: {candidate.relative_path}"
            )
        os.unlink(relative.name, dir_fd=descriptor)
        os.fsync(descriptor)
    except OSError as exc:
        raise CleanupRefusedError(
            f"cleanup candidate parent or object changed: {candidate.relative_path}"
        ) from exc
    finally:
        os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="matching PASS evidence JSON")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--execute", action="store_true", help="perform the inventoried deletion")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="new report-side cleanup manifest (required with --execute)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_cleanup_plan(
            args.project_root,
            args.run_root,
            args.evidence,
        )
        if args.execute:
            if args.manifest is None:
                raise CleanupRefusedError("--manifest is required with --execute")
            result = execute_cleanup(plan, args.manifest)
        else:
            if args.manifest is not None:
                raise CleanupRefusedError("--manifest is only accepted with --execute")
            result = _manifest(plan, status="dry_run")
    except CleanupRefusedError as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
