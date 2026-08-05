import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
FAULT_PROBE = ROOT / "scripts" / "miyabi" / "plan02_fault_probe.py"
CHECKER = ROOT / "scripts" / "miyabi" / "check_plan02_feasibility.py"
PBS_PROBE = ROOT / "scripts" / "miyabi" / "plan02_pbs_capability.py"


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


def test_old_cache_writer_probe_selects_canonical_and_repairs(tmp_path):
    payload = _run_fault_probe(tmp_path, "old-cache-writer")
    assert payload["counterexample_reproduced"] is True
    assert set(payload["polluted_cache_epochs"].values()) == {1}
    assert payload["selected_canonical"]["published_by_epoch"] == 2
    assert set(payload["repair_epochs"].values()) == {2}
    assert payload["canonical_discovery_count"] == 2


def test_source_pinning_probe_blocks_each_mismatch_before_runtime(tmp_path):
    payload = _run_fault_probe(
        tmp_path,
        "source-pinning",
        "--project-root",
        str(ROOT),
    )
    assert payload["status"] == "PASS"
    assert payload["cases"]["matching_identity"]["status"] == "PASS"
    for name in (
        "commit_mismatch",
        "dirty_fingerprint_mismatch",
        "config_mismatch",
        "descriptor_mismatch",
    ):
        case = payload["cases"][name]
        assert case["status"] == "BLOCKED"
        assert case["runtime_started"] is False
        assert case["fs_diloco_imported"] is False
    assert payload["business_db_before"] == payload["business_db_after"]


def test_pbs_state_normalization_and_classification():
    module = _load_script(PBS_PROBE, "plan02_pbs_capability")
    assert module.normalize_job_id("12345.miyabi\n") == "12345"
    assert module.normalize_job_id("12345[7].miyabi") == "12345[7]"
    assert module.classify_scheduler_state({"job_state": "Q", "substate": "10"}) == "queued"
    assert module.classify_scheduler_state({"job_state": "R", "substate": "41"}) == "prologue"
    assert module.classify_scheduler_state({"job_state": "R", "substate": "42"}) == "running"
    assert module.classify_scheduler_state({"job_state": "F", "substate": "92"}) == "finished"
    assert module.classify_scheduler_state(None) == "unknown"
    parsed = module.parse_qstat_full(
        """Job Id: 12345.miyabi\n    Job_Name = p02_request\n    job_state = R\n    substate = 42\n    Variable_List = A=1,\n\tB=2\n"""
    )
    assert parsed["Job_Name"] == "p02_request"
    assert parsed["Variable_List"] == "A=1,B=2"


def test_pbs_array_probe_explicitly_requests_rerunable_mode():
    source = PBS_PROBE.read_text(encoding="utf-8")
    assert 'command.extend(("-r", "y", "-J", "0-1"))' in source


def _passing_checker_input() -> dict:
    source_case = {"status": "BLOCKED", "runtime_started": False, "fs_diloco_imported": False}
    business = {"sha256": "abc", "counts": {"syncer_leader": 0}, "integrity_check": ["ok"]}
    return {
        "sqlite_writer_lock": {
            "status": "PASS",
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
            "cache_pollution_reported": True,
            "business_state_failed": False,
            "selected_canonical": {"published_by_epoch": 2},
            "repair_epochs": {"latest": 2, "stop": 2, "summary": 2},
            "canonical_discovery_count": 2,
        },
        "clock_sqlite": {
            "clock": {"host_count": 2, "within_bound": True},
            "visibility": {
                "same_committed_state": True,
                "writer_hostname": "mg001",
                "reader_hostname": "mg002",
            },
            "contention": {
                "writer_count": 8,
                "all_transactions_committed": True,
                "starvation_count": 0,
                "integrity_check": ["ok"],
                "pragmas": {"journal_mode": "delete", "synchronous": 2},
            },
        },
        "pbs_capability": {
            "status": "PASS",
            "state_classifier_validated": True,
            "manual_independent_restart_supported": True,
            "automatic_submission_supported": False,
            "job_array_supported": False,
            "initial_learner_orchestration": "independent_manifest",
        },
        "source_pinning": {
            "status": "PASS",
            "cases": {
                "matching_identity": {"status": "PASS"},
                "commit_mismatch": dict(source_case),
                "dirty_fingerprint_mismatch": dict(source_case),
                "config_mismatch": dict(source_case),
                "descriptor_mismatch": dict(source_case),
            },
            "mismatch_actor_business_writes": 0,
            "business_db_before": business,
            "business_db_after": business,
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
