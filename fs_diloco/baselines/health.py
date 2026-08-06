"""Read-only acceptance checks for formal torch baseline runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..storage.atomic_io import safe_read_json
from .artifacts import BaselineRunPaths


FATAL_LOG_PATTERN = re.compile(
    r"Traceback \(most recent call last\)|non-finite (?:loss|gradient|parameter)|worker lost",
    re.IGNORECASE,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (FileNotFoundError, OSError, csv.Error):
        return []


def _fatal_log_evidence(paths: BaselineRunPaths) -> list[str]:
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


def query_pbs_job(job_id: str) -> dict[str, Any]:
    """Return current/recent PBS state without mutating the scheduler."""

    commands = (["qstat", "-f", job_id], ["qstat", "-H", "-f", job_id])
    output = ""
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"state": "unknown", "error": repr(exc)}
        output = completed.stdout + completed.stderr
        if completed.returncode == 0:
            break
    state_match = re.search(r"^\s*job_state\s*=\s*(\S+)", output, re.MULTILINE)
    exit_match = re.search(r"^\s*Exit_status\s*=\s*(-?\d+)", output, re.MULTILINE)
    return {
        "state": state_match.group(1) if state_match else "unknown",
        "exit_status": int(exit_match.group(1)) if exit_match else None,
    }


def evaluate_health(
    run_root: str | Path,
    *,
    mode: str,
    expected_world_size: int,
    target_step: int,
    job_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = BaselineRunPaths(Path(run_root).resolve())
    result: dict[str, Any] = {
        "status": "PENDING",
        "passed": False,
        "run_root": str(paths.root),
        "mode": mode,
        "expected_world_size": expected_world_size,
        "target_step": target_step,
        "checks": {},
        "failures": [],
        "pending": [],
    }
    failures: list[str] = result["failures"]
    pending: list[str] = result["pending"]
    checks: dict[str, Any] = result["checks"]

    manifest = safe_read_json(paths.manifest)
    if manifest is None:
        pending.append("training manifest is not available")
        return result
    checks["manifest"] = True
    if manifest.get("mode") != mode:
        failures.append(
            f"manifest mode is {manifest.get('mode')!r}, expected {mode!r}"
        )
    if int(manifest.get("world_size", -1)) != expected_world_size:
        failures.append(
            f"manifest world size is {manifest.get('world_size')}, expected {expected_world_size}"
        )
    if manifest.get("backend") != "nccl":
        failures.append(f"formal backend is {manifest.get('backend')!r}, expected 'nccl'")

    runtimes = manifest.get("runtimes") or []
    runtime_ranks = sorted(int(item.get("rank", -1)) for item in runtimes)
    runtime_hosts = {str(item.get("hostname")) for item in runtimes}
    checks["runtime_ranks"] = runtime_ranks
    checks["runtime_hosts"] = sorted(runtime_hosts)
    if runtime_ranks != list(range(expected_world_size)):
        failures.append(f"runtime rank set is incomplete: {runtime_ranks}")
    if len(runtime_hosts) != expected_world_size:
        failures.append(
            f"runtime host set has {len(runtime_hosts)} hosts, expected {expected_world_size}"
        )
    if any(item.get("device_type") != "cuda" for item in runtimes):
        failures.append("not every runtime used CUDA")
    if any(item.get("backend") != "nccl" for item in runtimes):
        failures.append("not every runtime used NCCL")

    summary = safe_read_json(paths.summary)
    if summary is not None and summary.get("status") == "failed":
        failures.append(f"training summary reports failure: {summary.get('error')}")
    max_steps = int(manifest.get("max_steps", 0))
    completed_full_run = bool(
        summary is not None
        and summary.get("status") == "completed"
        and int(summary.get("exit_status", -1)) == 0
        and int(summary.get("final_step", -1)) == max_steps
    )
    pbs_job_id = manifest.get("pbs_job_id")
    if completed_full_run:
        checks["job_liveness"] = "completed_full_run"
    elif pbs_job_id:
        observed_job = (
            job_status if job_status is not None else query_pbs_job(str(pbs_job_id))
        )
        checks["pbs_job"] = observed_job
        if observed_job.get("state") not in {"R", "B", "Q"}:
            failures.append(
                f"PBS job {pbs_job_id} is not active: {observed_job.get('state')}"
            )
    else:
        checks["job_liveness"] = "not_applicable_without_pbs_job_id"
    fatal_logs = _fatal_log_evidence(paths)
    checks["fatal_log_evidence"] = fatal_logs
    if fatal_logs:
        failures.append(f"fatal worker log evidence found in {len(fatal_logs)} location(s)")

    losses_by_step: dict[int, list[float]] = {
        step: [] for step in range(1, target_step + 1)
    }
    rank_max_steps: dict[int, int] = {}
    metrics_ready = True
    for rank in range(expected_world_size):
        rows = _read_csv(paths.rank_metrics(rank))
        parsed: dict[int, dict[str, str]] = {}
        for row in rows:
            try:
                step = int(row["step"])
            except (KeyError, TypeError, ValueError):
                continue
            parsed[step] = row
        rank_max_steps[rank] = max(parsed, default=0)
        if rank_max_steps[rank] < target_step:
            metrics_ready = False
            terminal_status = summary.get("status") if summary is not None else None
            message = (
                f"rank {rank} reached step {rank_max_steps[rank]}, "
                f"waiting for {target_step}"
            )
            if terminal_status in {"completed", "failed"}:
                failures.append(message.replace("waiting for", "terminal run expected"))
            else:
                pending.append(message)
            continue
        missing = [step for step in range(1, target_step + 1) if step not in parsed]
        if missing:
            failures.append(
                f"rank {rank} metrics are missing optimizer steps: {missing[:8]}"
            )
            continue
        for step in range(1, target_step + 1):
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

    if metrics_ready and not failures:
        incomplete_loss_steps = [
            step
            for step, values in losses_by_step.items()
            if len(values) != expected_world_size
        ]
        if incomplete_loss_steps:
            failures.append(
                f"loss matrix is incomplete at steps: {incomplete_loss_steps[:8]}"
            )
        else:
            step_means = {
                step: sum(values) / expected_world_size
                for step, values in losses_by_step.items()
            }
            first_window_end = min(50, target_step)
            tail_window_start = max(1, target_step - 49)
            first_mean = sum(
                step_means[step] for step in range(1, first_window_end + 1)
            ) / first_window_end
            tail_count = target_step - tail_window_start + 1
            tail_mean = sum(
                step_means[step] for step in range(tail_window_start, target_step + 1)
            ) / tail_count
            checks["loss_first_window_mean"] = first_mean
            checks["loss_tail_window_mean"] = tail_mean
            if not tail_mean < first_mean:
                failures.append(
                    f"loss did not decline: tail mean {tail_mean} is not below {first_mean}"
                )

    sync_rows = _read_csv(paths.sync_metrics)
    observed_sync_steps: set[int] = set()
    for row in sync_rows:
        try:
            step = int(row["step"])
        except (KeyError, TypeError, ValueError):
            continue
        expected_kind = (
            "gradient_all_reduce" if mode == "ddp" else "parameter_average"
        )
        if row.get("sync_kind") == expected_kind and step <= target_step:
            observed_sync_steps.add(step)
    expected_sync_steps = (
        set(range(1, target_step + 1))
        if mode == "ddp"
        else set(
            range(
                int(manifest.get("average_interval", 0)),
                target_step + 1,
                int(manifest.get("average_interval", 0)) or target_step + 1,
            )
        )
    )
    checks["observed_sync_steps"] = sorted(observed_sync_steps)
    checks["expected_sync_steps"] = sorted(expected_sync_steps)
    if metrics_ready:
        missing_syncs = sorted(expected_sync_steps - observed_sync_steps)
        if missing_syncs:
            failures.append(f"missing required synchronization steps: {missing_syncs[:8]}")

    if failures:
        result["status"] = "FAIL"
    elif pending:
        result["status"] = "PENDING"
    else:
        result["status"] = "PASS"
        result["passed"] = True
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("ddp", "periodic_average"), required=True)
    parser.add_argument("--expected-world-size", type=int, default=8)
    parser.add_argument("--target-step", type=int, default=200)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        result = evaluate_health(
            args.run_root,
            mode=args.mode,
            expected_world_size=args.expected_world_size,
            target_step=args.target_step,
        )
        if result["status"] != "PENDING" or not args.wait:
            break
        if time.monotonic() >= deadline:
            result["pending"].append("health-check wait timeout expired")
            break
        time.sleep(args.poll_seconds)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 2 if result["status"] == "PENDING" else 1)


if __name__ == "__main__":
    main()
