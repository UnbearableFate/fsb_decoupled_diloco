from __future__ import annotations

import hashlib
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


def _completed_run(project: Path, name: str = "completed-run") -> tuple[Path, Path]:
    run = project / "runs" / "full_protocol" / name
    reports = project / "reports" / "DOING" / "test"
    (run / "control").mkdir(parents=True)
    reports.mkdir(parents=True, exist_ok=True)
    summary = {"run_id": name, "final_version": 4, "all_learners_stopped": True}
    stop = {"run_id": name, "final_version": 4}
    (run / "control" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run / "control" / "stop.json").write_text(json.dumps(stop), encoding="utf-8")
    descriptor = {
        "run_id": name,
        "descriptor_sha256": f"descriptor-{name}",
        "source_fingerprint": "sha256:test-source",
    }
    (run / "control" / "run_descriptor.json").write_text(
        json.dumps(descriptor), encoding="utf-8"
    )
    policy = run / "control" / "artifact_policy.json"
    policy.write_text(json.dumps(build_artifact_policy()), encoding="utf-8")
    policy.chmod(0o444)
    database = run / "control" / "syncer_metadata.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE updates(payload_relative_path TEXT, status TEXT);
            CREATE TABLE artifact_publications(relative_path TEXT, state TEXT);
            CREATE TABLE gc_candidates(relative_path TEXT, state TEXT);
            CREATE TABLE terminal_state(
                singleton INTEGER, state TEXT, final_version INTEGER,
                finalized_by_owner_id TEXT, finalized_by_epoch INTEGER, generation INTEGER
            );
            CREATE TABLE controller_state(singleton INTEGER, state TEXT);
            CREATE TABLE run_identity(singleton INTEGER, run_id TEXT);
            CREATE TABLE global_versions(version INTEGER);
            INSERT INTO terminal_state VALUES (1, 'finalized', 4, 'syncer-owner', 1, 1);
            INSERT INTO controller_state VALUES (1, 'finalized');
            INSERT INTO run_identity VALUES (1, 'completed-run-placeholder');
            INSERT INTO global_versions VALUES (4);
            """
        )
        connection.execute("UPDATE run_identity SET run_id=?", (name,))
    owner_short = hashlib.sha256(b"syncer-owner").hexdigest()[:12]
    immutable_stop = (
        run
        / "control/syncer_epochs"
        / f"e000001_{owner_short}"
        / "terminal/stop_g000001.json"
    )
    immutable_stop.parent.mkdir(parents=True)
    immutable_stop.write_text(json.dumps(stop), encoding="utf-8")
    immutable_stop.chmod(0o444)
    evidence = reports / f"{name}.json"
    evidence.write_text(
        json.dumps(
            {
                "artifact_version": 1,
                "status": "PASS",
                "errors": [],
                "run_root": str(run),
                "identity": descriptor,
                "authority": {"final_version": 4},
                "terminal_summary": summary,
                "cleanup": {
                    "owner": "full_protocol_harness",
                    "eligible": True,
                    "targets": [str(run)],
                },
            }
        ),
        encoding="utf-8",
    )
    return run, evidence


def _write(path: Path, text: str = "generated") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dry_run_preserves_authority_and_selects_only_policy_owned_redundancy(
    tmp_path: Path,
) -> None:
    run, evidence = _completed_run(tmp_path)
    preserved = [
        run / "control/syncer_metadata.sqlite3",
        run / "weights/global.safetensors",
        run / "optim/outer.safetensors",
        run / "logs/learner_000.jsonl",
    ]
    selected = [
        run / "logs/learner_001.jsonl",
        run / "heartbeats/learner_000.json",
        run / "updates/latest/learner_000.json",
        run / "updates/payloads/learner_000/update.safetensors",
        run / "weights/.tmp-checkpoint",
    ]
    for path in (*preserved[1:], *selected):
        _write(path)

    plan = build_cleanup_plan(tmp_path, run, evidence)

    assert plan.retained_representative_learner_log == "logs/learner_000.jsonl"
    assert {candidate.path for candidate in plan.candidates} == set(selected)
    assert all(path.exists() for path in (*preserved, *selected))


def test_execute_rechecks_plan_and_publishes_exact_manifest(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "execute")
    _write(run / "logs/learner_000.jsonl")
    candidate = run / "heartbeats/learner_000.json"
    _write(candidate)
    plan = build_cleanup_plan(tmp_path, run, evidence)
    manifest = tmp_path / "reports/DOING/test/cleanup.json"

    result = execute_cleanup(plan, manifest)

    assert result["status"] == "complete"
    assert result["deleted_count"] == 1
    assert not candidate.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "complete"


def test_live_authority_reference_blocks_cleanup(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "live-reference")
    candidate = run / "updates/payloads/learner_000/update.safetensors"
    _write(candidate)
    with sqlite3.connect(run / "control/syncer_metadata.sqlite3") as connection:
        connection.execute(
            "INSERT INTO updates(payload_relative_path, status) VALUES (?, 'pending')",
            (candidate.relative_to(run).as_posix(),),
        )

    with pytest.raises(CleanupRefusedError, match="authority still references"):
        build_cleanup_plan(tmp_path, run, evidence)


@pytest.mark.parametrize("state", ["pending", "claimed"])
def test_authority_gc_ownership_retains_candidate(tmp_path: Path, state: str) -> None:
    run, evidence = _completed_run(tmp_path, f"gc-{state}")
    candidate = run / "updates/payloads/learner_000/update.safetensors"
    _write(candidate)
    relative = candidate.relative_to(run).as_posix()
    with sqlite3.connect(run / "control/syncer_metadata.sqlite3") as connection:
        connection.execute(
            "INSERT INTO gc_candidates(relative_path, state) VALUES (?, ?)",
            (relative, state),
        )

    plan = build_cleanup_plan(tmp_path, run, evidence)

    assert plan.retained_authority_owned_gc_paths == (relative,)
    assert plan.candidates == ()
    assert candidate.exists()


def test_missing_policy_or_nonpass_evidence_fails_closed(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "fail-closed")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["status"] = "BLOCKED"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CleanupRefusedError, match="error-free PASS"):
        build_cleanup_plan(tmp_path, run, evidence)
    payload["status"] = "PASS"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    policy = run / "control/artifact_policy.json"
    policy.chmod(0o644)
    policy.unlink()
    with pytest.raises(CleanupRefusedError, match="policy is required"):
        build_cleanup_plan(tmp_path, run, evidence)


def test_mismatched_evidence_and_broad_run_target_are_rejected(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "first")
    other, _ = _completed_run(tmp_path, "second")
    with pytest.raises(CleanupRefusedError, match="different run|identity"):
        build_cleanup_plan(tmp_path, other, evidence)
    with pytest.raises(CleanupRefusedError, match="one exact run"):
        build_cleanup_plan(tmp_path, tmp_path / "runs", evidence)
    assert run.exists()


def test_symlinked_candidate_parent_is_never_followed(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "symlink-parent")
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "update.safetensors", "must survive")
    payloads = run / "updates/payloads"
    payloads.parent.mkdir(parents=True)
    payloads.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CleanupRefusedError, match="symlink|owned directory"):
        build_cleanup_plan(tmp_path, run, evidence)
    assert (outside / "update.safetensors").read_text(encoding="utf-8") == "must survive"


def test_evidence_descriptor_and_terminal_identities_must_match(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "identity-mismatch")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["identity"]["descriptor_sha256"] = "wrong"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CleanupRefusedError, match="descriptor identity"):
        build_cleanup_plan(tmp_path, run, evidence)

    payload["identity"]["descriptor_sha256"] = "descriptor-identity-mismatch"
    payload["authority"]["final_version"] = 3
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CleanupRefusedError, match="final version"):
        build_cleanup_plan(tmp_path, run, evidence)


def test_terminal_projection_and_cleanup_target_must_match_immutable_evidence(
    tmp_path: Path,
) -> None:
    run, evidence = _completed_run(tmp_path, "terminal-binding")
    stop_path = run / "control/stop.json"
    stop_path.write_text(
        json.dumps({"run_id": run.name, "final_version": 4, "changed": True}),
        encoding="utf-8",
    )
    with pytest.raises(CleanupRefusedError, match="immutable authority output"):
        build_cleanup_plan(tmp_path, run, evidence)

    stop_path.write_text(
        json.dumps({"run_id": run.name, "final_version": 4}),
        encoding="utf-8",
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["cleanup"]["targets"] = [str(tmp_path / "runs")]
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CleanupRefusedError, match="authorize this cleanup target"):
        build_cleanup_plan(tmp_path, run, evidence)


def test_authority_sidecar_or_symlinked_database_blocks_cleanup(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "authority-files")
    database = run / "control/syncer_metadata.sqlite3"
    sidecar = database.with_name(database.name + "-wal")
    _write(sidecar)
    with pytest.raises(CleanupRefusedError, match="may still be active"):
        build_cleanup_plan(tmp_path, run, evidence)

    sidecar.unlink()
    outside = tmp_path / "outside.sqlite3"
    database.rename(outside)
    database.symlink_to(outside)
    with pytest.raises(CleanupRefusedError, match="regular non-symlink"):
        build_cleanup_plan(tmp_path, run, evidence)


def test_execute_revalidates_evidence_and_candidate_identity(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "revalidate")
    candidate = run / "heartbeats/learner_000.json"
    _write(candidate)
    plan = build_cleanup_plan(tmp_path, run, evidence)
    _write(candidate, "changed")
    manifest = tmp_path / "reports/DOING/test/changed-candidate.json"
    with pytest.raises(CleanupRefusedError, match="changed after inventory"):
        execute_cleanup(plan, manifest)
    assert candidate.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "failed"

    plan = build_cleanup_plan(tmp_path, run, evidence)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["status"] = "BLOCKED"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    manifest = tmp_path / "reports/DOING/test/changed-evidence.json"
    with pytest.raises(CleanupRefusedError, match="error-free PASS"):
        execute_cleanup(plan, manifest)
    assert candidate.exists()


def test_execute_never_follows_a_parent_swapped_to_a_symlink(tmp_path: Path) -> None:
    run, evidence = _completed_run(tmp_path, "parent-swap")
    candidate = run / "updates/payloads/learner_000/update.safetensors"
    _write(candidate)
    plan = build_cleanup_plan(tmp_path, run, evidence)
    outside = tmp_path / "outside-swap"
    outside_candidate = outside / "learner_000/update.safetensors"
    _write(outside_candidate, "must survive")
    payloads = run / "updates/payloads"
    original = run / "updates/payloads-original"
    payloads.rename(original)
    payloads.symlink_to(outside, target_is_directory=True)
    manifest = tmp_path / "reports/DOING/test/parent-swap.json"

    with pytest.raises(CleanupRefusedError, match="changed|parent|directory"):
        execute_cleanup(plan, manifest)
    assert outside_candidate.read_text(encoding="utf-8") == "must survive"
    assert (original / "learner_000/update.safetensors").exists()


def test_cli_has_one_explicit_dry_run_and_execute_interface(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, evidence = _completed_run(tmp_path, "cli")
    _write(run / "heartbeats/learner_000.json")

    assert (
        main(
            [
                "--run-root",
                str(run),
                "--evidence",
                str(evidence),
                "--project-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["candidate_count"] == 1

    manifest = tmp_path / "reports/DOING/test/cli-cleanup.json"
    assert (
        main(
            [
                "--run-root",
                str(run),
                "--evidence",
                str(evidence),
                "--project-root",
                str(tmp_path),
                "--execute",
                "--manifest",
                str(manifest),
            ]
        )
        == 0
    )
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "complete"
