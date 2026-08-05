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
    _require(lock.get("integrity_check") == ["ok"], "writer-lock DB integrity failed", lock_failures)
    requirements["FEAS-01"] = {"status": "PASS" if not lock_failures else "BLOCKED", "failures": lock_failures}

    cache = payload.get("old_cache_writer", {})
    cache_failures: list[str] = []
    _require(cache.get("status") == "PASS", "old-cache probe did not pass", cache_failures)
    _require(cache.get("counterexample_reproduced") is True, "old writer did not overwrite fixed cache", cache_failures)
    _require(cache.get("cache_pollution_reported") is True, "cache pollution was not reported", cache_failures)
    _require(cache.get("business_state_failed") is False, "cache pollution changed business state", cache_failures)
    selected = cache.get("selected_canonical", {})
    _require(int(selected.get("published_by_epoch", -1)) == 2, "reader did not select highest canonical epoch", cache_failures)
    _require(
        all(int(value) == 2 for value in cache.get("repair_epochs", {}).values()),
        "current leader did not repair all fixed caches",
        cache_failures,
    )
    _require(int(cache.get("canonical_discovery_count", 0)) >= 2, "canonical scan was silently empty", cache_failures)
    requirements["FEAS-02"] = {"status": "PASS" if not cache_failures else "BLOCKED", "failures": cache_failures}

    clock_sqlite = payload.get("clock_sqlite", {})
    sqlite_failures: list[str] = []
    clock = clock_sqlite.get("clock", {})
    visibility = clock_sqlite.get("visibility", {})
    contention = clock_sqlite.get("contention", {})
    _require(int(clock.get("host_count", 0)) >= 2, "clock probe did not cover two hosts", sqlite_failures)
    _require(clock.get("within_bound") is True, "observed clock bound exceeded configured maximum", sqlite_failures)
    _require(visibility.get("same_committed_state") is True, "cross-node reopen did not see committed state", sqlite_failures)
    _require(
        visibility.get("writer_hostname") != visibility.get("reader_hostname"),
        "visibility probe did not use distinct hosts",
        sqlite_failures,
    )
    _require(int(contention.get("writer_count", 0)) >= 2, "contention probe had fewer than two writers", sqlite_failures)
    _require(contention.get("all_transactions_committed") is True, "contention lost transactions", sqlite_failures)
    _require(int(contention.get("starvation_count", -1)) == 0, "contention starvation occurred", sqlite_failures)
    _require(contention.get("integrity_check") == ["ok"], "contention DB integrity failed", sqlite_failures)
    pragmas = contention.get("pragmas", {})
    _require(str(pragmas.get("journal_mode", "")).lower() == "delete", "journal mode is not DELETE", sqlite_failures)
    _require(int(pragmas.get("synchronous", -1)) == 2, "synchronous is not FULL", sqlite_failures)
    requirements["FEAS-03"] = {"status": "PASS" if not sqlite_failures else "BLOCKED", "failures": sqlite_failures}

    pbs = payload.get("pbs_capability", {})
    pbs_failures: list[str] = []
    _require(pbs.get("status") == "PASS", "PBS capability probe did not complete", pbs_failures)
    _require(pbs.get("state_classifier_validated") is True, "PBS state classifier matrix failed", pbs_failures)
    _require(
        pbs.get("manual_independent_restart_supported") is True,
        "manual independent restart/query path is unavailable",
        pbs_failures,
    )
    _require(
        pbs.get("initial_learner_orchestration") in {"pbs_job_array", "independent_manifest"},
        "no valid initial learner orchestration was selected",
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
    _require(cases.get("matching_identity", {}).get("status") == "PASS", "matching source identity was rejected", source_failures)
    for name in ("commit_mismatch", "dirty_fingerprint_mismatch", "config_mismatch", "descriptor_mismatch"):
        case = cases.get(name, {})
        _require(case.get("status") == "BLOCKED", f"{name} did not fail closed", source_failures)
        _require(case.get("runtime_started") is False, f"{name} reached runtime", source_failures)
        _require(case.get("fs_diloco_imported") is False, f"{name} imported fs_diloco", source_failures)
    _require(int(source.get("mismatch_actor_business_writes", -1)) == 0, "mismatch actor wrote business state", source_failures)
    _require(source.get("business_db_before") == source.get("business_db_after"), "source gate mutated business DB", source_failures)
    requirements["FEAS-05"] = {"status": "PASS" if not source_failures else "BLOCKED", "failures": source_failures}

    for requirement_id in REQUIREMENTS:
        failures.extend(
            f"{requirement_id}: {failure}"
            for failure in requirements[requirement_id]["failures"]
        )
    return requirements, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        source = _read_object(args.input_json)
        requirements, failures = evaluate(source)
    except BaseException as exc:
        requirements = {requirement_id: {"status": "BLOCKED", "failures": []} for requirement_id in REQUIREMENTS}
        failures = [f"checker exception: {type(exc).__name__}: {exc}"]
        source = {}
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
