#!/usr/bin/env python3
"""Independent plan-01 run invariant checker with a three-value output contract."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected an object at {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected an object row at {path}")
            rows.append(value)
    return rows


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def validate_resume_progress(
    events: list[dict[str, Any]],
    *,
    resume_generation: dict[str, Any] | None,
    expected_learners: int,
    final_version: int,
) -> dict[str, Any]:
    """Validate that the most recent resume opened a new live generation and progressed."""
    resume_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("event_type") == "run_resumed"
    ]
    if not resume_indexes:
        raise RuntimeError("run_resumed event is missing")
    resume_index = resume_indexes[-1]
    resume_event = events[resume_index]
    resume_version = int(resume_event["version"])
    if not isinstance(resume_generation, dict):
        raise RuntimeError("resume_generation run_state is missing")
    if str(resume_generation.get("resume_id")) != str(resume_event.get("resume_id")):
        raise RuntimeError("resume event disagrees with resume_generation")
    heartbeat_fences = resume_generation.get("heartbeat_fences")
    if not isinstance(heartbeat_fences, dict) or len(heartbeat_fences) != expected_learners:
        raise RuntimeError("resume heartbeat fence set is incomplete")
    if int(resume_event.get("heartbeat_fence_count", -1)) != expected_learners:
        raise RuntimeError("run_resumed heartbeat fence count is incomplete")

    forbidden = {"input_exhausted", "stop_published", "error"}
    active_index: int | None = None
    progress_index: int | None = None
    for index in range(resume_index + 1, len(events)):
        event = events[index]
        event_type = str(event.get("event_type"))
        if event_type in forbidden:
            raise RuntimeError(f"{event_type} occurred before post-resume progress")
        if event_type == "learner_liveness_updated" and int(event.get("active", 0)) > 0:
            active_index = index
        if event_type in {"outer_step_applied", "global_published"} and int(
            event.get("version", -1)
        ) > resume_version:
            progress_index = index
            break
    if active_index is None:
        raise RuntimeError("no active current-generation learner observed after resume")
    if progress_index is None:
        raise RuntimeError("no strictly newer commit observed after resume")
    if final_version <= resume_version:
        raise RuntimeError("final version did not advance beyond resume version")

    progress_event = events[progress_index]
    return {
        "resume_event_index": resume_index,
        "resume_id": str(resume_event.get("resume_id")),
        "resume_version": resume_version,
        "heartbeat_fence_count": len(heartbeat_fences),
        "active_liveness_event_index": active_index,
        "progress_event_index": progress_index,
        "progress_event_type": str(progress_event.get("event_type")),
        "progress_version": int(progress_event["version"]),
        "final_version": final_version,
    }


def write_resume_artifact(path: str | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    artifact_path = Path(path).resolve()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def check(args: argparse.Namespace) -> str:
    root = Path(args.run_root).resolve()
    control = root / "control"
    latest = read_json(control / "latest.json")
    fragment_mode = latest.get("latest_kind") == "fragment"
    db_path = control / "syncer_metadata.sqlite3"
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    resume_generation: dict[str, Any] | None = None
    try:
        if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
            raise RuntimeError("integrity check failed")
        pragmas = {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        }
        if str(pragmas["journal_mode"]).lower() != "delete" or int(
            pragmas["synchronous"]
        ) != 2:
            raise RuntimeError("unsafe SQLite pragmas")
        if fragment_mode:
            current_fragments = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM fragment_versions ORDER BY fragment_id"
                )
            ]
            latest_fragments = latest.get("fragments")
            if not isinstance(latest_fragments, dict) or len(current_fragments) != len(
                latest_fragments
            ):
                raise RuntimeError("active fragment set is not current-only")
            version = int(latest["global_merge_event"])
            active_table = "fragment_updates"
        else:
            globals_ = [dict(row) for row in conn.execute("SELECT * FROM global_versions")]
            if len(globals_) != 1:
                raise RuntimeError("active global set is not current-only")
            current = globals_[0]
            version = int(current["version"])
            active_table = "updates"
        if version < args.expected_version:
            raise RuntimeError("expected version not reached")
        if args.require_resume_progress:
            run_state_row = conn.execute(
                "SELECT value FROM run_state WHERE key = 'resume_generation'"
            ).fetchone()
            if run_state_row is not None:
                decoded = json.loads(str(run_state_row[0]))
                if isinstance(decoded, dict):
                    resume_generation = decoded
        active_updates = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {active_table} "
                "WHERE status IN ('pending', 'selected')"
            ).fetchone()[0]
        )
        active_bound = 2 * args.expected_learners * (
            max(1, len(current_fragments)) if fragment_mode else 1
        )
        if active_updates > active_bound:
            raise RuntimeError("active proposal bound exceeded")
        gc_pending_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'gc_pending'"
        ).fetchone()
        if gc_pending_table is not None:
            gc_pending_rows = int(
                conn.execute("SELECT COUNT(*) FROM gc_pending").fetchone()[0]
            )
            if args.require_complete and gc_pending_rows != 0:
                raise RuntimeError("terminal gc_pending rows remain")
    finally:
        conn.close()

    if int(latest["version"]) != version:
        raise RuntimeError("latest disagrees with DB")
    if fragment_mode:
        expected_fragment_weights: set[Path] = set()
        expected_fragment_optim: set[Path] = set()
        for current_fragment in current_fragments:
            fragment_id = str(current_fragment["fragment_id"])
            latest_fragment = latest["fragments"].get(fragment_id)
            if not isinstance(latest_fragment, dict):
                raise RuntimeError("latest fragment entry missing")
            for key, expected_set in (
                ("weight_path", expected_fragment_weights),
                ("optim_path", expected_fragment_optim),
            ):
                current_path = Path(str(current_fragment[key])).resolve()
                if latest_fragment.get(key) != current_fragment[key] or not current_path.is_file():
                    raise RuntimeError("committed fragment checkpoint mismatch")
                expected_set.add(current_path)
            if int(latest_fragment.get("version", -1)) != int(current_fragment["version"]):
                raise RuntimeError("latest fragment version disagrees with DB")
        actual_fragment_weights = {
            path.resolve() for path in (root / "fragments" / "weights").glob("**/*.safetensors")
        }
        actual_fragment_optim = {
            path.resolve() for path in (root / "fragments" / "optim").glob("**/*.safetensors")
        }
        if (
            actual_fragment_weights != expected_fragment_weights
            or actual_fragment_optim != expected_fragment_optim
        ):
            raise RuntimeError("fragment checkpoint retention is not current-only")
        materialized = Path(str(latest["materialized_weight_path"])).resolve()
        if not materialized.is_file() or {
            path.resolve() for path in (root / "weights").glob("*.safetensors")
        } != {materialized}:
            raise RuntimeError("materialized checkpoint retention is not current-only")
    else:
        for key in ("weight_path", "optim_path"):
            if latest[key] != current[key] or not Path(str(current[key])).is_file():
                raise RuntimeError("committed checkpoint mismatch")
        expected_weights = {Path(str(current["weight_path"])).resolve()}
        expected_optim = {Path(str(current["optim_path"])).resolve()}
        actual_weights = {path.resolve() for path in (root / "weights").glob("*.safetensors")}
        actual_optim = {path.resolve() for path in (root / "optim").glob("*.safetensors")}
        if actual_weights != expected_weights or actual_optim != expected_optim:
            raise RuntimeError("checkpoint retention is not current-only")
        pointer_count = len(list((root / "updates" / "latest").glob("learner_*.json")))
        if pointer_count != args.expected_learners:
            raise RuntimeError("fixed proposal surface mismatch")
    if (root / "db_dumps").exists() or list(root.glob("**/*-wal")):
        raise RuntimeError("legacy DB artifact exists")

    archive = read_jsonl(root / "metrics" / "update_history.jsonl")
    update_ids = [str(row["update_id"]) for row in archive]
    if len(update_ids) != len(set(update_ids)):
        raise RuntimeError("duplicate update archive identity")
    version_archive = read_jsonl(root / "metrics" / "global_version_history.jsonl")
    version_ids = [
        (
            str(row.get("version_kind", "full")),
            int(row.get("fragment_id", -1)),
            int(row["version"]),
        )
        for row in version_archive
    ]
    if len(version_ids) != len(set(version_ids)):
        raise RuntimeError("duplicate version archive identity")
    if fragment_mode:
        for current_fragment in current_fragments:
            fragment_id = int(current_fragment["fragment_id"])
            archived_versions = {
                archived_version
                for kind, archived_fragment_id, archived_version in version_ids
                if kind == "fragment" and archived_fragment_id == fragment_id
            }
            if not set(range(int(current_fragment["version"]))).issubset(archived_versions):
                raise RuntimeError("fragment version archive is incomplete")
    else:
        archived_global_versions = {
            archived_version
            for kind, _fragment_id, archived_version in version_ids
            if kind == "full"
        }
        if not set(range(version)).issubset(archived_global_versions):
            raise RuntimeError("global version archive is incomplete")

    events = read_jsonl(root / "logs" / "syncer.jsonl")
    event_types = {str(row.get("event_type")) for row in events}
    if event_types & {"error", "no_progress_timeout", "db_dumped"}:
        raise RuntimeError("failure or dump event exists")

    resume_details: dict[str, Any] | None = None
    if args.require_resume_progress:
        resume_details = validate_resume_progress(
            events,
            resume_generation=resume_generation,
            expected_learners=args.expected_learners,
            final_version=version,
        )

    if args.require_complete:
        stop = read_json(control / "stop.json")
        summary = read_json(control / "summary.json")
        if int(stop["version"]) != version or int(summary["final_version"]) != version:
            raise RuntimeError("terminal versions disagree")
        if version != args.expected_version or len(version_archive) != version:
            raise RuntimeError("completed version/history count mismatch")
        if active_updates:
            raise RuntimeError("terminal active proposal rows remain")
        if list((root / "updates" / "payloads").glob("**/*.safetensors")):
            raise RuntimeError("terminal proposal tensor remains")
        if list((root / "updates" / "payloads").glob("**/*.meta.json")):
            raise RuntimeError("terminal proposal metadata remains")
        if list(root.glob("**/.*.tmp")):
            raise RuntimeError("temporary artifact remains")
        with (root / "metrics" / "syncer_metrics.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            metric_rows = list(csv.DictReader(handle))
        if len(metric_rows) != version:
            raise RuntimeError("metric/global version count mismatch")
        sqlite_seconds = [
            float(row["sqlite_commit_seconds"])
            for row in metric_rows
            if row.get("sqlite_commit_seconds") not in (None, "")
        ]
        maintenance_seconds = [float(row["maintenance_seconds"]) for row in metric_rows]
        if sqlite_seconds and percentile_95(sqlite_seconds) >= 2.0:
            raise RuntimeError("SQLite commit p95 exceeded")
        training_seconds = float(summary["complete_training_time_seconds"])
        if (sum(sqlite_seconds) + sum(maintenance_seconds)) / training_seconds >= 0.05:
            raise RuntimeError("SQLite plus maintenance overhead exceeded")
        result = "PASS"
    else:
        result = "PASS_WITH_FOLLOWUPS"
    if args.require_resume_progress:
        write_resume_artifact(
            args.resume_artifact,
            {
                "result": result,
                "run_root": str(root),
                "latest_version": int(latest["version"]),
                "resume_progress": resume_details,
            },
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--expected-learners", type=int, required=True)
    parser.add_argument("--expected-version", type=int, required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--require-resume-progress", action="store_true")
    parser.add_argument("--resume-artifact")
    args = parser.parse_args()
    try:
        result = check(args)
    except Exception as exc:
        result = "BLOCKED"
        if args.require_resume_progress:
            write_resume_artifact(
                args.resume_artifact,
                {
                    "result": result,
                    "run_root": str(Path(args.run_root).resolve()),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
    print(result)
    raise SystemExit(0 if result != "BLOCKED" else 1)


if __name__ == "__main__":
    main()
