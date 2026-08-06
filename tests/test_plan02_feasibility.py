import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
FAULT_PROBE = ROOT / "scripts" / "miyabi" / "plan02_fault_probe.py"
CHECKER = ROOT / "scripts" / "miyabi" / "check_plan02_feasibility.py"
PBS_PROBE = ROOT / "scripts" / "miyabi" / "plan02_pbs_capability.py"
AGGREGATE = ROOT / "scripts" / "miyabi" / "plan02_phase0_aggregate.py"
SOURCE_GATE = ROOT / "scripts" / "miyabi" / "plan02_source_gate.py"


def _load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_fault_probe(tmp_path: Path, mode: str, *extra: str) -> dict:
    output = tmp_path / f"{mode}.json"
    subprocess.run(
        [
            sys.executable,
            str(FAULT_PROBE),
            mode,
            "--work-dir",
            str(tmp_path / f"{mode}-work"),
            "--output-json",
            str(output),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def test_writer_lock_probe_proves_availability_boundary(tmp_path):
    payload = _run_fault_probe(tmp_path, "writer-lock")
    assert payload["status"] == "PASS"
    assert payload["visible_rows_before_kill"] == 0
    assert payload["post_kill_rows"] == [["successor", "committed"]]
    assert payload["availability_boundary"] == {
        "paused_writer_transaction_blocks_takeover": True,
        "killing_holder_releases_lock": True,
        "uncommitted_state_rolled_back": True,
    }


def test_writer_lock_combiner_requires_distinct_cross_node_hosts(tmp_path):
    module = _load_script(FAULT_PROBE, "plan02_writer_lock_combiner")
    single = tmp_path / "single.json"
    holder = tmp_path / "holder.json"
    contender = tmp_path / "contender.json"
    single.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    holder.write_text(
        json.dumps(
            {
                "status": "PASS",
                "hostname": "mg001",
                "holder_pid": 123,
                "holder_stopped_state": "T",
                "holder_exit_status": -9,
            }
        ),
        encoding="utf-8",
    )
    contender_payload = {
        "status": "PASS",
        "holder_hostname": "mg001",
        "contender_hostname": "mg002",
        "successor_hostname": "mg002",
        "contender_error": "database is locked",
        "visible_rows_before_kill": 0,
        "post_kill_rows": [["successor", "committed"]],
        "integrity_check": ["ok"],
    }
    contender.write_text(json.dumps(contender_payload), encoding="utf-8")
    combined = module.combine_writer_lock_evidence(single, holder, contender)
    assert combined["hostnames"] == ["mg001", "mg002"]
    assert combined["host_count"] == 2
    assert all(combined["availability_boundary"].values())

    contender_payload["contender_hostname"] = "mg001"
    contender_payload["successor_hostname"] = "mg001"
    contender.write_text(json.dumps(contender_payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="single-node and cross-node"):
        module.combine_writer_lock_evidence(single, holder, contender)


def test_old_cache_writer_probe_selects_canonical_and_repairs(tmp_path):
    payload = _run_fault_probe(tmp_path, "old-cache-writer")
    assert payload["counterexample_reproduced"] is True
    assert set(payload["polluted_cache_epochs"].values()) == {1}
    assert set(payload["selected_canonical"]) == {"latest", "stop", "summary"}
    assert {item["published_by_epoch"] for item in payload["selected_canonical"].values()} == {2}
    assert set(payload["repair_epochs"].values()) == {2}
    assert payload["canonical_discovery_counts"] == {
        "latest": 2,
        "stop": 2,
        "summary": 2,
    }


def test_source_pinning_probe_blocks_each_mismatch_before_runtime(tmp_path):
    payload = _run_fault_probe(
        tmp_path,
        "source-pinning",
        "--project-root",
        str(ROOT),
    )
    assert payload["status"] == "PASS"
    matching = payload["cases"]["matching_identity"]
    assert matching["status"] == "PASS"
    assert matching["runtime_started"] is True
    assert matching["fs_diloco_imported"] is True
    assert matching["gate_fs_diloco_imported"] is False
    assert matching["business_writes"] == 1
    for name in (
        "commit_mismatch",
        "dirty_fingerprint_mismatch",
        "config_mismatch",
        "descriptor_mismatch",
        "protocol_mismatch",
        "schema_mismatch",
        "run_id_mismatch",
    ):
        case = payload["cases"][name]
        assert case["status"] == "BLOCKED"
        assert case["runtime_started"] is False
        assert case["fs_diloco_imported"] is False
        assert case["gate_fs_diloco_imported"] is False
        assert case["business_writes"] == 0
    assert payload["business_db_before"]["counts"] == {
        "syncer_leader": 0,
        "learner_instances": 0,
        "global_versions": 0,
    }
    assert payload["business_db_after"]["counts"] == {
        "syncer_leader": 1,
        "learner_instances": 0,
        "global_versions": 0,
    }
    assert payload["matching_actor_business_writes"] == 1
    assert payload["mismatch_actor_business_writes"] == 0


def test_pbs_state_normalization_and_classification():
    module = _load_script(PBS_PROBE, "plan02_pbs_capability")
    assert module.normalize_job_id("12345.miyabi\n") == "12345"
    assert module.normalize_job_id("12345[7].miyabi") == "12345[7]"
    assert module.classify_scheduler_state({"job_state": "Q", "substate": "10"}) == "queued"
    assert module.classify_scheduler_state({"job_state": "R", "substate": "41"}) == "prologue"
    assert module.classify_scheduler_state({"job_state": "R", "substate": "42"}) == "running"
    assert module.classify_scheduler_state({"job_state": "F", "substate": "92"}) == "finished"
    assert module.classify_scheduler_state(None) == "query_failed"
    assert module.classify_scheduler_state({}) == "no_record"
    assert module.classify_scheduler_state({"job_state": "S", "substate": "45"}) == "suspended"
    parsed = module.parse_qstat_full(
        """Job Id: 12345.miyabi\n    Job_Name = p02_request\n    job_state = R\n    substate = 42\n    Variable_List = A=1,\n\tB=2\n"""
    )
    assert parsed["Job_Name"] == "p02_request"
    assert parsed["Variable_List"] == "A=1,B=2"
    safe = module._safe_qstat_fields(
        {
            **parsed,
            "Exit_status": "0",
            "Secret_Field": "must-not-persist",
            "Variable_List": "TOKEN=secret,PLAN02_REQUEST_FINGERPRINT=request-1",
        }
    )
    assert safe == {
        "Job_Name": "p02_request",
        "job_state": "R",
        "substate": "42",
        "Exit_status": "0",
        "request_variables": {"PLAN02_REQUEST_FINGERPRINT": "request-1"},
    }


def test_pbs_array_probe_explicitly_requests_rerunable_mode(tmp_path, monkeypatch):
    module = _load_script(PBS_PROBE, "plan02_pbs_array_command")
    commands = []

    def fake_run(command, *, check=False):
        del check
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "12345[].opbs\n", "")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module._submit_child(
        child_script=tmp_path / "child.pbs",
        probe_root=tmp_path,
        request_fingerprint="request-array",
        project_root=ROOT,
        array=True,
    )
    assert result["returncode"] == 0
    assert commands[0][-5:-1] == ["-r", "y", "-J", "0-1"]
    assert commands[0][-1] == str(tmp_path / "child.pbs")


def test_pbs_scalar_probe_requests_a_future_start_for_queued_evidence(tmp_path, monkeypatch):
    module = _load_script(PBS_PROBE, "plan02_pbs_queued_command")
    commands = []

    def fake_run(command, *, check=False):
        del check
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "12345.opbs\n", "")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module._submit_child(
        child_script=tmp_path / "child.pbs",
        probe_root=tmp_path,
        request_fingerprint="request-scalar",
        project_root=ROOT,
        array=False,
        start_delay_seconds=5.0,
    )
    assert result["returncode"] == 0
    assert commands[0][-3] == "-a"
    assert commands[0][-1] == str(tmp_path / "child.pbs")
    assert result["start_at"] == commands[0][-2]


def test_pbs_child_marker_does_not_hide_terminal_failure(tmp_path, monkeypatch):
    module = _load_script(PBS_PROBE, "plan02_pbs_terminal_failure")
    request = "request-terminal-failure"
    artifact = tmp_path / f"child_{request}_scalar.json"
    artifact.write_text(json.dumps({"request_fingerprint": request}), encoding="utf-8")

    def fake_query(job_id, *, historical=False):
        del job_id
        if historical:
            return {
                "classification": "finished",
                "returncode": 0,
                "fields": {"job_state": "F", "Exit_status": "7"},
                "stderr": "",
            }
        return {
            "classification": "no_record",
            "returncode": 0,
            "fields": {},
            "stderr": "",
        }

    monkeypatch.setattr(module, "_query_job", fake_query)
    completion = module._wait_for_child(
        {"job_id_raw": "123.opbs", "request_fingerprint": request},
        probe_root=tmp_path,
        expected_artifacts=1,
        timeout_seconds=1.0,
    )
    assert completion["workload_artifacts_complete"] is True
    assert completion["terminal_state_observed"] is True
    assert completion["exit_status"] == 7
    assert completion["completed"] is False


def _passing_checker_input() -> dict:
    source_case = {
        "status": "BLOCKED",
        "runtime_started": False,
        "fs_diloco_imported": False,
        "gate_fs_diloco_imported": False,
        "business_writes": 0,
    }
    business_before = {
        "sha256": "before",
        "counts": {
            "syncer_leader": 0,
            "learner_instances": 0,
            "global_versions": 0,
        },
        "integrity_check": ["ok"],
    }
    business_after = {
        "sha256": "after",
        "counts": {
            "syncer_leader": 1,
            "learner_instances": 0,
            "global_versions": 0,
        },
        "integrity_check": ["ok"],
    }
    return {
        "sqlite_writer_lock": {
            "status": "PASS",
            "single_node": {"status": "PASS"},
            "host_count": 2,
            "holder_hostname": "mg001",
            "contender_hostname": "mg002",
            "successor_hostname": "mg002",
            "holder_stopped_state": "T",
            "contender_error": "database is locked",
            "visible_rows_before_kill": 0,
            "holder_exit_status": -9,
            "post_kill_rows": [["successor", "committed"]],
            "availability_boundary": {
                "paused_writer_transaction_blocks_takeover": True,
                "killing_holder_releases_lock": True,
                "uncommitted_state_rolled_back": True,
            },
            "integrity_check": ["ok"],
        },
        "old_cache_writer": {
            "status": "PASS",
            "counterexample_reproduced": True,
            "business_state_failed": False,
            "polluted_cache_epochs": {"latest": 1, "stop": 1, "summary": 1},
            "selected_canonical": {
                kind: {"artifact_kind": kind, "published_by_epoch": 2}
                for kind in ("latest", "stop", "summary")
            },
            "repair_epochs": {"latest": 2, "stop": 2, "summary": 2},
            "canonical_discovery_counts": {"latest": 2, "stop": 2, "summary": 2},
        },
        "clock_sqlite": {
            "clock": {
                "status": "PASS",
                "method": "filesystem_two_way_offset_interval",
                "host_count": 2,
                "rounds_completed": 20,
                "intersection_valid": True,
                "clock_stable": True,
                "maximum_wall_monotonic_elapsed_delta_seconds": 0.001,
                "max_clock_discontinuity_seconds": 0.1,
                "absolute_offset_upper_bound_seconds": 0.01,
                "max_clock_skew_seconds": 2.0,
                "within_bound": True,
            },
            "visibility": {
                "same_committed_state": True,
                "writer_hostname": "mg001",
                "reader_hostname": "mg002",
            },
            "contention": {
                "writer_count": 8,
                "distinct_db_writers": 8,
                "host_count": 2,
                "requested_transactions": 400,
                "committed_transactions": 400,
                "event_count": 400,
                "all_transactions_committed": True,
                "busy_errors": 20,
                "starvation_count": 0,
                "action_counts": {"acquire": 20, "renew": 380},
                "integrity_check": ["ok"],
                "pragmas": {"journal_mode": "delete", "synchronous": 2},
            },
        },
        "pbs_capability": {
            "status": "PASS",
            "state_classifier_validated": True,
            "scheduler_query_supported": True,
            "independent_job_query_supported": True,
            "automatic_submission_supported": False,
            "job_array_supported": False,
            "terminal_state_query_supported": True,
            "observed_scheduler_states": ["queued", "prologue", "running", "finished"],
            "initial_learner_orchestration": "independent_manifest",
        },
        "source_pinning": {
            "status": "PASS",
            "cases": {
                "matching_identity": {
                    "status": "PASS",
                    "runtime_started": True,
                    "fs_diloco_imported": True,
                    "gate_fs_diloco_imported": False,
                    "business_writes": 1,
                },
                "commit_mismatch": dict(source_case),
                "dirty_fingerprint_mismatch": dict(source_case),
                "config_mismatch": dict(source_case),
                "descriptor_mismatch": dict(source_case),
                "protocol_mismatch": dict(source_case),
                "schema_mismatch": dict(source_case),
                "run_id_mismatch": dict(source_case),
            },
            "matching_actor_business_writes": 1,
            "mismatch_actor_business_writes": 0,
            "business_db_before": business_before,
            "business_db_after": business_after,
        },
    }


def test_feasibility_checker_stdout_contract_and_fail_closed(tmp_path):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps(_passing_checker_input()), encoding="utf-8")
    passed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--input-json",
            str(input_path),
            "--output-json",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert passed.returncode == 0
    assert passed.stdout == "PASS\n"
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "PASS"

    blocked_input = _passing_checker_input()
    blocked_input["old_cache_writer"]["counterexample_reproduced"] = False
    input_path.write_text(json.dumps(blocked_input), encoding="utf-8")
    blocked = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--input-json",
            str(input_path),
            "--output-json",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 1
    assert blocked.stdout == "BLOCKED\n"
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["requirements"]["FEAS-02"]["status"] == "BLOCKED"


def test_feasibility_checker_rejects_missing_or_self_asserted_evidence():
    module = _load_script(CHECKER, "plan02_checker_strictness")

    empty_repair = _passing_checker_input()
    empty_repair["old_cache_writer"]["repair_epochs"] = {}
    assert module.evaluate(empty_repair)[0]["FEAS-02"]["status"] == "BLOCKED"

    not_polluted = _passing_checker_input()
    not_polluted["old_cache_writer"]["polluted_cache_epochs"]["latest"] = 2
    assert module.evaluate(not_polluted)[0]["FEAS-02"]["status"] == "BLOCKED"

    missing_lock_observation = _passing_checker_input()
    missing_lock_observation["sqlite_writer_lock"].pop("contender_error")
    assert module.evaluate(missing_lock_observation)[0]["FEAS-01"]["status"] == "BLOCKED"

    one_host_lock = _passing_checker_input()
    one_host_lock["sqlite_writer_lock"]["host_count"] = 1
    assert module.evaluate(one_host_lock)[0]["FEAS-01"]["status"] == "BLOCKED"

    no_contention = _passing_checker_input()
    no_contention["clock_sqlite"]["contention"]["busy_errors"] = 0
    assert module.evaluate(no_contention)[0]["FEAS-03"]["status"] == "BLOCKED"

    one_contention_host = _passing_checker_input()
    one_contention_host["clock_sqlite"]["contention"]["host_count"] = 1
    assert module.evaluate(one_contention_host)[0]["FEAS-03"]["status"] == "BLOCKED"

    clock_step = _passing_checker_input()
    clock_step["clock_sqlite"]["clock"]["maximum_wall_monotonic_elapsed_delta_seconds"] = 0.2
    assert module.evaluate(clock_step)[0]["FEAS-03"]["status"] == "BLOCKED"

    no_terminal = _passing_checker_input()
    no_terminal["pbs_capability"]["automatic_submission_supported"] = True
    no_terminal["pbs_capability"]["terminal_state_query_supported"] = False
    assert module.evaluate(no_terminal)[0]["FEAS-04"]["status"] == "BLOCKED"

    no_incarnation = _passing_checker_input()
    no_incarnation["pbs_capability"]["job_array_supported"] = True
    assert module.evaluate(no_incarnation)[0]["FEAS-04"]["status"] == "BLOCKED"

    inconsistent_orchestration = _passing_checker_input()
    inconsistent_orchestration["pbs_capability"]["initial_learner_orchestration"] = "pbs_job_array"
    assert module.evaluate(inconsistent_orchestration)[0]["FEAS-04"]["status"] == "BLOCKED"

    missing_business_snapshots = _passing_checker_input()
    missing_business_snapshots["source_pinning"].pop("business_db_before")
    missing_business_snapshots["source_pinning"].pop("business_db_after")
    assert module.evaluate(missing_business_snapshots)[0]["FEAS-05"]["status"] == "BLOCKED"


def test_clock_aggregate_requires_measured_two_way_bound(tmp_path):
    module = _load_script(AGGREGATE, "plan02_phase0_aggregate")
    exchange = tmp_path / "clock.json"
    exchange.write_text(
        json.dumps(
            {
                "status": "PASS",
                "method": "filesystem_two_way_offset_interval",
                "host_count": 2,
                "rounds_completed": 5,
                "intersection_valid": True,
                "maximum_wall_monotonic_elapsed_delta_seconds": 0.001,
                "absolute_offset_upper_bound_seconds": 0.25,
            }
        ),
        encoding="utf-8",
    )
    assert module._clock_summary(exchange, 0.5)["within_bound"] is True
    assert module._clock_summary(exchange, 0.1)["within_bound"] is False


def test_contention_aggregate_requires_structured_host_evidence(tmp_path):
    module = _load_script(AGGREGATE, "plan02_contention_aggregate")
    sqlite_probe = ROOT / "scripts" / "miyabi" / "sqlite_shared_fs_probe.py"
    db = tmp_path / "contention.sqlite3"
    subprocess.run(
        [sys.executable, str(sqlite_probe), "verify", "--db", str(db)],
        check=True,
        capture_output=True,
        text=True,
    )
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            """
            INSERT INTO probe_contention_events(
                writer_id, sequence, action, epoch, wait_seconds,
                busy_retries, committed_at
            ) VALUES (?, 0, ?, ?, 0.01, 1, 1.0)
            """,
            (("writer-0", "acquire", 1), ("writer-1", "renew", 1)),
        )
        conn.commit()
    finally:
        conn.close()
    paths = []
    for index, hostname in enumerate(("mg001", "mg002")):
        path = tmp_path / f"writer-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "hostname": hostname,
                    "pid": 100 + index,
                    "requested_transactions": 1,
                    "committed_transactions": 1,
                    "busy_errors": 1,
                    "starved": False,
                    "wait_seconds": {"max": 0.01},
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)
    result = module._contention_summary(paths, db)
    assert result["host_count"] == 2
    assert result["distinct_db_writers"] == result["writer_count"] == 2
    assert result["action_counts"] == {"acquire": 1, "renew": 1}

    paths[0].write_text(
        json.dumps(
            {
                "pid": 100,
                "requested_transactions": 1,
                "committed_transactions": 1,
                "busy_errors": 1,
                "starved": False,
                "wait_seconds": {"max": None},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(KeyError, match="hostname"):
        module._contention_summary(paths, db)


def test_source_gate_directly_rejects_independent_identity_and_marker_errors(tmp_path, monkeypatch):
    module = _load_script(SOURCE_GATE, "plan02_source_gate_direct")
    source = {
        "git_commit": "abc123",
        "git_dirty": False,
        "source_fingerprint": "sha256:" + "1" * 64,
    }
    monkeypatch.setattr(module, "_capture_current_source", lambda project_root: source)
    config = tmp_path / "run.yaml"
    config.write_text("run: {}\n", encoding="utf-8")
    descriptor_path = tmp_path / "descriptor.json"
    marker_path = tmp_path / "marker.json"

    def write_pair(*, descriptor_updates=None, marker_updates=None):
        descriptor = {
            **source,
            "run_id": "run-1",
            "protocol_version": 1,
            "schema_version": 2,
            "resolved_config_path": str(config),
            "resolved_config_sha256": module._sha256_file(config),
        }
        descriptor.update(descriptor_updates or {})
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        marker = {
            key: descriptor[key]
            for key in (
                "git_commit",
                "git_dirty",
                "source_fingerprint",
                "run_id",
                "protocol_version",
                "schema_version",
            )
        }
        marker.update(
            {
                "descriptor_sha256": module._sha256_file(descriptor_path),
                "config_sha256": descriptor["resolved_config_sha256"],
            }
        )
        marker.update(marker_updates or {})
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        return module.check_gate(
            project_root=tmp_path,
            descriptor_path=descriptor_path,
            bootstrap_marker_path=marker_path,
            expected_run_id="run-1",
            expected_protocol_version=1,
            expected_schema_version=2,
        )

    assert write_pair()["status"] == "PASS"
    assert "git_dirty" in write_pair(descriptor_updates={"git_dirty": True})["mismatches"]
    assert (
        "marker_protocol_version"
        in write_pair(marker_updates={"protocol_version": 2})["mismatches"]
    )
    assert (
        "marker_config_sha256"
        in write_pair(marker_updates={"config_sha256": "sha256:mismatch"})["mismatches"]
    )
    write_pair()
    config.unlink()
    missing_config = module.check_gate(
        project_root=tmp_path,
        descriptor_path=descriptor_path,
        bootstrap_marker_path=marker_path,
        expected_run_id="run-1",
        expected_protocol_version=1,
        expected_schema_version=2,
    )
    assert "resolved_config_missing" in missing_config["mismatches"]


def test_checker_persists_probe_failure_in_blocked_artifact(tmp_path):
    input_path = tmp_path / "failure.json"
    output_path = tmp_path / "blocked.json"
    failure = {"probe_failure": {"failed_line": 123, "returncode": 7}}
    input_path.write_text(json.dumps(failure), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--input-json",
            str(input_path),
            "--output-json",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert completed.stdout == "BLOCKED\n"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["status"] == "BLOCKED"
    assert output["source_evidence"] == failure


def test_phase0_pbs_scripts_use_literal_group_and_have_workload_markers():
    parent = (ROOT / "scripts" / "miyabi" / "run_plan02_phase0_feasibility.pbs").read_text()
    child = (ROOT / "scripts" / "miyabi" / "run_plan02_capability_child.pbs").read_text()
    for script in (parent, child):
        assert "#PBS -W group_list=xg24i002" in script
        assert "<group_id>" not in script
    assert "PLAN02_PHASE0_COMPLETE=" in parent
    assert "PLAN02_CAPABILITY_CHILD_COMPLETE=" in child
    assert "WORK_ROOT" not in parent
    assert 'PLAN02_PHASE0_WORK_DIR="$PLAN02_PHASE0_ARTIFACT_DIR/work_' in parent
    assert 'find "$RESOLVED_WORK_DIR" -depth -delete' in parent
    assert "emit_phase0_blocked" in parent
    assert "phase0_failure_input.json" in parent
