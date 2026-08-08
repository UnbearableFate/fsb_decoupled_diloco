from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fs_diloco.protocol.contributor import StaticContributorFence, StaticMembershipScope
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    MembershipFenceError,
    initialize_authority_v4,
)


def test_static_binding_requires_terminal_old_attempt_and_increments_generation(
    tmp_path: Path,
) -> None:
    identity = AuthorityIdentity(
        "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=lambda: 100.0)
    with LeaderAuthority(database, identity, scope, wall_clock=lambda: 100.0) as authority:
        token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        leader = authority.open_leader(token)
        first = leader.bind_or_replace_static_attempt(
            command_id="bind-1",
            learner_id="learner-0",
            logical_launch_id="launch-0",
            attempt_id="attempt-1",
        )
        with pytest.raises(MembershipFenceError, match="must be terminal"):
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
        "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=lambda: 100.0)
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
