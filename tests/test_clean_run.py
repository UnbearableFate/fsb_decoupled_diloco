from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.storage.artifact_policy import build_artifact_policy
from fs_diloco.tools.clean_run import (
    CleanupRefusedError,
    build_cleanup_plan,
    execute_cleanup,
    main,
)


PLAN03_REQUIREMENTS = frozenset({"AUDIT-03", "FS-05"})


def _completed_run(project: Path, name: str = "completed-run") -> tuple[Path, Path]:
    run = project / "runs" / "fs_diloco" / name
    reports = project / "reports" / "DOING" / "plan" / "artifacts"
    (run / "control").mkdir(parents=True)
    reports.mkdir(parents=True, exist_ok=True)
    (run / "control" / "summary.json").write_text(
        json.dumps(
            {
                "run_id": name,
                "final_version": 12,
                "all_learners_stopped": True,
            }
        ),
        encoding="utf-8",
    )
    (run / "control" / "stop.json").write_text(
        json.dumps({"run_id": name, "final_version": 12}),
        encoding="utf-8",
    )
    (run / "control" / "run_descriptor.json").write_text(
        json.dumps(
            {
                "run_id": name,
                "descriptor_sha256": f"descriptor-{name}",
                "source_fingerprint": "sha256:test-source",
            }
        ),
        encoding="utf-8",
    )
    evidence = reports / f"{name}-completed.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "PASS",
                "errors": [],
                "run_root": str(run),
                "identity": {
                    "run_id": name,
                    "descriptor_sha256": f"descriptor-{name}",
                    "source_fingerprint": "sha256:test-source",
                },
                "authority": {"terminal": {"final_version": 12}},
            }
        ),
        encoding="utf-8",
    )
    return run, evidence


def _write(path: Path, text: str = "generated") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_clean_run_preserves_authority_and_one_learner_log(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path)
    preserved = [
        run / "control" / "syncer_metadata.sqlite3",
        run / "weights" / "global_v000012.safetensors",
        run / "optim" / "outer_v000012.safetensors",
        run / "logs" / "syncer.jsonl",
        run / "logs" / "learner_000.jsonl",
        run / "metrics" / "update_history.jsonl",
    ]
    deleted = [
        run / "logs" / "learner_001.jsonl",
        run / "logs" / "wandb" / "offline-run" / "debug.log",
        run / "metrics" / "update_manifest.csv",
        run / "metrics" / "learner_metrics.csv",
        run / "heartbeats" / "learner_000.json",
        run / "updates" / "latest" / "learner_000.json",
        run / "updates" / "payloads" / "learner_000" / "update.safetensors",
        run / "weights" / ".tmp-checkpoint",
    ]
    for path in (*preserved, *deleted):
        _write(path)

    plan = build_cleanup_plan(tmp_path, run, evidence)

    assert plan.retained_representative_learner_log == "logs/learner_000.jsonl"
    assert {candidate.path for candidate in plan.candidates} == set(deleted)
    assert all(path.exists() for path in preserved)
    assert all(path.exists() for path in deleted)

    manifest = tmp_path / "reports" / "DOING" / "plan" / "cleanup.json"
    result = execute_cleanup(plan, manifest)

    assert result["status"] == "complete"
    assert result["deleted_count"] == len(deleted)
    assert all(path.exists() for path in preserved)
    assert all(not path.exists() for path in deleted)
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "complete"


@pytest.mark.parametrize("evidence_status", ["BLOCKED", "PASS_WITH_FOLLOWUPS"])
def test_clean_run_requires_exact_pass_evidence(
    tmp_path: Path,
    evidence_status: str,
) -> None:
    run, evidence = _completed_run(tmp_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["status"] = evidence_status
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CleanupRefusedError, match="error-free PASS"):
        build_cleanup_plan(tmp_path, run, evidence)


def test_clean_run_refuses_mismatched_evidence_and_broad_target(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path)
    other, _other_evidence = _completed_run(tmp_path, "other-run")
    with pytest.raises(
        CleanupRefusedError,
        match="different run|run identity does not match",
    ):
        build_cleanup_plan(tmp_path, other, evidence)
    with pytest.raises(CleanupRefusedError, match="one exact run"):
        build_cleanup_plan(tmp_path, tmp_path / "runs", evidence)
    assert run.exists()


def test_clean_run_accepts_each_run_from_matched_performance_evidence(tmp_path: Path) -> None:
    static_run, static_evidence = _completed_run(tmp_path, "matched-static")
    dynamic_run, _dynamic_evidence = _completed_run(tmp_path, "matched-dynamic")
    matched_evidence = static_evidence.with_name("matched-performance.json")
    matched_evidence.write_text(
        json.dumps(
            {
                "status": "PASS",
                "errors": [],
                "identity": {
                    "static_descriptor_sha256": "descriptor-matched-static",
                    "dynamic_descriptor_sha256": "descriptor-matched-dynamic",
                    "source_fingerprint": "sha256:test-source",
                },
                "static": {
                    "run_root": str(static_run),
                    "summary": {"run_id": static_run.name, "final_version": 12},
                },
                "dynamic": {
                    "run_root": str(dynamic_run),
                    "summary": {"run_id": dynamic_run.name, "final_version": 12},
                },
            }
        ),
        encoding="utf-8",
    )

    assert build_cleanup_plan(tmp_path, static_run, matched_evidence).run_id == static_run.name
    assert build_cleanup_plan(tmp_path, dynamic_run, matched_evidence).run_id == dynamic_run.name


def test_clean_run_rejects_mismatched_descriptor_identity(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["identity"]["descriptor_sha256"] = "wrong-descriptor"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CleanupRefusedError, match="descriptor identity does not match"):
        build_cleanup_plan(tmp_path, run, evidence)


@pytest.mark.parametrize("terminal_binding", [None, 11])
def test_clean_run_requires_evidence_for_current_terminal_version(
    tmp_path: Path,
    terminal_binding: int | None,
) -> None:
    run, evidence = _completed_run(tmp_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    if terminal_binding is None:
        payload.pop("authority")
    else:
        payload["authority"]["terminal"]["final_version"] = terminal_binding
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CleanupRefusedError, match="terminal final version"):
        build_cleanup_plan(tmp_path, run, evidence)


def test_clean_run_refuses_authority_sidecar_and_changed_candidate(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path)
    sidecar = run / "control" / "syncer_metadata.sqlite3-wal"
    _write(sidecar)
    with pytest.raises(CleanupRefusedError, match="may still be active"):
        build_cleanup_plan(tmp_path, run, evidence)
    sidecar.unlink()
    sidecar.symlink_to(tmp_path / "missing-sidecar")
    with pytest.raises(CleanupRefusedError, match="may still be active"):
        build_cleanup_plan(tmp_path, run, evidence)
    sidecar.unlink()
    candidate = run / "logs" / "learner_001.jsonl"
    representative = run / "logs" / "learner_000.jsonl"
    _write(representative)
    _write(candidate)
    plan = build_cleanup_plan(tmp_path, run, evidence)
    _write(candidate, "changed after inventory")
    manifest = tmp_path / "reports" / "DOING" / "plan" / "changed-cleanup.json"

    with pytest.raises(CleanupRefusedError, match="changed after inventory"):
        execute_cleanup(plan, manifest)

    assert candidate.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "failed"


def test_clean_run_revalidates_completion_evidence_before_delete(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path)
    candidate = run / "metrics" / "update_manifest.csv"
    _write(candidate)
    plan = build_cleanup_plan(tmp_path, run, evidence)
    evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))
    evidence_payload["status"] = "BLOCKED"
    evidence.write_text(json.dumps(evidence_payload), encoding="utf-8")
    manifest = tmp_path / "reports" / "DOING" / "plan" / "revalidated-cleanup.json"

    with pytest.raises(CleanupRefusedError, match="error-free PASS"):
        execute_cleanup(plan, manifest)

    assert candidate.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "failed"


def test_clean_run_cli_is_dry_run_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, evidence = _completed_run(tmp_path)
    candidate = run / "metrics" / "update_manifest.csv"
    _write(candidate)

    assert main([str(run), "--evidence", str(evidence), "--project-root", str(tmp_path)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "dry_run"
    assert output["candidate_count"] == 1
    assert candidate.exists()


def test_clean_run_delete_requires_report_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, evidence = _completed_run(tmp_path)
    assert (
        main(
            [
                str(run),
                "--evidence",
                str(evidence),
                "--project-root",
                str(tmp_path),
                "--delete",
            ]
        )
        == 2
    )
    assert "--manifest-output is required" in capsys.readouterr().err


def test_clean_run_applies_artifact_policy_and_authority_live_references(
    tmp_path: Path,
) -> None:
    run, evidence = _completed_run(tmp_path, "policy-run")
    policy_path = run / "control" / "artifact_policy.json"
    policy_path.write_text(json.dumps(build_artifact_policy()), encoding="utf-8")
    policy_path.chmod(0o444)
    database = run / "control" / "syncer_metadata.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE updates(status TEXT, payload_relative_path TEXT)")
    candidate = run / "updates/payloads/learner-0/update.safetensors"
    _write(candidate)
    connection.execute(
        "INSERT INTO updates(status, payload_relative_path) VALUES ('pending', ?)",
        (candidate.relative_to(run).as_posix(),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CleanupRefusedError, match="authority still references"):
        build_cleanup_plan(tmp_path, run, evidence)

    connection = sqlite3.connect(database)
    connection.execute("UPDATE updates SET status='applied'")
    connection.commit()
    connection.close()
    plan = build_cleanup_plan(tmp_path, run, evidence)
    assert plan.artifact_policy_sha256 == build_artifact_policy()["policy_sha256"]
    assert {item.relative_path for item in plan.candidates} == {
        "updates/payloads/learner-0/update.safetensors"
    }


def test_clean_run_refuses_symlinked_policy_or_authority_database(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "policy-symlink-run")
    policy = run / "control" / "artifact_policy.json"
    policy.symlink_to(tmp_path / "missing-policy")
    with pytest.raises(CleanupRefusedError, match="immutable regular"):
        build_cleanup_plan(tmp_path, run, evidence)
    policy.unlink()
    policy.write_text(json.dumps(build_artifact_policy()), encoding="utf-8")
    policy.chmod(0o444)
    database = run / "control" / "syncer_metadata.sqlite3"
    database.touch()
    database.unlink()
    database.symlink_to(tmp_path / "outside-authority.sqlite3")

    with pytest.raises(CleanupRefusedError, match="regular non-symlink"):
        build_cleanup_plan(tmp_path, run, evidence)
