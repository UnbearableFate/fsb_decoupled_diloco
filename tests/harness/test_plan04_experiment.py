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
        CREATE TABLE global_versions(version INTEGER, committed_by_epoch INTEGER);
        CREATE TABLE contributor_progress(stable_contributor_key TEXT);
        CREATE TABLE capacity_observations(observation_seq INTEGER);
        CREATE TABLE updates(
            update_id TEXT, inner_steps INTEGER, status TEXT, applied_version INTEGER
        );
        INSERT INTO controller_state VALUES(1, 1, 'finalized');
        INSERT INTO terminal_state VALUES(1, 'finalized', 10);
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
    for stream in range(8):
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

    for version in range(11):
        connection.execute(
            "INSERT INTO global_versions VALUES(?, ?)",
            (version, 2 if takeover and version >= 6 else 1),
        )
        if version == 0:
            continue
        for contributor in range(4):
            connection.execute(
                "INSERT INTO updates VALUES(?, 100, 'applied', ?)",
                (f"update-{version}-{contributor}", version),
            )
    connection.commit()
    connection.close()
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
    """The package exposes only baseline plus the seven requested experiments."""

    module = _module()

    assert tuple(module.SCENARIOS) == (
        "baseline",
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

    baseline = load_config(PACKAGE / "baseline.yaml")
    experiment = load_config(PACKAGE / "experiment.yaml")
    timed = load_config(PACKAGE / "timed_experiment.yaml")
    fault = load_config(PACKAGE / "fault_experiment.yaml")
    assert (baseline.training.inner_steps, baseline.sync.quorum_min) == (100, 4)
    for config in (experiment, timed, fault):
        assert config.training.inner_steps == 100
        assert config.sync.stop_after_outer_steps == 10
        assert config.sync.quorum_min == config.sync.quorum_max == 4
        assert config.membership.stream_pool_size == config.membership.bootstrap_instances == 8
    for config in (baseline, experiment, timed, fault):
        assert config.training.gradient_accumulation_steps == 2
        assert config.sync.scan_interval_seconds == 0.5
    assert baseline.terminal.admission_close_policy == "global_target_or_launch_budget"
    assert experiment.terminal.admission_close_policy == "global_target_or_launch_budget"
    assert timed.terminal.admission_close_policy == "manual"
    assert fault.terminal.admission_close_policy == "manual"
    assert experiment.scaling.enabled is False
    assert timed.scaling.enabled is False
    assert fault.scaling.enabled is True
    assert fault.scaling.learner_queue == "regular-g"
    assert fault.scaling.learner_walltime == "00:30:00"


def test_manual_scenario_close_is_bound_to_the_exact_global_target(tmp_path: Path) -> None:
    """Timed scenarios cannot publish their close request before or after version 10."""

    module = _module()
    run_root = tmp_path / "run"
    control = run_root / "control"
    control.mkdir(parents=True)
    (control / "run_descriptor.json").write_text(
        json.dumps({"run_id": "run-current", "descriptor_sha256": "d" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exact global target"):
        module._publish_scenario_close(
            run_root,
            scenario_name="stagger_3_3_2",
            observed_version=9,
            target_version=10,
        )
    evidence = module._publish_scenario_close(
        run_root,
        scenario_name="stagger_3_3_2",
        observed_version=10,
        target_version=10,
    )

    assert evidence["observed_global_version"] == 10
    assert evidence["request"]["reason"] == "plan04_scenario_complete:stagger_3_3_2"
    assert (control / "terminal_close_request.json").is_file()


def test_manual_scenario_waits_for_every_admitted_learner_runtime(tmp_path: Path) -> None:
    """Manual close cannot race a learner that has not consumed its admission response."""

    module = _module()
    run_root = tmp_path / "run"
    admissions = []
    for stream in range(8):
        actor_id = f"instance-{stream}"
        job_id = str(stream)
        admissions.append({"instance_id": actor_id, "pbs_job_id": f"{job_id}.opbs"})
        _write_attestation(
            run_root,
            actor_kind="learner",
            actor_id=actor_id,
            job_id=job_id,
        )

    attestations = module._wait_initial_runtime_attestations(
        run_root,
        admissions,
        timeout_seconds=0.1,
    )

    assert {row["actor_id"] for row in attestations} == {
        f"instance-{stream}" for stream in range(8)
    }
    assert {row["scheduler_job_id"] for row in attestations} == {
        f"{stream}.opbs" for stream in range(8)
    }

    # Scheduler identity is part of readiness and cannot be replaced by actor ID alone.
    wrong_job = [dict(row) for row in admissions]
    wrong_job[7]["pbs_job_id"] = "999.opbs"
    with pytest.raises(RuntimeError, match="wrong scheduler job"):
        module._wait_initial_runtime_attestations(
            run_root,
            wrong_job,
            timeout_seconds=0.1,
        )


def test_one_line_submitter_freezes_regular_queue_and_thirty_minute_jobs() -> None:
    """Human-facing submission must enforce the plan's queue, walltime, and static gates."""

    submitter = (PACKAGE / "submit.sh").read_text(encoding="utf-8")
    wrapper = (PACKAGE / "run_experiment.pbs").read_text(encoding="utf-8")

    assert "QUEUE=regular-g" in submitter
    assert "WALLTIME=00:30:00" in submitter
    assert 'CONFIG="$SCRIPT_DIR/timed_experiment.yaml"' in submitter
    assert 'bash -n "$PROJECT_ROOT"/scripts/miyabi/agent/*.pbs' in submitter
    assert "#PBS -q regular-g" in wrapper
    assert "#PBS -W group_list=xg24i002" in wrapper
    assert "#PBS -l walltime=00:30:00" in wrapper
    assert "scenario_supervisor.py" in wrapper


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
        successor_syncer=_submission(role="syncer_candidate", submitted_at=origin + 60.0),
        syncer_qdel={"requested_at": origin + 120.0},
        conflict_snapshot={
            "candidate_scheduler_state": "running",
            "candidate_running_observed_at": origin + 60.0,
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
    assert normal["merge_counts"] == {version: 4 for version in range(1, 11)}
    assert {row["stable_contributor_key"] for row in normal["contributor_progress"]} == {
        str(stream) for stream in range(8) if stream != 5
    }

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
        dict(zip(("version", "committed_by_epoch"), row, strict=True))
        for row in connection.execute(
            "SELECT version, committed_by_epoch FROM global_versions ORDER BY version"
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

    assert [row["version"] for row in evidence["versions"]] == list(range(11))


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


def test_summary_comparison_requires_equal_work_and_enforces_twenty_percent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal acceptance compares equal aggregate work and flags material metric drift."""

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
        "merge_contributors",
        "synchronization_interval",
        "synchronization_count",
        "final_mean_loss",
        "training_time_seconds",
    ]
    shared = {
        "run_dir": str(run_root),
        "source_fingerprint": "sha256:" + "f" * 64,
        "model_name_or_path": "synthetic-tiny",
        "model_revision": "",
        "model_dtype": "float32",
        "dataset_name": "synthetic",
        "dataset_config_name": "",
        "dataset_revision": "",
        "train_split": "train",
        "block_size": "16",
        "micro_batch_size": "1",
        "gradient_accumulation_steps": "2",
        "learning_rate": "5e-05",
        "optimizer_beta1": "0.9",
        "optimizer_beta2": "0.95",
        "optimizer_epsilon": "1e-08",
        "weight_decay": "0.0",
        "synchronization_count": "10",
    }
    rows = [
        *[
            {
                **shared,
                "run_id": f"plan04_e0_baseline_repeat{repeat}",
                "merge_contributors": "4",
                "synchronization_interval": "100",
                "final_mean_loss": str(2.0 + repeat * 0.01),
                "training_time_seconds": str(100.0 + repeat),
            }
            for repeat in range(3)
        ],
        *[
            {
                **shared,
                "run_id": run_root.name if repeat == 2 else f"plan04_e1_normal_repeat{repeat}",
                "merge_contributors": "4",
                "synchronization_interval": "100",
                "final_mean_loss": str(2.1 + repeat * 0.01),
                "training_time_seconds": str(113.0 + repeat),
            }
            for repeat in range(3)
        ],
    ]
    with (runs / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(
        module,
        "_load_summary_tool",
        lambda _root: SimpleNamespace(update_summary_csv=lambda *_args: (0, 1, 2)),
    )

    _row, comparison = module._append_summary(
        project_root=project,
        run_root=run_root,
        scenario=module.SCENARIOS["normal"],
        source_fingerprint="sha256:" + "f" * 64,
        comparison_output=tmp_path / "comparison.json",
    )
    assert comparison["equal_applied_local_steps"] is True
    assert comparison["repeat_complete"] is True
    assert comparison["metrics"]["training_time_seconds"][
        "signed_relative_median_difference_95pct_interval"
    ]
    assert comparison["investigation_required"] is False
