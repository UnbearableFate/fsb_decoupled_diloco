"""Initialize an immutable Full Protocol run before submitting PBS actors."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..core.config import (
    Config,
    resolve_config,
    resolved_config_bytes,
    write_resolved_config,
)
from ..core.source_identity import bind_source_identity
from ..core.versions import (
    AUTHORITY_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    RUN_DESCRIPTOR_FORMAT_VERSION,
    SOURCE_MANIFEST_FORMAT_VERSION,
)
from ..protocol.contributor import DynamicMembershipScope, StaticMembershipScope
from ..storage.atomic_io import atomic_write_json, sha256_file
from ..storage.artifact_policy import build_artifact_policy
from ..storage.paths import RunPaths, prepare_authority_dirs
from ..storage.run_initializer import (
    claim_identity_reservation,
    create_staging_root,
    find_reserved_staging,
    build_complete_manifest,
    publish_staged_run,
    remove_completed_staging,
    validate_completed_run,
    write_complete_source,
    write_run_identity,
)
from ..storage.authority import AuthorityIdentity, initialize_authority


def _sha256_json(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def initialize_run(
    config: Config,
    *,
    project_root: str | Path,
    allow_dirty_snapshot: bool = False,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    config.validate()
    shared = config
    if not shared.run.run_id or not shared.run.shared_root:
        raise ValueError("resolved run_id and shared_root are required")
    if not shared.run.git_commit:
        raise ValueError("run bootstrap requires run.git_commit")
    if not shared.run.source_fingerprint:
        raise ValueError("run bootstrap requires run.source_fingerprint")
    if shared.run.git_dirty is not False and not allow_dirty_snapshot:
        raise ValueError("formal bootstrap requires clean source or --allow-dirty-snapshot")

    project_root = Path(project_root).resolve()
    final_root = Path(shared.run.shared_root).resolve()
    final_paths = RunPaths(final_root)
    expected_config_sha256 = hashlib.sha256(resolved_config_bytes(config)).hexdigest()
    expected_mode = shared.membership.mode
    if final_paths.run_complete_file.is_file():
        validate_completed_run(final_root)
        completed_staging = find_reserved_staging(final_root, allow_missing_owner=True)
        if completed_staging is not None:
            remove_completed_staging(final_root=final_root, staging_root=completed_staging)
        descriptor = json.loads(final_paths.run_descriptor_json.read_text(encoding="utf-8"))
        expected_descriptor_identity = {
            "run_id": shared.run.run_id,
            "shared_root": str(final_root),
            "resolved_config_sha256": expected_config_sha256,
            "source_fingerprint": shared.run.source_fingerprint,
            "git_commit": shared.run.git_commit,
            "git_dirty": shared.run.git_dirty,
            "mode": shared.membership.mode,
        }
        mismatches = {
            key: (descriptor.get(key), value)
            for key, value in expected_descriptor_identity.items()
            if descriptor.get(key) != value
        }
        if mismatches:
            raise FileExistsError(
                f"completed run root belongs to a different full config identity: {mismatches}"
            )
        bootstrap = json.loads(final_paths.bootstrap_complete_json.read_text(encoding="utf-8"))
        return {"descriptor": descriptor, "bootstrap": bootstrap, "recovered": True}
    final_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = find_reserved_staging(final_root)
    recovered = staging_root is not None
    if staging_root is None:
        if final_root.exists():
            raise FileExistsError("partial run root has no matching identity reservation")
        staging_root = create_staging_root(final_root)
        _populate_staging(
            config,
            project_root=project_root,
            staging_root=staging_root,
            logical_root=final_root,
        )
        claim_identity_reservation(
            final_root=final_root,
            staging_root=staging_root,
            fault_hook=fault_hook,
        )
    else:
        identity = json.loads((staging_root / ".identity").read_text(encoding="utf-8"))
        expected_identity = {
            "run_id": shared.run.run_id,
            "source_fingerprint": shared.run.source_fingerprint,
            "config_sha256": expected_config_sha256,
            "mode": expected_mode,
        }
        mismatches = {
            key: (identity.get(key), value)
            for key, value in expected_identity.items()
            if identity.get(key) != value
        }
        if mismatches:
            raise FileExistsError(
                f"reserved staging root belongs to a different full config identity: {mismatches}"
            )
    publish_staged_run(
        final_root=final_root,
        staging_root=staging_root,
        fault_hook=fault_hook,
    )
    remove_completed_staging(final_root=final_root, staging_root=staging_root)
    descriptor = json.loads(final_paths.run_descriptor_json.read_text(encoding="utf-8"))
    bootstrap = json.loads(final_paths.bootstrap_complete_json.read_text(encoding="utf-8"))
    return {"descriptor": descriptor, "bootstrap": bootstrap, "recovered": recovered}


def _populate_staging(
    config: Config,
    *,
    project_root: Path,
    staging_root: Path,
    logical_root: Path,
) -> None:
    shared = config
    paths = RunPaths(staging_root)
    prepare_authority_dirs(paths)
    write_resolved_config(config, paths.resolved_config_yaml)
    write_resolved_config(config, paths.run_root_config_yaml)
    config_sha256 = sha256_file(paths.resolved_config_yaml)
    lock_path = project_root / "uv.lock"
    source_manifest = {
        "format_version": SOURCE_MANIFEST_FORMAT_VERSION,
        "git_commit": shared.run.git_commit,
        "git_dirty": shared.run.git_dirty,
        "source_fingerprint": shared.run.source_fingerprint,
        "project_root": str(project_root),
        "uv_lock_sha256": sha256_file(lock_path) if lock_path.is_file() else None,
        "python_entrypoint": "python -m fs_diloco.tools.init_run",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "created_at": time.time(),
    }
    source_manifest["manifest_sha256"] = _sha256_json(source_manifest)
    atomic_write_json(paths.run_source_manifest_json, source_manifest)
    dynamic = shared.membership.mode == "dynamic"
    schema_version = AUTHORITY_SCHEMA_VERSION
    descriptor_mode = shared.membership.mode
    bootstrap_slots = shared.membership.bootstrap_instances if dynamic else shared.sync.num_learners
    descriptor = {
        "format_version": RUN_DESCRIPTOR_FORMAT_VERSION,
        "run_id": shared.run.run_id,
        "shared_root": str(logical_root),
        "resolved_config_path": str(logical_root / "control/run_config.resolved.yaml"),
        "resolved_config_sha256": config_sha256,
        "source_manifest_path": str(logical_root / "control/run_source_manifest.json"),
        "source_manifest_sha256": sha256_file(paths.run_source_manifest_json),
        "source_fingerprint": shared.run.source_fingerprint,
        "git_commit": shared.run.git_commit,
        "git_dirty": shared.run.git_dirty,
        "mode": descriptor_mode,
        "bootstrap_slots": int(bootstrap_slots),
        "static_learner_ids": (
            [f"learner_{index:03d}" for index in range(shared.sync.num_learners)]
            if not dynamic
            else None
        ),
        "stream_pool_size": (int(shared.membership.stream_pool_size) if dynamic else None),
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": schema_version,
        "source_lock_sha256": source_manifest["uv_lock_sha256"],
        "model_identity": {
            "name_or_path": shared.model.name_or_path,
            "revision": shared.model.revision,
        },
        "tokenizer_identity": {
            "name_or_path": shared.model.name_or_path,
            "revision": shared.model.tokenizer_revision or shared.model.revision,
        },
        "dataset_identity": {
            "name": shared.data.dataset_name,
            "config_name": shared.data.dataset_config_name,
            "revision": shared.data.revision,
            "train_split": shared.data.train_split,
            "block_size": shared.data.block_size,
        },
        "created_at": time.time(),
    }
    descriptor["descriptor_sha256"] = _sha256_json(descriptor)
    atomic_write_json(paths.run_descriptor_json, descriptor)
    identity = AuthorityIdentity(
        run_id=shared.run.run_id,
        source_fingerprint=shared.run.source_fingerprint,
        config_sha256=config_sha256,
    )
    membership_scope = (
        DynamicMembershipScope(shared.membership.stream_pool_size)
        if dynamic
        else StaticMembershipScope(
            tuple(f"learner_{index:03d}" for index in range(shared.sync.num_learners))
        )
    )
    initialize_authority(
        paths.sqlite_db,
        identity,
        membership_scope,
        marker_path=paths.bootstrap_complete_json,
        busy_timeout_ms=config.leader.business_busy_timeout_ms,
    )
    artifact_policy = build_artifact_policy()
    atomic_write_json(paths.artifact_policy_json, artifact_policy)
    write_run_identity(
        staging_root,
        {
            "format_version": 1,
            "run_id": shared.run.run_id,
            "source_fingerprint": shared.run.source_fingerprint,
            "config_sha256": config_sha256,
            "mode": shared.membership.mode,
            "logical_root": str(logical_root),
        },
    )
    for path in staging_root.rglob("*"):
        if path.is_file() and path != paths.sqlite_db:
            os.chmod(path, 0o444)
    manifest = build_complete_manifest(staging_root, logical_root=logical_root)
    write_complete_source(staging_root, manifest)


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
    bind_source_identity(config, args.project_root)
    result = initialize_run(
        config,
        project_root=args.project_root,
        allow_dirty_snapshot=args.allow_dirty_snapshot,
    )
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
