"""Exercise the current experiment04 scenario registry and durable authority oracle."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
from types import ModuleType

import pytest


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


def _finalized_authority(path: Path, *, hard_crash_stream: int | None = None) -> None:
    """Create the minimal finalized authority required by current formal oracles."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE controller_state(singleton INTEGER, generation INTEGER, state TEXT);
        CREATE TABLE terminal_state(singleton INTEGER, final_version INTEGER);
        CREATE TABLE terminal_contributor_fences(
            generation INTEGER,
            stable_contributor_key TEXT,
            state TEXT,
            final_cycle_seq INTEGER,
            hard_crash_gap_tokens_upper_bound INTEGER
        );
        CREATE TABLE updates(
            update_id TEXT,
            status TEXT,
            applied_version INTEGER,
            inner_steps INTEGER,
            fence_json TEXT,
            created_at REAL
        );
        CREATE TABLE launch_requests(
            request_id TEXT,
            bootstrap_slot INTEGER,
            role TEXT,
            reason TEXT,
            stream_id INTEGER,
            state TEXT,
            admitted_instance_id TEXT,
            created_at REAL
        );
        CREATE TABLE learner_instances(
            instance_id TEXT,
            registered_at REAL,
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
            committed_by_epoch INTEGER,
            committed_at REAL
        );
        INSERT INTO controller_state VALUES(1, 1, 'finalized');
        INSERT INTO terminal_state VALUES(1, 10);
        INSERT INTO syncer_epochs VALUES(1, '100.opbs', 'released');
        """
    )
    for stream in range(8):
        crashed = stream == hard_crash_stream
        connection.execute(
            "INSERT INTO terminal_contributor_fences VALUES(1, ?, ?, ?, ?)",
            (
                str(stream),
                "hard_crash" if crashed else "acked",
                None if crashed else 4,
                1 if crashed else 0,
            ),
        )
        connection.execute(
            "INSERT INTO learner_instances VALUES(?, ?, ?, ?, 1, ?, 1, 1, ?)",
            (
                f"instance-{stream}",
                float(stream),
                "expired" if crashed else "stopped",
                f"placement-{stream}",
                stream,
                f"{stream:x}" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO launch_requests VALUES(?, ?, 'bootstrap', 'initial_bootstrap', "
            "?, 'admitted', ?, ?)",
            (f"bootstrap-{stream}", stream, stream, f"instance-{stream}", float(stream)),
        )
    for version in range(11):
        connection.execute(
            "INSERT INTO global_versions VALUES(?, 1, ?)",
            (version, 100.0 + version),
        )
    for version in range(1, 11):
        for contributor in range(4):
            stream = contributor if hard_crash_stream != contributor else 4
            connection.execute(
                "INSERT INTO updates VALUES(?, 'applied', ?, 200, ?, ?)",
                (
                    f"update-{version}-{contributor}",
                    version,
                    json.dumps(
                        _fence(f"instance-{stream}", stream), sort_keys=True, separators=(",", ":")
                    ),
                    100.0 + version,
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


def test_no_failure_authority_oracle_requires_ten_exact_four_way_merges(tmp_path: Path) -> None:
    """The healthy oracle rejects any global version lacking four 200-step proposals."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _finalized_authority(database)

    evidence = module._final_authority_evidence(
        database,
        run_root=tmp_path,
        scenario=module.SCENARIOS["no_failure"],
        first_syncer_job_id="100.opbs",
        victim=None,
        replacement=None,
    )
    assert evidence["integrity_check"] == ["ok"]
    assert len(evidence["merge_counts"]) == 10
    assert len(evidence["bootstrap_launches"]) == 8

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM updates WHERE update_id='update-10-3'")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="ten exact"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            scenario=module.SCENARIOS["no_failure"],
            first_syncer_job_id="100.opbs",
            victim=None,
            replacement=None,
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
        scenario=module.SCENARIOS["failure_no_replacement"],
        first_syncer_job_id="100.opbs",
        victim=victim,
        replacement=None,
    )

    assert evidence["launch_requests"] == []
    assert evidence["replacement_boundary"]["late_created_update_ids"] == []
