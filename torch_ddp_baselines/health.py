"""Read-only completion checks for standalone distributed baseline runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from .artifacts import BaselineRunPaths, safe_read_json


FATAL_LOG_PATTERN = re.compile(
    r"Traceback \(most recent call last\)|non-finite (?:loss|gradient|parameter)",
    re.IGNORECASE,
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
SOURCE_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a complete CSV or return no rows for absent or malformed content."""

    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (FileNotFoundError, OSError, csv.Error):
        return []


def _fatal_log_evidence(paths: BaselineRunPaths) -> list[str]:
    """Locate structured or textual fatal evidence in rank logs."""

    evidence: list[str] = []
    for log_path in sorted(paths.logs_dir.glob("rank_*.jsonl")):
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            fatal = bool(FATAL_LOG_PATTERN.search(line))
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("event_type") == "baseline_failed":
                fatal = True
            if fatal:
                evidence.append(f"{log_path}:{line_number}")
    return evidence


def evaluate_health(run_root: str | Path, *, mode: str) -> dict[str, Any]:
    """Evaluate terminal artifacts against the run's own immutable manifest."""

    paths = BaselineRunPaths(Path(run_root).resolve())
    result: dict[str, Any] = {
        "status": "FAIL",
        "passed": False,
        "run_root": str(paths.root),
        "mode": mode,
        "checks": {},
        "failures": [],
    }
    checks: dict[str, Any] = result["checks"]
    failures: list[str] = result["failures"]
    manifest = safe_read_json(paths.manifest)
    if manifest is None:
        failures.append("training manifest is missing or invalid")
        return result
    checks["manifest"] = True
    if manifest.get("mode") != mode:
        failures.append(f"manifest mode is {manifest.get('mode')!r}, expected {mode!r}")
    expected_world_size = int(manifest.get("expected_world_size", 0))
    max_steps = int(manifest.get("max_steps", 0))
    interval = int(manifest.get("periodic_average_interval", 0))
    if int(manifest.get("world_size", -1)) != expected_world_size:
        failures.append("manifest world size does not match its declared topology")
    runtimes = manifest.get("runtimes") or []
    runtime_ranks = sorted(int(item.get("rank", -1)) for item in runtimes)
    runtime_hosts = {str(item.get("hostname")) for item in runtimes}
    checks["runtime_ranks"] = runtime_ranks
    checks["runtime_hosts"] = sorted(runtime_hosts)
    if runtime_ranks != list(range(expected_world_size)):
        failures.append(f"runtime rank set is incomplete: {runtime_ranks}")
    if manifest.get("require_distinct_hosts") and len(runtime_hosts) != expected_world_size:
        failures.append(
            f"runtime host set has {len(runtime_hosts)} hosts, expected {expected_world_size}"
        )
    if manifest.get("backend") == "nccl" and any(
        item.get("device_type") != "cuda" for item in runtimes
    ):
        failures.append("not every NCCL runtime used CUDA")
    source_identity = manifest.get("source_identity")
    if not isinstance(source_identity, dict):
        failures.append("manifest source identity is missing")
    else:
        checks["source_identity"] = source_identity
        if (
            COMMIT_SHA.fullmatch(str(source_identity.get("git_commit", ""))) is None
            or source_identity.get("git_dirty") is not False
            or SOURCE_FINGERPRINT.fullmatch(str(source_identity.get("source_fingerprint", "")))
            is None
            or not isinstance(source_identity.get("source_scopes"), list)
            or not source_identity["source_scopes"]
        ):
            failures.append("manifest source identity is incomplete or dirty")
        if safe_read_json(paths.source_identity) != source_identity:
            failures.append("source identity artifact differs from the manifest")

    summary = safe_read_json(paths.summary)
    if summary is None:
        failures.append("terminal summary is missing or invalid")
    elif not (
        summary.get("status") == "completed"
        and int(summary.get("exit_status", -1)) == 0
        and int(summary.get("final_step", -1)) == max_steps
    ):
        failures.append(f"training summary is not a successful {max_steps}-step run")
    checkpoint_files = sorted(
        path.name for path in paths.final_checkpoint.glob("*") if path.is_file()
    )
    checks["checkpoint_files"] = checkpoint_files
    if not checkpoint_files or not any(name.endswith(".safetensors") for name in checkpoint_files):
        failures.append("final safetensors checkpoint is missing")

    fatal_logs = _fatal_log_evidence(paths)
    checks["fatal_log_evidence"] = fatal_logs
    if fatal_logs:
        failures.append(f"fatal worker log evidence found in {len(fatal_logs)} locations")

    losses_by_step: dict[int, list[float]] = {step: [] for step in range(1, max_steps + 1)}
    rank_max_steps: dict[int, int] = {}
    for rank in range(expected_world_size):
        parsed: dict[int, dict[str, str]] = {}
        for row in _read_csv(paths.rank_metrics(rank)):
            try:
                parsed[int(row["step"])] = row
            except (KeyError, TypeError, ValueError):
                continue
        rank_max_steps[rank] = max(parsed, default=0)
        missing_steps = [step for step in range(1, max_steps + 1) if step not in parsed]
        if missing_steps:
            failures.append(f"rank {rank} metrics are missing steps: {missing_steps[:8]}")
            continue
        for step in range(1, max_steps + 1):
            try:
                loss = float(parsed[step]["loss"])
            except (KeyError, TypeError, ValueError):
                failures.append(f"rank {rank} step {step} has an invalid loss")
                continue
            if not math.isfinite(loss):
                failures.append(f"rank {rank} step {step} loss is non-finite")
            else:
                losses_by_step[step].append(loss)
    checks["rank_max_steps"] = rank_max_steps
    if not failures:
        incomplete = [
            step for step, losses in losses_by_step.items() if len(losses) != expected_world_size
        ]
        if incomplete:
            failures.append(f"loss matrix is incomplete at steps: {incomplete[:8]}")
        else:
            means = {
                step: sum(losses) / expected_world_size for step, losses in losses_by_step.items()
            }
            window = min(50, max_steps)
            first_mean = sum(means[step] for step in range(1, window + 1)) / window
            tail_mean = (
                sum(means[step] for step in range(max_steps - window + 1, max_steps + 1)) / window
            )
            checks["loss_first_window_mean"] = first_mean
            checks["loss_tail_window_mean"] = tail_mean
            if tail_mean >= first_mean:
                failures.append(
                    f"loss did not decline: tail mean {tail_mean} is not below {first_mean}"
                )

    observed_sync_steps = {
        int(row["step"])
        for row in _read_csv(paths.sync_metrics)
        if row.get("sync_kind") == ("gradient_all_reduce" if mode == "ddp" else "parameter_average")
        and row.get("step", "").isdigit()
    }
    expected_sync_steps = (
        set(range(1, max_steps + 1))
        if mode == "ddp"
        else set(range(interval, max_steps + 1, interval))
    )
    checks["observed_sync_steps"] = sorted(observed_sync_steps)
    missing_syncs = sorted(expected_sync_steps - observed_sync_steps)
    if missing_syncs:
        failures.append(f"missing required synchronization steps: {missing_syncs[:8]}")

    if not failures:
        result["status"] = "PASS"
        result["passed"] = True
    return result


def build_parser() -> argparse.ArgumentParser:
    """Construct the terminal health-check command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("ddp", "periodic_average"), required=True)
    return parser


def main() -> None:
    """Print machine-readable health evidence and return a gate-compatible status."""

    args = build_parser().parse_args()
    result = evaluate_health(args.run_root, mode=args.mode)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
