from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.protocol.cycle_receipt import CycleReceiptV1
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    MembershipFenceError,
    initialize_authority,
)
from tests.support.protocol import (
    proposal_payload,
    publish_checkpoint_pair,
    publish_proposal_payload,
    receipt_payload,
)


def test_static_binding_requires_terminal_old_attempt_and_increments_generation(
    tmp_path: Path,
) -> None:
    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=lambda: 100.0)
    with LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        first = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        with pytest.raises(MembershipFenceError, match="requires expected_generation"):
            leader.bind_or_replace_static_attempt(
                command_id="bind-while-active",
                learner_id="learner-0",
                logical_launch_id="launch-0",
                attempt_id="attempt-2",
            )
        old_fence = StaticContributorFence(
            "static",
            first.learner_id,
            first.logical_launch_id,
            first.attempt_id,
            first.binding_generation,
        )
        leader.mark_static_attempt_terminal(command_id="terminal-1", fence=old_fence)
        second = leader.bind_or_replace_static_attempt(
            command_id="bind-2",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-2",
            expected_generation=1,
        )

        assert second.binding_generation == 2
        assert second.status == "active"
        with pytest.raises(MembershipFenceError, match="stale"):
            leader.mark_static_attempt_terminal(command_id="old-terminal", fence=old_fence)


def test_new_static_logical_launch_requires_explicit_replacement(tmp_path: Path) -> None:
    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=lambda: 100.0)
    with LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        first = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        leader.mark_static_attempt_terminal(
            command_id="terminal-1",
            fence=StaticContributorFence(
                "static", "learner-0", "launch-0", "attempt-1", first.binding_generation
            ),
        )
        with pytest.raises(MembershipFenceError, match="explicit replacement"):
            leader.bind_or_replace_static_attempt(
                command_id="new-launch-blocked",
                learner_id="learner-0",
                logical_launch_id="launch-1",
                attempt_id="attempt-2",
            )
        replacement = leader.bind_or_replace_static_attempt(
            command_id="new-launch-authorized",
            learner_id="learner-0",
            logical_launch_id="launch-1",
            attempt_id="attempt-2",
            allow_logical_replacement=True,
        )
        assert replacement.logical_launch_id == "launch-1"
        assert replacement.binding_generation == 2


def test_active_static_replacement_atomically_abandons_prepared_work(tmp_path: Path) -> None:
    identity = AuthorityIdentity(
        "run-current", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority(database, identity, scope, wall_clock=lambda: 100.0)
    with LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        binding = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        fence = StaticContributorFence(
            "static",
            binding.learner_id,
            binding.logical_launch_id,
            binding.attempt_id,
            binding.binding_generation,
        )
        receipt = CycleReceiptV1.from_dict(receipt_payload(fence=fence.as_dict()))
        leader.ingest_cycle_receipt(command_id="receipt-1", receipt=receipt)
        proposal = FullUpdateProposalV2.from_dict(
            proposal_payload(receipt_sha256=receipt.immutable_sha256(), fence=fence.as_dict())
        )
        publish_proposal_payload(tmp_path, proposal)
        leader.ingest_proposal(command_id="proposal-1", proposal=proposal)
        leader.initialize_genesis(
            command_id="initialize-v0",
            publication_id="publication-v0",
            **publish_checkpoint_pair(tmp_path, version=0),
        )
        attempt = leader.try_select_batch(command_id="select-v1", quorum_min=1, quorum_max=1)
        batch = attempt.batch
        assert batch is not None
        leader.prepare_publication(
            command_id="prepare-v1",
            publication_id="publication-v1",
            target_version=1,
            selection_batch_id=batch.batch_id,
            **publish_checkpoint_pair(tmp_path, version=1),
        )

        replacement = leader.bind_or_replace_static_attempt(
            command_id="replace-active",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-2",
            expected_generation=1,
            replacement_reason="process_restart",
        )

        assert replacement.binding_generation == 2
        assert replacement.attempt_id == "attempt-2"
        connection = sqlite3.connect(database)
        try:
            assert (
                connection.execute(
                    "SELECT status FROM updates WHERE update_id=?", (proposal.update_id,)
                ).fetchone()[0]
                == "dropped"
            )
            assert (
                connection.execute(
                    "SELECT state FROM selection_batches WHERE batch_id=?", (batch.batch_id,)
                ).fetchone()[0]
                == "abandoned"
            )
            assert (
                connection.execute(
                    "SELECT state FROM publication_intents WHERE publication_id='publication-v1'"
                ).fetchone()[0]
                == "abandoned"
            )
            assert (
                connection.execute(
                    "SELECT direct_fate FROM token_fates WHERE receipt_id=?", (receipt.receipt_id,)
                ).fetchone()[0]
                == "dropped"
            )
        finally:
            connection.close()
