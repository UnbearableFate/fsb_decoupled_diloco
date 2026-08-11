"""Exercise syncer admission and proposal-ingestion composition boundaries."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from fs_diloco.core.config import Config
from fs_diloco.core.run_descriptor import DescriptorAuthorityIdentity, LoadedRunDescriptor
from fs_diloco.protocol.authority import ProposalDisposition
from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1, canonical_receipt_relative_path
from fs_diloco.runtime.syncer import _admit_requests, _ingest_proposals
from fs_diloco.storage.admission import (
    publish_static_replacement_authorization,
    publish_static_request_with_sha256,
)
from fs_diloco.storage.atomic_io import read_json
from fs_diloco.storage.authority import (
    AuthoritySchemaError,
    AuthorityIdentity,
    CommandConflictError,
    LeaderAuthority,
    initialize_authority,
)
from fs_diloco.storage.leader_lease import StaleLeaderTokenError
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from tests.support.protocol import proposal, receipt_payload


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


@pytest.mark.parametrize("matching_authorization", [True, False])
def test_active_static_replacement_consumes_exact_authorization(
    tmp_path: Path, matching_authorization: bool
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
        current_fence = StaticContributorFence(
            kind="static",
            learner_id=first.learner_id,
            logical_launch_id=first.logical_launch_id,
            attempt_id=first.attempt_id,
            binding_generation=(
                first.binding_generation if matching_authorization else first.binding_generation + 1
            ),
        )
        publish_static_replacement_authorization(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            old_fence=current_fence,
            new_logical_launch_id="logical-2",
            new_attempt_id="attempt-2",
            reason="operator recovery",
        )
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
        disposition = read_json(paths.registration_disposition_path(request_sha256))
        if matching_authorization:
            assert current is not None
            assert current.binding_generation == 2
            assert current.logical_launch_id == "logical-2"
            history = authority.read.static_binding_history("learner_000", 1)
            assert history is not None and history["final_status"] == "replaced"
            assert disposition["outcome"] == "admitted"
            assert telemetry.events[-1][0] == "learner_admitted"
        else:
            assert current == first
            assert disposition["outcome"] == "rejected"
            assert disposition["error_type"] == "AdmissionAuthorizationError"
            assert telemetry.events[-1][0] == "admission_rejected"


@pytest.mark.parametrize(
    "error_type",
    [AuthoritySchemaError, CommandConflictError, StaleLeaderTokenError],
)
def test_receipt_ingest_propagates_integrity_failures(
    tmp_path: Path, error_type: type[RuntimeError]
) -> None:
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
            error_type("receipt integrity failure")
        )
    )
    control = SimpleNamespace(publish_receipt_ack=lambda *_args, **_kwargs: None)

    with pytest.raises(error_type, match="receipt integrity failure"):
        _ingest_proposals(
            _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
            authority,
            leader,
            control,
            _Telemetry(),
        )


@pytest.mark.parametrize(
    "error_type",
    [AuthoritySchemaError, CommandConflictError, StaleLeaderTokenError],
)
def test_proposal_ingest_propagates_integrity_failures(
    tmp_path: Path, error_type: type[RuntimeError]
) -> None:
    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    candidate = proposal()
    proposal_path = paths.shared_root / "updates/proposals/learner-0/proposal.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_bytes(candidate.canonical_bytes())
    authority = SimpleNamespace(read=SimpleNamespace(current_contributor_fences=lambda: ()))
    leader = SimpleNamespace(
        ingest_proposal=lambda **_kwargs: (_ for _ in ()).throw(
            error_type("proposal integrity failure")
        )
    )

    with pytest.raises(error_type, match="proposal integrity failure"):
        _ingest_proposals(
            _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
            authority,
            leader,
            SimpleNamespace(),
            _Telemetry(),
        )


def test_proposal_ingest_reconsiders_merge_after_each_new_payload(tmp_path: Path) -> None:
    """Expensive payload verification must not run past the next quorum opportunity."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    candidates = [proposal(cycle_seq=sequence) for sequence in (1, 2)]
    for candidate in candidates:
        proposal_path = (
            paths.shared_root
            / "updates"
            / "proposals"
            / candidate.stable_contributor_key
            / f"{candidate.update_id}.json"
        )
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_bytes(candidate.canonical_bytes())
    ingested: list[str] = []

    def ingest_proposal(**kwargs: object) -> ProposalDisposition:
        """Record the proposal that reached the expensive authority boundary."""

        candidate = kwargs["proposal"]
        assert hasattr(candidate, "update_id")
        ingested.append(candidate.update_id)
        return ProposalDisposition.ACCEPTED

    authority = SimpleNamespace(read=SimpleNamespace(current_contributor_fences=lambda: ()))
    leader = SimpleNamespace(ingest_proposal=ingest_proposal)

    _ingest_proposals(
        _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
        authority,
        leader,
        SimpleNamespace(),
        _Telemetry(),
    )

    assert ingested == [candidates[0].update_id]


def test_proposal_ingest_scans_past_exact_command_replays(tmp_path: Path) -> None:
    """A persistent accepted proposal must not hide later proposal objects forever."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    candidates = [proposal(cycle_seq=sequence) for sequence in (1, 2)]
    for candidate in candidates:
        proposal_path = (
            paths.shared_root
            / "updates"
            / "proposals"
            / candidate.stable_contributor_key
            / f"{candidate.update_id}.json"
        )
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_bytes(candidate.canonical_bytes())
    accepted: set[str] = set()
    inspected: list[str] = []

    def ingest_proposal(**kwargs: object) -> ProposalDisposition:
        """Model durable command replay semantics across repeated directory scans."""

        candidate = kwargs["proposal"]
        assert hasattr(candidate, "update_id")
        inspected.append(candidate.update_id)
        if candidate.update_id in accepted:
            return ProposalDisposition.EXACT_REPLAY
        accepted.add(candidate.update_id)
        return ProposalDisposition.ACCEPTED

    authority = SimpleNamespace(read=SimpleNamespace(current_contributor_fences=lambda: ()))
    leader = SimpleNamespace(ingest_proposal=ingest_proposal)
    loaded = _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest())

    _ingest_proposals(loaded, authority, leader, SimpleNamespace(), _Telemetry())
    _ingest_proposals(loaded, authority, leader, SimpleNamespace(), _Telemetry())

    assert inspected == [
        candidates[0].update_id,
        candidates[0].update_id,
        candidates[1].update_id,
    ]


@pytest.mark.parametrize(
    ("sqlite_code", "propagates"),
    [(sqlite3.SQLITE_BUSY, False), (sqlite3.SQLITE_LOCKED, False), (sqlite3.SQLITE_IOERR, True)],
)
def test_receipt_ingest_retries_only_sqlite_contention(
    tmp_path: Path, sqlite_code: int, propagates: bool
) -> None:
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
    error = sqlite3.OperationalError("sqlite ingest failure")
    error.sqlite_errorcode = sqlite_code
    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (fence,),
            contributor_progress=lambda _key: None,
        )
    )
    leader = SimpleNamespace(ingest_cycle_receipt=lambda **_kwargs: (_ for _ in ()).throw(error))
    telemetry = _Telemetry()

    if propagates:
        with pytest.raises(sqlite3.OperationalError, match="sqlite ingest failure"):
            _ingest_proposals(
                _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
                authority,
                leader,
                SimpleNamespace(),
                telemetry,
            )
        assert telemetry.events == []
    else:
        _ingest_proposals(
            _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
            authority,
            leader,
            SimpleNamespace(publish_receipt_ack=lambda *_args, **_kwargs: None),
            telemetry,
        )
        assert telemetry.events[-1][0] == "receipt_ingest_rejected"
