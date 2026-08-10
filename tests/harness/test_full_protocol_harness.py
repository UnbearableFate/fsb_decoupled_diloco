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
from fs_diloco.storage.atomic_io import atomic_write_json, publish_immutable_bytes
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority
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
) -> tuple[Path, Path, list[str], dict[str, str]]:
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
    config.sync.stop_after_outer_steps = 1
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
    update_id = "00000000-0000-4000-8000-000000000001"
    receipt_payload = {
        "cycle_receipt_format_version": 1,
        "run_id": descriptor["run_id"],
        "stable_contributor_key": "learner_000",
        "cycle_seq": 1,
        "cycle_id": "10000000-0000-4000-8000-000000000001",
        "receipt_id": "receipt-learner_000-1",
        "previous_receipt_id": None,
        "previous_receipt_sha256": None,
        "processed_tokens_this_cycle": 16,
        "effective_tokens_this_cycle": 16,
        "local_discarded_tokens_this_cycle": 0,
        "retained_tokens_since_base": 16,
        "data_cursor_start": 0,
        "data_cursor_end": 1,
        "proposal_expected": True,
        "planned_update_id": update_id,
        "planned_payload_sha256": PAYLOAD_DIGEST,
        "contributor_fence": {},
        "created_at": 101.0,
    }
    with LeaderAuthority(
        loaded.paths.sqlite_db,
        identity,
        scope,
        run_root=run_root,
    ) as authority:
        token = authority.acquire_leader(
            owner_id="checker-syncer", hostname=socket.gethostname(), pid=os.getpid()
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
        receipt_payload["contributor_fence"] = fence.as_dict()
        receipt = CycleReceiptV1.from_dict(receipt_payload)
        leader.initialize_genesis(
            command_id="genesis",
            publication_id="publication-0",
            **publish_checkpoint_pair(run_root, version=0),
        )
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        proposal = FullUpdateProposalV2.from_dict(
            {
                "proposal_format_version": 2,
                "run_id": descriptor["run_id"],
                "stable_contributor_key": "learner_000",
                "cycle_seq": 1,
                "cycle_id": receipt.cycle_id,
                "update_id": update_id,
                "cycle_receipt_id": receipt.receipt_id,
                "cycle_receipt_sha256": receipt.immutable_sha256(),
                "base_global_version": 0,
                "local_step_start": 0,
                "local_step_end": 1,
                "inner_steps": 1,
                "processed_tokens_this_cycle": 16,
                "effective_tokens_this_update": 16,
                "local_discarded_tokens_this_cycle": 0,
                "retained_tokens_since_base": 16,
                "data_cursor_start": 0,
                "data_cursor_end": 1,
                "contributor_fence": fence.as_dict(),
                "payload_relative_path": canonical_update_relative_path(
                    "learner_000", update_id
                ),
                "payload_size": len(DEFAULT_PAYLOAD),
                "payload_sha256": PAYLOAD_DIGEST,
                "tensor_schema_sha256": SCHEMA_DIGEST,
                "tensor_dtype": "float32",
                "tensor_numel": 1,
                "created_at": 101.0,
            }
        )
        publish_proposal_payload(run_root, proposal)
        leader.ingest_proposal(command_id="proposal-1", proposal=proposal)
        selected = leader.try_select_batch(
            command_id="select-1", quorum_min=1, quorum_max=1
        )
        assert selected.batch is not None
        leader.prepare_publication(
            command_id="prepare-1",
            publication_id="publication-1",
            target_version=1,
            selection_batch_id=selected.batch.batch_id,
            **publish_checkpoint_pair(run_root, version=1),
        )
        leader.commit_merge(command_id="commit-1", publication_id="publication-1")
        leader.begin_terminal_close(command_id="close", reason="fixture complete")
        leader.acknowledge_terminal_contributor(
            command_id="ack-learner",
            fence=fence,
            final_cycle_seq=1,
            final_update_id=update_id,
        )
        leader.finalize_terminal(command_id="finalize", reason="fixture complete")
        authority.release_leader(token)

    stop = {"state": "finalized", "generation": 1, "final_version": 1}
    atomic_write_json(loaded.paths.stop_json, stop)
    owner_short = hashlib.sha256(b"checker-syncer").hexdigest()[:12]
    immutable_stop = (
        loaded.paths.syncer_epochs
        / f"e{token.epoch:06d}_{owner_short}"
        / "terminal/stop_g000001.json"
    )
    publish_immutable_bytes(
        immutable_stop,
        json.dumps(stop, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
    )
    atomic_write_json(
        loaded.paths.summary_json,
        {"state": "finalized", "final_version": 1, "all_learners_stopped": True},
    )

    log_root.mkdir()
    atomic_write_json(log_root / "source_identity.json", capture_source_identity(ROOT))
    atomic_write_json(log_root / "init_run.json", {"run_id": descriptor["run_id"]})
    atomic_write_json(log_root / "summary.json", {"final_version": 1})
    runtime_evidence = {
        "torch_version": "fixture",
        "cuda_runtime_version": None,
        "gpu_driver_version": None,
        "module_environment": [],
        "resource_allocation": {"nodes": 1},
    }
    scheduler_job_id = "fixture.opbs"
    write_actor_attestation(
        loaded,
        actor_kind="learner",
        actor_id="learner_000",
        attempt_id="fixture-attempt",
        runtime_evidence=runtime_evidence,
        scheduler_job_id=scheduler_job_id,
    )
    write_actor_attestation(
        loaded,
        actor_kind="syncer",
        actor_id="checker-syncer",
        attempt_id="syncer-attempt",
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
        "1",
        "--expected-inner-steps",
        "1",
        "--expected-contributors",
        "1",
        "--expected-hosts",
        "1",
        "--output",
        str(output),
    ]
    environment = {**os.environ, "PBS_JOBID": scheduler_job_id, "PBS_NODEFILE": str(nodefile)}
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


def test_aggregate_checker_accepts_one_valid_current_terminal_run(tmp_path: Path) -> None:
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
        "processed_tokens": 16,
        "direct_weight_tokens_applied": 16,
        "cursor_terminal": {"learner_000": 1},
    }
    assert artifact["authority"]["integrity"] == ["ok"]
    assert [row["final_state"] for row in artifact["authority"]["epochs"]] == [
        "released"
    ]
    assert artifact["metrics"]["token_balance"] == 0
    assert artifact["metrics"]["publication_object_count"] == 4
    assert artifact["topology"]["learner_attestation_count"] == 1
    assert artifact["topology"]["syncer_attestation_count"] == 1
    assert artifact["cleanup"] == {
        "owner": "full_protocol_harness",
        "eligible": True,
        "targets": [str(run_root)],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "source_identity",
        "resolved_config",
        "epoch_lifecycle",
        "attested_topology",
        "terminal_authority",
        "archive_integrity",
        "exact_workload",
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
    elif mutation == "archive_integrity":
        archive = run_root / "audit/batches/authority_history/corrupt.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text('{"not":"a-current-audit-batch"}\n', encoding="utf-8")
        archive.chmod(0o444)
    elif mutation == "exact_workload":
        with sqlite3.connect(paths.sqlite_db) as connection:
            connection.execute(
                "UPDATE cycle_receipts SET processed_tokens_this_cycle=15"
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
            "--expected-syncer-epochs",
            "2",
            "--expected-replaced-learner",
            "learner_000",
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
        "expected_syncer_epochs": 2,
        "expected_replaced_learner": "learner_000",
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
    run_root = tmp_path / "run"
    log_root = tmp_path / "logs"
    (run_root / "control").mkdir(parents=True)
    log_root.mkdir()
    (run_root / "control/run_descriptor.json").write_text(
        "not-json\n", encoding="utf-8"
    )
    output = tmp_path / "evidence.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--gate",
            "F1-functional",
            "--experiment-id",
            "functional-malformed-run",
            "--requirement-id",
            "FUNC-4L1S-01",
            "--project-root",
            str(ROOT),
            "--run-root",
            str(run_root),
            "--log-root",
            str(log_root),
            "--expected-global-steps",
            "1",
            "--expected-inner-steps",
            "1",
            "--expected-contributors",
            "1",
            "--expected-hosts",
            "1",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert artifact["status"] == "FAIL"
    assert any("JSONDecodeError" in error for error in artifact["errors"])
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
    ("count", "states"),
    [(1, ("released",)), (2, ("expired", "released"))],
)
def test_checker_registers_exact_epoch_lifecycle(
    count: int, states: tuple[str, ...]
) -> None:
    module = _checker_module()

    assert module._registered_epoch_states(count) == states


def test_checker_rejects_unregistered_epoch_count() -> None:
    module = _checker_module()

    with pytest.raises(ValueError, match="one or two"):
        module._registered_epoch_states(3)


def test_pbs_scripts_bind_literal_group_minimum_walltime_and_one_current_runner() -> None:
    pbs_scripts = tuple(sorted((ROOT / "scripts/miyabi").glob("*.pbs")))
    assert pbs_scripts
    for path in pbs_scripts:
        source = path.read_text(encoding="utf-8")
        assert "#PBS -W group_list=xg24i002" in source
        assert "group_list=<" not in source
    wrapper = (ROOT / "scripts/miyabi/run_full_protocol.pbs").read_text(encoding="utf-8")
    assert "#PBS -l walltime=00:15:00" in wrapper
    assert all(
        f'${{{name}:?{name} is required}}' in wrapper
        for name in ("GATE", "EXPERIMENT_ID", "REQUIREMENT_ID")
    )
    assert wrapper.count("run_full_protocol_allocation.sh") == 1
    allocation = (ROOT / "scripts/miyabi/run_full_protocol_allocation.sh").read_text(
        encoding="utf-8"
    )
    assert '"RUN_ID=$RUN_ID"' in allocation
    assert "--map-by ppr:1:node" in allocation
    rank_runner = (ROOT / "scripts/miyabi/run_full_protocol_rank.sh").read_text(
        encoding="utf-8"
    )
    assert "request_static_replacement" in rank_runner
    assert "FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION=2" in rank_runner
    assert "kill -KILL \"$primary_pid\"" in rank_runner
    assert "trap publish_blocked_on_exit EXIT" in wrapper
    assert "--blocked-reason" in wrapper
