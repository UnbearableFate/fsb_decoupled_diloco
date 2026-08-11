"""Exercise syncer admission and proposal-ingestion composition boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from fs_diloco.core.config import Config
from fs_diloco.core.run_descriptor import DescriptorAuthorityIdentity, LoadedRunDescriptor
from fs_diloco.protocol.authority import ProposalDisposition
from fs_diloco.protocol.contributor import ContributorFence, MembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1, canonical_receipt_relative_path
from fs_diloco.runtime.syncer import _admit_requests, _ingest_proposals
from fs_diloco.storage.admission import (
    publish_admission_request_with_sha256,
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
from tests.support.protocol import contributor_fence, proposal, receipt_payload


class _Telemetry:
    """Record syncer composition events without external telemetry I/O."""

    def __init__(self) -> None:
        """Start with an empty ordered event sequence."""

        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        """Append one structured syncer event."""

        self.events.append((name, fields))


def _loaded(paths: RunPaths, *, config_sha256: str) -> LoadedRunDescriptor:
    """Build the minimal loaded descriptor required by syncer composition helpers."""

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


def test_duplicate_bootstrap_request_is_rejected_after_first_admission(
    tmp_path: Path,
) -> None:
    """A consumed bootstrap slot cannot admit a second learner instance."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    config_sha256 = hashlib.sha256(b"config").hexdigest()
    identity = AuthorityIdentity("run-current", "source", config_sha256)
    scope = MembershipScope(1)
    initialize_authority(paths.sqlite_db, identity, scope)
    telemetry = _Telemetry()

    with LeaderAuthority(paths.sqlite_db, identity, scope) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        )
        loaded = _loaded(paths, config_sha256=config_sha256)
        leader.initialize_membership(command_id="initialize-membership")
        publish_admission_request_with_sha256(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            instance_id="instance-1",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="a" * 64,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        assert authority.read.instances()[0]["instance_id"] == "instance-1"
        _path, request_sha256 = publish_admission_request_with_sha256(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            instance_id="instance-2",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="b" * 64,
        )
        _admit_requests(loaded, authority, leader, telemetry)

        assert authority.read.streams()[0]["current_instance_id"] == "instance-1"
        disposition = read_json(paths.registration_disposition_path(request_sha256))
        assert disposition["outcome"] == "rejected"
        assert disposition["error_type"] == "MembershipFenceError"
        assert telemetry.events[-1][0] == "admission_rejected"


@pytest.mark.parametrize("matching_authorization", [True, False])
def test_replacement_request_consumes_exact_launch_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matching_authorization: bool,
) -> None:
    """Filesystem replacement admission binds the exact launch and qsub receipt."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    config_sha256 = hashlib.sha256(b"config").hexdigest()
    identity = AuthorityIdentity("run-current", "source", config_sha256)
    scope = MembershipScope(1)
    initialize_authority(paths.sqlite_db, identity, scope, wall_clock=lambda: 100.0)
    telemetry = _Telemetry()

    with LeaderAuthority(paths.sqlite_db, identity, scope, wall_clock=lambda: 100.0) as authority:
        leader = authority.open_leader(
            authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        )
        loaded = _loaded(paths, config_sha256=config_sha256)
        leader.initialize_membership(command_id="initialize-membership")
        monkeypatch.setenv("PBS_JOBID", "100.opbs")
        publish_admission_request_with_sha256(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            instance_id="instance-1",
            stream_id=0,
            bootstrap_slot=0,
            admission_token_sha256="a" * 64,
        )
        _admit_requests(loaded, authority, leader, telemetry)
        first = authority.read.current_contributor_fences()[0]
        leader.record_capacity_observation(
            command_id="observe-replacement",
            observation_key="capacity-replacement",
            global_version=0,
            eligible_contributors=0,
            selected_contributors=0,
            productive_instances=0,
            reserved_launch_capacity=0,
            desired_contributors=1,
            action="replace",
            retention_count=4,
        )
        planned = leader.plan_launch_request(
            command_id="plan-replacement",
            request_id="launch-replacement",
            observation_key="capacity-replacement",
            stream_id=0,
            replace_instance_id=first.instance_id,
            reason="scheduler_terminal",
            expires_at=1000.0,
            max_pending_requests=1,
            max_total_requests=1,
            expected_scheduler_job_id="100.opbs",
        )
        submitting = leader.transition_launch_request(
            command_id="submitting",
            request_id=planned["request_id"],
            expected_state="planned",
            state="submitting",
            pbs_job_id=None,
            scheduler_state="qsub_started",
            evidence_source="qsub_started",
        )
        leader.transition_launch_request(
            command_id="submitted",
            request_id=planned["request_id"],
            expected_state=submitting["state"],
            state="submitted",
            pbs_job_id="200.opbs",
            scheduler_state="queued",
            evidence_source="qsub_receipt",
        )
        monkeypatch.setenv("PBS_JOBID", "200.opbs" if matching_authorization else "999.opbs")
        _path, request_sha256 = publish_admission_request_with_sha256(
            paths,
            run_id="run-current",
            descriptor_sha256="d" * 64,
            instance_id="instance-2",
            stream_id=0,
            launch_request_id="launch-replacement",
            replace_instance_id=first.instance_id,
            admission_token_sha256="b" * 64,
        )

        _admit_requests(loaded, authority, leader, telemetry)

        current_fences = authority.read.current_contributor_fences()
        disposition = read_json(paths.registration_disposition_path(request_sha256))
        if matching_authorization:
            current = current_fences[0]
            assert current.instance_id == "instance-2"
            assert current.stream_epoch == first.stream_epoch + 1
            assert disposition["outcome"] == "admitted"
            assert telemetry.events[-1][0] == "learner_admitted"
        else:
            assert current_fences == ()
            assert authority.read.instances()[0]["status"] == "expired"
            assert disposition["outcome"] == "rejected"
            assert disposition["error_type"] == "MembershipFenceError"
            assert telemetry.events[-1][0] == "admission_rejected"


@pytest.mark.parametrize(
    "error_type",
    [AuthoritySchemaError, CommandConflictError, StaleLeaderTokenError],
)
def test_receipt_ingest_propagates_integrity_failures(
    tmp_path: Path, error_type: type[RuntimeError]
) -> None:
    """Receipt ingestion propagates authority integrity failures without downgrade."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    fence = ContributorFence.from_dict(contributor_fence())
    receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
    receipt_path = paths.shared_root / canonical_receipt_relative_path(fence, 1)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt.canonical_bytes())
    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (fence,),
            contributor_progress=lambda _key: None,
            pending_update_contributor_keys=lambda: (),
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
    """Proposal ingestion propagates authority integrity failures without downgrade."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    candidate = proposal()
    proposal_path = paths.shared_root / "updates/proposals/0/proposal.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_bytes(candidate.canonical_bytes())
    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (candidate.contributor_fence,),
            contributor_progress=lambda _key: None,
            controller_status=lambda: {"state": "open"},
            pending_update_contributor_keys=lambda: (),
        )
    )
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

    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (candidates[0].contributor_fence,),
            contributor_progress=lambda _key: None,
            controller_status=lambda: {"state": "open"},
            pending_update_contributor_keys=lambda: (),
        )
    )
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

    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (candidates[0].contributor_fence,),
            contributor_progress=lambda _key: None,
            controller_status=lambda: {"state": "open"},
            pending_update_contributor_keys=lambda: (),
        )
    )
    leader = SimpleNamespace(ingest_proposal=ingest_proposal)
    loaded = _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest())

    _ingest_proposals(loaded, authority, leader, SimpleNamespace(), _Telemetry())
    _ingest_proposals(loaded, authority, leader, SimpleNamespace(), _Telemetry())

    assert inspected == [
        candidates[0].update_id,
        candidates[0].update_id,
        candidates[1].update_id,
    ]


def test_proposal_ingest_prioritizes_streams_missing_from_pending_quorum(
    tmp_path: Path,
) -> None:
    """A fast stream cannot starve a slower stream from the pending quorum."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    fences = tuple(
        ContributorFence.from_dict(
            contributor_fence(stream_id=stream_id, instance_id=f"instance-{stream_id}")
        )
        for stream_id in range(2)
    )
    candidates = (
        proposal(fence=fences[0].as_dict()),
        proposal(
            fence=fences[1].as_dict(),
            stable_contributor_key="1",
            update_id="10000000-0000-4000-8000-000000000001",
        ),
    )
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
        """Record the only proposal allowed to reach the authority boundary."""

        candidate = kwargs["proposal"]
        assert hasattr(candidate, "stable_contributor_key")
        ingested.append(candidate.stable_contributor_key)
        return ProposalDisposition.ACCEPTED

    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: fences,
            contributor_progress=lambda _key: None,
            controller_status=lambda: {"state": "open"},
            pending_update_contributor_keys=lambda: ("0",),
        )
    )

    _ingest_proposals(
        _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
        authority,
        SimpleNamespace(ingest_proposal=ingest_proposal),
        SimpleNamespace(),
        _Telemetry(),
    )

    assert ingested == ["1"]


def test_proposal_ingest_skips_stale_fence_before_payload_verification(tmp_path: Path) -> None:
    """A terminalized fence must not trigger repeated reads of its large payloads."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    candidate = proposal()
    proposal_path = (
        paths.shared_root
        / "updates"
        / "proposals"
        / candidate.stable_contributor_key
        / f"{candidate.update_id}.json"
    )
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_bytes(candidate.canonical_bytes())
    telemetry = _Telemetry()
    authority = SimpleNamespace(
        read=SimpleNamespace(
            current_contributor_fences=lambda: (candidate.contributor_fence,),
            contributor_progress=lambda _key: None,
            controller_status=lambda: {"state": "draining"},
            pending_update_contributor_keys=lambda: (),
            terminal_contributor_fences=lambda: (
                {
                    "stable_contributor_key": candidate.stable_contributor_key,
                    "fence_json": json.dumps(candidate.contributor_fence.as_dict()),
                    "state": "acked",
                    "final_update_id": "different-update",
                },
            ),
        )
    )
    leader = SimpleNamespace(
        ingest_proposal=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale proposal reached payload verification")
        )
    )

    _ingest_proposals(
        _loaded(paths, config_sha256=hashlib.sha256(b"config").hexdigest()),
        authority,
        leader,
        SimpleNamespace(),
        telemetry,
    )

    assert telemetry.events == [
        (
            "proposal_disposition",
            {"update_id": candidate.update_id, "disposition": "stale_fence"},
        )
    ]


@pytest.mark.parametrize(
    ("sqlite_code", "propagates"),
    [(sqlite3.SQLITE_BUSY, False), (sqlite3.SQLITE_LOCKED, False), (sqlite3.SQLITE_IOERR, True)],
)
def test_receipt_ingest_retries_only_sqlite_contention(
    tmp_path: Path, sqlite_code: int, propagates: bool
) -> None:
    """Receipt scans retry SQLite contention but propagate storage integrity errors."""

    paths = RunPaths(tmp_path)
    prepare_authority_dirs(paths)
    fence = ContributorFence.from_dict(contributor_fence())
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
            pending_update_contributor_keys=lambda: (),
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
