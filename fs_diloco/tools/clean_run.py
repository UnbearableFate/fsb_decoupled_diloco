"""Conservatively prune redundant output from one completed run.

The cleaner intentionally accepts only an exact run directory and a matching
PASS evidence artifact bound to the current terminal version.  It preserves
the authority database, fsync-before-prune histories, checkpoints,
configuration, control publications, syncer logs, and one representative
learner log.  Deletion is opt-in and always leaves an immutable report-side
manifest of the resolved targets.
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


PLAN03_REQUIREMENTS = frozenset({"AUDIT-03", "FS-05"})


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
    artifact_policy_sha256: str | None
    candidates: tuple[CleanupCandidate, ...]
    retained_representative_learner_log: str | None

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
        for name in tuple(directory_names):
            child = current_path / name
            child_metadata = child.lstat()
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise CleanupRefusedError(
                    f"cleanup scan refuses a symlink or non-directory parent: {child}"
                )
        for name in file_names:
            child = current_path / name
            child_metadata = child.lstat()
            if not stat.S_ISREG(child_metadata.st_mode):
                raise CleanupRefusedError(
                    f"cleanup scan refuses a symlink or non-regular entry: {child}"
                )
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
    return run_id, summary, stop


def _matching_pass_evidence(
    evidence_path: Path,
    run_root: Path,
    run_id: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    evidence = _load_json(evidence_path, label="completion evidence")
    if evidence.get("status") != "PASS" or evidence.get("errors") not in (None, []):
        raise CleanupRefusedError("completion evidence is not an error-free PASS")

    identity = evidence.get("identity")
    if not isinstance(identity, dict):
        raise CleanupRefusedError("completion evidence has no source identity")
    evidence_run_root = evidence.get("run_root")
    direct_evidence = isinstance(evidence_run_root, str)
    descriptor_identity_key = "descriptor_sha256"
    if not direct_evidence:
        matched_branches: list[str] = []
        for branch_name in ("static", "dynamic"):
            branch = evidence.get(branch_name)
            if not isinstance(branch, dict) or not isinstance(branch.get("run_root"), str):
                continue
            try:
                branch_root = Path(str(branch["run_root"])).resolve(strict=True)
            except OSError:
                continue
            if branch_root != run_root:
                continue
            branch_summary = branch.get("summary")
            if (
                not isinstance(branch_summary, dict)
                or str(branch_summary.get("run_id") or "") != run_id
                or int(branch_summary.get("final_version", -1))
                != int(summary.get("final_version", -2))
            ):
                raise CleanupRefusedError("matched evidence run summary does not match")
            matched_branches.append(branch_name)
        if len(matched_branches) != 1:
            raise CleanupRefusedError("completion evidence does not identify exactly one run_root")
        branch_name = matched_branches[0]
        evidence_run_root = str(evidence[branch_name]["run_root"])
        descriptor_identity_key = f"{branch_name}_descriptor_sha256"

    try:
        resolved_evidence_run = Path(evidence_run_root).resolve(strict=True)
    except OSError as exc:
        raise CleanupRefusedError("completion evidence run_root no longer exists") from exc
    if resolved_evidence_run != run_root:
        raise CleanupRefusedError("completion evidence belongs to a different run")
    if direct_evidence and str(identity.get("run_id") or "") != run_id:
        raise CleanupRefusedError("completion evidence run identity does not match")
    if direct_evidence:
        authority = evidence.get("authority")
        terminal_candidates: list[dict[str, Any]] = []
        if isinstance(evidence.get("terminal"), dict):
            terminal_candidates.append(evidence["terminal"])
        if isinstance(authority, dict):
            if isinstance(authority.get("terminal"), dict):
                terminal_candidates.append(authority["terminal"])
            terminal_candidates.append(authority)
        terminal_versions: set[int] = set()
        for candidate in terminal_candidates:
            if candidate.get("final_version") is None:
                continue
            try:
                terminal_versions.add(int(candidate["final_version"]))
            except (TypeError, ValueError) as exc:
                raise CleanupRefusedError(
                    "completion evidence terminal final version is invalid"
                ) from exc
        if terminal_versions != {int(summary.get("final_version", -1))}:
            raise CleanupRefusedError(
                "completion evidence terminal final version does not match the run"
            )

    descriptor = _load_json(run_root / "control" / "run_descriptor.json", label="run descriptor")
    if str(descriptor.get("run_id") or "") != run_id:
        raise CleanupRefusedError("run descriptor identity does not match")
    if str(identity.get(descriptor_identity_key) or "") != str(
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


def _authority_live_paths(run_root: Path) -> set[str]:
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
    live: set[str] = set()
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "updates" in tables:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(updates)").fetchall()
            }
            path_column = (
                "payload_relative_path"
                if "payload_relative_path" in columns
                else "file_path"
                if "file_path" in columns
                else None
            )
            if path_column is not None and "status" in columns:
                rows = connection.execute(
                    f"SELECT {path_column} FROM updates WHERE status IN ('pending','selected')"
                ).fetchall()
                for row in rows:
                    raw = Path(str(row[0]))
                    if raw.is_absolute():
                        try:
                            raw = raw.resolve().relative_to(run_root)
                        except ValueError:
                            continue
                    live.add(raw.as_posix())
        if "artifact_publications" in tables:
            rows = connection.execute(
                """
                SELECT relative_path FROM artifact_publications
                WHERE state IN ('prepared','committed','orphan')
                """
            ).fetchall()
            live.update(str(row[0]) for row in rows)
        if "gc_candidates" in tables:
            rows = connection.execute(
                "SELECT relative_path FROM gc_candidates WHERE state IN ('pending','claimed')"
            ).fetchall()
            live.update(str(row[0]) for row in rows)
    except sqlite3.Error as exc:
        raise CleanupRefusedError("cannot inspect authority live references") from exc
    finally:
        connection.close()
    return live


def _validate_policy_candidates(
    *,
    run_root: Path,
    policy: ArtifactPolicy,
    candidates: tuple[CleanupCandidate, ...],
) -> None:
    live = _authority_live_paths(run_root)
    for candidate in candidates:
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
        if candidate.relative_path in live:
            raise CleanupRefusedError(
                f"authority still references cleanup candidate: {candidate.relative_path}"
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

    select(
        _files_below(run / "logs" / "wandb", run_root=run),
        "offline experiment cache",
    )
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
    for relative in (
        "metrics/learner_metrics.csv",
        "metrics/update_manifest.csv",
    ):
        path = run / relative
        if path.is_file() and not path.is_symlink():
            selected[path] = "superseded raw telemetry"
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

    candidates = tuple(
        _candidate(path, run_root=run, reason=selected[path])
        for path in sorted(selected, key=lambda item: item.relative_to(run).as_posix())
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
        refreshed = build_cleanup_plan(plan.project_root, plan.run_root, plan.evidence_path)
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
    parser.add_argument("run_root", type=Path, help="one exact completed run directory")
    parser.add_argument("--evidence", type=Path, required=True, help="matching PASS evidence JSON")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--delete", action="store_true", help="perform the inventoried deletion")
    parser.add_argument(
        "--manifest-output",
        type=Path,
        help="new report-side cleanup manifest (required with --delete)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_cleanup_plan(args.project_root, args.run_root, args.evidence)
        if args.delete:
            if args.manifest_output is None:
                raise CleanupRefusedError("--manifest-output is required with --delete")
            result = execute_cleanup(plan, args.manifest_output)
        else:
            if args.manifest_output is not None:
                raise CleanupRefusedError("--manifest-output is only accepted with --delete")
            result = _manifest(plan, status="dry_run")
    except CleanupRefusedError as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
