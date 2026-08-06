#!/usr/bin/env python3
"""Check Plan 02 Phase 0 feasibility evidence with a two-value stdout contract."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


REQUIREMENTS = ("FEAS-01", "FEAS-02", "FEAS-03", "FEAS-04", "FEAS-05")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _require(condition: bool, message: str, facts: list[str]) -> None:
    if not condition:
        facts.append(message)


def evaluate(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    requirements: dict[str, dict[str, Any]] = {}

    lock = payload.get("sqlite_writer_lock", {})
    lock_failures: list[str] = []
    boundary = lock.get("availability_boundary", {})
    _require(lock.get("status") == "PASS", "writer-lock probe did not pass", lock_failures)
    _require(
        lock.get("single_node", {}).get("status") == "PASS",
        "single-node writer-lock evidence is missing",
        lock_failures,
    )
    _require(
        int(lock.get("host_count", 0)) >= 2,
        "writer-lock probe did not cover two hosts",
        lock_failures,
    )
    _require(
        lock.get("holder_hostname") != lock.get("contender_hostname")
        and lock.get("successor_hostname") == lock.get("contender_hostname"),
        "writer-lock holder and successor were not placed on distinct hosts",
        lock_failures,
    )
    contender_error = str(lock.get("contender_error", "")).lower()
    _require(
        lock.get("holder_stopped_state") == "T",
        "writer-lock holder was not observed stopped",
        lock_failures,
    )
    _require(
        any(token in contender_error for token in ("locked", "busy")),
        "writer-lock contender did not report lock contention",
        lock_failures,
    )
    _require(
        lock.get("visible_rows_before_kill") == 0,
        "uncommitted writer row became visible",
        lock_failures,
    )
    _require(
        lock.get("holder_exit_status") == -9,
        "writer-lock holder was not killed with SIGKILL",
        lock_failures,
    )
    _require(
        lock.get("post_kill_rows") == [["successor", "committed"]],
        "successor did not observe rollback and commit the next row",
        lock_failures,
    )
    _require(
        boundary.get("paused_writer_transaction_blocks_takeover") is True,
        "paused writer did not block contender",
        lock_failures,
    )
    _require(
        boundary.get("killing_holder_releases_lock") is True,
        "killing lock holder did not release writer lock",
        lock_failures,
    )
    _require(
        boundary.get("uncommitted_state_rolled_back") is True,
        "uncommitted state survived holder death",
        lock_failures,
    )
    _require(
        lock.get("integrity_check") == ["ok"], "writer-lock DB integrity failed", lock_failures
    )
    requirements["FEAS-01"] = {
        "status": "PASS" if not lock_failures else "BLOCKED",
        "failures": lock_failures,
    }

    cache = payload.get("old_cache_writer", {})
    cache_failures: list[str] = []
    _require(cache.get("status") == "PASS", "old-cache probe did not pass", cache_failures)
    _require(
        cache.get("counterexample_reproduced") is True,
        "old writer did not overwrite fixed cache",
        cache_failures,
    )
    _require(
        cache.get("business_state_failed") is False,
        "cache pollution changed business state",
        cache_failures,
    )
    expected_kinds = {"latest", "stop", "summary"}
    polluted_epochs = cache.get("polluted_cache_epochs", {})
    _require(
        isinstance(polluted_epochs, dict)
        and set(polluted_epochs) == expected_kinds
        and all(int(value) == 1 for value in polluted_epochs.values()),
        "fixed-cache pollution evidence is missing or not stale",
        cache_failures,
    )
    selected = cache.get("selected_canonical", {})
    _require(
        isinstance(selected, dict)
        and set(selected) == expected_kinds
        and all(
            isinstance(payload, dict)
            and payload.get("artifact_kind") == kind
            and int(payload.get("published_by_epoch", -1)) == 2
            for kind, payload in selected.items()
        ),
        "reader did not select current canonical artifacts for every kind",
        cache_failures,
    )
    repair_epochs = cache.get("repair_epochs", {})
    _require(
        isinstance(repair_epochs, dict)
        and set(repair_epochs) == expected_kinds
        and all(int(value) == 2 for value in repair_epochs.values()),
        "current leader did not repair all fixed caches",
        cache_failures,
    )
    discovery_counts = cache.get("canonical_discovery_counts", {})
    _require(
        isinstance(discovery_counts, dict)
        and set(discovery_counts) == expected_kinds
        and all(int(value) >= 2 for value in discovery_counts.values()),
        "canonical scan was incomplete or silently empty",
        cache_failures,
    )
    requirements["FEAS-02"] = {
        "status": "PASS" if not cache_failures else "BLOCKED",
        "failures": cache_failures,
    }

    clock_sqlite = payload.get("clock_sqlite", {})
    sqlite_failures: list[str] = []
    clock = clock_sqlite.get("clock", {})
    visibility = clock_sqlite.get("visibility", {})
    contention = clock_sqlite.get("contention", {})
    _require(clock.get("status") == "PASS", "clock exchange did not pass", sqlite_failures)
    _require(
        clock.get("method") == "filesystem_two_way_offset_interval",
        "clock probe did not use a two-way offset interval",
        sqlite_failures,
    )
    _require(
        int(clock.get("host_count", 0)) >= 2, "clock probe did not cover two hosts", sqlite_failures
    )
    _require(
        int(clock.get("rounds_completed", 0)) >= 3,
        "clock exchange had too few rounds",
        sqlite_failures,
    )
    _require(
        clock.get("intersection_valid") is True,
        "clock offset intervals did not intersect",
        sqlite_failures,
    )
    _require(
        clock.get("clock_stable") is True, "wall clock changed during exchange", sqlite_failures
    )
    _require(
        float(clock.get("maximum_wall_monotonic_elapsed_delta_seconds", float("inf")))
        <= float(clock.get("max_clock_discontinuity_seconds", 0.0)),
        "wall/monotonic clock discontinuity exceeded the configured maximum",
        sqlite_failures,
    )
    _require(
        float(clock.get("absolute_offset_upper_bound_seconds", float("inf")))
        <= float(clock.get("max_clock_skew_seconds", 0.0)),
        "clock offset upper bound exceeded configured maximum",
        sqlite_failures,
    )
    _require(
        clock.get("within_bound") is True,
        "observed clock bound exceeded configured maximum",
        sqlite_failures,
    )
    _require(
        visibility.get("same_committed_state") is True,
        "cross-node reopen did not see committed state",
        sqlite_failures,
    )
    _require(
        visibility.get("writer_hostname") != visibility.get("reader_hostname"),
        "visibility probe did not use distinct hosts",
        sqlite_failures,
    )
    _require(
        int(contention.get("writer_count", 0)) >= 2,
        "contention probe had fewer than two writers",
        sqlite_failures,
    )
    _require(
        int(contention.get("distinct_db_writers", -1)) == int(contention.get("writer_count", 0)),
        "contention writer evidence did not match DB writers",
        sqlite_failures,
    )
    _require(
        int(contention.get("host_count", 0)) >= 2,
        "contention did not cover two hosts",
        sqlite_failures,
    )
    requested = int(contention.get("requested_transactions", -1))
    committed = int(contention.get("committed_transactions", -2))
    event_count = int(contention.get("event_count", -3))
    _require(
        requested > 0 and requested == committed == event_count,
        "contention transaction counts disagree",
        sqlite_failures,
    )
    _require(
        contention.get("all_transactions_committed") is True,
        "contention lost transactions",
        sqlite_failures,
    )
    _require(
        int(contention.get("busy_errors", 0)) > 0,
        "contention produced no busy events",
        sqlite_failures,
    )
    _require(
        int(contention.get("starvation_count", -1)) == 0,
        "contention starvation occurred",
        sqlite_failures,
    )
    actions = contention.get("action_counts", {})
    _require(
        int(actions.get("acquire", 0)) > 0 and int(actions.get("renew", 0)) > 0,
        "contention did not exercise both acquire and renew",
        sqlite_failures,
    )
    _require(
        contention.get("integrity_check") == ["ok"],
        "contention DB integrity failed",
        sqlite_failures,
    )
    pragmas = contention.get("pragmas", {})
    _require(
        str(pragmas.get("journal_mode", "")).lower() == "delete",
        "journal mode is not DELETE",
        sqlite_failures,
    )
    _require(int(pragmas.get("synchronous", -1)) == 2, "synchronous is not FULL", sqlite_failures)
    requirements["FEAS-03"] = {
        "status": "PASS" if not sqlite_failures else "BLOCKED",
        "failures": sqlite_failures,
    }

    pbs = payload.get("pbs_capability", {})
    pbs_failures: list[str] = []
    _require(pbs.get("status") == "PASS", "PBS capability probe did not complete", pbs_failures)
    _require(
        pbs.get("state_classifier_validated") is True,
        "PBS state classifier matrix failed",
        pbs_failures,
    )
    _require(
        pbs.get("scheduler_query_supported") is True,
        "scheduler query path is unavailable",
        pbs_failures,
    )
    _require(
        pbs.get("independent_job_query_supported") is True,
        "independent parent job query path is unavailable",
        pbs_failures,
    )
    if pbs.get("automatic_submission_supported") is True:
        observed_states = set(pbs.get("observed_scheduler_states", []))
        _require(
            {"queued", "prologue", "running", "finished"}.issubset(observed_states),
            "real scheduler lifecycle evidence is incomplete",
            pbs_failures,
        )
        _require(
            pbs.get("terminal_state_query_supported") is True,
            "scheduler terminal-state query was not verified",
            pbs_failures,
        )
    orchestration = pbs.get("initial_learner_orchestration")
    array_supported = pbs.get("job_array_supported") is True
    if array_supported:
        _require(
            pbs.get("automatic_submission_supported") is True and orchestration == "pbs_job_array",
            "array capability and selected orchestration are inconsistent",
            pbs_failures,
        )
        array_evidence = pbs.get("rerunable_job_evidence", {}).get("array", {})
        incarnations = array_evidence.get("physical_incarnations", {})
        _require(
            str(array_evidence.get("Rerunable", "")).lower() == "true",
            "array job was not recorded as rerunable",
            pbs_failures,
        )
        _require(
            isinstance(incarnations, dict)
            and len(incarnations) == 2
            and all(
                int(item.get("run_count", 0)) >= 1 and int(item.get("Exit_status", -1)) == 0
                for item in incarnations.values()
            ),
            "array physical incarnation evidence is incomplete",
            pbs_failures,
        )
    else:
        _require(
            orchestration == "independent_manifest",
            "array fallback did not select the independent manifest",
            pbs_failures,
        )
    requirements["FEAS-04"] = {
        "status": "PASS" if not pbs_failures else "BLOCKED",
        "failures": pbs_failures,
        "automatic_submission_supported": pbs.get("automatic_submission_supported"),
        "job_array_supported": pbs.get("job_array_supported"),
        "selected_orchestration": pbs.get("initial_learner_orchestration"),
    }

    source = payload.get("source_pinning", {})
    source_failures: list[str] = []
    _require(source.get("status") == "PASS", "source pinning probe did not pass", source_failures)
    cases = source.get("cases", {})
    matching_case = cases.get("matching_identity", {})
    _require(
        matching_case.get("status") == "PASS",
        "matching source identity was rejected",
        source_failures,
    )
    _require(
        matching_case.get("gate_fs_diloco_imported") is False
        and matching_case.get("runtime_started") is True
        and matching_case.get("fs_diloco_imported") is True
        and int(matching_case.get("business_writes", 0)) == 1,
        "matching source identity did not reach the guarded runtime write",
        source_failures,
    )
    for name in (
        "commit_mismatch",
        "dirty_fingerprint_mismatch",
        "config_mismatch",
        "descriptor_mismatch",
        "protocol_mismatch",
        "schema_mismatch",
        "run_id_mismatch",
    ):
        case = cases.get(name, {})
        _require(case.get("status") == "BLOCKED", f"{name} did not fail closed", source_failures)
        _require(case.get("runtime_started") is False, f"{name} reached runtime", source_failures)
        _require(
            case.get("fs_diloco_imported") is False, f"{name} imported fs_diloco", source_failures
        )
        _require(
            case.get("gate_fs_diloco_imported") is False,
            f"{name} gate imported fs_diloco",
            source_failures,
        )
        _require(
            int(case.get("business_writes", -1)) == 0,
            f"{name} wrote business state",
            source_failures,
        )
    business_before = source.get("business_db_before")
    business_after = source.get("business_db_after")
    _require(
        isinstance(business_before, dict)
        and business_before.get("counts")
        == {"syncer_leader": 0, "learner_instances": 0, "global_versions": 0},
        "source-gate business DB did not start empty",
        source_failures,
    )
    _require(
        isinstance(business_after, dict)
        and business_after.get("counts")
        == {"syncer_leader": 1, "learner_instances": 0, "global_versions": 0},
        "source-gate control runtime write was not isolated",
        source_failures,
    )
    _require(
        int(source.get("matching_actor_business_writes", 0)) == 1,
        "matching actor did not write control row",
        source_failures,
    )
    _require(
        int(source.get("mismatch_actor_business_writes", -1)) == 0,
        "mismatch actor wrote business state",
        source_failures,
    )
    requirements["FEAS-05"] = {
        "status": "PASS" if not source_failures else "BLOCKED",
        "failures": source_failures,
    }

    for requirement_id in REQUIREMENTS:
        failures.extend(
            f"{requirement_id}: {failure}" for failure in requirements[requirement_id]["failures"]
        )
    return requirements, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source: dict[str, Any] = {}
    try:
        source = _read_object(args.input_json)
        requirements, failures = evaluate(source)
    except BaseException as exc:
        requirements = {
            requirement_id: {"status": "BLOCKED", "failures": []} for requirement_id in REQUIREMENTS
        }
        failures = [f"checker exception: {type(exc).__name__}: {exc}"]
    status = "PASS" if not failures else "BLOCKED"
    output = {
        "artifact_format_version": 1,
        "plan_id": "fsb_decoupled_diloco_plan_02",
        "phase": "Phase 0",
        "checked_at": time.time(),
        "status": status,
        "requirements": requirements,
        "failures": failures,
        "source_evidence": source,
    }
    _atomic_write_json(args.output_json, output)
    print(status)
    raise SystemExit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()
