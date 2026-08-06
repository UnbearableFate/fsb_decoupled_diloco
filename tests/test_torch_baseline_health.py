import csv
import json
from pathlib import Path

from fs_diloco.baselines.artifacts import RANK_METRIC_FIELDS, SYNC_METRIC_FIELDS
from fs_diloco.baselines.health import evaluate_health


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _build_run(
    root: Path,
    *,
    mode: str,
    metric_steps: int = 200,
    missing_rank: int | None = None,
    nonfinite: bool = False,
    declining: bool = True,
    missing_periodic_step: int | None = None,
    completed: bool = False,
):
    root.mkdir()
    manifest = {
        "format_version": 1,
        "mode": mode,
        "backend": "nccl",
        "world_size": 8,
        "expected_world_size": 8,
        "max_steps": 5000,
        "average_interval": 100,
        "pbs_job_id": "123.miyabi",
        "runtimes": [
            {
                "rank": rank,
                "hostname": f"mg{rank}",
                "backend": "nccl",
                "device_type": "cuda",
            }
            for rank in range(8)
        ],
    }
    (root / "training_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for rank in range(8):
        if rank == missing_rank:
            continue
        rows = []
        for step in range(1, metric_steps + 1):
            loss = 4.0 - 0.005 * step + rank * 0.001 if declining else 4.0
            if nonfinite and rank == 3 and step == 75:
                loss = float("nan")
            rows.append(
                {
                    "timestamp": step,
                    "mode": mode,
                    "rank": rank,
                    "hostname": f"mg{rank}",
                    "step": step,
                    "loss": loss,
                    "learning_rate": 1e-4,
                    "tokens": 1,
                    "cumulative_tokens": step,
                    "tokens_per_second": 1,
                    "global_tokens_per_second": 8 if rank == 0 else "",
                    "step_time_seconds": 1,
                    "grad_norm": 1,
                    "gradient_sync_count": step if mode == "ddp" else 0,
                    "parameter_average_count": step // 100 if mode != "ddp" else 0,
                    "last_model_average_step": step // 100 * 100 if mode != "ddp" else 0,
                }
            )
        _write_csv(root / "metrics" / f"rank_{rank:03d}.csv", RANK_METRIC_FIELDS, rows)
    sync_steps = range(1, metric_steps + 1) if mode == "ddp" else range(100, metric_steps + 1, 100)
    sync_rows = [
        {
            "timestamp": step,
            "mode": mode,
            "step": step,
            "sync_kind": "gradient_all_reduce" if mode == "ddp" else "parameter_average",
            "duration_seconds": 0.1,
            "flattened_numel": 1,
            "world_size": 8,
            "cumulative_sync_count": step if mode == "ddp" else step // 100,
        }
        for step in sync_steps
        if step != missing_periodic_step
    ]
    _write_csv(root / "metrics" / "synchronization.csv", SYNC_METRIC_FIELDS, sync_rows)
    if completed:
        (root / "summary.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "exit_status": 0,
                    "final_step": 5000,
                }
            ),
            encoding="utf-8",
        )


def test_health_checker_pending_before_all_ranks_reach_target(tmp_path):
    root = tmp_path / "pending"
    _build_run(root, mode="ddp", metric_steps=20)
    result = evaluate_health(
        root,
        mode="ddp",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "R"},
    )
    assert result["status"] == "PENDING"
    assert result["checks"]["rank_max_steps"] == {rank: 20 for rank in range(8)}


def test_health_checker_fails_when_job_ends_before_target(tmp_path):
    root = tmp_path / "ended-early"
    _build_run(root, mode="ddp", metric_steps=20)
    result = evaluate_health(
        root,
        mode="ddp",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "F", "exit_status": 1},
    )
    assert result["status"] == "FAIL"
    assert any("not active" in failure for failure in result["failures"])


def test_health_checker_passes_ddp_at_step_200_with_active_job(tmp_path):
    root = tmp_path / "ddp-pass"
    _build_run(root, mode="ddp")
    result = evaluate_health(
        root,
        mode="ddp",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "R"},
    )
    assert result["status"] == "PASS"
    assert len(result["checks"]["observed_sync_steps"]) == 200
    assert result["checks"]["loss_tail_window_mean"] < result["checks"]["loss_first_window_mean"]


def test_health_checker_passes_periodic_syncs_at_100_and_200(tmp_path):
    root = tmp_path / "periodic-pass"
    _build_run(root, mode="periodic_average")
    result = evaluate_health(
        root,
        mode="periodic_average",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "R"},
    )
    assert result["status"] == "PASS"
    assert result["checks"]["observed_sync_steps"] == [100, 200]


def test_health_checker_fails_terminal_run_with_missing_rank(tmp_path):
    root = tmp_path / "missing-rank"
    _build_run(root, mode="ddp", missing_rank=7, completed=True)
    result = evaluate_health(
        root,
        mode="ddp",
        expected_world_size=8,
        target_step=200,
    )
    assert result["status"] == "FAIL"
    assert any("rank 7" in failure for failure in result["failures"])


def test_health_checker_fails_nonfinite_or_nondeclining_loss(tmp_path):
    nonfinite_root = tmp_path / "nonfinite"
    flat_root = tmp_path / "flat"
    _build_run(nonfinite_root, mode="ddp", nonfinite=True)
    _build_run(flat_root, mode="ddp", declining=False)

    nonfinite = evaluate_health(
        nonfinite_root,
        mode="ddp",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "R"},
    )
    flat = evaluate_health(
        flat_root,
        mode="ddp",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "R"},
    )
    assert nonfinite["status"] == "FAIL"
    assert any("non-finite" in failure for failure in nonfinite["failures"])
    assert flat["status"] == "FAIL"
    assert any("did not decline" in failure for failure in flat["failures"])


def test_health_checker_fails_missing_periodic_sync(tmp_path):
    root = tmp_path / "missing-sync"
    _build_run(
        root,
        mode="periodic_average",
        missing_periodic_step=200,
    )
    result = evaluate_health(
        root,
        mode="periodic_average",
        expected_world_size=8,
        target_step=200,
        job_status={"state": "R"},
    )
    assert result["status"] == "FAIL"
    assert any("200" in failure for failure in result["failures"])
