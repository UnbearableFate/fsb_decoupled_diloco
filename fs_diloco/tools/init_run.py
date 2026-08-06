"""Initialize an immutable full-mode HA run before submitting any PBS role."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from ..core.config import Config, resolve_config, write_resolved_config
from ..core.constants import DYNAMIC_SCHEMA_VERSION, HA_SCHEMA_VERSION, PROTOCOL_VERSION
from ..storage.atomic_io import atomic_write_json, sha256_file
from ..storage.paths import RunPaths, prepare_authority_dirs
from ..storage.schema_bootstrap import BootstrapIdentity, initialize_new_run


def _sha256_json(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def initialize_run(
    config: Config,
    *,
    project_root: str | Path,
    allow_dirty_snapshot: bool = False,
) -> dict[str, Any]:
    if not config.coordination.syncer_ha.enabled:
        raise ValueError("init-run requires coordination.syncer_ha.enabled=true")
    if config.fragments.enabled:
        raise ValueError("fragment mode cannot use HA bootstrap")
    if not config.run.run_id or not config.run.shared_root:
        raise ValueError("resolved run_id and shared_root are required")
    if not config.run.git_commit:
        raise ValueError("HA bootstrap requires run.git_commit")
    if not config.run.source_fingerprint:
        raise ValueError("HA bootstrap requires run.source_fingerprint")
    if config.run.git_dirty is not False and not allow_dirty_snapshot:
        raise ValueError("formal HA bootstrap requires clean source or --allow-dirty-snapshot")

    project_root = Path(project_root).resolve()
    paths = RunPaths(Path(config.run.shared_root).resolve())
    if paths.shared_root.exists():
        raise FileExistsError(
            f"run root already exists; bootstrap recovery is operator-only: {paths.shared_root}"
        )
    paths.shared_root.mkdir(parents=True, exist_ok=False)
    prepare_authority_dirs(paths)
    write_resolved_config(config, paths.resolved_config_yaml)
    write_resolved_config(config, paths.run_root_config_yaml)
    config_sha256 = sha256_file(paths.resolved_config_yaml)
    source_manifest = {
        "format_version": 1,
        "git_commit": config.run.git_commit,
        "git_dirty": config.run.git_dirty,
        "source_fingerprint": config.run.source_fingerprint,
        "project_root": str(project_root),
        "python_entrypoint": "python -m fs_diloco.tools.init_run",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "created_at": time.time(),
    }
    source_manifest["manifest_sha256"] = _sha256_json(source_manifest)
    atomic_write_json(paths.run_source_manifest_json, source_manifest)
    dynamic = config.membership.mode == "dynamic"
    schema_version = DYNAMIC_SCHEMA_VERSION if dynamic else HA_SCHEMA_VERSION
    descriptor_mode = "full_ha_dynamic" if dynamic else "full_ha_static"
    bootstrap_slots = (
        config.membership.bootstrap_instances if dynamic else config.sync.num_learners
    )
    descriptor = {
        "format_version": 1,
        "run_id": config.run.run_id,
        "shared_root": str(paths.shared_root),
        "resolved_config_path": str(paths.resolved_config_yaml),
        "resolved_config_sha256": config_sha256,
        "source_manifest_path": str(paths.run_source_manifest_json),
        "source_manifest_sha256": sha256_file(paths.run_source_manifest_json),
        "source_fingerprint": config.run.source_fingerprint,
        "git_commit": config.run.git_commit,
        "git_dirty": config.run.git_dirty,
        "mode": descriptor_mode,
        "bootstrap_slots": int(bootstrap_slots),
        "stream_pool_size": (
            int(config.membership.stream_pool_size) if dynamic else None
        ),
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": schema_version,
        "created_at": time.time(),
    }
    descriptor["descriptor_sha256"] = _sha256_json(descriptor)
    atomic_write_json(paths.run_descriptor_json, descriptor)
    identity = BootstrapIdentity(
        run_id=config.run.run_id,
        source_fingerprint=config.run.source_fingerprint,
        config_sha256=config_sha256,
        mode="full_dynamic" if dynamic else "full",
    )
    marker = initialize_new_run(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        busy_timeout_ms=config.coordination.syncer_ha.lease_busy_timeout_ms,
    )
    return {"descriptor": descriptor, "bootstrap": marker}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--shared-root")
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--allow-dirty-snapshot", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = resolve_config(
        args.config,
        run_id=args.run_id,
        shared_root=args.shared_root,
        project_root=args.project_root,
    )
    result = initialize_run(
        config,
        project_root=args.project_root,
        allow_dirty_snapshot=args.allow_dirty_snapshot,
    )
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
