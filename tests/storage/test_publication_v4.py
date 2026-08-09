from __future__ import annotations

import hashlib
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
import torch
from safetensors.torch import save

from fs_diloco.protocol.contributor import StaticMembershipScope
from fs_diloco.storage.atomic_io import publish_immutable_bytes
from fs_diloco.storage.authority import AuthorityIdentity, LeaderAuthority, initialize_authority_v4
from fs_diloco.storage.leader_lease import StaleLeaderTokenError
from fs_diloco.storage.tensor_codec import tensor_content_sha256
import fs_diloco.storage.atomic_io as atomic_io_module


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def checkpoint_bytes(version: int) -> tuple[bytes, bytes, str]:
    theta = torch.tensor([float(version)], dtype=torch.float32)
    theta_sha256 = tensor_content_sha256(theta)
    metadata = {"fs_diloco_theta_sha256": theta_sha256}
    weight = save(
        {"parameter": theta},
        metadata={**metadata, "fs_diloco_theta_order": '["parameter"]'},
    )
    optim = save(
        {"theta": theta, "step": torch.tensor(version, dtype=torch.int64)},
        metadata=metadata,
    )
    return weight, optim, theta_sha256


def metadata_for_bytes(weight: bytes, optim: bytes, theta_sha256: str) -> dict[str, object]:
    return {
        "weight_relative_path": "weights/epochs/e1/v0.safetensors",
        "weight_size": len(weight),
        "weight_sha256": hashlib.sha256(weight).hexdigest(),
        "optim_relative_path": "optim/epochs/e1/v0.safetensors",
        "optim_size": len(optim),
        "optim_sha256": hashlib.sha256(optim).hexdigest(),
        "weight_theta_sha256": theta_sha256,
        "optim_theta_sha256": theta_sha256,
    }


def open_static(tmp_path: Path, clock: Clock) -> LeaderAuthority:
    identity = AuthorityIdentity(
        "run-v4", "source-fingerprint", hashlib.sha256(b"config").hexdigest()
    )
    scope = StaticMembershipScope(("learner-0",))
    database = tmp_path / "authority.sqlite3"
    initialize_authority_v4(database, identity, scope, wall_clock=clock)
    return LeaderAuthority(database, identity, scope, wall_clock=clock)


def test_immutable_publication_is_create_no_replace_and_exact_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    target = tmp_path / "updates/payloads/learner/update.safetensors"
    first = publish_immutable_bytes(target, b"first")
    replay = publish_immutable_bytes(target, b"first")

    assert first.created is True
    assert replay.created is False
    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) & 0o222 == 0
    with pytest.raises(PermissionError):
        target.write_bytes(b"mutated-in-place")
    with pytest.raises(FileExistsError, match="collision"):
        publish_immutable_bytes(target, b"second")
    assert target.read_bytes() == b"first"

    writable = tmp_path / "writable-existing-object"
    writable.write_bytes(b"first")
    with pytest.raises(FileExistsError, match="collision"):
        publish_immutable_bytes(writable, b"first")
    with pytest.raises(ValueError, match="must not contain write bits"):
        publish_immutable_bytes(tmp_path / "bad-mode", b"first", mode=0o644)


def test_concurrent_immutable_publishers_never_overwrite_the_winner(tmp_path: Path) -> None:
    target = tmp_path / "object.safetensors"
    payloads = [b"alpha", b"beta"] * 8

    def publish(payload: bytes) -> tuple[str, bool]:
        try:
            result = publish_immutable_bytes(target, payload)
        except FileExistsError:
            return (payload.decode(), False)
        return (payload.decode(), result.created)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(publish, payloads))

    winner = target.read_bytes()
    assert winner in {b"alpha", b"beta"}
    assert sum(created for _payload, created in outcomes) == 1
    assert all(payload == winner.decode() or not created for payload, created in outcomes)


@pytest.mark.crash_matrix
@pytest.mark.parametrize("repetition", range(10))
@pytest.mark.parametrize("object_kind", ("tensor", "proposal_metadata"))
@pytest.mark.parametrize("crash_boundary", ("temporary_fsync", "create"))
def test_immutable_publication_crash_boundaries_replay_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repetition: int,
    object_kind: str,
    crash_boundary: str,
) -> None:
    target = tmp_path / f"{object_kind}-{crash_boundary}-{repetition}.immutable"
    payload = checkpoint_bytes(0)[0] if object_kind == "tensor" else b'{"proposal":1}\n'

    def inject(name: str) -> None:
        if name == crash_boundary:
            raise RuntimeError(f"injected crash at {name}")

    monkeypatch.setattr(atomic_io_module, "_immutable_publication_boundary", inject)
    with pytest.raises(RuntimeError, match=f"injected crash at {crash_boundary}"):
        publish_immutable_bytes(target, payload)
    if crash_boundary == "temporary_fsync":
        assert not target.exists()
    else:
        assert target.read_bytes() == payload

    monkeypatch.setattr(atomic_io_module, "_immutable_publication_boundary", lambda _name: None)
    replay = publish_immutable_bytes(target, payload)
    assert replay.created is (crash_boundary == "temporary_fsync")
    assert target.read_bytes() == payload
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(f".{target.name}.")]


def test_prepared_intent_precedes_io_and_commit_verifies_exact_theta_pair(
    tmp_path: Path,
) -> None:
    clock = Clock()
    weight, optim, theta_sha256 = checkpoint_bytes(0)
    metadata = metadata_for_bytes(weight, optim, theta_sha256)
    with open_static(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        intent = leader.prepare_publication(
            command_id="prepare-v0",
            publication_id="publication-v0",
            target_version=0,
            selection_batch_id=None,
            **metadata,
        )

        with pytest.raises(ValueError, match="not_found"):
            leader.commit_merge(command_id="commit-before-io", publication_id=intent.publication_id)
        publish_immutable_bytes(tmp_path / str(metadata["weight_relative_path"]), weight)
        publish_immutable_bytes(tmp_path / str(metadata["optim_relative_path"]), optim)
        committed = leader.commit_merge(
            command_id="commit-after-io", publication_id=intent.publication_id
        )

        assert committed.version == 0
        assert committed.theta_sha256 == theta_sha256
        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_publications WHERE state='committed'"
                ).fetchone()[0]
                == 2
            )
        finally:
            connection.close()


def test_mismatched_theta_identity_never_commits(tmp_path: Path) -> None:
    clock = Clock()
    weight, optim, theta_sha256 = checkpoint_bytes(0)
    metadata = metadata_for_bytes(weight, optim, theta_sha256)
    with open_static(tmp_path, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        with pytest.raises(ValueError, match="theta identities"):
            leader.prepare_publication(
                command_id="mismatch-pair",
                publication_id="publication-mismatch",
                target_version=0,
                selection_batch_id=None,
                **{**metadata, "optim_theta_sha256": "f" * 64},
            )

        forged = {**metadata, "weight_theta_sha256": "f" * 64, "optim_theta_sha256": "f" * 64}
        intent = leader.prepare_publication(
            command_id="forged-prepare",
            publication_id="publication-forged",
            target_version=0,
            selection_batch_id=None,
            **forged,
        )
        publish_immutable_bytes(tmp_path / str(metadata["weight_relative_path"]), weight)
        publish_immutable_bytes(tmp_path / str(metadata["optim_relative_path"]), optim)
        with pytest.raises(ValueError, match="theta identity mismatch"):
            leader.commit_merge(command_id="forged-commit", publication_id=intent.publication_id)
        assert authority.read.latest_committed_version() is None


def test_takeover_reconciles_predecessor_intent_and_orphan_grace_is_lease_safe(
    tmp_path: Path,
) -> None:
    clock = Clock()
    weight, optim, theta_sha256 = checkpoint_bytes(0)
    metadata = metadata_for_bytes(weight, optim, theta_sha256)
    publish_immutable_bytes(tmp_path / str(metadata["weight_relative_path"]), weight)
    publish_immutable_bytes(tmp_path / str(metadata["optim_relative_path"]), optim)
    with open_static(tmp_path, clock) as authority:
        first_token = authority.acquire_leader(owner_id="owner-1", hostname="host", pid=1)
        first = authority.open_leader(first_token)
        first.prepare_publication(
            command_id="prepare-v0",
            publication_id="publication-v0",
            target_version=0,
            selection_batch_id=None,
            **metadata,
        )
        clock.now = 193.0
        second_token = authority.acquire_leader(owner_id="owner-2", hostname="host", pid=2)
        second = authority.open_leader(second_token)

        with pytest.raises(StaleLeaderTokenError):
            first.commit_merge(command_id="stale-commit", publication_id="publication-v0")
        assert second.reconcile_publications(command_id="reconcile") == ("publication-v0",)
        assert second.claim_orphan_gc(command_id="claim-too-early") == ()
        clock.now = 280.0
        authority.renew_leader(second_token)
        clock.now = 288.0
        claimed = second.claim_orphan_gc(command_id="claim-after-grace")
        assert {item["relative_path"] for item in claimed} == {
            str(metadata["weight_relative_path"]),
            str(metadata["optim_relative_path"]),
        }
        assert {item["sha256"] for item in claimed} == {
            str(metadata["weight_sha256"]),
            str(metadata["optim_sha256"]),
        }

        connection = sqlite3.connect(tmp_path / "authority.sqlite3")
        try:
            assert (
                connection.execute(
                    "SELECT state FROM publication_intents WHERE publication_id='publication-v0'"
                ).fetchone()[0]
                == "abandoned"
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_publications WHERE state='orphan'"
                ).fetchone()[0]
                == 2
            )
        finally:
            connection.close()


@pytest.mark.crash_matrix
@pytest.mark.parametrize("repetition", range(10))
@pytest.mark.parametrize(
    "crash_point",
    ["before_prepare", "after_prepare", "after_weight", "after_outer", "after_commit"],
)
def test_publication_crash_prefix_is_reconciled_idempotently(
    tmp_path: Path,
    repetition: int,
    crash_point: str,
) -> None:
    root = tmp_path / f"{crash_point}-{repetition}"
    root.mkdir()
    clock = Clock()
    weight, optim, theta_sha256 = checkpoint_bytes(0)
    metadata = metadata_for_bytes(weight, optim, theta_sha256)
    with open_static(root, clock) as authority:
        token = authority.acquire_leader(owner_id="owner", hostname="host", pid=1)
        leader = authority.open_leader(token)
        if crash_point == "before_prepare":
            assert authority.read.latest_committed_version() is None
            return
        intent = leader.prepare_publication(
            command_id="prepare",
            publication_id="publication-v0",
            target_version=0,
            selection_batch_id=None,
            **metadata,
        )
        if crash_point == "after_prepare":
            with pytest.raises(ValueError, match="not_found"):
                leader.commit_merge(command_id="incomplete", publication_id=intent.publication_id)
            return
        publish_immutable_bytes(root / str(metadata["weight_relative_path"]), weight)
        if crash_point == "after_weight":
            with pytest.raises(ValueError, match="not_found"):
                leader.commit_merge(command_id="incomplete", publication_id=intent.publication_id)
            return
        publish_immutable_bytes(root / str(metadata["optim_relative_path"]), optim)
        committed = leader.commit_merge(command_id="commit", publication_id=intent.publication_id)
        assert committed.version == 0
        if crash_point == "after_commit":
            assert (
                leader.commit_merge(command_id="commit", publication_id=intent.publication_id)
                == committed
            )
