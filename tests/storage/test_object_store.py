"""Validate identity-bound proposal payload reads and tensor inspection."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import struct
from typing import Any
from pathlib import Path

import pytest

from fs_diloco.protocol.authority import ReadStatus
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.object_store import (
    ArtifactIdentityError,
    consume_verified_artifact,
    tensor_schema_sha256,
    verify_proposal_payload,
)
from fs_diloco.storage import object_store
from tests.support.protocol import proposal_payload


def safetensors_payload(value: float = 1.0, *, key: str = "flat_update") -> bytes:
    """Build one minimal float32 safetensors payload for object-store tests."""

    header = json.dumps(
        {key: {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}},
        separators=(",", ":"),
    ).encode("utf-8")
    padding = b" " * ((8 - len(header) % 8) % 8)
    padded = header + padding
    return len(padded).to_bytes(8, "little") + padded + struct.pack("<f", value)


def proposal_for(content: bytes, *, key: str = "flat_update") -> FullUpdateProposalV2:
    """Bind one minimal float32 payload to a valid proposal descriptor."""

    payload = proposal_payload(
        payload_size=len(content), payload_sha256=hashlib.sha256(content).hexdigest()
    )
    payload["tensor_schema_sha256"] = tensor_schema_sha256(
        [{"key": key, "dtype": "float32", "shape": [1]}]
    )
    return FullUpdateProposalV2.from_dict(payload)


def test_verified_payload_requires_regular_nonsymlink_identity_bound_file(
    tmp_path: Path,
) -> None:
    content = safetensors_payload()
    proposal = proposal_for(content)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    result = verify_proposal_payload(tmp_path, proposal)

    assert result.status is ReadStatus.OK
    assert result.value is not None
    assert result.value.sha256 == hashlib.sha256(content).hexdigest()


def test_payload_symlink_and_parent_symlink_fail_closed(tmp_path: Path) -> None:
    content = safetensors_payload()
    proposal = proposal_for(content)
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(content)
    payload_path = tmp_path / proposal.payload_relative_path
    payload_path.parent.mkdir(parents=True)
    payload_path.symlink_to(outside)

    assert verify_proposal_payload(tmp_path, proposal).status is ReadStatus.IDENTITY_MISMATCH

    payload_path.unlink()
    payload_path.parent.rmdir()
    (tmp_path / "updates" / "payloads").rmdir()
    (tmp_path / "updates").rmdir()
    (tmp_path / "real-parent").mkdir()
    (tmp_path / "updates").symlink_to(tmp_path / "real-parent", target_is_directory=True)
    assert verify_proposal_payload(tmp_path, proposal).status is ReadStatus.IDENTITY_MISMATCH


def test_payload_missing_size_and_digest_results_are_typed(tmp_path: Path) -> None:
    content = safetensors_payload()
    proposal = proposal_for(content)
    assert verify_proposal_payload(tmp_path, proposal).status is ReadStatus.NOT_FOUND

    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"wrong")
    mismatch = verify_proposal_payload(tmp_path, proposal)
    assert mismatch.status is ReadStatus.MALFORMED


def test_payload_digest_tensor_schema_and_nonfinite_values_fail_closed(tmp_path: Path) -> None:
    """Reject content, schema, and finite-value violations independently."""

    content = safetensors_payload(1.0)
    proposal = proposal_for(content)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)

    path.write_bytes(safetensors_payload(2.0))
    digest_mismatch = verify_proposal_payload(tmp_path, proposal)
    assert digest_mismatch.status is ReadStatus.IDENTITY_MISMATCH
    assert "SHA-256" in str(digest_mismatch.diagnostic)

    schema_content = safetensors_payload(1.0, key="different_key")
    schema_proposal = proposal_for(schema_content)
    path.write_bytes(schema_content)
    schema_mismatch = verify_proposal_payload(tmp_path, schema_proposal)
    assert schema_mismatch.status is ReadStatus.IDENTITY_MISMATCH
    assert "schema" in str(schema_mismatch.diagnostic)

    nonfinite_content = safetensors_payload(float("nan"))
    nonfinite_proposal = proposal_for(nonfinite_content)
    path.write_bytes(nonfinite_content)
    nonfinite = verify_proposal_payload(tmp_path, nonfinite_proposal)
    assert nonfinite.status is ReadStatus.MALFORMED
    assert "non-finite" in str(nonfinite.diagnostic)


def test_bfloat16_finite_scan_rejects_nan_without_scalar_python_iteration(
    tmp_path: Path,
) -> None:
    """Keep the formal BF16 payload path finite while using vectorized inspection."""

    header = json.dumps(
        {"flat_update": {"dtype": "BF16", "shape": [2], "data_offsets": [0, 4]}},
        separators=(",", ":"),
    ).encode("utf-8")
    padded = header + b" " * ((8 - len(header) % 8) % 8)
    content = len(padded).to_bytes(8, "little") + padded + struct.pack("<HH", 0x3F80, 0x7FC0)
    payload = proposal_payload(
        payload_size=len(content), payload_sha256=hashlib.sha256(content).hexdigest()
    )
    payload["tensor_schema_sha256"] = tensor_schema_sha256(
        [{"key": "flat_update", "dtype": "bfloat16", "shape": [2]}]
    )
    payload["tensor_dtype"] = "bfloat16"
    payload["tensor_numel"] = 2
    proposal = FullUpdateProposalV2.from_dict(payload)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    result = verify_proposal_payload(tmp_path, proposal)

    assert result.status is ReadStatus.MALFORMED
    assert "non-finite" in str(result.diagnostic)


def test_payload_rename_race_fails_identity_check(tmp_path: Path, monkeypatch: Any) -> None:
    content = safetensors_payload()
    proposal = proposal_for(content)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    replacement = path.with_name("replacement.safetensors")
    replacement.write_bytes(content)
    inspect = object_store._inspect_safetensors

    def replace_name(descriptor: int, *, file_size: int):
        replacement.replace(path)
        return inspect(descriptor, file_size=file_size)

    monkeypatch.setattr(object_store, "_inspect_safetensors", replace_name)

    result = verify_proposal_payload(tmp_path, proposal)

    assert result.status is ReadStatus.IDENTITY_MISMATCH
    assert any(
        message in str(result.diagnostic)
        for message in ("name changed", "changed while its tensor schema")
    )


def test_point_of_use_consumer_rejects_name_replacement_during_deserialization(
    tmp_path: Path,
) -> None:
    """A consumer result is not released when its verified directory entry changes."""

    relative_path = "objects/artifact.bin"
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"accepted")
    replacement = path.with_name("replacement.bin")
    replacement.write_bytes(b"accepted")

    def replace_after_read(descriptor_path: str) -> bytes:
        """Read accepted bytes, then swap the name before returning them."""

        value = Path(descriptor_path).read_bytes()
        replacement.replace(path)
        return value

    with pytest.raises(ArtifactIdentityError, match="identity changed"):
        consume_verified_artifact(
            tmp_path,
            relative_path,
            expected_size=len(b"accepted"),
            expected_sha256=hashlib.sha256(b"accepted").hexdigest(),
            consumer=replace_after_read,
        )


def test_payload_mutation_during_schema_inspection_fails_identity_check(
    tmp_path: Path, monkeypatch: Any
) -> None:
    content = safetensors_payload(1.0)
    proposal = proposal_for(content)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    inspect = object_store._inspect_safetensors

    def mutate_same_inode(descriptor: int, *, file_size: int):
        path.write_bytes(safetensors_payload(2.0))
        return inspect(descriptor, file_size=file_size)

    monkeypatch.setattr(object_store, "_inspect_safetensors", mutate_same_inode)

    result = verify_proposal_payload(tmp_path, proposal)

    assert result.status is ReadStatus.IDENTITY_MISMATCH
    assert "schema inspection" in str(result.diagnostic)


def test_regular_file_to_fifo_race_is_nonblocking_and_fails_closed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    content = safetensors_payload()
    proposal = proposal_for(content)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    real_open = os.open

    def replace_with_fifo(target: str | os.PathLike[str], flags: int) -> int:
        path.unlink()
        os.mkfifo(path)
        return real_open(target, flags)

    monkeypatch.setattr(object_store.os, "open", replace_with_fifo)

    result = verify_proposal_payload(tmp_path, proposal)

    assert result.status is ReadStatus.IDENTITY_MISMATCH
    assert "regular file" in str(result.diagnostic) or "identity changed" in str(result.diagnostic)


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [
        (errno.ENOENT, ReadStatus.NOT_FOUND),
        (errno.ESTALE, ReadStatus.TRANSIENT_IO),
        (errno.EIO, ReadStatus.TRANSIENT_IO),
    ],
)
def test_open_errno_is_classified_without_collapsing_transient_io(
    tmp_path: Path,
    monkeypatch: Any,
    error_number: int,
    expected: ReadStatus,
) -> None:
    content = safetensors_payload()
    proposal = proposal_for(content)
    path = tmp_path / proposal.payload_relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    def fail_open(*_args: Any, **_kwargs: Any) -> int:
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(object_store.os, "open", fail_open)

    assert verify_proposal_payload(tmp_path, proposal).status is expected
