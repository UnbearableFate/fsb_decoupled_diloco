"""Exercise the current experiment04 scenario registry and durable authority oracle."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
from types import ModuleType

import pytest

from fs_diloco.storage.paths import RunPaths


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "do_experiments" / "experiment04" / "scenario_supervisor.py"


def _module() -> ModuleType:
    """Load the experiment supervisor as a standalone source-bound module."""

    specification = importlib.util.spec_from_file_location("plan05_scenario_supervisor", SUPERVISOR)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _fence(instance_id: str, stream_id: int) -> dict[str, object]:
    """Build the sole current contributor-fence wire shape for an oracle fixture."""

    return {
        "instance_id": instance_id,
        "placement_id": f"placement-{stream_id}",
        "placement_epoch": 1,
        "stream_id": stream_id,
        "stream_epoch": 1,
        "admission_generation": 1,
        "admission_token_sha256": f"{stream_id:x}" * 64,
    }


def _write_attestation(
    run_root: Path,
    *,
    actor_kind: str,
    actor_id: str,
    attempt_id: str,
    job_id: str,
    hostname: str,
) -> None:
    """Publish one immutable actor attestation with exact formal-run identity."""

    payload: dict[str, object] = {
        "format_version": 1,
        "run_id": "run-current",
        "descriptor_sha256": "d" * 64,
        "source_fingerprint": f"sha256:{'f' * 64}",
        "source_lock_sha256": "l" * 64,
        "model_identity": {
            "name_or_path": "gpt2",
            "revision": "607a30d783dfa663caf39e06633721c8d4cfcd7e",
        },
        "tokenizer_identity": {
            "name_or_path": "gpt2",
            "revision": "607a30d783dfa663caf39e06633721c8d4cfcd7e",
        },
        "dataset_identity": {
            "name": "Salesforce/wikitext",
            "config_name": "wikitext-2-raw-v1",
            "revision": "b08601e04326c79dfdd32d625aee71d232d685c3",
            "train_split": "train",
            "block_size": 1024,
        },
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "hostname": hostname,
        "pid": 1,
        "python_version": "3.13",
        "runtime_evidence": {
            "torch_version": "2.7.0",
            "cuda_runtime_version": "12.8",
            "gpu_driver_version": "570.86.15",
            "module_environment": [],
            "resource_allocation": {"pbs_job_id": f"{job_id}.opbs"},
        },
        "scheduler_job_id": f"{job_id}.opbs",
        "accelerator_identity": "0",
        "observed_at": 1.0,
    }
    payload["attestation_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    path = run_root / "metrics" / "attestations" / actor_kind / actor_id / f"{attempt_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o444)


def _finalized_authority(
    path: Path,
    *,
    hard_crash_stream: int | None = None,
    replacement_stream: int | None = None,
) -> None:
    """Create one finalized authority with exact receipt and token-ledger evidence."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE controller_state(singleton INTEGER, generation INTEGER, state TEXT);
        CREATE TABLE terminal_state(
            singleton INTEGER, state TEXT, final_version INTEGER,
            direct_weight_tokens_applied INTEGER
        );
        CREATE TABLE terminal_contributor_fences(
            generation INTEGER,
            stable_contributor_key TEXT,
            fence_json TEXT,
            state TEXT,
            final_cycle_seq INTEGER,
            hard_crash_gap_tokens_upper_bound INTEGER
        );
        CREATE TABLE cycle_receipts(
            receipt_id TEXT,
            receipt_sha256 TEXT,
            stable_contributor_key TEXT,
            cycle_seq INTEGER,
            previous_receipt_id TEXT,
            previous_receipt_sha256 TEXT,
            processed_tokens_this_cycle INTEGER,
            effective_tokens_this_cycle INTEGER,
            local_discarded_tokens_this_cycle INTEGER,
            retained_tokens_since_base INTEGER,
            data_cursor_start INTEGER,
            data_cursor_end INTEGER,
            proposal_expected INTEGER,
            planned_update_id TEXT,
            fence_json TEXT,
            ingested_at REAL
        );
        CREATE TABLE contributor_progress(
            stable_contributor_key TEXT,
            last_cycle_seq INTEGER,
            last_receipt_id TEXT,
            last_receipt_sha256 TEXT,
            data_cursor INTEGER
        );
        CREATE TABLE updates(
            update_id TEXT,
            cycle_receipt_id TEXT,
            cycle_receipt_sha256 TEXT,
            stable_contributor_key TEXT,
            cycle_seq INTEGER,
            status TEXT,
            applied_version INTEGER,
            inner_steps INTEGER,
            processed_tokens_this_cycle INTEGER,
            effective_tokens_this_update INTEGER,
            local_discarded_tokens_this_cycle INTEGER,
            data_cursor_start INTEGER,
            data_cursor_end INTEGER,
            fence_json TEXT,
            created_at REAL,
            ingested_at REAL
        );
        CREATE TABLE token_fates(
            receipt_id TEXT,
            local_discarded_tokens INTEGER,
            direct_weight_tokens INTEGER,
            direct_fate TEXT
        );
        CREATE TABLE token_rollups(
            singleton INTEGER,
            adjudicated_processed INTEGER,
            local_discarded INTEGER,
            direct_applied INTEGER,
            direct_dropped INTEGER,
            direct_quarantined_or_conflicted INTEGER,
            direct_reported_unpublished INTEGER,
            direct_outstanding INTEGER,
            carried_ancestry INTEGER
        );
        CREATE TABLE launch_requests(
            request_id TEXT,
            observation_key TEXT,
            bootstrap_slot INTEGER,
            role TEXT,
            reason TEXT,
            stream_id INTEGER,
            replace_instance_id TEXT,
            state TEXT,
            admitted_instance_id TEXT,
            pbs_job_id TEXT,
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE learner_instances(
            instance_id TEXT,
            registered_at REAL,
            admitted_at REAL,
            status TEXT,
            placement_id TEXT,
            placement_epoch INTEGER,
            stream_id INTEGER,
            stream_epoch INTEGER,
            admission_generation INTEGER,
            admission_token_sha256 TEXT
        );
        CREATE TABLE syncer_epochs(
            epoch INTEGER,
            pbs_job_id TEXT,
            final_state TEXT
        );
        CREATE TABLE global_versions(
            version INTEGER,
            predecessor_version INTEGER,
            publication_id TEXT,
            weight_relative_path TEXT,
            weight_size INTEGER,
            weight_sha256 TEXT,
            optim_relative_path TEXT,
            optim_size INTEGER,
            optim_sha256 TEXT,
            committed_by_epoch INTEGER,
            committed_by_owner_id TEXT,
            committed_at REAL,
            direct_weight_tokens_applied INTEGER
        );
        CREATE TABLE gc_candidates(relative_path TEXT, state TEXT);
        CREATE TABLE command_records(
            command_id TEXT,
            command_kind TEXT,
            result_json TEXT
        );
        CREATE TABLE capacity_observations(
            observation_key TEXT,
            observation_seq INTEGER,
            kind TEXT,
            action TEXT,
            desired_contributors INTEGER,
            productive_instances INTEGER,
            reserved_launch_capacity INTEGER
        );
        CREATE TABLE admission_history(
            admission_id INTEGER,
            instance_id TEXT,
            event TEXT
        );
        INSERT INTO controller_state VALUES(1, 1, 'finalized');
        INSERT INTO terminal_state VALUES(1, 'finalized', 10, 131072000);
        INSERT INTO syncer_epochs VALUES(1, '100.opbs', 'released');
        INSERT INTO token_rollups VALUES(
            1, 131072000, 0, 131072000, 0, 0, 0, 0, 0
        );
        """
    )
    receipt_sha256: dict[tuple[int, int], str] = {}
    for stream in range(8):
        crashed = stream == hard_crash_stream
        replaced = stream == replacement_stream
        terminal_instance = f"instance-{stream}-replacement" if replaced else f"instance-{stream}"
        terminal_fence = _fence(terminal_instance, stream)
        if replaced:
            terminal_fence["placement_id"] = f"placement-{stream}-replacement"
            terminal_fence["placement_epoch"] = 2
            terminal_fence["stream_epoch"] = 2
            terminal_fence["admission_generation"] = 2
            terminal_fence["admission_token_sha256"] = f"{stream + 1:x}" * 64
        connection.execute(
            "INSERT INTO terminal_contributor_fences VALUES(1, ?, ?, ?, ?, ?)",
            (
                str(stream),
                json.dumps(terminal_fence, sort_keys=True, separators=(",", ":")),
                "hard_crash" if crashed else "acked",
                None if crashed else 5,
                3_276_800 if crashed else 0,
            ),
        )
        connection.execute(
            "INSERT INTO learner_instances VALUES(?, ?, ?, ?, ?, 1, ?, 1, 1, ?)",
            (
                f"instance-{stream}",
                float(stream),
                float(stream),
                "expired" if crashed or replaced else "stopped",
                f"placement-{stream}",
                stream,
                f"{stream:x}" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO launch_requests VALUES(?, NULL, ?, 'bootstrap', "
            "'initial_bootstrap', ?, NULL, 'admitted', ?, ?, ?, ?)",
            (
                f"bootstrap-{stream}",
                stream,
                stream,
                f"instance-{stream}",
                f"{stream}.opbs",
                float(stream),
                float(stream),
            ),
        )
        connection.execute(
            "INSERT INTO admission_history VALUES(?, ?, 'admitted')",
            (stream + 1, f"instance-{stream}"),
        )
        if replaced:
            connection.execute(
                "INSERT INTO learner_instances VALUES(?, 104.0, 104.0, 'stopped', ?, 2, ?, 2, 2, ?)",
                (
                    terminal_instance,
                    f"placement-{stream}-replacement",
                    stream,
                    f"{stream + 1:x}" * 64,
                ),
            )
            connection.execute(
                "INSERT INTO capacity_observations VALUES("
                "'capacity-replacement', 1, 'scheduler_window', 'sufficient', 8, 8, 0)"
            )
            connection.execute(
                "INSERT INTO launch_requests VALUES("
                "'launch-replacement', 'capacity-replacement', NULL, 'replacement', "
                "'confirmed_scheduler_terminal_after_progress_stall', ?, ?, 'admitted', ?, "
                "'200.opbs', 103.0, 104.0)",
                (stream, f"instance-{stream}", terminal_instance),
            )
            connection.execute(
                "INSERT INTO command_records VALUES(?, 'transition_launch_request', ?)",
                (
                    "submitted-replacement",
                    json.dumps(
                        {
                            "request_id": "launch-replacement",
                            "state": "submitted",
                            "evidence_source": "qsub_receipt",
                            "pbs_job_id": "200",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO admission_history VALUES(?, ?, ?)",
                (
                    (100, f"instance-{stream}", "expired"),
                    (101, terminal_instance, "admitted"),
                    (102, terminal_instance, "stopped"),
                ),
            )
    paths = RunPaths(path.parent)
    for version in range(11):
        publication_id = f"00000000-0000-0000-0000-{version:012d}"
        weight_path = paths.epoch_weight_path(1, "syncer-1", version, publication_id)
        optim_path = paths.epoch_outer_optim_path(1, "syncer-1", version, publication_id)
        weight_payload = f"weight-{version}".encode("utf-8")
        optim_payload = f"outer-state-{version}".encode("utf-8")
        for object_path, payload in (
            (weight_path, weight_payload),
            (optim_path, optim_payload),
        ):
            object_path.parent.mkdir(parents=True, exist_ok=True)
            object_path.write_bytes(payload)
            object_path.chmod(0o444)
        connection.execute(
            "INSERT INTO global_versions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                version,
                None if version == 0 else version - 1,
                publication_id,
                weight_path.relative_to(path.parent).as_posix(),
                len(weight_payload),
                hashlib.sha256(weight_payload).hexdigest(),
                optim_path.relative_to(path.parent).as_posix(),
                len(optim_payload),
                hashlib.sha256(optim_payload).hexdigest(),
                "syncer-1",
                100.0 + version,
                0 if version == 0 else 13_107_200,
            ),
        )
    for version in range(1, 11):
        for contributor in range(4):
            stream = (version * 4 + contributor) % 8
            cycle_seq = (version + 1) // 2
            instance_id = f"instance-{stream}"
            fence = _fence(instance_id, stream)
            if stream == replacement_stream and cycle_seq >= 3:
                fence = _fence(f"instance-{stream}-replacement", stream)
                fence["placement_id"] = f"placement-{stream}-replacement"
                fence["placement_epoch"] = 2
                fence["stream_epoch"] = 2
                fence["admission_generation"] = 2
                fence["admission_token_sha256"] = f"{stream + 1:x}" * 64
            fence_json = json.dumps(fence, sort_keys=True, separators=(",", ":"))
            receipt_id = f"receipt-{stream}-{cycle_seq}"
            digest = f"{stream:x}{cycle_seq:x}".ljust(64, "0")
            receipt_sha256[(stream, cycle_seq)] = digest
            previous_id = None if cycle_seq == 1 else f"receipt-{stream}-{cycle_seq - 1}"
            previous_sha = None if cycle_seq == 1 else receipt_sha256[(stream, cycle_seq - 1)]
            cursor_start = (cycle_seq - 1) * 1600
            update_id = f"update-{version}-{contributor}"
            ingested_at = 100.0 + version
            connection.execute(
                "INSERT INTO cycle_receipts VALUES(?, ?, ?, ?, ?, ?, 3276800, 3276800, "
                "0, 3276800, ?, ?, 1, ?, ?, ?)",
                (
                    receipt_id,
                    digest,
                    str(stream),
                    cycle_seq,
                    previous_id,
                    previous_sha,
                    cursor_start,
                    cursor_start + 1600,
                    update_id,
                    fence_json,
                    ingested_at,
                ),
            )
            connection.execute(
                "INSERT INTO updates VALUES(?, ?, ?, ?, ?, 'applied', ?, 200, 3276800, "
                "3276800, 0, ?, ?, ?, ?, ?)",
                (
                    update_id,
                    receipt_id,
                    digest,
                    str(stream),
                    cycle_seq,
                    version,
                    cursor_start,
                    cursor_start + 1600,
                    fence_json,
                    ingested_at,
                    ingested_at,
                ),
            )
            connection.execute(
                "INSERT INTO token_fates VALUES(?, 0, 3276800, 'applied')",
                (receipt_id,),
            )
    if hard_crash_stream is not None:
        stream = hard_crash_stream
        receipt_id = f"receipt-{stream}-6"
        digest = f"{stream:x}6".ljust(64, "0")
        fence_json = json.dumps(
            _fence(f"instance-{stream}", stream), sort_keys=True, separators=(",", ":")
        )
        receipt_sha256[(stream, 6)] = digest
        connection.execute(
            "INSERT INTO cycle_receipts VALUES(?, ?, ?, 6, ?, ?, 3276800, 3276800, "
            "0, 3276800, 8000, 9600, 1, ?, ?, 111.0)",
            (
                receipt_id,
                digest,
                str(stream),
                f"receipt-{stream}-5",
                receipt_sha256[(stream, 5)],
                f"orphan-update-{stream}",
                fence_json,
            ),
        )
        connection.execute(
            "INSERT INTO token_fates VALUES(?, 0, 3276800, 'dropped')", (receipt_id,)
        )
        connection.execute(
            "UPDATE token_rollups SET adjudicated_processed=adjudicated_processed+3276800, "
            "direct_dropped=direct_dropped+3276800"
        )
    for stream in range(8):
        final_cycle = 6 if stream == hard_crash_stream else 5
        connection.execute(
            "INSERT INTO contributor_progress VALUES(?, ?, ?, ?, ?)",
            (
                str(stream),
                final_cycle,
                f"receipt-{stream}-{final_cycle}",
                receipt_sha256[(stream, final_cycle)],
                final_cycle * 1600,
            ),
        )
    connection.commit()
    connection.close()


def test_registered_scenarios_are_the_exact_plan05_fault_matrix() -> None:
    """Formal orchestration exposes only fixed control and authorized replacement scenarios."""

    module = _module()

    assert set(module.SCENARIOS) == {
        "no_failure",
        "failure_no_replacement",
        "failure_authorized_replacement",
    }
    assert module.SCENARIOS["no_failure"].inject_learner_failure is False
    assert module.SCENARIOS["failure_no_replacement"].scaling_enabled is False
    assert module.SCENARIOS["failure_authorized_replacement"].scaling_enabled is True
    assert module.SCENARIOS["failure_authorized_replacement"].fault_delay == 60.0


def test_formal_configs_differ_only_in_capacity_policy_and_keep_200_by_10_workload() -> None:
    """Fixed and replacement runs retain identical model, data, quorum, and workload identity."""

    from dataclasses import asdict

    from fs_diloco.core.config import load_config

    module = _module()
    scaling = load_config(ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10.yaml")
    fixed = load_config(ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml")

    assert scaling.scaling.enabled is True
    assert fixed.scaling.enabled is False
    assert scaling.training.inner_steps == fixed.training.inner_steps == 200
    assert scaling.sync.stop_after_outer_steps == fixed.sync.stop_after_outer_steps == 10
    assert scaling.sync.quorum_min == scaling.sync.quorum_max == 4
    assert fixed.sync.quorum_min == fixed.sync.quorum_max == 4
    assert scaling.membership.stream_pool_size == fixed.membership.stream_pool_size == 8
    assert scaling.scaling.desired_contributors == 8
    assert scaling.scaling.low_contributor_threshold == 7
    assert asdict(scaling.model) == asdict(fixed.model)
    assert asdict(scaling.data) == asdict(fixed.data)
    assert module._formal_workload_contract(scaling)["seed"] == 1337
    scaling.training.seed = 42
    with pytest.raises(RuntimeError, match="workload contract"):
        module._formal_workload_contract(scaling)


def test_qsub_output_replacement_and_victim_selection_are_exact() -> None:
    """Actor command rewriting and reproducible victim selection preserve all other IDs."""

    module = _module()
    command = ["qsub", "-q", "debug-g", "-o", "/old.log", "actor.pbs"]
    admissions = [
        {"stream_id": stream, "instance_id": f"instance-{stream}", "pbs_job_id": f"{stream}.opbs"}
        for stream in range(8)
    ]

    replaced = module._replace_output_path(command, Path("/new.log"))

    assert replaced == ["qsub", "-q", "debug-g", "-o", "/new.log", "actor.pbs"]
    assert command[4] == "/old.log"
    assert module._choose_learner_victim("run-fixed", admissions) == module._choose_learner_victim(
        "run-fixed", list(reversed(admissions))
    )


def test_replacement_topology_binds_the_admitted_instance_attestation(tmp_path: Path) -> None:
    """Independent topology includes the exact replacement instance and scheduler job."""

    module = _module()
    jobs = [str(index) for index in range(8)]
    config = module.load_config(ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml")
    attestations = [
        ("learner", f"instance-{index}", f"instance-{index}", str(index), f"host-{index}")
        for index in range(8)
    ]
    attestations.extend(
        (
            ("syncer", "syncer-1", "syncer-attempt", "100", "host-8"),
            (
                "learner",
                "instance-7-replacement",
                "instance-7-replacement",
                "200",
                "host-7",
            ),
        )
    )
    for actor_kind, actor_id, attempt_id, job_id, hostname in attestations:
        _write_attestation(
            tmp_path,
            actor_kind=actor_kind,
            actor_id=actor_id,
            attempt_id=attempt_id,
            job_id=job_id,
            hostname=hostname,
        )

    topology = module._attestation_topology(
        tmp_path,
        jobs,
        "100.opbs",
        config=config,
        run_id="run-current",
        descriptor_sha256="d" * 64,
        source_fingerprint=f"sha256:{'f' * 64}",
        source_lock_sha256="l" * 64,
        initial_instance_by_job={str(index): f"instance-{index}" for index in range(8)},
        syncer_owner_id="syncer-1",
        replacement_job_id="200.opbs",
        replacement_instance_id="instance-7-replacement",
    )

    assert len(topology["initial_distinct_hosts"]) == 9
    assert topology["replacement_actor"]["actor_id"] == "instance-7-replacement"

    # One scheduler allocation cannot attest two independent actor identities.
    _write_attestation(
        tmp_path,
        actor_kind="learner",
        actor_id="duplicate-replacement",
        attempt_id="duplicate-replacement",
        job_id="200",
        hostname="host-7",
    )
    with pytest.raises(RuntimeError, match="multiple actor attestations"):
        module._attestation_topology(
            tmp_path,
            jobs,
            "100.opbs",
            config=config,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            source_fingerprint=f"sha256:{'f' * 64}",
            source_lock_sha256="l" * 64,
            initial_instance_by_job={str(index): f"instance-{index}" for index in range(8)},
            syncer_owner_id="syncer-1",
            replacement_job_id="200.opbs",
            replacement_instance_id="instance-7-replacement",
        )


def test_no_failure_authority_oracle_requires_ten_exact_four_way_merges(tmp_path: Path) -> None:
    """The healthy oracle rejects any global version lacking four 200-step proposals."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database)

    evidence = module._final_authority_evidence(
        database,
        run_root=tmp_path,
        config=module.load_config(ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml"),
        scenario=module.SCENARIOS["no_failure"],
        first_syncer_job_id="100.opbs",
        victim=None,
        replacement=None,
    )
    assert evidence["integrity_check"] == ["ok"]
    assert len(evidence["merge_counts"]) == 10
    assert len(evidence["bootstrap_launches"]) == 8
    assert len(evidence["publication_objects"]) == 22

    connection = sqlite3.connect(database)
    connection.execute("UPDATE updates SET inner_steps=199 WHERE update_id='update-10-3'")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="ten exact"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            config=module.load_config(
                ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml"
            ),
            scenario=module.SCENARIOS["no_failure"],
            first_syncer_job_id="100.opbs",
            victim=None,
            replacement=None,
        )


def test_authority_oracle_rejects_token_rollup_drift(tmp_path: Path) -> None:
    """Formal success requires receipt-level token fates to equal the terminal rollup."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE token_rollups SET direct_applied=direct_applied-1")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="token rollup"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            config=module.load_config(
                ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml"
            ),
            scenario=module.SCENARIOS["no_failure"],
            first_syncer_job_id="100.opbs",
            victim=None,
            replacement=None,
        )


def test_authority_oracle_rejects_checkpoint_identity_drift(tmp_path: Path) -> None:
    """Formal success requires every retained checkpoint to match its durable identity."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE global_versions SET weight_sha256=? WHERE version=10",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="publication object identity"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            config=module.load_config(
                ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml"
            ),
            scenario=module.SCENARIOS["no_failure"],
            first_syncer_job_id="100.opbs",
            victim=None,
            replacement=None,
        )


def test_checkpoint_oracle_accepts_only_completed_archive_gc(tmp_path: Path) -> None:
    """Archived checkpoint absence is valid only after its durable GC claim is complete."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    versions = [
        dict(row) for row in connection.execute("SELECT * FROM global_versions ORDER BY version")
    ]
    connection.close()
    version_zero = versions[0]
    missing_paths = (
        str(version_zero["weight_relative_path"]),
        str(version_zero["optim_relative_path"]),
    )
    for relative_path in missing_paths:
        (tmp_path / relative_path).unlink()

    evidence = module._publication_object_evidence(
        tmp_path,
        versions,
        hot_versions=set(range(1, 11)),
        gc_candidates={},
    )

    collected = [item for item in evidence if item["availability"] == "garbage_collected"]
    assert {item["relative_path"] for item in collected} == set(missing_paths)
    assert all(item["authority_location"] == "archive" for item in collected)
    with pytest.raises(RuntimeError, match="before GC completion"):
        module._publication_object_evidence(
            tmp_path,
            versions,
            hot_versions=set(range(1, 11)),
            gc_candidates={missing_paths[0]: "pending"},
        )


def test_fixed_failure_oracle_requires_one_hard_crash_and_no_launch(tmp_path: Path) -> None:
    """A fixed-capacity learner loss closes with one bounded gap and no launch request."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database, hard_crash_stream=7)
    victim = {
        "instance_id": "instance-7",
        "fault_requested_at": 150.0,
    }

    evidence = module._final_authority_evidence(
        database,
        run_root=tmp_path,
        config=module.load_config(ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10_fixed.yaml"),
        scenario=module.SCENARIOS["failure_no_replacement"],
        first_syncer_job_id="100.opbs",
        victim=victim,
        replacement=None,
    )

    assert evidence["launch_requests"] == []
    assert evidence["replacement_boundary"]["late_created_update_ids"] == []
    assert evidence["token_accounting"]["receipt_count"] == 41
    assert evidence["token_accounting"]["update_count"] == 40
    assert evidence["token_accounting"]["rollup"]["direct_dropped"] == 3_276_800


def test_authorized_replacement_oracle_requires_capacity_qsub_and_cursor_continuity(
    tmp_path: Path,
) -> None:
    """Replacement evidence binds capacity, qsub, fence order, and resumed stream cursor."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database, replacement_stream=7)
    config = module.load_config(ROOT / "configs/experiments/gpt2_wikitext2_8l_200x10.yaml")
    victim = {"instance_id": "instance-7", "fault_requested_at": 103.0}
    replacement = {
        "request_id": "launch-replacement",
        "admitted_instance_id": "instance-7-replacement",
        "pbs_job_id": "200.opbs",
    }

    evidence = module._final_authority_evidence(
        database,
        run_root=tmp_path,
        config=config,
        scenario=module.SCENARIOS["failure_authorized_replacement"],
        first_syncer_job_id="100.opbs",
        victim=victim,
        replacement=replacement,
    )

    boundary = evidence["replacement_boundary"]
    assert boundary["capacity_observation"]["observation_key"] == "capacity-replacement"
    assert boundary["qsub_receipt_transition"]["pbs_job_id"] == "200"
    assert boundary["last_old_receipt"]["cycle_seq"] == 2
    assert boundary["first_new_receipt"]["cycle_seq"] == 3
    assert boundary["late_old_receipt_ids"] == []
    assert boundary["late_old_update_ids"] == []

    connection = sqlite3.connect(database)
    connection.execute("UPDATE cycle_receipts SET ingested_at=200.0 WHERE receipt_id='receipt-7-2'")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="later old-fence effect"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            config=config,
            scenario=module.SCENARIOS["failure_authorized_replacement"],
            first_syncer_job_id="100.opbs",
            victim=victim,
            replacement=replacement,
        )

    # The capacity classification must agree with the exact durable counter snapshot.
    connection = sqlite3.connect(database)
    connection.execute("UPDATE cycle_receipts SET ingested_at=103.0 WHERE receipt_id='receipt-7-2'")
    connection.execute(
        "UPDATE capacity_observations SET action='low' WHERE observation_key='capacity-replacement'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="scheduler-confirmed capacity observation"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            config=config,
            scenario=module.SCENARIOS["failure_authorized_replacement"],
            first_syncer_job_id="100.opbs",
            victim=victim,
            replacement=replacement,
        )
