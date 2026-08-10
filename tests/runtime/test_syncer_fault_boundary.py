from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from fs_diloco.runtime.syncer import _pause_candidate_at_fault_boundary
from fs_diloco.storage.authority import (
    AuthorityIdentity,
    LeaderAuthority,
    initialize_authority,
)
from fs_diloco.protocol.contributor import StaticMembershipScope


class RecordingAuthority:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def assert_outside_transaction(self) -> None:
        self.events.append("authority-outside-transaction")


class RecordingRenewer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextmanager
    def quiesce_for_fault_injection(self):
        self.events.append("renewer-quiesced")
        try:
            yield
        finally:
            self.events.append("renewer-released")


def test_syncer_fault_boundary_orders_quiesce_marker_and_sigstop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "fault-boundary.json"
    events: list[str] = []
    monkeypatch.setenv("FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION", "2")
    monkeypatch.setenv("FS_DILOCO_FAULT_PAUSE_MARKER_PATH", str(marker))
    monkeypatch.setattr(os, "getpid", lambda: 4242)

    def record_kill(pid: int, sig: signal.Signals) -> None:
        events.append(f"kill:{pid}:{sig.name}")

    monkeypatch.setattr(os, "kill", record_kill)
    leader = SimpleNamespace(token=SimpleNamespace(epoch=7, owner_id="syncer-owner"))

    _pause_candidate_at_fault_boundary(
        RecordingAuthority(events),
        leader,
        RecordingRenewer(events),
        version=2,
    )

    assert events == [
        "renewer-quiesced",
        "authority-outside-transaction",
        "kill:4242:SIGSTOP",
        "renewer-released",
        "authority-outside-transaction",
    ]
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "format_version": 1,
        "pid": 4242,
        "epoch": 7,
        "owner_id": "syncer-owner",
        "committed_version": 2,
        "sqlite_transaction_active": False,
        "lease_renewer_quiesced": True,
    }


def test_syncer_fault_boundary_refuses_an_active_authority_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "authority.sqlite3"
    identity = AuthorityIdentity("run", "source", "a" * 64)
    scope = StaticMembershipScope(("learner_000",))
    initialize_authority(database, identity, scope)
    with LeaderAuthority(database, identity, scope) as authority:
        authority._connection.execute("BEGIN")
        try:
            with pytest.raises(RuntimeError, match="active SQLite transaction"):
                authority.assert_outside_transaction()
        finally:
            authority._connection.rollback()
