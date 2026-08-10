"""Explicit deterministic cursor for indexed/materialized training data."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from ._validation import identity, sha256, strict_int


@dataclass(frozen=True, order=True)
class IndexedBlockCursor:
    """The next block position for one stable contributor stream."""

    stable_contributor_key: str
    dataset_identity_sha256: str
    seed: int
    block_index: int
    shard_index: int = 0
    shard_count: int = 1

    def __post_init__(self) -> None:
        identity(self.stable_contributor_key, name="stable_contributor_key")
        sha256(self.dataset_identity_sha256, name="dataset_identity_sha256")
        strict_int(self.seed, name="seed", minimum=0)
        strict_int(self.block_index, name="block_index", minimum=0)
        shard_index = strict_int(self.shard_index, name="shard_index", minimum=0)
        shard_count = strict_int(self.shard_count, name="shard_count", minimum=1)
        if shard_index >= shard_count:
            raise ValueError("shard_index must be smaller than shard_count")

    def advance(self, blocks: int = 1) -> "IndexedBlockCursor":
        count = strict_int(blocks, name="blocks", minimum=1)
        return IndexedBlockCursor(
            stable_contributor_key=self.stable_contributor_key,
            dataset_identity_sha256=self.dataset_identity_sha256,
            seed=self.seed,
            block_index=self.block_index + count,
            shard_index=self.shard_index,
            shard_count=self.shard_count,
        )

    def deterministic_sample_index(self, *, dataset_blocks: int) -> int:
        size = strict_int(dataset_blocks, name="dataset_blocks", minimum=1)
        if size < self.shard_count:
            raise ValueError("dataset_blocks must be at least shard_count")
        # All shards share one keyed bijection. Interleaving shard positions
        # before applying it guarantees no overlap until the finite dataset
        # wraps; hashing each contributor independently cannot provide that.
        digest = hashlib.sha256(
            (f"{self.dataset_identity_sha256}\0{self.seed}\0{self.shard_count}").encode("utf-8")
        ).digest()
        multiplier = int.from_bytes(digest[:8], "big") % size
        while math.gcd(multiplier, size) != 1:
            multiplier = (multiplier + 1) % size
        offset = int.from_bytes(digest[8:16], "big") % size
        logical_position = self.block_index * self.shard_count + self.shard_index
        return (multiplier * (logical_position % size) + offset) % size

    @property
    def stream_identity_sha256(self) -> str:
        raw = (
            f"{self.stable_contributor_key}\0{self.dataset_identity_sha256}\0{self.seed}\0"
            f"{self.shard_index}\0{self.shard_count}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ContributorResumeState:
    cursor: int
    last_receipt_id: str | None
    last_receipt_sha256: str | None
    last_update_id: str | None
    next_cycle_seq: int
    stream_epoch: int | None = None

    def __post_init__(self) -> None:
        cursor = strict_int(self.cursor, name="cursor", minimum=0)
        sequence = strict_int(self.next_cycle_seq, name="next_cycle_seq", minimum=1)
        if sequence == 1:
            if (
                self.last_receipt_id is not None
                or self.last_receipt_sha256 is not None
                or self.last_update_id is not None
            ):
                raise ValueError("empty progress cannot name a previous receipt or update")
        else:
            identity(self.last_receipt_id, name="last_receipt_id")
            sha256(self.last_receipt_sha256, name="last_receipt_sha256")
            if self.last_update_id is not None:
                identity(self.last_update_id, name="last_update_id")
        if self.stream_epoch is not None:
            strict_int(self.stream_epoch, name="stream_epoch", minimum=1)
        object.__setattr__(self, "cursor", cursor)
