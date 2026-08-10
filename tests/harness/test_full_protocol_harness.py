from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.core.run_descriptor import load_run_descriptor, write_actor_attestation
from fs_diloco.core.source_identity import bind_source_identity, capture_source_identity
from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2, canonical_update_relative_path
from fs_diloco.storage.atomic_io import atomic_write_json
from fs_diloco.storage.audit_archive import build_audit_batch, publish_audit_batch
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority
from fs_diloco.storage.control import ControlPublisher
from fs_diloco.storage.paths import RunPaths
from fs_diloco.tools.init_run import initialize_run
from tests.support.protocol import (
    DEFAULT_PAYLOAD,
    PAYLOAD_DIGEST,
    SCHEMA_DIGEST,
    publish_checkpoint_pair,
    publish_proposal_payload,
)


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/miyabi/check_full_protocol_run.py"


def _checker_module():
    specification = importlib.util.spec_from_file_location("check_full_protocol_run", CHECKER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _build_valid_checker_fixture(
    tmp_path: Path,
    *,
    fault_scenario: str = "none",
    syncer_takeover_boundary_version: int = 2,
    independent_scheduler_jobs: bool = False,
) -> tuple[Path, Path, list[str], dict[str, str]]:
    if independent_scheduler_jobs and fault_scenario != "none":
        raise ValueError("independent scheduler fixture only covers the fault-free scenario")
    global_steps_by_scenario = {
        "none": 1,
        "learner_replacement": 2,
        "syncer_takeover": 3,
    }
    expected_global_steps = global_steps_by_scenario[fault_scenario]
    run_root = tmp_path / "run"
    log_root = tmp_path / "logs"
    config = resolve_config(
        ROOT / "configs/full_protocol_functional.yaml",
        run_id="checker-fixture",
        shared_root=str(run_root),
        project_root=ROOT,
    )
    config.sync.num_learners = 1
    config.sync.quorum_min = 1
    config.sync.quorum_max = 1
    config.sync.stop_after_outer_steps = expected_global_steps
    config.membership.stream_pool_size = 1
    config.membership.bootstrap_instances = 1
    config.scaling.desired_contributors = 1
    config.scaling.low_contributor_threshold = 0
    config.training.inner_steps = 1
    bind_source_identity(config, ROOT)
    config.validate()
    initialize_run(config, project_root=ROOT)

    loaded = load_run_descriptor(run_root)
    descriptor = loaded.descriptor
    identity = AuthorityIdentity(
        run_id=str(descriptor["run_id"]),
        source_fingerprint=str(descriptor["source_fingerprint"]),
        config_sha256=str(descriptor["resolved_config_sha256"]),
    )
    scope = StaticMembershipScope(("learner_000",))
    scheduler_job_id = (
        "fixture-syncer.opbs" if independent_scheduler_jobs else "fixture.opbs"
    )
    learner_scheduler_job_id = (
        "fixture-learner.opbs" if independent_scheduler_jobs else scheduler_job_id
    )
    clock_value = [100.0]
    syncer_attempts = [("checker-syncer", "syncer-attempt", 101)]
    learner_attempts = ["fixture-attempt"]
    replacement_evidence = None
    takeover_evidence = None
    with LeaderAuthority(
        loaded.paths.sqlite_db,
        identity,
        scope,
        run_root=run_root,
        lease_duration_seconds=5.0,
        max_clock_skew_seconds=1.0,
        wall_clock=lambda: clock_value[0],
    ) as authority:
        token = authority.acquire_leader(
            owner_id="checker-syncer",
            hostname=socket.gethostname(),
            pid=101,
            pbs_job_id=scheduler_job_id,
        )
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="bind-learner",
            learner_id="learner_000",
            logical_launch_id="fixture-launch",
            attempt_id="fixture-attempt",
        )
        fence = StaticContributorFence(
            kind="static",
            learner_id=binding.learner_id,
            logical_launch_id=binding.logical_launch_id,
            attempt_id=binding.attempt_id,
            binding_generation=binding.binding_generation,
        )
        leader.initialize_genesis(
            command_id="genesis",
            publication_id="publication-0",
            **publish_checkpoint_pair(run_root, version=0, epoch=token.epoch),
        )

        def publish_cycle(
            sequence: int,
            current_fence: StaticContributorFence,
            previous: CycleReceiptV1 | None,
            *,
            commit: bool,
        ) -> tuple[CycleReceiptV1, FullUpdateProposalV2]:
            update_id = f"00000000-0000-4000-8000-{sequence:012d}"
            receipt = CycleReceiptV1.from_dict(
                {
                    "cycle_receipt_format_version": 1,
                    "run_id": descriptor["run_id"],
                    "stable_contributor_key": "learner_000",
                    "cycle_seq": sequence,
                    "cycle_id": f"10000000-0000-4000-8000-{sequence:012d}",
                    "receipt_id": f"receipt-learner_000-{sequence}",
                    "previous_receipt_id": None if previous is None else previous.receipt_id,
                    "previous_receipt_sha256": (
                        None if previous is None else previous.immutable_sha256()
                    ),
                    "processed_tokens_this_cycle": 16,
                    "effective_tokens_this_cycle": 16,
                    "local_discarded_tokens_this_cycle": 0,
                    "retained_tokens_since_base": 16,
                    "data_cursor_start": sequence - 1,
                    "data_cursor_end": sequence,
                    "proposal_expected": True,
                    "planned_update_id": update_id,
                    "planned_payload_sha256": PAYLOAD_DIGEST,
                    "contributor_fence": current_fence.as_dict(),
                    "created_at": 100.0 + sequence,
                }
            )
            leader.ingest_cycle_receipt(command_id=f"receipt-{sequence}", receipt=receipt)
            proposal = FullUpdateProposalV2.from_dict(
                {
                    "proposal_format_version": 2,
                    "run_id": descriptor["run_id"],
                    "stable_contributor_key": "learner_000",
                    "cycle_seq": sequence,
                    "cycle_id": receipt.cycle_id,
                    "update_id": update_id,
                    "cycle_receipt_id": receipt.receipt_id,
                    "cycle_receipt_sha256": receipt.immutable_sha256(),
                    "base_global_version": min(sequence - 1, expected_global_steps),
                    "local_step_start": sequence - 1,
                    "local_step_end": sequence,
                    "inner_steps": 1,
                    "processed_tokens_this_cycle": 16,
                    "effective_tokens_this_update": 16,
                    "local_discarded_tokens_this_cycle": 0,
                    "retained_tokens_since_base": 16,
                    "data_cursor_start": sequence - 1,
                    "data_cursor_end": sequence,
                    "contributor_fence": current_fence.as_dict(),
                    "payload_relative_path": canonical_update_relative_path(
                        "learner_000", update_id
                    ),
                    "payload_size": len(DEFAULT_PAYLOAD),
                    "payload_sha256": PAYLOAD_DIGEST,
                    "tensor_schema_sha256": SCHEMA_DIGEST,
                    "tensor_dtype": "float32",
                    "tensor_numel": 1,
                    "created_at": 100.0 + sequence,
                }
            )
            publish_proposal_payload(run_root, proposal)
            leader.ingest_proposal(command_id=f"proposal-{sequence}", proposal=proposal)
            if commit:
                selected = leader.try_select_batch(
                    command_id=f"select-{sequence}", quorum_min=1, quorum_max=1
                )
                assert selected.batch is not None
                leader.prepare_publication(
                    command_id=f"prepare-{sequence}",
                    publication_id=f"publication-{sequence}",
                    target_version=sequence,
                    selection_batch_id=selected.batch.batch_id,
                    **publish_checkpoint_pair(run_root, version=sequence, epoch=leader.token.epoch),
                )
                leader.commit_merge(
                    command_id=f"commit-{sequence}",
                    publication_id=f"publication-{sequence}",
                )
            return receipt, proposal

        previous_receipt = None
        for sequence in range(1, expected_global_steps + 1):
            previous_receipt, _proposal = publish_cycle(
                sequence, fence, previous_receipt, commit=True
            )
            if fault_scenario == "learner_replacement" and sequence == 1:
                old_fence = fence
                replacement = leader.bind_or_replace_static_attempt(
                    command_id="replace-learner",
                    learner_id="learner_000",
                    logical_launch_id="fixture-launch",
                    attempt_id="fixture-attempt-replacement",
                    expected_generation=1,
                    replacement_reason="fixture injected process replacement",
                )
                fence = StaticContributorFence(
                    kind="static",
                    learner_id=replacement.learner_id,
                    logical_launch_id=replacement.logical_launch_id,
                    attempt_id=replacement.attempt_id,
                    binding_generation=replacement.binding_generation,
                )
                learner_attempts.append(replacement.attempt_id)
                replacement_evidence = {
                    "learner_id": "learner_000",
                    "injected_exit_status": 143,
                    "old_attempt_id": old_fence.attempt_id,
                    "new_attempt_id": replacement.attempt_id,
                    "old_binding_generation": old_fence.binding_generation,
                }
            if fault_scenario == "syncer_takeover" and sequence == 2:
                takeover_evidence = {
                    "primary_exit_status": 137,
                    "primary_pid": 101,
                    "fault_boundary": {
                        "sqlite_transaction_active": False,
                        "lease_renewer_quiesced": True,
                        "committed_version": syncer_takeover_boundary_version,
                        "pid": 101,
                        "epoch": token.epoch,
                    },
                }
                clock_value[0] = 107.0
                token = authority.acquire_leader(
                    owner_id="checker-syncer-successor",
                    hostname=socket.gethostname(),
                    pid=102,
                    pbs_job_id=scheduler_job_id,
                )
                leader = authority.open_leader(token)
                syncer_attempts.append(
                    ("checker-syncer-successor", "syncer-successor-attempt", 102)
                )

        extra_receipt, extra_proposal = publish_cycle(
            expected_global_steps + 1,
            fence,
            previous_receipt,
            commit=False,
        )
        leader.begin_terminal_close(command_id="close", reason="fixture complete")
        leader.acknowledge_terminal_contributor(
            command_id="ack-learner",
            fence=fence,
            final_cycle_seq=extra_receipt.cycle_seq,
            final_update_id=extra_proposal.update_id,
        )
        leader.finalize_terminal(command_id="finalize", reason="fixture complete")
        terminal = authority.read.terminal_record()
        assert terminal is not None
        ControlPublisher(loaded.paths, token).publish_terminal(terminal)
        authority.release_leader(token)

    log_root.mkdir()
    atomic_write_json(log_root / "source_identity.json", capture_source_identity(ROOT))
    atomic_write_json(log_root / "init_run.json", {"run_id": descriptor["run_id"]})
    if independent_scheduler_jobs:
        atomic_write_json(
            log_root / "submission_receipt.json",
            {
                "submission_status": "submitted",
                "membership_mode": "static",
                "syncer_job_id": scheduler_job_id,
                "learner_job_ids": [learner_scheduler_job_id],
            },
        )
    atomic_write_json(log_root / "summary.json", {"final_version": expected_global_steps})
    if replacement_evidence is not None:
        atomic_write_json(log_root / "learner_replacement.json", replacement_evidence)
    if takeover_evidence is not None:
        atomic_write_json(log_root / "syncer_takeover.json", takeover_evidence)
    publish_audit_batch(
        loaded.paths,
        build_audit_batch(
            batch_id="checker-fixture",
            record_kind="authority_history",
            cutoff_version=expected_global_steps,
            records=[],
        ),
    )
    runtime_evidence = {
        "torch_version": "fixture",
        "cuda_runtime_version": None,
        "gpu_driver_version": None,
        "module_environment": [],
        "resource_allocation": {"nodes": 1},
    }
    for attempt_id in learner_attempts:
        write_actor_attestation(
            loaded,
            actor_kind="learner",
            actor_id="learner_000",
            attempt_id=attempt_id,
            runtime_evidence=runtime_evidence,
            scheduler_job_id=learner_scheduler_job_id,
        )
    for actor_id, attempt_id, _pid in syncer_attempts:
        write_actor_attestation(
            loaded,
            actor_kind="syncer",
            actor_id=actor_id,
            attempt_id=attempt_id,
            runtime_evidence=runtime_evidence,
            scheduler_job_id=scheduler_job_id,
        )
    nodefile = tmp_path / "pbs-nodes"
    nodefile.write_text(socket.gethostname() + "\n", encoding="utf-8")
    output = tmp_path / "gate.json"
    command = [
        sys.executable,
        str(CHECKER),
        "--gate",
        "U1-checker",
        "--experiment-id",
        "aggregate-fixture",
        "--requirement-id",
        "HARNESS-01",
        "--project-root",
        str(ROOT),
        "--run-root",
        str(run_root),
        "--log-root",
        str(log_root),
        "--expected-global-steps",
        str(expected_global_steps),
        "--expected-inner-steps",
        "1",
        "--expected-contributors",
        "1",
        "--expected-hosts",
        "1",
        "--expected-scheduler-jobs",
        "2" if independent_scheduler_jobs else "1",
        "--fault-scenario",
        fault_scenario,
        "--syncer-takeover-boundary-version",
        str(syncer_takeover_boundary_version),
        "--output",
        str(output),
    ]
    environment = {
        **os.environ,
        "PBS_JOBID": "fixture-checker.opbs" if independent_scheduler_jobs else scheduler_job_id,
        "PBS_NODEFILE": str(nodefile),
    }
    return run_root, output, command, environment


def _run_checker(command: list[str], environment: dict[str, str], output: Path):
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed, json.loads(output.read_text(encoding="utf-8"))


def test_aggregate_checker_accepts_adjudicated_terminal_overshoot(tmp_path: Path) -> None:
    run_root, output, command, environment = _build_valid_checker_fixture(tmp_path)

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 0, completed.stderr
    assert artifact["status"] == "PASS"
    assert artifact["errors"] == []
    assert artifact["source_identity"]["dirty"] is False
    assert artifact["config_schema_identity"]["version"] == 1
    assert artifact["protocol_schema_identity"]["mode"] == "static"
    assert artifact["environment"]["pbs_job_id"] == "fixture.opbs"
    assert artifact["environment"]["packages"]["torch"] != "not-installed"
    assert artifact["workload_identity"] == {
        "configured_local_steps": 1,
        "committed_global_steps": 1,
        "processed_tokens": 32,
        "direct_weight_tokens_applied": 16,
        "cursor_terminal": {"learner_000": 2},
    }
    assert artifact["authority"]["integrity"] == ["ok"]
    assert [row["final_state"] for row in artifact["authority"]["epochs"]] == ["released"]
    assert artifact["metrics"]["token_balance"] == 0
    assert artifact["metrics"]["applied_proposal_count"] == 1
    assert artifact["metrics"]["dropped_proposal_count"] == 1
    assert artifact["metrics"]["direct_dropped_tokens"] == 16
    assert artifact["metrics"]["publication_object_count"] == 4
    assert artifact["topology"]["learner_attestation_count"] == 1
    assert artifact["topology"]["syncer_attestation_count"] == 1
    assert artifact["cleanup"] == {
        "owner": "full_protocol_harness",
        "eligible": True,
        "targets": [str(run_root)],
    }


def test_aggregate_checker_accepts_independent_actor_jobs(tmp_path: Path) -> None:
    run_root, output, command, environment = _build_valid_checker_fixture(
        tmp_path,
        independent_scheduler_jobs=True,
    )

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 0, completed.stderr
    assert artifact["status"] == "PASS"
    assert artifact["topology"]["expected_scheduler_jobs"] == 2
    assert artifact["topology"]["attested_scheduler_job_ids"] == [
        "fixture-learner.opbs",
        "fixture-syncer.opbs",
    ]
    assert artifact["environment"]["pbs_job_id"] == "fixture-checker.opbs"
    assert artifact["environment"]["launch_topology"] == {
        "syncer_job_id": "fixture-syncer.opbs",
        "learner_job_ids": ["fixture-learner.opbs"],
    }
    assert artifact["run_root"] == str(run_root)


def test_aggregate_checker_rejects_submission_receipt_mismatch(tmp_path: Path) -> None:
    _run_root, output, command, environment = _build_valid_checker_fixture(
        tmp_path,
        independent_scheduler_jobs=True,
    )
    receipt_path = tmp_path / "logs/submission_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["learner_job_ids"] = ["wrong-learner.opbs"]
    receipt_path.chmod(0o644)
    receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 1
    assert artifact["status"] == "FAIL"
    assert "actor attestations do not match the independent submission receipt" in artifact[
        "errors"
    ]


@pytest.mark.parametrize(
    ("scenario", "global_steps", "epoch_states", "learner_attempts", "syncer_attempts"),
    [
        ("learner_replacement", 2, ["released"], 2, 1),
        ("syncer_takeover", 3, ["expired", "released"], 1, 2),
    ],
)
def test_aggregate_checker_accepts_registered_fault_behavior(
    tmp_path: Path,
    scenario: str,
    global_steps: int,
    epoch_states: list[str],
    learner_attempts: int,
    syncer_attempts: int,
) -> None:
    _run_root, output, command, environment = _build_valid_checker_fixture(
        tmp_path,
        fault_scenario=scenario,
    )

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 0, completed.stderr
    assert artifact["status"] == "PASS"
    assert artifact["fault_scenario"] == scenario
    assert artifact["authority"]["final_version"] == global_steps
    assert [row["final_state"] for row in artifact["authority"]["epochs"]] == epoch_states
    assert artifact["metrics"]["learner_attestation_count"] == learner_attempts
    assert artifact["metrics"]["syncer_attestation_count"] == syncer_attempts
    if scenario == "learner_replacement":
        assert artifact["authority"]["static_binding_history"][0]["final_status"] == "replaced"
        assert artifact["fault_evidence"]["learner_replacement"] is not None
    else:
        assert artifact["fault_evidence"]["syncer_takeover"] is not None


def test_syncer_takeover_boundary_is_argument_bound_and_durable(tmp_path: Path) -> None:
    _run_root, output, command, environment = _build_valid_checker_fixture(
        tmp_path / "matching",
        fault_scenario="syncer_takeover",
        syncer_takeover_boundary_version=3,
    )

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 0, completed.stderr
    assert artifact["status"] == "PASS"
    assert artifact["syncer_takeover_boundary_version"] == 3

    _run_root, output, command, environment = _build_valid_checker_fixture(
        tmp_path / "mismatch",
        fault_scenario="syncer_takeover",
        syncer_takeover_boundary_version=3,
    )
    boundary_index = command.index("--syncer-takeover-boundary-version") + 1
    command[boundary_index] = "2"

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 1
    assert artifact["status"] == "FAIL"
    assert (
        "syncer takeover evidence does not prove the registered fault layer" in artifact["errors"]
    )


@pytest.mark.parametrize(
    ("scenario", "mutation", "expected_error"),
    [
        (
            "learner_replacement",
            "replacement_history",
            "registered learner replacement history is not exact",
        ),
        (
            "learner_replacement",
            "replacement_evidence",
            "registered learner replacement evidence is missing",
        ),
        (
            "syncer_takeover",
            "takeover_boundary",
            "syncer takeover evidence does not prove the registered fault layer",
        ),
        (
            "syncer_takeover",
            "takeover_linkage",
            "syncer takeover epoch linkage is invalid",
        ),
    ],
)
def test_aggregate_checker_fault_mutations_change_acceptance_to_fail(
    tmp_path: Path,
    scenario: str,
    mutation: str,
    expected_error: str,
) -> None:
    run_root, output, command, environment = _build_valid_checker_fixture(
        tmp_path,
        fault_scenario=scenario,
    )
    if mutation == "replacement_history":
        with sqlite3.connect(RunPaths(run_root).sqlite_db) as connection:
            connection.execute("UPDATE static_binding_history SET final_status='terminal'")
    elif mutation == "replacement_evidence":
        (tmp_path / "logs/learner_replacement.json").unlink()
    elif mutation == "takeover_boundary":
        path = tmp_path / "logs/syncer_takeover.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["fault_boundary"]["lease_renewer_quiesced"] = False
        atomic_write_json(path, evidence)
    elif mutation == "takeover_linkage":
        with sqlite3.connect(RunPaths(run_root).sqlite_db) as connection:
            connection.execute("UPDATE syncer_epochs SET superseded_by_epoch=NULL WHERE epoch=1")
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 1
    assert artifact["status"] == "FAIL"
    assert expected_error in artifact["errors"]


@pytest.mark.parametrize(
    "mutation",
    [
        "source_identity",
        "resolved_config",
        "epoch_lifecycle",
        "attested_topology",
        "terminal_authority",
        "terminal_applied_total",
        "terminal_stop_schema",
        "terminal_summary_schema",
        "terminal_control_mutability",
        "archive_integrity",
        "exact_workload",
        "normal_non_drop_fate",
        "unregistered_replacement",
        "publication_identity",
    ],
)
def test_aggregate_checker_mutations_change_acceptance_to_fail(
    tmp_path: Path, mutation: str
) -> None:
    run_root, output, command, environment = _build_valid_checker_fixture(tmp_path)
    paths = RunPaths(run_root)
    if mutation == "source_identity":
        source_path = tmp_path / "logs/source_identity.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["source_fingerprint"] = "sha256:" + "0" * 64
        atomic_write_json(source_path, source)
    elif mutation == "resolved_config":
        paths.resolved_config_yaml.chmod(0o644)
        with paths.resolved_config_yaml.open("a", encoding="utf-8") as handle:
            handle.write("removed_config_key: true\n")
    elif mutation == "epoch_lifecycle":
        with sqlite3.connect(paths.sqlite_db) as connection:
            connection.execute("UPDATE syncer_epochs SET final_state='error'")
    elif mutation == "attested_topology":
        next((paths.metrics / "attestations/syncer").glob("*/*.json")).unlink()
    elif mutation == "terminal_authority":
        with sqlite3.connect(paths.sqlite_db) as connection:
            connection.execute("UPDATE terminal_state SET final_version=0")
    elif mutation == "terminal_applied_total":
        with sqlite3.connect(paths.sqlite_db) as connection:
            connection.execute(
                "UPDATE terminal_state SET direct_weight_tokens_applied=0 WHERE singleton=1"
            )
        stop = json.loads(paths.stop_json.read_text(encoding="utf-8"))
        stop["direct_weight_tokens_applied"] = 0
        atomic_write_json(paths.stop_json, stop)
        summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
        summary["direct_weight_tokens_applied"] = 0
        atomic_write_json(paths.summary_json, summary)
        immutable_stop = next(paths.syncer_epochs.glob("e*_*/terminal/stop_*.json"))
        immutable = json.loads(immutable_stop.read_text(encoding="utf-8"))
        immutable["direct_weight_tokens_applied"] = 0
        atomic_write_json(immutable_stop, immutable, mode=0o444)
    elif mutation == "terminal_stop_schema":
        stop = json.loads(paths.stop_json.read_text(encoding="utf-8"))
        stop["obsolete_field"] = True
        atomic_write_json(paths.stop_json, stop)
    elif mutation == "terminal_summary_schema":
        summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
        summary["obsolete_field"] = True
        atomic_write_json(paths.summary_json, summary)
    elif mutation == "terminal_control_mutability":
        immutable_stop = next(paths.syncer_epochs.glob("e*_*/terminal/stop_*.json"))
        immutable_stop.chmod(0o644)
    elif mutation == "archive_integrity":
        archive = run_root / "audit/batches/authority_history/checker-fixture.json"
        archive.chmod(0o644)
        archive.write_text('{"not":"a-current-audit-batch"}\n', encoding="utf-8")
        archive.chmod(0o444)
    elif mutation == "exact_workload":
        with sqlite3.connect(paths.sqlite_db) as connection:
            connection.execute(
                "UPDATE cycle_receipts SET processed_tokens_this_cycle=15, "
                "effective_tokens_this_cycle=15"
            )
            mutated_workload = connection.execute(
                "SELECT processed_tokens_this_cycle, effective_tokens_this_cycle "
                "FROM cycle_receipts"
            ).fetchone()
        assert mutated_workload == (15, 15)
    elif mutation == "normal_non_drop_fate":
        with sqlite3.connect(paths.sqlite_db) as connection:
            dropped_receipt = connection.execute(
                "SELECT cycle_receipt_id FROM updates WHERE status='dropped'"
            ).fetchone()
            assert dropped_receipt is not None
            connection.execute(
                "UPDATE token_fates SET direct_fate='quarantined', "
                "fate_reason='test mutation' WHERE receipt_id=?",
                (dropped_receipt[0],),
            )
            connection.execute(
                "UPDATE token_rollups SET direct_dropped=0, "
                "direct_quarantined_or_conflicted=16 WHERE singleton=1"
            )
    elif mutation == "unregistered_replacement":
        with sqlite3.connect(paths.sqlite_db) as connection:
            binding = connection.execute(
                "SELECT learner_id, logical_launch_id, attempt_id, bound_by_epoch, bound_at "
                "FROM static_contributor_bindings WHERE learner_id='learner_000'"
            ).fetchone()
            assert binding is not None
            connection.execute(
                "INSERT INTO static_binding_history "
                "(learner_id, binding_generation, logical_launch_id, attempt_id, "
                "final_status, bound_by_epoch, bound_at, finalized_by_epoch, finalized_at) "
                "VALUES (?, 1, ?, ?, 'replaced', ?, ?, ?, ?)",
                (*binding, 1, 200.0),
            )
            connection.execute(
                "UPDATE static_contributor_bindings SET binding_generation=2 "
                "WHERE learner_id='learner_000'"
            )
    elif mutation == "publication_identity":
        weight = run_root / "weights/epochs/e1/v1.safetensors"
        weight.chmod(0o644)
        with weight.open("ab") as handle:
            handle.write(b"corrupt")
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 1
    assert artifact["status"] == "FAIL"
    assert artifact["errors"]
    assert artifact["cleanup"]["eligible"] is False
    if mutation == "exact_workload":
        assert "at least one durable receipt has the wrong cycle workload" in artifact["errors"]
    if mutation == "normal_non_drop_fate":
        assert (
            "fault-free direct work is not exact applied-or-dropped adjudication"
            in artifact["errors"]
        )
    if mutation == "terminal_applied_total":
        assert "terminal direct applied tokens are not exact" in artifact["errors"]


def test_checker_parser_freezes_topology_workload_and_fault_oracles(tmp_path: Path) -> None:
    module = _checker_module()
    args = module.build_parser().parse_args(
        [
            "--gate",
            "F1-functional",
            "--experiment-id",
            "functional-normal",
            "--requirement-id",
            "FUNC-4L1S-01",
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "run"),
            "--log-root",
            str(tmp_path / "logs"),
            "--expected-global-steps",
            "4",
            "--expected-inner-steps",
            "20",
            "--expected-contributors",
            "4",
            "--expected-hosts",
            "5",
            "--expected-scheduler-jobs",
            "1",
            "--fault-scenario",
            "learner_replacement",
            "--syncer-takeover-boundary-version",
            "2",
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )

    assert vars(args) == {
        "gate": "F1-functional",
        "experiment_id": "functional-normal",
        "requirement_id": "FUNC-4L1S-01",
        "project_root": ROOT,
        "run_root": tmp_path / "run",
        "log_root": tmp_path / "logs",
        "expected_global_steps": 4,
        "expected_inner_steps": 20,
        "expected_contributors": 4,
        "expected_hosts": 5,
        "expected_scheduler_jobs": 1,
        "fault_scenario": "learner_replacement",
        "syncer_takeover_boundary_version": 2,
        "blocked_reason": None,
        "output": tmp_path / "evidence.json",
    }


def test_checker_classifies_missing_run_as_structured_blocked_artifact(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "invocation.log").write_text("missing run fixture\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--gate",
            "F1-functional",
            "--experiment-id",
            "functional-missing-run",
            "--requirement-id",
            "FUNC-4L1S-01",
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "missing"),
            "--log-root",
            str(log_root),
            "--expected-global-steps",
            "4",
            "--expected-inner-steps",
            "20",
            "--expected-contributors",
            "4",
            "--expected-hosts",
            "5",
            "--expected-scheduler-jobs",
            "1",
            "--fault-scenario",
            "none",
            "--syncer-takeover-boundary-version",
            "2",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert artifact["artifact_version"] == 1
    assert artifact["status"] == "BLOCKED"
    assert artifact["gate"] == "F1-functional"
    assert artifact["experiment_id"] == "functional-missing-run"
    assert artifact["requirements_covered"] == ["FUNC-4L1S-01"]
    assert artifact["source_identity"]["scopes"]
    assert artifact["environment"]["interpreter"]["executable"] == sys.executable
    assert artifact["cleanup"]["eligible"] is False
    assert artifact["evidence_paths"] == [str((log_root / "invocation.log").resolve())]
    assert len(artifact["errors"]) == 1


def test_checker_classifies_malformed_current_run_as_fail(tmp_path: Path) -> None:
    run_root, output, command, environment = _build_valid_checker_fixture(tmp_path)
    descriptor = run_root / "control/run_descriptor.json"
    descriptor.chmod(0o644)
    descriptor.write_text("not-json\n", encoding="utf-8")

    completed, artifact = _run_checker(command, environment, output)

    assert completed.returncode == 1
    assert artifact["status"] == "FAIL"
    assert artifact["errors"]
    assert artifact["cleanup"]["eligible"] is False


def test_checker_explicit_execution_block_publishes_current_schema(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--gate",
            "F1-functional",
            "--experiment-id",
            "allocation-aborted",
            "--requirement-id",
            "FUNC-4L1S-01",
            "--project-root",
            str(ROOT),
            "--run-root",
            str(tmp_path / "missing"),
            "--log-root",
            str(tmp_path / "logs"),
            "--expected-global-steps",
            "1",
            "--expected-inner-steps",
            "1",
            "--expected-contributors",
            "1",
            "--expected-hosts",
            "1",
            "--expected-scheduler-jobs",
            "1",
            "--fault-scenario",
            "none",
            "--syncer-takeover-boundary-version",
            "2",
            "--blocked-reason",
            "allocation actor exited before validation",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert artifact["status"] == "BLOCKED"
    assert artifact["errors"] == [
        "GatePrerequisiteUnavailable: allocation actor exited before validation"
    ]
    assert artifact["cleanup"] == {
        "owner": "full_protocol_harness",
        "eligible": False,
        "targets": [],
    }


def test_checker_verifies_publication_size_and_hash(tmp_path: Path) -> None:
    module = _checker_module()
    weight = tmp_path / "weights/global.safetensors"
    outer = tmp_path / "optim/global.safetensors"
    weight.parent.mkdir(parents=True)
    outer.parent.mkdir(parents=True)
    weight.write_bytes(b"weight")
    outer.write_bytes(b"outer")
    version = {
        "version": 0,
        "weight_relative_path": weight.relative_to(tmp_path).as_posix(),
        "weight_size": weight.stat().st_size,
        "weight_sha256": hashlib.sha256(b"weight").hexdigest(),
        "optim_relative_path": outer.relative_to(tmp_path).as_posix(),
        "optim_size": outer.stat().st_size,
        "optim_sha256": hashlib.sha256(b"outer").hexdigest(),
    }
    errors: list[str] = []

    evidence = module._verify_publication_objects(tmp_path, [version], errors)

    assert errors == []
    assert {item["status"] for item in evidence} == {"ok"}
    weight.write_bytes(b"changed")
    module._verify_publication_objects(tmp_path, [version], errors)
    assert errors == ["publication object identity mismatch: weights/global.safetensors"]


def test_checker_rejects_escaped_or_symlinked_publication_objects(tmp_path: Path) -> None:
    module = _checker_module()
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"outside")
    linked = tmp_path / "weights/linked.safetensors"
    linked.parent.mkdir()
    linked.symlink_to(outside)
    digest = hashlib.sha256(b"outside").hexdigest()
    versions = [
        {
            "version": 1,
            "weight_relative_path": "../outside.safetensors",
            "weight_size": 7,
            "weight_sha256": digest,
            "optim_relative_path": "weights/linked.safetensors",
            "optim_size": 7,
            "optim_sha256": digest,
        }
    ]
    errors: list[str] = []

    evidence = module._verify_publication_objects(tmp_path, versions, errors)

    assert {item["status"] for item in evidence} == {"invalid_path", "invalid_type"}
    assert len(errors) == 2


def test_checker_rejects_publication_below_symlinked_parent(tmp_path: Path) -> None:
    module = _checker_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "global.safetensors").write_bytes(b"weight")
    (tmp_path / "weights").symlink_to(outside, target_is_directory=True)
    digest = hashlib.sha256(b"weight").hexdigest()
    version = {
        "version": 1,
        "weight_relative_path": "weights/global.safetensors",
        "weight_size": 6,
        "weight_sha256": digest,
        "optim_relative_path": "weights/global.safetensors",
        "optim_size": 6,
        "optim_sha256": digest,
    }
    errors: list[str] = []

    evidence = module._verify_publication_objects(tmp_path, [version], errors)

    assert {item["status"] for item in evidence} == {"invalid_path"}
    assert len(errors) == 2


def test_gate_artifact_validator_rejects_self_proof(tmp_path: Path) -> None:
    module = _checker_module()
    output = tmp_path / "evidence.json"
    output.write_text("{}\n", encoding="utf-8")
    payload = {
        "artifact_version": 1,
        "status": "BLOCKED",
        "gate": "F1-functional",
        "experiment_id": "self-proof",
        "requirements_covered": ["FUNC-4L1S-01"],
        "fault_scenario": "none",
        "syncer_takeover_boundary_version": 2,
        "source_identity": None,
        "config_schema_identity": None,
        "protocol_schema_identity": None,
        "environment": {},
        "workload_identity": None,
        "metrics": {},
        "errors": ["missing run"],
        "evidence_paths": [str(output)],
        "cleanup": {"owner": "full_protocol_harness", "eligible": False, "targets": []},
    }

    with pytest.raises(RuntimeError, match="evidence path is invalid"):
        module.validate_gate_artifact(payload, output=output)


def test_token_balance_oracle_classifies_every_direct_fate() -> None:
    module = _checker_module()
    rollup = {
        "adjudicated_processed": 21,
        "local_discarded": 1,
        "direct_applied": 10,
        "direct_dropped": 2,
        "direct_quarantined_or_conflicted": 3,
        "direct_reported_unpublished": 4,
        "direct_outstanding": 1,
    }

    assert module._token_balance(rollup) == 0
    rollup["direct_applied"] = 9
    assert module._token_balance(rollup) == 1


@pytest.mark.parametrize(
    ("scenario", "states", "replaced_learner"),
    [
        ("none", ("released",), None),
        ("learner_replacement", ("released",), "learner_000"),
        ("syncer_takeover", ("expired", "released"), None),
    ],
)
def test_checker_registers_exact_fault_scenario(
    scenario: str,
    states: tuple[str, ...],
    replaced_learner: str | None,
) -> None:
    module = _checker_module()

    assert module._scenario_expectations(scenario) == (states, replaced_learner)


def test_checker_rejects_unregistered_fault_scenario() -> None:
    module = _checker_module()

    with pytest.raises(ValueError, match="unregistered"):
        module._scenario_expectations("unknown")


def test_actor_identity_shell_exports_exact_descriptor_bound_source(tmp_path: Path) -> None:
    identity = capture_source_identity(ROOT)
    descriptor = tmp_path / "run_descriptor.json"
    descriptor.write_text(
        json.dumps(
            {
                "resolved_config_path": str(tmp_path / "run_config.resolved.yaml"),
                "descriptor_sha256": "d" * 64,
                "git_commit": identity["git_commit"],
                "source_fingerprint": identity["source_fingerprint"],
                "git_dirty": identity["git_dirty"],
                "mode": "static",
            }
        ),
        encoding="utf-8",
    )
    script = ROOT / "scripts/miyabi/actor_identity.sh"
    probe = """
set -eu
source "$1"
prepare_actor_identity "$2" "$3" "$4"
printf '%s\n' \
  "$FS_DILOCO_RESOLVED_CONFIG" \
  "$FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256" \
  "$FS_DILOCO_EXPECTED_GIT_COMMIT" \
  "$FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT" \
  "$FS_DILOCO_EXPECTED_GIT_DIRTY" \
  "$FS_DILOCO_MEMBERSHIP_MODE" \
  "$FS_DILOCO_REQUIRE_SOURCE_IDENTITY"
"""

    completed = subprocess.run(
        ["bash", "-c", probe, "probe", str(script), str(descriptor), str(ROOT), sys.executable],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == [
        str(tmp_path / "run_config.resolved.yaml"),
        "d" * 64,
        identity["git_commit"],
        identity["source_fingerprint"],
        str(int(identity["git_dirty"])),
        "static",
        "1",
    ]


def test_pbs_scripts_bind_literal_group_minimum_walltime_and_one_current_runner() -> None:
    pbs_scripts = tuple(sorted((ROOT / "scripts/miyabi").glob("*.pbs")))
    assert pbs_scripts
    for path in pbs_scripts:
        source = path.read_text(encoding="utf-8")
        assert "#PBS -W group_list=xg24i002" in source
        assert "group_list=<" not in source
    wrapper = (ROOT / "scripts/miyabi/run_full_protocol.pbs").read_text(encoding="utf-8")
    assert "#PBS -l walltime=00:10:00" in wrapper
    assert all(
        f"${{{name}:?{name} is required}}" in wrapper
        for name in ("GATE", "EXPERIMENT_ID", "REQUIREMENT_ID")
    )
    assert wrapper.count("run_full_protocol_allocation.sh") == 1
    assert 'export FS_DILOCO_FAULT_SCENARIO="${FS_DILOCO_FAULT_SCENARIO:-none}"' in wrapper
    assert '--fault-scenario "$FS_DILOCO_FAULT_SCENARIO"' in wrapper
    assert "EXPECTED_SYNCER_EPOCHS" not in wrapper
    assert "EXPECTED_REPLACED_LEARNER" not in wrapper
    assert "readonly SYNCER_TAKEOVER_BOUNDARY_VERSION=2" in wrapper
    allocation = (ROOT / "scripts/miyabi/run_full_protocol_allocation.sh").read_text(
        encoding="utf-8"
    )
    assert '"RUN_ID=$RUN_ID"' in allocation
    assert "--map-by ppr:1:node" in allocation
    rank_runner = (ROOT / "scripts/miyabi/run_full_protocol_rank.sh").read_text(encoding="utf-8")
    assert "request_static_replacement" in rank_runner
    assert (
        'FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION="$SYNCER_TAKEOVER_BOUNDARY_VERSION"'
        in rank_runner
    )
    assert '--syncer-takeover-boundary-version "$SYNCER_TAKEOVER_BOUNDARY_VERSION"' in wrapper
    assert '"SYNCER_TAKEOVER_BOUNDARY_VERSION=$SYNCER_TAKEOVER_BOUNDARY_VERSION"' in allocation
    assert 'kill -KILL "$primary_pid"' in rank_runner
    assert "trap publish_blocked_on_exit EXIT" in wrapper
    assert "--blocked-reason" in wrapper

    independent_launcher = (
        ROOT / "scripts/miyabi/run_independent_launcher.pbs"
    ).read_text(encoding="utf-8")
    assert "fs_diloco.tools.launch_independent_run" in independent_launcher
    assert '--log-root "$LOG_ROOT"' in independent_launcher
    assert 'cp -- "$LAUNCH_RECEIPT" "$LOG_ROOT/init_run.json"' in independent_launcher
    independent_checker = (
        ROOT / "scripts/miyabi/check_independent_run.pbs"
    ).read_text(encoding="utf-8")
    assert '--expected-scheduler-jobs "$EXPECTED_SCHEDULER_JOBS"' in independent_checker
    assert "capture_source_identity.py" in independent_checker
    assert "trap publish_blocked_on_exit EXIT" in independent_checker

    validation_wrapper = (
        ROOT / "scripts/miyabi/run_validation_suite.pbs"
    ).read_text(encoding="utf-8")
    assert "run_validation_suite.py" in validation_wrapper
    assert '${VALIDATION_RAW_LOG:?VALIDATION_RAW_LOG is required}' in validation_wrapper
    assert '${VALIDATION_OUTPUT:?VALIDATION_OUTPUT is required}' in validation_wrapper

    review_runner = (ROOT / "scripts/miyabi/run_multi_agent_review.pbs").read_text(encoding="utf-8")
    assert review_runner.count("run_claude &") == 1
    assert review_runner.count("run_opencode \\") == 3
    assert 'CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-5}"' in review_runner
    assert 'OPENCODE_GLM_MODEL="${OPENCODE_GLM_MODEL:-opencode-go/glm-5.2}"' in review_runner
    assert (
        'OPENCODE_DEEPSEEK_MODEL="${OPENCODE_DEEPSEEK_MODEL:-opencode-go/deepseek-v4-flash}"'
        in review_runner
    )
    assert (
        'OPENCODE_MINIMAX_MODEL="${OPENCODE_MINIMAX_MODEL:-opencode-go/minimax-m3}"'
        in review_runner
    )
    assert "OPENCODE_KIMI_MODEL" not in review_runner
    assert '"$RUN_TMP/minimax-m3.tsv"' in review_runner


def test_pbs_wrapper_publishes_blocked_artifact_when_allocation_exits(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    scripts = project_root / "scripts/miyabi"
    scripts.mkdir(parents=True)
    (scripts / "run_full_protocol_allocation.sh").write_text(
        "#!/bin/bash\nexit 23\n",
        encoding="utf-8",
    )
    (scripts / "check_full_protocol_run.py").write_text(
        """#!/bin/bash
set -eu
output=
blocked_reason=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    --blocked-reason)
      blocked_reason="$2"
      shift 2
      ;;
    *)
      shift 2
      ;;
  esac
done
[[ -n "$output" && -n "$blocked_reason" ]]
printf '{"status":"BLOCKED","errors":["%s"]}\n' "$blocked_reason" >"$output"
""",
        encoding="utf-8",
    )
    command_bin = tmp_path / "bin"
    command_bin.mkdir()
    module = command_bin / "module"
    module.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    module.chmod(0o755)
    output = tmp_path / "evidence.json"
    environment = {
        **os.environ,
        "PATH": f"{command_bin}:{os.environ['PATH']}",
        "EVIDENCE_OUTPUT": str(output),
        "GATE": "F1-functional",
        "EXPERIMENT_ID": "allocation-exit-trap",
        "REQUIREMENT_ID": "FUNC-4L1S-01",
        "PROJECT_ROOT": str(project_root),
        "PRIMARY_WORKTREE_ROOT": str(tmp_path),
        "PYTHON_BIN": "/bin/bash",
        "RUN_ID": "allocation-exit-trap",
        "SHARED_ROOT": str(tmp_path / "run"),
        "LOG_ROOT": str(tmp_path / "logs"),
        "EXPECTED_NODES": "1",
        "NUM_LEARNERS": "1",
        "EXPECTED_INNER_STEPS": "1",
        "EXPECTED_GLOBAL_STEPS": "1",
        "FS_DILOCO_FAULT_SCENARIO": "none",
        "PBS_JOBID": "fixture.opbs",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/miyabi/run_full_protocol.pbs")],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
    )

    assert completed.returncode == 23
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "BLOCKED",
        "errors": ["Full Protocol execution exited before gate evidence publication (exit=23)"],
    }
