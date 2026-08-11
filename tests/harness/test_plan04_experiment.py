"""Exercise the plan04 scenario registry and durable authority oracle."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
import sys
from types import ModuleType

import pytest

from fs_diloco.storage.audit_archive import build_audit_batch, publish_audit_batch
from fs_diloco.storage.paths import RunPaths


ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "do_experiments" / "experiment04" / "scenario_supervisor.py"


def _module() -> ModuleType:
    """Load the experiment supervisor as a standalone source-bound module."""

    specification = importlib.util.spec_from_file_location("plan04_scenario_supervisor", SUPERVISOR)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _normal_authority(path: Path) -> None:
    """Create the minimal finalized authority required by the normal-scenario oracle."""

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
            fence_json TEXT
        );
        CREATE TABLE launch_requests(
            request_id TEXT,
            role TEXT,
            created_at REAL
        );
        CREATE TABLE learner_instances(
            instance_id TEXT,
            registered_at REAL
        );
        CREATE TABLE syncer_epochs(
            epoch INTEGER,
            pbs_job_id TEXT,
            final_state TEXT
        );
        CREATE TABLE syncer_leader(
            singleton INTEGER,
            epoch INTEGER,
            owner_id TEXT,
            pbs_job_id TEXT,
            state TEXT,
            acquired_at REAL,
            lease_expires_at REAL
        );
        CREATE TABLE global_versions(
            version INTEGER,
            committed_by_epoch INTEGER,
            committed_at REAL
        );
        INSERT INTO controller_state VALUES(1, 1, 'finalized');
        INSERT INTO terminal_state VALUES(1, 10);
        INSERT INTO syncer_epochs VALUES(1, '100.opbs', 'released');
        INSERT INTO syncer_leader VALUES(1, 1, 'syncer-1', '100.opbs', 'active', 90.0, 130.0);
        """
    )
    for stream in range(8):
        connection.execute(
            "INSERT INTO terminal_contributor_fences VALUES(1, ?, 'acked', 10, 0)",
            (str(stream),),
        )
        connection.execute(
            "INSERT INTO launch_requests VALUES(?, 'bootstrap', ?)",
            (f"bootstrap-{stream}", float(stream)),
        )
        connection.execute(
            "INSERT INTO learner_instances VALUES(?, ?)",
            (f"instance-{stream}", float(stream)),
        )
    for version in range(11):
        connection.execute(
            "INSERT INTO global_versions VALUES(?, 1, ?)",
            (version, 100.0 + version),
        )
    for version in range(1, 11):
        for contributor in range(4):
            connection.execute(
                "INSERT INTO updates VALUES(?, 'applied', ?, 200, '{}')",
                (f"update-{version}-{contributor}", version),
            )
    connection.commit()
    connection.close()


def _archive_early_updates(run_root: Path, database: Path) -> None:
    """Move versions 1 through 9 into one immutable authority-history batch."""

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM updates WHERE applied_version<=9 ORDER BY update_id"
        )
    ]
    payload = build_audit_batch(
        batch_id="through-v9",
        record_kind="authority_history",
        cutoff_version=9,
        records=[{"table": "updates", "primary_key": row["update_id"], "row": row} for row in rows],
    )
    publish_audit_batch(RunPaths(run_root), payload)
    connection.execute("DELETE FROM updates WHERE applied_version<=9")
    connection.commit()
    connection.close()


def test_registered_scenarios_cover_eight_bootstrap_slots_once() -> None:
    """Every scenario must submit all eight unique bootstrap slots on its fixed timeline."""

    module = _module()

    assert set(module.SCENARIOS) == {
        "normal",
        "staggered_4_4",
        "staggered_3_3_2",
        "learner_loss",
        "staggered_learner_loss",
        "syncer_loss",
        "dual_syncer",
    }
    for scenario in module.SCENARIOS.values():
        slots = [slot for _delay, batch in scenario.learner_batches for slot in batch]
        assert slots == list(range(8))
    assert [delay for delay, _batch in module.SCENARIOS["staggered_4_4"].learner_batches] == [
        0.0,
        30.0,
    ]
    assert [delay for delay, _batch in module.SCENARIOS["staggered_3_3_2"].learner_batches] == [
        0.0,
        30.0,
        60.0,
    ]


def test_syncer_fault_timelines_preserve_registered_waits() -> None:
    """Syncer scenarios must encode the required delete, wait, and conflict windows."""

    module = _module()

    assert module.SCENARIOS["syncer_loss"].fault_delay == 60.0
    assert module.SCENARIOS["syncer_loss"].second_syncer_delay == 80.0
    assert module.SCENARIOS["dual_syncer"].second_syncer_delay == 60.0
    assert module.SCENARIOS["dual_syncer"].fault_delay == 120.0


def test_syncer_fault_requires_the_first_submitted_job_to_hold_the_active_lease(
    tmp_path: Path,
) -> None:
    """Fault injection must fail closed before deleting a non-leader syncer job."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _normal_authority(database)

    leader = module._require_active_first_syncer(database, "100.opbs")

    assert leader["epoch"] == 1
    with pytest.raises(RuntimeError, match="not the active first syncer"):
        module._require_active_first_syncer(database, "200.opbs")


def test_qsub_output_replacement_and_victim_selection_are_exact() -> None:
    """Actor commands and reproducible learner fault selection must not change other IDs."""

    module = _module()
    command = ["qsub", "-q", "debug-g", "-o", "/old.log", "actor.pbs"]

    replaced = module._replace_output_path(command, Path("/new.log"))
    admissions = [
        {"stream_id": stream, "instance_id": f"instance-{stream}", "pbs_job_id": f"{stream}.opbs"}
        for stream in range(8)
    ]

    assert replaced == ["qsub", "-q", "debug-g", "-o", "/new.log", "actor.pbs"]
    assert command[4] == "/old.log"
    assert module._choose_learner_victim("run-fixed", admissions) == module._choose_learner_victim(
        "run-fixed", list(reversed(admissions))
    )


def test_normal_authority_oracle_requires_ten_exact_four_way_merges(tmp_path: Path) -> None:
    """The normal oracle must reject a global version with fewer than four 200-step updates."""

    module = _module()
    database = tmp_path / "authority.sqlite3"
    _normal_authority(database)
    _archive_early_updates(tmp_path, database)

    evidence = module._final_authority_evidence(
        database,
        run_root=tmp_path,
        scenario=module.SCENARIOS["normal"],
        first_syncer_job_id="100.opbs",
        second_syncer_job_id=None,
        victim=None,
        replacement=None,
    )
    assert evidence["integrity_check"] == ["ok"]
    assert len(evidence["merge_counts"]) == 10

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM updates WHERE update_id='update-10-3'")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="ten exact"):
        module._final_authority_evidence(
            database,
            run_root=tmp_path,
            scenario=module.SCENARIOS["normal"],
            first_syncer_job_id="100.opbs",
            second_syncer_job_id=None,
            victim=None,
            replacement=None,
        )
