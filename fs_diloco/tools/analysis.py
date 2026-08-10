"""Inspect one current Full Protocol run through its durable authority."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Any

from ..core.run_descriptor import LoadedRunDescriptor, load_run_descriptor
from ..protocol.contributor import DynamicMembershipScope, StaticMembershipScope
from ..storage.authority import AuthorityIdentity, AuthorityReader


def _scope(loaded: LoadedRunDescriptor) -> StaticMembershipScope | DynamicMembershipScope:
    if loaded.descriptor["mode"] == "dynamic":
        return DynamicMembershipScope(int(loaded.descriptor["stream_pool_size"]))
    return StaticMembershipScope(tuple(loaded.descriptor["static_learner_ids"]))


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def summarize_run(shared_root: str | Path) -> dict[str, Any]:
    loaded = load_run_descriptor(shared_root)
    authority = AuthorityReader(
        loaded.paths.sqlite_db,
        AuthorityIdentity(**loaded.identity.as_dict()),
        _scope(loaded),
        marker_path=loaded.paths.bootstrap_complete_json,
        busy_timeout_ms=loaded.config.leader.business_busy_timeout_ms,
    )
    try:
        fences = authority.read.current_contributor_fences()
        progress = {
            fence.stable_contributor_key: _json_value(
                authority.read.contributor_progress(fence.stable_contributor_key)
            )
            for fence in fences
        }
        summary = {
            "run": {
                "shared_root": str(loaded.paths.shared_root),
                "run_id": loaded.identity.run_id,
                "mode": loaded.descriptor["mode"],
                "descriptor_sha256": loaded.descriptor["descriptor_sha256"],
                "source_fingerprint": loaded.identity.source_fingerprint,
                "config_sha256": loaded.identity.config_sha256,
            },
            "authority": _json_value(authority.read.metadata()),
            "integrity_check": list(authority.read.integrity_check()),
            "controller": authority.read.controller_status(),
            "latest_version": _json_value(authority.read.latest_committed_version()),
            "current_contributor_fences": [_json_value(fence) for fence in fences],
            "contributor_progress": progress,
            "token_ledger": _json_value(authority.read.token_ledger_summary()),
            "terminal": authority.read.terminal_record(),
            "terminal_contributor_fences": list(
                authority.read.terminal_contributor_fences()
            ),
            "syncer_epochs": list(authority.read.syncer_epochs()),
            "dynamic": {
                "streams": list(authority.read.dynamic_streams()),
                "instances": list(authority.read.dynamic_instances()),
                "launch_requests": list(authority.read.dynamic_launch_requests()),
                "capacity_observations": list(authority.read.capacity_observations()),
            },
            "audit": authority.read.audit_archive_summary(),
        }
    finally:
        authority.close()
    return summary


def assert_summary(
    summary: dict[str, Any],
    *,
    expected_learners: int | None,
    expected_global_steps: int | None,
    require_terminal: bool,
) -> None:
    errors: list[str] = []
    if summary["integrity_check"] != ["ok"]:
        errors.append(f"authority integrity check failed: {summary['integrity_check']}")
    latest = summary.get("latest_version") or {}
    if expected_global_steps is not None and latest.get("version") != expected_global_steps:
        errors.append(
            f"latest version is {latest.get('version')}, expected {expected_global_steps}"
        )
    if expected_learners is not None:
        terminal_fences = summary.get("terminal_contributor_fences") or []
        current_fences = summary.get("current_contributor_fences") or []
        observed = terminal_fences if terminal_fences else current_fences
        if len(observed) != expected_learners:
            errors.append(f"contributor count is {len(observed)}, expected {expected_learners}")
    if require_terminal and summary.get("terminal") is None:
        errors.append("terminal authority record is missing")
    if errors:
        raise RuntimeError("run assertion failed:\n" + "\n".join(f"- {item}" for item in errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shared_root", type=Path)
    parser.add_argument("--expected-learners", type=int)
    parser.add_argument("--expected-global-steps", type=int)
    parser.add_argument("--require-terminal", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    summary = summarize_run(args.shared_root)
    assert_summary(
        summary,
        expected_learners=args.expected_learners,
        expected_global_steps=args.expected_global_steps,
        require_terminal=args.require_terminal,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
