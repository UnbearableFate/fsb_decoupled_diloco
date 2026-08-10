from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.core.config import Config
from fs_diloco.core.run_descriptor import DescriptorAuthorityIdentity, LoadedRunDescriptor
from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1, canonical_receipt_relative_path
from fs_diloco.runtime.syncer import _admit_requests, _ingest_proposals
from fs_diloco.storage.admission import publish_static_request_with_sha256
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    CommandConflictError,
    LeaderAuthority,
    initialize_authority,
)
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from tests.support.protocol import receipt_payload


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


def _loaded(paths: RunPaths, *, config_sha256: str) -> LoadedRunDescriptor:
    return LoadedRunDescriptor(
        paths=paths,
        descriptor={"run_id": "run-current", "descriptor_sha256": "d" * 64},
        config=Config(),
        identity=DescriptorAuthorityIdentity(
            run_id="run-current",
            source_fingerprint="source",
            config_sha256=config_sha256,
        ),
    )


def test_active_static_attempt_replacement_requires_matching_authorization(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    config_sha256 = hashlib.sha256(b"config").hexdigest()
    identity = AuthorityIdentity("run-current", "source", config_sha256)
    scope = StaticMembershipScope(("learner_000",))
    initialize_authority(paths.sqlite_db, identity, scope)
    telemetry = _Telemetry()

    with LeaderAuthority(paths.sqlite_db, identity, scope) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        )
        loaded = _loaded(paths, config_sha256=config_sha256)
        publish_static_request_with_sha256(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-1",
            attempt_id="attempt-1",
            expected_generation=None,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        first = authority.read.static_binding("learner_000")
        assert first is not None and first.binding_generation == 1
        _path, request_sha256 = publish_static_request_with_sha256(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            learner_id="learner_000",
            logical_launch_id="logical-2",
            attempt_id="attempt-2",
            expected_generation=1,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        current = authority.read.static_binding("learner_000")
        assert current == first
        disposition = read_json(paths.registration_disposition_path(request_sha256))
        assert disposition["outcome"] == "rejected"
        assert disposition["error_type"] == "MembershipFenceError"
        assert telemetry.events[-1][0] == "admission_rejected"


def test_ingest_proposals_propagates_command_conflict(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    fence = StaticContributorFence(
        kind="static",
        learner_id="learner-0",
        logical_launch_id="launch-0",
        attempt_id="attempt-1",
        binding_generation=1,
    )
    receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
    receipt_path = paths.shared_root / canonical_receipt_relative_path(fence, 1)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt.canonical_bytes())
    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (fence,),
            contributor_progress=lambda _key: None,
        )
    )
    leader = SimpleNamespace(
        ingest_cycle_receipt=lambda **_kwargs: (_ for _ in ()).throw(
            CommandConflictError("conflicting receipt command")
        )
    )
    control = SimpleNamespace(publish_receipt_ack=lambda *_args, **_kwargs: None)

    with pytest.raises(CommandConflictError, match="conflicting receipt command"):
        _ingest_proposals(
            _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
            authority,
            leader,
            control,
            _Telemetry(),
        )
