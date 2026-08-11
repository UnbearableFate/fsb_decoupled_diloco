"""Identity-bound reads for immutable payload objects."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import torch
from safetensors import SafetensorError, safe_open

from ..protocol.authority import ReadResult, ReadStatus
from ..protocol.proposal import FullUpdateProposalV2, canonical_update_relative_path
from .tensor_identity import tensor_content_sha256


_TRANSIENT_ERRNOS = {
    errno.EAGAIN,
    errno.EBUSY,
    errno.EINTR,
    errno.EIO,
    errno.ESTALE,
    errno.ETIMEDOUT,
}


@dataclass(frozen=True)
class VerifiedPayload:
    relative_path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int


@dataclass(frozen=True)
class TensorSchema:
    sha256: str
    dtype: str
    total_numel: int


@dataclass(frozen=True)
class VerifiedArtifact:
    relative_path: str
    size_bytes: int
    sha256: str
    theta_sha256: str
    device: int
    inode: int


def resolve_run_relative_path(run_root: str | Path, relative_path: str) -> Path:
    root = Path(run_root).resolve(strict=True)
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or not relative_path
        or str(pure) != relative_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("artifact path must be normalized and run-root-relative")
    current = root
    for component in pure.parts[:-1]:
        current = current / component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("artifact parent must be a real directory, not a symlink")
    candidate = root.joinpath(*pure.parts)
    if candidate.parent.resolve(strict=True) != current.resolve(strict=True):
        raise ValueError("artifact parent escaped the canonical run root")
    if (
        root != candidate.parent.resolve(strict=True)
        and root not in candidate.parent.resolve(strict=True).parents
    ):
        raise ValueError("artifact path escaped the canonical run root")
    return candidate


def verify_proposal_payload(
    run_root: str | Path,
    proposal: FullUpdateProposalV2,
    *,
    chunk_size: int = 1024 * 1024,
) -> ReadResult[VerifiedPayload]:
    expected = canonical_update_relative_path(proposal.stable_contributor_key, proposal.update_id)
    if proposal.payload_relative_path != expected:
        return ReadResult(
            ReadStatus.IDENTITY_MISMATCH,
            diagnostic="proposal path does not match its contributor/update identity",
        )
    try:
        path = resolve_run_relative_path(run_root, proposal.payload_relative_path)
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return ReadResult(
                ReadStatus.IDENTITY_MISMATCH,
                diagnostic="payload must be a regular non-symlink file",
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="opened payload is not a regular file",
                )
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="payload identity changed between lstat and open",
                )
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="payload changed while it was being verified",
                )
            try:
                tensor_schema = _inspect_safetensors(descriptor, file_size=size)
            except ValueError as exc:
                return ReadResult(ReadStatus.MALFORMED, diagnostic=str(exc))
            verification_digest = hashlib.sha256()
            offset = 0
            while offset < size:
                chunk = os.pread(descriptor, min(chunk_size, size - offset), offset)
                if not chunk:
                    return ReadResult(
                        ReadStatus.IDENTITY_MISMATCH,
                        diagnostic="payload became truncated during identity recheck",
                    )
                verification_digest.update(chunk)
                offset += len(chunk)
            if verification_digest.digest() != digest.digest():
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="payload content changed during tensor schema inspection",
                    fingerprint=verification_digest.hexdigest(),
                )
            final = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="payload changed while its tensor schema was being inspected",
                )
            current_name = path.lstat()
            if (current_name.st_dev, current_name.st_ino) != (
                final.st_dev,
                final.st_ino,
            ):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="payload name changed while it was being verified",
                )
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        return ReadResult(ReadStatus.NOT_FOUND, diagnostic=str(exc))
    except OSError as exc:
        status = (
            ReadStatus.TRANSIENT_IO
            if exc.errno in _TRANSIENT_ERRNOS
            else ReadStatus.IDENTITY_MISMATCH
        )
        return ReadResult(status, diagnostic=str(exc))
    except ValueError as exc:
        return ReadResult(ReadStatus.IDENTITY_MISMATCH, diagnostic=str(exc))
    digest_value = digest.hexdigest()
    if size != proposal.payload_size:
        return ReadResult(
            ReadStatus.IDENTITY_MISMATCH,
            diagnostic=f"payload size mismatch: expected {proposal.payload_size}, got {size}",
            fingerprint=digest_value,
        )
    if digest_value != proposal.payload_sha256:
        return ReadResult(
            ReadStatus.IDENTITY_MISMATCH,
            diagnostic="payload SHA-256 mismatch",
            fingerprint=digest_value,
        )
    if tensor_schema.sha256 != proposal.tensor_schema_sha256:
        return ReadResult(
            ReadStatus.IDENTITY_MISMATCH,
            diagnostic="tensor schema SHA-256 mismatch",
            fingerprint=tensor_schema.sha256,
        )
    if tensor_schema.dtype != proposal.tensor_dtype:
        return ReadResult(
            ReadStatus.IDENTITY_MISMATCH,
            diagnostic="tensor dtype mismatch",
            fingerprint=tensor_schema.sha256,
        )
    if tensor_schema.total_numel != proposal.tensor_numel:
        return ReadResult(
            ReadStatus.IDENTITY_MISMATCH,
            diagnostic="tensor numel mismatch",
            fingerprint=tensor_schema.sha256,
        )
    return ReadResult(
        ReadStatus.OK,
        value=VerifiedPayload(
            relative_path=proposal.payload_relative_path,
            size_bytes=size,
            sha256=digest_value,
            device=int(final.st_dev),
            inode=int(final.st_ino),
        ),
        fingerprint=digest_value,
    )


def verify_publication_artifact(
    run_root: str | Path,
    relative_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_theta_sha256: str,
    theta_tensor_key: str | None,
    chunk_size: int = 1024 * 1024,
) -> ReadResult[VerifiedArtifact]:
    """Verify an immutable safetensors checkpoint and its bound theta digest."""

    try:
        path = resolve_run_relative_path(run_root, relative_path)
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return ReadResult(
                ReadStatus.IDENTITY_MISMATCH,
                diagnostic="publication artifact must be a regular non-symlink file",
            )
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="publication artifact identity changed during open",
                )
            digest = hashlib.sha256()
            offset = 0
            while offset < opened.st_size:
                chunk = os.pread(descriptor, min(chunk_size, opened.st_size - offset), offset)
                if not chunk:
                    return ReadResult(
                        ReadStatus.IDENTITY_MISMATCH,
                        diagnostic="publication artifact became truncated",
                    )
                digest.update(chunk)
                offset += len(chunk)
            digest_value = digest.hexdigest()
            if opened.st_size != expected_size or digest_value != expected_sha256:
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="publication artifact size or SHA-256 mismatch",
                    fingerprint=digest_value,
                )
            descriptor_path = f"/proc/self/fd/{descriptor}"
            with safe_open(descriptor_path, framework="pt", device="cpu") as checkpoint:
                metadata = checkpoint.metadata() or {}
                theta_sha256 = metadata.get("fs_diloco_theta_sha256")
                keys = tuple(checkpoint.keys())
                if not keys:
                    return ReadResult(
                        ReadStatus.MALFORMED,
                        diagnostic="publication artifact has no tensors",
                    )
                if theta_tensor_key is None:
                    try:
                        tensor_order = json.loads(metadata["fs_diloco_theta_order"])
                    except (KeyError, TypeError, json.JSONDecodeError) as exc:
                        raise ValueError(
                            "weight artifact lacks valid theta order metadata"
                        ) from exc
                    if (
                        not isinstance(tensor_order, list)
                        or not tensor_order
                        or any(not isinstance(item, str) for item in tensor_order)
                        or len(set(tensor_order)) != len(tensor_order)
                        or set(tensor_order) != set(keys)
                    ):
                        raise ValueError("weight artifact theta order does not match tensor keys")
                    actual_theta = torch.cat(
                        [checkpoint.get_tensor(item).reshape(-1) for item in tensor_order]
                    )
                else:
                    if theta_tensor_key not in keys:
                        return ReadResult(
                            ReadStatus.MALFORMED,
                            diagnostic=f"publication artifact lacks {theta_tensor_key!r}",
                        )
                    actual_theta = checkpoint.get_tensor(theta_tensor_key)
                actual_theta_sha256 = tensor_content_sha256(actual_theta)
            verification_digest = hashlib.sha256()
            offset = 0
            while offset < opened.st_size:
                chunk = os.pread(descriptor, min(chunk_size, opened.st_size - offset), offset)
                if not chunk:
                    return ReadResult(
                        ReadStatus.IDENTITY_MISMATCH,
                        diagnostic="publication artifact became truncated during recheck",
                    )
                verification_digest.update(chunk)
                offset += len(chunk)
            if verification_digest.hexdigest() != digest_value:
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="publication artifact changed during safetensors inspection",
                    fingerprint=verification_digest.hexdigest(),
                )
            if theta_sha256 != expected_theta_sha256:
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="publication artifact theta identity mismatch",
                    fingerprint=theta_sha256,
                )
            if actual_theta_sha256 != expected_theta_sha256:
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="publication artifact theta tensor digest mismatch",
                    fingerprint=actual_theta_sha256,
                )
            final = os.fstat(descriptor)
            named = path.lstat()
            identity_before = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            identity_after = (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            )
            if identity_before != identity_after or (named.st_dev, named.st_ino) != (
                final.st_dev,
                final.st_ino,
            ):
                return ReadResult(
                    ReadStatus.IDENTITY_MISMATCH,
                    diagnostic="publication artifact changed during verification",
                )
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        return ReadResult(ReadStatus.NOT_FOUND, diagnostic=str(exc))
    except (SafetensorError, ValueError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
            return ReadResult(ReadStatus.TRANSIENT_IO, diagnostic=str(exc))
        return ReadResult(ReadStatus.MALFORMED, diagnostic=str(exc))
    return ReadResult(
        ReadStatus.OK,
        value=VerifiedArtifact(
            relative_path=relative_path,
            size_bytes=int(final.st_size),
            sha256=digest_value,
            theta_sha256=expected_theta_sha256,
            device=int(final.st_dev),
            inode=int(final.st_ino),
        ),
        fingerprint=digest_value,
    )


def tensor_schema_sha256(schema: list[dict[str, object]]) -> str:
    """Return the canonical digest for one safetensors tensor schema."""

    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _tensor_bytes_are_finite(raw: bytes, *, dtype: str) -> bool:
    """Check one homogeneous tensor region with vectorized CPU kernels."""

    torch_dtype = {"float32": torch.float32, "bfloat16": torch.bfloat16}[dtype]
    # A writable buffer prevents torch.frombuffer from warning; this copy is still
    # substantially cheaper than interpreting every scalar in Python.
    values = torch.frombuffer(bytearray(raw), dtype=torch_dtype)
    return bool(torch.isfinite(values).all().item())


def _inspect_safetensors(descriptor: int, *, file_size: int) -> TensorSchema:
    """Validate safetensors structure and finite tensor values from an open file."""

    if file_size < 12:
        raise ValueError("safetensors payload is too small")
    prefix = os.pread(descriptor, 8, 0)
    if len(prefix) != 8:
        raise ValueError("safetensors header length is truncated")
    header_size = int.from_bytes(prefix, byteorder="little", signed=False)
    if header_size <= 0 or header_size > 16 * 1024 * 1024 or header_size > file_size - 8:
        raise ValueError("safetensors header length is invalid")
    header_bytes = os.pread(descriptor, header_size, 8)
    if len(header_bytes) != header_size:
        raise ValueError("safetensors header is truncated")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"safetensors header contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        header = json.loads(header_bytes, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("safetensors header is not valid UTF-8 JSON") from exc
    if not isinstance(header, dict):
        raise ValueError("safetensors header must be an object")
    entries: list[tuple[int, int, str, str, list[int], int]] = []
    schema: list[dict[str, object]] = []
    observed_dtype: str | None = None
    total_numel = 0
    dtype_sizes = {"F32": ("float32", 4), "BF16": ("bfloat16", 2)}
    for key in sorted(item for item in header if item != "__metadata__"):
        raw_entry = header[key]
        if not isinstance(key, str) or not key or not isinstance(raw_entry, dict):
            raise ValueError("safetensors tensor entries must be named objects")
        if set(raw_entry) != {"dtype", "shape", "data_offsets"}:
            raise ValueError(f"safetensors tensor {key!r} has an invalid schema")
        wire_dtype = raw_entry["dtype"]
        shape = raw_entry["shape"]
        offsets = raw_entry["data_offsets"]
        if wire_dtype not in dtype_sizes:
            raise ValueError(f"unsupported safetensors dtype: {wire_dtype}")
        if not isinstance(shape, list) or any(
            isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 0
            for dimension in shape
        ):
            raise ValueError(f"safetensors tensor {key!r} has an invalid shape")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(offset, bool) or not isinstance(offset, int) for offset in offsets)
            or offsets[0] < 0
            or offsets[1] < offsets[0]
        ):
            raise ValueError(f"safetensors tensor {key!r} has invalid data offsets")
        dtype, element_size = dtype_sizes[wire_dtype]
        numel = math.prod(shape)
        if offsets[1] - offsets[0] != numel * element_size:
            raise ValueError(f"safetensors tensor {key!r} byte size does not match its shape")
        if observed_dtype is not None and observed_dtype != dtype:
            raise ValueError("mixed tensor dtypes are not valid for a flat update payload")
        observed_dtype = dtype
        total_numel += numel
        schema.append({"key": key, "dtype": dtype, "shape": shape})
        entries.append((offsets[0], offsets[1], key, dtype, shape, element_size))
    if not entries or observed_dtype is None or total_numel <= 0:
        raise ValueError("safetensors payload must contain at least one non-empty tensor")
    entries.sort()
    expected_offset = 0
    data_start = 8 + header_size
    for start, end, key, dtype, _shape, element_size in entries:
        if start != expected_offset:
            raise ValueError("safetensors data offsets must be contiguous and non-overlapping")
        raw = os.pread(descriptor, end - start, data_start + start)
        if len(raw) != end - start:
            raise ValueError(f"safetensors tensor {key!r} data is truncated")
        if not _tensor_bytes_are_finite(raw, dtype=dtype):
            raise ValueError(f"safetensors tensor {key!r} contains non-finite values")
        expected_offset = end
    if data_start + expected_offset != file_size:
        raise ValueError("safetensors data length does not match the payload size")
    return TensorSchema(
        sha256=tensor_schema_sha256(schema),
        dtype=observed_dtype,
        total_numel=total_numel,
    )
