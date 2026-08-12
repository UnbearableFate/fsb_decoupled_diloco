"""Exercise plan04's current seven-scenario experiment package and oracle."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from fs_diloco.core.config import load_config
from torch_ddp_baselines.config import load_config as load_baseline_config


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "do_experiments/full_protocol/experiment04"
SUPERVISOR = PACKAGE / "scenario_supervisor.py"


def _module() -> ModuleType:
    """Load the experiment supervisor as one standalone source-bound module."""

    specification = importlib.util.spec_from_file_location("plan04_scenario_supervisor", SUPERVISOR)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _fence(instance_id: str, stream_id: int, *, epoch: int = 1) -> dict[str, object]:
    """Build one current contributor fence for a terminal-authority fixture."""

    return {
        "instance_id": instance_id,
        "placement_id": f"placement-{stream_id}-e{epoch}",
        "placement_epoch": epoch,
        "stream_id": stream_id,
        "stream_epoch": epoch,
        "admission_generation": epoch,
        "admission_token_sha256": f"{stream_id + epoch:x}" * 64,
    }


def _write_attestation(
    run_root: Path,
    *,
    actor_kind: str,
    actor_id: str,
    job_id: str,
) -> None:
    """Publish one immutable descriptor-bound actor attestation fixture."""

    payload: dict[str, object] = {
        "run_id": "run-current",
        "descriptor_sha256": "d" * 64,
        "source_fingerprint": f"sha256:{'f' * 64}",
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "attempt_id": actor_id,
        "scheduler_job_id": f"{job_id}.opbs",
        "hostname": f"host-{job_id}",
    }
    payload["attestation_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    path = run_root / "metrics/attestations" / actor_kind / actor_id / f"{actor_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o444)


def _write_authority(
    run_root: Path,
    *,
    replacement: bool = False,
    takeover: bool = False,
    admitted_streams: int = 8,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Create a minimal finalized authority that reaches the plan04 oracle."""

    control = run_root / "control"
    control.mkdir(parents=True)
    (control / "run_descriptor.json").write_text(
        json.dumps(
            {
                "run_id": "run-current",
                "descriptor_sha256": "d" * 64,
                "source_fingerprint": f"sha256:{'f' * 64}",
            }
        ),
        encoding="utf-8",
    )
    database = control / "syncer_metadata.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE controller_state(singleton INTEGER, generation INTEGER, state TEXT);
        CREATE TABLE terminal_state(singleton INTEGER, state TEXT, final_version INTEGER);
        CREATE TABLE syncer_epochs(
            epoch INTEGER, owner_id TEXT, pbs_job_id TEXT, final_state TEXT,
            superseded_by_epoch INTEGER
        );
        CREATE TABLE learner_instances(
            instance_id TEXT, pbs_job_id TEXT, status TEXT, stream_id INTEGER,
            stream_epoch INTEGER, placement_epoch INTEGER, registered_at REAL
        );
        CREATE TABLE launch_requests(
            request_id TEXT, role TEXT, bootstrap_slot INTEGER, created_at REAL,
            replace_instance_id TEXT, reason TEXT, state TEXT, pbs_job_id TEXT,
            admitted_instance_id TEXT
        );
        CREATE TABLE terminal_contributor_fences(
            generation INTEGER, stable_contributor_key TEXT, fence_json TEXT, state TEXT
        );
        CREATE TABLE global_versions(
            version INTEGER, committed_by_epoch INTEGER, weight_relative_path TEXT
        );
        CREATE TABLE contributor_progress(stable_contributor_key TEXT);
        CREATE TABLE capacity_observations(observation_seq INTEGER);
        CREATE TABLE updates(
            update_id TEXT, inner_steps INTEGER, status TEXT, applied_version INTEGER
        );
        INSERT INTO controller_state VALUES(1, 1, 'finalized');
        INSERT INTO terminal_state VALUES(1, 'finalized', 25);
        """
    )
    if takeover:
        connection.executemany(
            "INSERT INTO syncer_epochs VALUES(?, ?, ?, ?, ?)",
            (
                (1, "syncer-primary", "100.opbs", "expired", 2),
                (2, "syncer-successor", "200.opbs", "released", None),
            ),
        )
    else:
        connection.execute(
            "INSERT INTO syncer_epochs VALUES(1, 'syncer-primary', '100.opbs', 'released', NULL)"
        )

    victim: dict[str, object] | None = None
    successor: dict[str, object] | None = None
    for stream in range(admitted_streams):
        initial_id = f"instance-{stream}"
        terminal_id = "instance-7-replacement" if replacement and stream == 7 else initial_id
        connection.execute(
            "INSERT INTO learner_instances VALUES(?, ?, ?, ?, 1, 1, ?)",
            (
                initial_id,
                f"{stream}.opbs",
                "expired" if replacement and stream == 7 else "stopped",
                stream,
                float(stream),
            ),
        )
        connection.execute(
            "INSERT INTO launch_requests VALUES(?, 'bootstrap', ?, ?, NULL, "
            "'initial_bootstrap', 'admitted', ?, ?)",
            (
                f"bootstrap-{stream}",
                stream,
                float(stream),
                f"{stream}.opbs",
                initial_id,
            ),
        )
        terminal_fence = _fence(terminal_id, stream, epoch=2 if terminal_id != initial_id else 1)
        connection.execute(
            "INSERT INTO terminal_contributor_fences VALUES(1, ?, ?, 'acked')",
            (str(stream), json.dumps(terminal_fence)),
        )
        # A learner admitted near version 10 may acknowledge terminal before completing a cycle.
        if stream != 5:
            connection.execute("INSERT INTO contributor_progress VALUES(?)", (str(stream),))
        _write_attestation(
            run_root,
            actor_kind="learner",
            actor_id=initial_id,
            job_id=str(stream),
        )
    if replacement:
        connection.execute(
            "INSERT INTO learner_instances VALUES("
            "'instance-7-replacement', '300.opbs', 'stopped', 7, 2, 2, 100.0)"
        )
        connection.execute(
            "INSERT INTO launch_requests VALUES("
            "'replacement-request', 'replacement', NULL, 90.0, 'instance-7', "
            "'confirmed_scheduler_terminal_after_progress_stall', 'admitted', '300.opbs', "
            "'instance-7-replacement')"
        )
        victim = {
            "instance_id": "instance-7",
            "qdel": {"job_id": "7.opbs", "requested_at": 60.0},
        }
        successor = {
            "request_id": "replacement-request",
            "admitted_instance_id": "instance-7-replacement",
            "pbs_job_id": "300.opbs",
        }
        _write_attestation(
            run_root,
            actor_kind="learner",
            actor_id="instance-7-replacement",
            job_id="300",
        )

    for version in range(26):
        committed_epoch = 2 if takeover and version >= 6 else 1
        weight_relative_path = (
            f"weights/epochs/e{committed_epoch:06d}/owner/"
            f"global_v{version:06d}_pfixture.safetensors"
        )
        connection.execute(
            "INSERT INTO global_versions VALUES(?, ?, ?)",
            (version, committed_epoch, weight_relative_path),
        )
        if version == 0:
            continue
        for contributor in range(4):
            connection.execute(
                "INSERT INTO updates VALUES(?, 200, 'applied', ?)",
                (f"update-{version}-{contributor}", version),
            )
    connection.commit()
    connection.close()
    final_weight = run_root / weight_relative_path
    final_weight.parent.mkdir(parents=True)
    final_weight.write_bytes(b"latest model weight")
    final_weight.chmod(0o444)
    _write_attestation(
        run_root,
        actor_kind="syncer",
        actor_id="syncer-primary",
        job_id="100",
    )
    if takeover:
        _write_attestation(
            run_root,
            actor_kind="syncer",
            actor_id="syncer-successor",
            job_id="200",
        )
    return victim, successor


def _submission(
    *,
    role: str,
    submitted_at: float,
    slot: int | None = None,
) -> dict[str, object]:
    """Return one minimal scheduler receipt for timeline checks."""

    return {"role": role, "slot": slot, "submitted_at": submitted_at}


def test_registry_and_configs_are_the_exact_current_plan04_matrix() -> None:
    """The Full Protocol package exposes the seven non-baseline experiments."""

    module = _module()

    assert tuple(module.SCENARIOS) == (
        "normal",
        "stagger_4_4",
        "stagger_3_3_2",
        "learner_failure_simultaneous",
        "learner_failure_staggered",
        "syncer_failure",
        "dual_syncer",
    )
    assert module.SCENARIOS["stagger_4_4"].learner_batches == (4, 4)
    assert module.SCENARIOS["stagger_3_3_2"].learner_batches == (3, 3, 2)
    assert module.SCENARIOS["syncer_failure"].syncer_fault == "restart"
    assert module.SCENARIOS["dual_syncer"].syncer_fault == "dual"

    experiment = load_config(PACKAGE / "experiment.yaml")
    fault = load_config(PACKAGE / "fault_experiment.yaml")
    baseline = load_baseline_config(
        ROOT / "torch_ddp_baselines/configs/gpt2_wikitext2_8n_5000steps.yaml"
    )
    for config in (experiment, fault):
        assert config.model.name_or_path == "gpt2"
        assert config.model.revision == baseline.model.revision
        assert config.model.tokenizer_revision == baseline.model.tokenizer_revision
        assert config.data.dataset_name == "Salesforce/wikitext"
        assert config.data.dataset_config_name == "wikitext-2-raw-v1"
        assert config.data.revision == baseline.data.revision
        assert config.data.block_size == 1024
        assert config.training.inner_steps == 200
        assert config.sync.stop_after_outer_steps == 25
        assert config.sync.quorum_min == config.sync.quorum_max == 4
        assert config.membership.stream_pool_size == config.membership.bootstrap_instances == 8
        assert config.training.micro_batch_size == 2
        assert config.training.gradient_accumulation_steps == 8
        assert config.sync.scan_interval_seconds == 0.5
        assert config.terminal.admission_close_policy == "global_target"
        assert config.inner_optimizer.scheduler_total_steps == 5000
        assert config.inner_optimizer.lr == baseline.optimizer.lr
        assert config.inner_optimizer.betas == baseline.optimizer.betas
        assert config.inner_optimizer.eps == baseline.optimizer.eps
        assert config.inner_optimizer.weight_decay == baseline.optimizer.weight_decay
        assert config.inner_optimizer.warmup_steps == baseline.optimizer.warmup_steps
        assert config.inner_optimizer.min_lr_ratio == baseline.optimizer.min_lr_ratio
    assert baseline.training.max_steps == 5000
    assert baseline.distributed.world_size == 8
    assert baseline.distributed.periodic_average_interval == 200
    assert experiment.scaling.enabled is False
    assert fault.scaling.enabled is True
    assert fault.scaling.learner_queue == "regular-g"
    assert fault.scaling.learner_walltime == "00:40:00"


def test_one_line_submitter_freezes_current_baseline_and_full_protocol_jobs() -> None:
    """One command must route the two baselines and 40-minute Full Protocol jobs."""

    submitter = (PACKAGE / "submit.sh").read_text(encoding="utf-8")
    wrapper = (PACKAGE / "run_experiment.pbs").read_text(encoding="utf-8")
    baseline_wrapper = (
        ROOT / "torch_ddp_baselines/scripts/miyabi/run_gpt2_wikitext2_5000steps.pbs"
    ).read_text(encoding="utf-8")

    assert "QUEUE=regular-g" in submitter
    assert "WALLTIME=00:40:00" in submitter
    assert "submit_5000steps.sh" in submitter
    assert 'CONFIG="$SCRIPT_DIR/experiment.yaml"' in submitter
    assert 'bash -n "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs' in submitter
    assert "#PBS -q regular-g" in wrapper
    assert "#PBS -W group_list=xg24i002" in wrapper
    assert "#PBS -l walltime=00:40:00" in wrapper
    assert "scenario_supervisor.py" in wrapper
    assert "#PBS -q regular-g" in baseline_wrapper
    assert "#PBS -W group_list=xg24i002" in baseline_wrapper
    assert "#PBS -l walltime=00:40:00" in baseline_wrapper


def test_staggered_and_dual_syncer_timelines_preserve_registered_boundaries() -> None:
    """Timeline evidence must prove every 30- or 60-second scheduling boundary."""

    module = _module()
    origin = 1_000.0
    learners = [
        _submission(
            role="learner",
            slot=slot,
            submitted_at=origin + (0.1 * slot if slot < 4 else 30.0 + 0.1 * slot),
        )
        for slot in range(8)
    ]
    evidence = module._validate_submission_timeline(
        scenario=module.SCENARIOS["stagger_4_4"],
        origin_wall=origin,
        primary_syncer=_submission(role="syncer_primary", submitted_at=origin),
        learners=learners,
        successor_syncer=None,
        syncer_qdel=None,
        conflict_snapshot=None,
        victim=None,
    )
    assert [row["size"] for row in evidence["batch_boundaries"]] == [4, 4]

    simultaneous = [
        _submission(role="learner", slot=slot, submitted_at=origin + slot * 0.1)
        for slot in range(8)
    ]
    dual = module._validate_submission_timeline(
        scenario=module.SCENARIOS["dual_syncer"],
        origin_wall=origin,
        primary_syncer=_submission(role="syncer_primary", submitted_at=origin),
        learners=simultaneous,
        successor_syncer=_submission(role="syncer_candidate", submitted_at=origin + 30.0),
        syncer_qdel={"requested_at": origin + 60.0},
        conflict_snapshot={
            "candidate_scheduler_state": "running",
            "candidate_running_observed_at": origin + 30.0,
            "syncer_epochs": [{"final_state": None}],
        },
        victim=None,
    )
    assert dual["batch_boundaries"][0]["size"] == 8


def test_timeline_rejects_a_short_stagger_delay() -> None:
    """A nominal batch label cannot substitute for an observed 30-second wait."""

    module = _module()
    learners = [
        _submission(
            role="learner",
            slot=slot,
            submitted_at=1_000.0 + (slot * 0.1 if slot < 4 else 20.0 + slot * 0.1),
        )
        for slot in range(8)
    ]
    with pytest.raises(RuntimeError, match="before its registered boundary"):
        module._validate_submission_timeline(
            scenario=module.SCENARIOS["stagger_4_4"],
            origin_wall=1_000.0,
            primary_syncer=_submission(role="syncer_primary", submitted_at=1_000.0),
            learners=learners,
            successor_syncer=None,
            syncer_qdel=None,
            conflict_snapshot=None,
            victim=None,
        )


def test_three_learner_phase_cannot_advance_the_global_version(tmp_path: Path) -> None:
    """The 3+3+2 scenario must capture the initial below-quorum boundary."""

    module = _module()
    missing = module._observe_pre_quorum_version(tmp_path / "missing.sqlite3")
    assert missing == {"authority_initialized": False, "global_version": None}

    database = tmp_path / "authority.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE global_versions(version INTEGER)")
    connection.execute("INSERT INTO global_versions VALUES(0)")
    connection.commit()
    connection.close()
    assert module._observe_pre_quorum_version(database) == {
        "authority_initialized": True,
        "global_version": 0,
    }

    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO global_versions VALUES(1)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="three-learner phase advanced"):
        module._observe_pre_quorum_version(database)


def test_learner_fault_timeline_binds_qdel_to_an_initial_job_after_sixty_seconds() -> None:
    """Fault acceptance requires a real bootstrap-job deletion at the registered boundary."""

    module = _module()
    origin = 1_000.0
    learners = [
        {
            **_submission(role="learner", slot=slot, submitted_at=origin + slot * 0.1),
            "job_id": f"{slot}.opbs",
        }
        for slot in range(8)
    ]
    victim = {
        "qdel": {
            "job_id": "3.opbs",
            "requested_at": origin + module.FAULT_DELAY_SECONDS,
        }
    }
    evidence = module._validate_submission_timeline(
        scenario=module.SCENARIOS["learner_failure_simultaneous"],
        origin_wall=origin,
        primary_syncer=_submission(role="syncer_primary", submitted_at=origin),
        learners=learners,
        successor_syncer=None,
        syncer_qdel=None,
        conflict_snapshot=None,
        victim=victim,
    )
    assert evidence["batch_boundaries"][0]["size"] == 8

    victim["qdel"]["requested_at"] = origin + 30.0
    with pytest.raises(RuntimeError, match="after 60 seconds"):
        module._validate_submission_timeline(
            scenario=module.SCENARIOS["learner_failure_simultaneous"],
            origin_wall=origin,
            primary_syncer=_submission(role="syncer_primary", submitted_at=origin),
            learners=learners,
            successor_syncer=None,
            syncer_qdel=None,
            conflict_snapshot=None,
            victim=victim,
        )


def test_authority_oracle_accepts_exact_normal_and_takeover_histories(tmp_path: Path) -> None:
    """Terminal acceptance permits an admitted late learner with no completed cycle."""

    module = _module()
    normal_root = tmp_path / "normal"
    _write_authority(normal_root)
    normal = module._authority_evidence(
        run_root=normal_root,
        config=load_config(PACKAGE / "experiment.yaml"),
        scenario=module.SCENARIOS["normal"],
        initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
        primary_syncer_job_id="100.opbs",
        successor_syncer_job_id=None,
        victim=None,
        replacement=None,
    )
    assert normal["integrity_check"] == ["ok"]
    assert normal["merge_counts"] == {version: 4 for version in range(1, 26)}
    assert {row["stable_contributor_key"] for row in normal["contributor_progress"]} == {
        str(stream) for stream in range(8) if stream != 5
    }

    # A completed live syncer scope must not retain a superseded model publication.
    obsolete_weight = (
        normal_root / "weights/epochs/e000001/owner/global_v000024_pfixture.safetensors"
    )
    obsolete_weight.write_bytes(b"obsolete model weight")
    obsolete_weight.chmod(0o444)
    with pytest.raises(RuntimeError, match="only the latest weight"):
        module._authority_evidence(
            run_root=normal_root,
            config=load_config(PACKAGE / "experiment.yaml"),
            scenario=module.SCENARIOS["normal"],
            initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
            primary_syncer_job_id="100.opbs",
            successor_syncer_job_id=None,
            victim=None,
            replacement=None,
        )
    obsolete_weight.unlink()

    # Progress remains authority evidence and cannot name a stream outside the run scope.
    connection = sqlite3.connect(normal_root / "control/syncer_metadata.sqlite3")
    connection.execute("INSERT INTO contributor_progress VALUES('8')")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="outside the configured pool"):
        module._authority_evidence(
            run_root=normal_root,
            config=load_config(PACKAGE / "experiment.yaml"),
            scenario=module.SCENARIOS["normal"],
            initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
            primary_syncer_job_id="100.opbs",
            successor_syncer_job_id=None,
            victim=None,
            replacement=None,
        )
    connection = sqlite3.connect(normal_root / "control/syncer_metadata.sqlite3")
    connection.execute("DELETE FROM contributor_progress WHERE stable_contributor_key='8'")
    connection.commit()
    connection.close()

    # Scheduler identity must remain bound to the same durable learner incarnation.
    attestation_path = normal_root / "metrics/attestations/learner/instance-0/instance-0.json"
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation["scheduler_job_id"] = "999.opbs"
    attestation["attestation_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in attestation.items() if key != "attestation_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    attestation_path.chmod(0o644)
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
    attestation_path.chmod(0o444)
    with pytest.raises(RuntimeError, match="bound to its authority row"):
        module._authority_evidence(
            run_root=normal_root,
            config=load_config(PACKAGE / "experiment.yaml"),
            scenario=module.SCENARIOS["normal"],
            initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
            primary_syncer_job_id="100.opbs",
            successor_syncer_job_id=None,
            victim=None,
            replacement=None,
        )

    takeover_root = tmp_path / "takeover"
    _write_authority(takeover_root, takeover=True)
    takeover = module._authority_evidence(
        run_root=takeover_root,
        config=load_config(PACKAGE / "fault_experiment.yaml"),
        scenario=module.SCENARIOS["syncer_failure"],
        initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
        primary_syncer_job_id="100.opbs",
        successor_syncer_job_id="200.opbs",
        victim=None,
        replacement=None,
    )
    assert [row["final_state"] for row in takeover["syncer_epochs"]] == [
        "expired",
        "released",
    ]


def test_authority_oracle_does_not_require_all_submitted_learners_to_admit(
    tmp_path: Path,
) -> None:
    """Terminal acceptance separates eight submissions from the admitted quorum."""

    module = _module()
    run_root = tmp_path / "four-admitted"
    _write_authority(run_root, admitted_streams=4)

    evidence = module._authority_evidence(
        run_root=run_root,
        config=load_config(PACKAGE / "experiment.yaml"),
        scenario=module.SCENARIOS["normal"],
        initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
        primary_syncer_job_id="100.opbs",
        successor_syncer_job_id=None,
        victim=None,
        replacement=None,
    )

    assert len(evidence["bootstrap_launches"]) == 4
    assert len(evidence["terminal_fences"]) == 4


def test_authority_oracle_reads_archived_global_version_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal acceptance must include archived versions after maintenance compaction."""

    module = _module()
    run_root = tmp_path / "archived-versions"
    _write_authority(run_root)
    database = run_root / "control/syncer_metadata.sqlite3"
    connection = sqlite3.connect(database)
    archived_versions = [
        dict(
            zip(
                ("version", "committed_by_epoch", "weight_relative_path"),
                row,
                strict=True,
            )
        )
        for row in connection.execute(
            "SELECT version, committed_by_epoch, weight_relative_path "
            "FROM global_versions ORDER BY version"
        )
    ]
    connection.execute("DELETE FROM global_versions WHERE version < 10")
    connection.commit()
    connection.close()
    original_reader = module.read_logical_authority_rows

    def logical_rows(
        connection: sqlite3.Connection,
        paths: object,
        *,
        table: str,
        primary_key: str,
    ) -> list[dict[str, object]]:
        """Supply compacted version history while delegating other logical tables."""

        if table == "global_versions":
            assert primary_key == "version"
            return archived_versions
        return original_reader(connection, paths, table=table, primary_key=primary_key)

    monkeypatch.setattr(module, "read_logical_authority_rows", logical_rows)
    evidence = module._authority_evidence(
        run_root=run_root,
        config=load_config(PACKAGE / "experiment.yaml"),
        scenario=module.SCENARIOS["normal"],
        initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
        primary_syncer_job_id="100.opbs",
        successor_syncer_job_id=None,
        victim=None,
        replacement=None,
    )

    assert [row["version"] for row in evidence["versions"]] == list(range(26))


def test_authority_oracle_binds_replacement_to_the_expired_stream(tmp_path: Path) -> None:
    """Learner recovery requires one authorized higher-epoch successor, not mere completion."""

    module = _module()
    run_root = tmp_path / "replacement"
    victim, replacement = _write_authority(run_root, replacement=True)
    evidence = module._authority_evidence(
        run_root=run_root,
        config=load_config(PACKAGE / "fault_experiment.yaml"),
        scenario=module.SCENARIOS["learner_failure_simultaneous"],
        initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
        primary_syncer_job_id="100.opbs",
        successor_syncer_job_id=None,
        victim=victim,
        replacement=replacement,
    )
    assert evidence["replacement"]["successor"]["stream_epoch"] == 2

    connection = sqlite3.connect(run_root / "control/syncer_metadata.sqlite3")
    connection.execute(
        "UPDATE learner_instances SET stream_epoch=1 WHERE instance_id='instance-7-replacement'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="expired-stream succession"):
        module._authority_evidence(
            run_root=run_root,
            config=load_config(PACKAGE / "fault_experiment.yaml"),
            scenario=module.SCENARIOS["learner_failure_simultaneous"],
            initial_learner_job_ids=[f"{index}.opbs" for index in range(8)],
            primary_syncer_job_id="100.opbs",
            successor_syncer_job_id=None,
            victim=victim,
            replacement=replacement,
        )


def test_cleanup_discovers_authorized_replacement_before_admission(tmp_path: Path) -> None:
    """Failure cleanup must own a submitted replacement even before it registers."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE learner_instances(pbs_job_id TEXT);
        CREATE TABLE syncer_epochs(pbs_job_id TEXT);
        CREATE TABLE launch_requests(pbs_job_id TEXT);
        INSERT INTO learner_instances VALUES('101.opbs');
        INSERT INTO syncer_epochs VALUES('102.opbs');
        INSERT INTO launch_requests VALUES('103.opbs');
        """
    )
    connection.commit()
    connection.close()

    assert module._discover_authority_jobs(database) == {
        "101.opbs",
        "102.opbs",
        "103.opbs",
    }


def test_summary_comparison_requires_one_registered_baseline_per_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal acceptance binds one 5,000-step DDP and periodic-average baseline."""

    module = _module()
    project = tmp_path / "project"
    runs = project / "runs"
    runs.mkdir(parents=True)
    run_root = runs / "full_protocol" / "plan04_e1_normal_current"
    run_root.mkdir(parents=True)
    control = run_root / "control"
    control.mkdir()
    (control / "run_config.resolved.yaml").write_text(
        (PACKAGE / "experiment.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    fields = [
        "run_id",
        "run_dir",
        "run_kind",
        "mode",
        "git_commit",
        "source_fingerprint",
        "model_name_or_path",
        "model_revision",
        "model_dtype",
        "dataset_name",
        "dataset_config_name",
        "dataset_revision",
        "train_split",
        "block_size",
        "micro_batch_size",
        "gradient_accumulation_steps",
        "learning_rate",
        "optimizer_beta1",
        "optimizer_beta2",
        "optimizer_epsilon",
        "weight_decay",
        "warmup_steps",
        "min_lr_ratio",
        "expected_contributors",
        "optimizer_steps_min",
        "optimizer_steps_max",
        "global_steps",
        "merge_contributors",
        "synchronization_interval",
        "synchronization_count",
        "final_mean_loss",
        "training_time_seconds",
    ]
    shared = {
        "run_dir": str(run_root),
        "git_commit": "1" * 40,
        "source_fingerprint": "sha256:" + "f" * 64,
        "model_name_or_path": "gpt2",
        "model_revision": "607a30d783dfa663caf39e06633721c8d4cfcd7e",
        "model_dtype": "bfloat16",
        "dataset_name": "Salesforce/wikitext",
        "dataset_config_name": "wikitext-2-raw-v1",
        "dataset_revision": "b08601e04326c79dfdd32d625aee71d232d685c3",
        "train_split": "train",
        "block_size": "1024",
        "micro_batch_size": "2",
        "gradient_accumulation_steps": "8",
        "learning_rate": "5e-05",
        "optimizer_beta1": "0.9",
        "optimizer_beta2": "0.95",
        "optimizer_epsilon": "1e-08",
        "weight_decay": "0.1",
        "warmup_steps": "100",
        "min_lr_ratio": "0.1",
        "expected_contributors": "8",
        "optimizer_steps_min": "5000",
        "optimizer_steps_max": "5000",
    }
    rows = [
        {
            **shared,
            "run_id": "baseline-ddp",
            "run_kind": "torch_ddp_baseline",
            "mode": "ddp",
            "global_steps": "",
            "merge_contributors": "8",
            "synchronization_interval": "1",
            "synchronization_count": "5000",
            "final_mean_loss": "3.0",
            "training_time_seconds": "600.0",
        },
        {
            **shared,
            "run_id": "baseline-periodic",
            "run_kind": "torch_ddp_baseline",
            "mode": "periodic_average",
            "global_steps": "",
            "merge_contributors": "8",
            "synchronization_interval": "200",
            "synchronization_count": "25",
            "final_mean_loss": "3.1",
            "training_time_seconds": "620.0",
        },
        {
            **shared,
            "run_id": run_root.name,
            "run_kind": "fs_diloco_full_protocol",
            "mode": "full_protocol",
            "global_steps": "25",
            "merge_contributors": "4",
            "synchronization_interval": "200",
            "synchronization_count": "25",
            "final_mean_loss": "3.2",
            "training_time_seconds": "650.0",
        },
    ]
    with (runs / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    def build_comparisons(selected: list[dict[str, str]]) -> dict[str, object]:
        """Return a minimal canonical comparison for the selected three rows."""

        assert [row["run_id"] for row in selected] == [
            "baseline-ddp",
            "baseline-periodic",
            run_root.name,
        ]
        return {
            "format_version": 1,
            "threshold": 0.30,
            "comparisons": [
                {"investigation_required": False},
                {"investigation_required": False},
            ],
        }

    monkeypatch.setattr(
        module,
        "_load_summary_tool",
        lambda _root: SimpleNamespace(
            COMPARISON_THRESHOLD=0.30,
            update_summary_csv=lambda *_args: (0, 1, 3),
            build_comparisons=build_comparisons,
        ),
    )

    _row, comparison = module._append_summary(
        project_root=project,
        run_root=run_root,
        scenario=module.SCENARIOS["normal"],
        comparison_output=tmp_path / "comparison.json",
    )
    assert comparison["registered_workload"]["valid"] is True
    assert comparison["baseline_run_ids"] == ["baseline-ddp", "baseline-periodic"]
    assert comparison["investigation_required"] is False
