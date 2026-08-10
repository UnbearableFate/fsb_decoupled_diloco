from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

import fs_diloco.storage.authority as authority_module
from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    CommandConflictError,
    LeaderAuthority,
    initialize_authority,
)
from fs_diloco.storage.leader_lease import StaleLeaderTokenError
from tests.support.protocol import (
    proposal_payload,
    publish_checkpoint_pair,
    publish_proposal_payload,
    receipt_payload,
)


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def open_authority(tmp_path: Path, clock: Clock) -> LeaderAuthority:
    database = tmp_path / "authority.sqlite3"
    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    initialize_authority(database, identity, scope, wall_clock=clock)
    return LeaderAuthority(
        database,
        identity,
        scope,
        lease_duration_seconds=20.0,
        max_clock_skew_seconds=2.0,
        wall_clock=clock,
    )


def commit_genesis(leader, run_root: Path) -> None:
    leader.initialize_genesis(
        command_id="initialize-v0",
        publication_id="publication-v0",
        **publish_checkpoint_pair(run_root, version=0),
    )


def test_genesis_uses_fenced_publication_chain_and_commit_is_idempotent(
    tmp_path: Path,
) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        pair = publish_checkpoint_pair(tmp_path, version=0)
        intent = leader.prepare_publication(
            command_id="prepare-v0",
            publication_id="publication-v0",
            target_version=0,
            selection_batch_id=None,
            **pair,
        )
        committed = leader.commit_merge(
            command_id="commit-v0", publication_id=intent.publication_id
        )
        replay = leader.commit_merge(command_id="commit-v0", publication_id=intent.publication_id)

        assert committed == replay
        assert committed.version == 0
        assert committed.predecessor_version is None
        assert authority.read.latest_committed_version() == committed


def test_command_id_replay_with_different_request_fails_closed(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )

        with pytest.raises(CommandConflictError, match="different"):
            leader.bind_or_replace_static_attempt(
                command_id="bind-1",
                learner_id="learner-0",
                logical_launch_id="launch-0",
                attempt_id="attempt-2",
            )


def test_stale_token_cannot_execute_a_named_business_command(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        first = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        stale = authority.open_leader(first)
        clock.now = 123.0
        second = authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        current = authority.open_leader(second)
        current.bind_or_replace_static_attempt(
            command_id="bind-current",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-2",
        )

        with pytest.raises(StaleLeaderTokenError):
            stale.begin_terminal_close(command_id="stale-close", reason="stale")


def test_authority_surface_does_not_offer_direct_sql_or_raw_connection(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)

        for forbidden in ("conn", "connection", "execute", "executemany", "transaction"):
            assert not hasattr(authority, forbidden)
            assert not hasattr(leader, forbidden)


def test_global_version_target_cannot_skip_or_duplicate(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        commit_genesis(leader, tmp_path)

        with pytest.raises(ValueError, match="next version 1"):
            leader.prepare_publication(
                command_id="prepare-v2",
                publication_id="publication-v2",
                target_version=2,
                selection_batch_id=None,
                weight_relative_path="weights/epochs/e1/v2.safetensors",
                weight_size=4,
                weight_sha256="a" * 64,
                optim_relative_path="optim/epochs/e1/v2.safetensors",
                optim_size=4,
                optim_sha256="b" * 64,
                weight_theta_sha256="e" * 64,
                optim_theta_sha256="e" * 64,
            )


def test_typed_receipt_proposal_selection_and_successor_commit_flow(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        fence = {
            "kind": "static",
            "learner_id": binding.learner_id,
            "logical_launch_id": binding.logical_launch_id,
            "attempt_id": binding.attempt_id,
            "binding_generation": binding.binding_generation,
        }
        receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence))
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        proposal_data = proposal_payload(receipt_sha256=receipt.immutable_sha256(), fence=fence)
        proposal = FullUpdateProposalV2.from_dict(proposal_data)
        publish_proposal_payload(tmp_path, proposal)

        assert (
            leader.ingest_proposal(command_id="proposal-1", proposal=proposal).value == "accepted"
        )
        assert (
            leader.ingest_proposal(command_id="proposal-replay", proposal=proposal).value
            == "exact_replay"
        )
        commit_genesis(leader, tmp_path)
        attempt = leader.try_select_batch(command_id="select-v1", quorum_min=1, quorum_max=1)
        batch = attempt.batch
        assert batch is not None
        assert batch.target_version == 1
        assert batch.candidates[0].proposal == proposal
        intent = leader.prepare_publication(
            command_id="prepare-v1",
            publication_id="publication-v1",
            target_version=1,
            selection_batch_id=batch.batch_id,
            **publish_checkpoint_pair(tmp_path, version=1),
        )
        committed = leader.commit_merge(
            command_id="commit-v1", publication_id=intent.publication_id
        )

        assert committed.version == 1
        assert committed.predecessor_version == 0
        assert committed.direct_weight_tokens_applied == 6


@pytest.mark.crash_matrix
@pytest.mark.parametrize("repetition", range(10))
@pytest.mark.parametrize(
    ("crash_boundary", "lifecycle"),
    (
        ("version_insert", "v0"),
        ("version_insert", "merge"),
        ("proposal_transition", "merge"),
        ("db_commit", "v0"),
        ("db_commit", "merge"),
    ),
)
def test_commit_fault_boundaries_roll_back_and_exact_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repetition: int,
    crash_boundary: str,
    lifecycle: str,
) -> None:
    clock = Clock()
    root = tmp_path / f"{crash_boundary}-{lifecycle}-{repetition}"
    with open_authority(root, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        proposal: FullUpdateProposalV2 | None = None
        if lifecycle == "v0":
            intent = leader.prepare_publication(
                command_id="prepare-v0",
                publication_id="publication-v0",
                target_version=0,
                selection_batch_id=None,
                **publish_checkpoint_pair(root, version=0),
            )
            expected_before = None
        else:
            binding = leader.bind_or_replace_static_attempt(
                command_id="bind-1",
                learner_id="learner-0",
                logical_launch_id="launch-0",
                attempt_id="attempt-1",
            )
            fence = {
                "kind": "static",
                "learner_id": binding.learner_id,
                "logical_launch_id": binding.logical_launch_id,
                "attempt_id": binding.attempt_id,
                "binding_generation": binding.binding_generation,
            }
            receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence))
            leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
            proposal = FullUpdateProposalV2.from_dict(
                proposal_payload(receipt_sha256=receipt.immutable_sha256(), fence=fence)
            )
            publish_proposal_payload(root, proposal)
            leader.ingest_proposal(command_id="proposal-1", proposal=proposal)
            commit_genesis(leader, root)
            selection = leader.try_select_batch(command_id="select-v1", quorum_min=1, quorum_max=1)
            assert selection.batch is not None
            intent = leader.prepare_publication(
                command_id="prepare-v1",
                publication_id="publication-v1",
                target_version=1,
                selection_batch_id=selection.batch.batch_id,
                **publish_checkpoint_pair(root, version=1),
            )
            expected_before = 0

        def inject(name: str) -> None:
            if name == crash_boundary:
                raise RuntimeError(f"injected crash at {name}")

        monkeypatch.setattr(authority_module, "_publication_commit_boundary", inject)
        with pytest.raises(RuntimeError, match=f"injected crash at {crash_boundary}"):
            leader.commit_merge(
                command_id=f"commit-{lifecycle}", publication_id=intent.publication_id
            )

        connection = sqlite3.connect(root / "authority.sqlite3")
        try:
            assert (
                connection.execute("SELECT MAX(version) FROM global_versions").fetchone()[0]
                == expected_before
            )
            if proposal is not None:
                assert (
                    connection.execute(
                        "SELECT status FROM updates WHERE update_id=?", (proposal.update_id,)
                    ).fetchone()[0]
                    == "selected"
                )
            assert (
                connection.execute(
                    "SELECT state FROM publication_intents WHERE publication_id=?",
                    (intent.publication_id,),
                ).fetchone()[0]
                == "prepared"
            )
        finally:
            connection.close()

        monkeypatch.setattr(authority_module, "_publication_commit_boundary", lambda _name: None)
        committed = leader.commit_merge(
            command_id=f"commit-{lifecycle}", publication_id=intent.publication_id
        )
        assert committed.version == (0 if lifecycle == "v0" else 1)
        assert authority.read.latest_committed_version() == committed


@pytest.mark.parametrize(
    "changes",
    [
        {"cycle_id": "20000000-0000-4000-8000-000000000001"},
        {
            "processed_tokens_this_cycle": 8,
            "effective_tokens_this_update": 5,
            "local_discarded_tokens_this_cycle": 3,
        },
        {"retained_tokens_since_base": 7},
        {"data_cursor_start": 1, "data_cursor_end": 9},
    ],
)
def test_proposal_must_match_every_shared_receipt_field(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        fence = {
            "kind": "static",
            "learner_id": binding.learner_id,
            "logical_launch_id": binding.logical_launch_id,
            "attempt_id": binding.attempt_id,
            "binding_generation": binding.binding_generation,
        }
        receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence))
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        payload = proposal_payload(receipt_sha256=receipt.immutable_sha256(), fence=fence)
        payload.update(changes)
        proposal = FullUpdateProposalV2.from_dict(payload)
        publish_proposal_payload(tmp_path, proposal)

        with pytest.raises(ValueError, match="immutable fields"):
            leader.ingest_proposal(command_id="proposal-mismatch", proposal=proposal)

        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert connection.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0
            assert (
                connection.execute("SELECT COUNT(*) FROM proposal_observations").fetchone()[0] == 0
            )
        finally:
            connection.close()


def test_newer_accepted_proposal_supersedes_old_pending_after_insert(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        fence = {
            "kind": "static",
            "learner_id": binding.learner_id,
            "logical_launch_id": binding.logical_launch_id,
            "attempt_id": binding.attempt_id,
            "binding_generation": binding.binding_generation,
        }
        first_receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence))
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=first_receipt)
        first = FullUpdateProposalV2.from_dict(
            proposal_payload(receipt_sha256=first_receipt.immutable_sha256(), fence=fence)
        )
        publish_proposal_payload(tmp_path, first)
        leader.ingest_proposal(command_id="proposal-1", proposal=first)
        second_receipt = CycleReceiptV1.from_dict(
            receipt_payload(
                cycle_seq=2,
                previous_receipt_id=first_receipt.receipt_id,
                previous_receipt_sha256=first_receipt.immutable_sha256(),
                cursor_start=8,
                cursor_end=16,
                fence=fence,
            )
        )
        leader.ingest_cycle_receipt(command_id="receipt-2", receipt=second_receipt)
        second = FullUpdateProposalV2.from_dict(
            proposal_payload(
                cycle_seq=2,
                receipt_sha256=second_receipt.immutable_sha256(),
                fence=fence,
            )
        )
        publish_proposal_payload(tmp_path, second)

        assert leader.ingest_proposal(command_id="proposal-2", proposal=second).value == "accepted"

        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            statuses = dict(connection.execute("SELECT update_id, status FROM updates"))
            assert statuses == {first.update_id: "dropped", second.update_id: "pending"}
            fate = connection.execute(
                "SELECT direct_fate, fate_reason FROM token_fates WHERE receipt_id=?",
                (first_receipt.receipt_id,),
            ).fetchone()
            assert fate == ("dropped", "superseded_by_newer_cycle")
        finally:
            connection.close()


def test_overlong_command_id_fails_before_mutation(tmp_path: Path) -> None:
    clock = Clock()
    with open_authority(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)

        with pytest.raises(ValueError, match="safe protocol identity"):
            leader.bind_or_replace_static_attempt(
                command_id="c" * 129,
                learner_id="learner-0",
                logical_launch_id="launch-0",
                attempt_id="attempt-1",
            )

        assert authority.read.static_binding("learner-0") is None
