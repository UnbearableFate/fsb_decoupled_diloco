"""Compare matched static/dynamic complete runtimes for Phase 2 overhead."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..core.config import config_to_dict
from ..core.run_descriptor import load_run_descriptor
from ..storage.atomic_io import atomic_write_json, safe_read_json
from ..storage.paths import RunPaths


def _summary(paths: RunPaths) -> dict[str, Any] | None:
    candidates = sorted(paths.syncer_epochs.glob("e*_*/terminal/summary_g*.json"))
    payloads = [safe_read_json(path) for path in candidates]
    valid = [payload for payload in payloads if isinstance(payload, dict)]
    return None if not valid else max(
        valid,
        key=lambda row: (int(row.get("epoch", 0)), int(row.get("generation", 0))),
    )


def _normalized_config(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(payload))
    normalized["run"]["run_id"] = "<matched>"
    normalized["run"]["shared_root"] = "<matched>"
    normalized["membership"]["mode"] = "<membership-switch>"
    normalized["scaling"]["enabled"] = "<scaling-switch>"
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-run-root", type=Path, required=True)
    parser.add_argument("--dynamic-run-root", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    static = load_run_descriptor(args.static_run_root)
    dynamic = load_run_descriptor(args.dynamic_run_root)
    static_summary = _summary(static.paths)
    dynamic_summary = _summary(dynamic.paths)
    receipts = safe_read_json(args.receipts) or {}
    errors: list[str] = []
    if receipts.get("status") != "PASS":
        errors.append("matched launcher receipts are not PASS")
    if static.descriptor.get("mode") != "full_ha_static":
        errors.append("static descriptor mode mismatch")
    if dynamic.descriptor.get("mode") != "full_ha_dynamic":
        errors.append("dynamic descriptor mode mismatch")
    for field in ("git_commit", "source_fingerprint"):
        if static.descriptor.get(field) != dynamic.descriptor.get(field):
            errors.append(f"matched descriptor {field} mismatch")
    if _normalized_config(config_to_dict(static.config)) != _normalized_config(
        config_to_dict(dynamic.config)
    ):
        errors.append("matched configs differ outside membership/scaling switches")
    for name, summary in (("static", static_summary), ("dynamic", dynamic_summary)):
        if summary is None:
            errors.append(f"{name} completed summary is missing")
            continue
        if summary.get("stop_reason") != "stop_after_outer_steps":
            errors.append(f"{name} stop reason mismatch")
        if int(summary.get("final_version", -1)) != int(
            dynamic.config.sync.stop_after_outer_steps or -1
        ):
            errors.append(f"{name} final version mismatch")
    static_seconds = (
        None
        if static_summary is None
        else float(static_summary["complete_training_time_seconds"])
    )
    dynamic_seconds = (
        None
        if dynamic_summary is None
        else float(dynamic_summary["complete_training_time_seconds"])
    )
    ratio = (
        None
        if static_seconds is None or dynamic_seconds is None or static_seconds <= 0.0
        else max(0.0, dynamic_seconds - static_seconds) / static_seconds
    )
    if ratio is None or ratio >= 0.05:
        errors.append(f"dynamic matched complete-time overhead ratio is {ratio}, expected < 0.05")
    payload = {
        "checker": "plan02_phase2_matched_performance",
        "status": "PASS" if not errors else "BLOCKED",
        "identity": {
            "git_commit": dynamic.descriptor["git_commit"],
            "source_fingerprint": dynamic.descriptor["source_fingerprint"],
            "dynamic_descriptor_sha256": dynamic.descriptor["descriptor_sha256"],
            "static_descriptor_sha256": static.descriptor["descriptor_sha256"],
        },
        "static": {
            "run_root": str(static.paths.shared_root),
            "complete_training_time_seconds": static_seconds,
            "summary": static_summary,
        },
        "dynamic": {
            "run_root": str(dynamic.paths.shared_root),
            "complete_training_time_seconds": dynamic_seconds,
            "summary": dynamic_summary,
        },
        "dynamic_control_overhead_ratio": ratio,
        "threshold_ratio": 0.05,
        "errors": errors,
    }
    atomic_write_json(args.output, payload)
    print(payload["status"])
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
