"""Verify the sole resumable production data permutation."""

from __future__ import annotations

from itertools import islice
from types import SimpleNamespace

import torch

from fs_diloco.core.config import Config
from fs_diloco.modeling.hf_data import build_indexed_batch_iterator
from fs_diloco.protocol.data_cursor import IndexedBlockCursor


def _config() -> Config:
    """Build a small synthetic configuration for indexed iterator tests."""

    config = Config()
    config.data.dataset_name = "synthetic"
    config.data.block_size = 8
    config.training.micro_batch_size = 2
    config.training.seed = 987
    return config


def _cursor(*, block_index: int = 0, shard_index: int = 0) -> IndexedBlockCursor:
    """Build one stable cursor in a two-shard data stream."""

    return IndexedBlockCursor(
        stable_contributor_key=f"stream-{shard_index}",
        dataset_identity_sha256="a" * 64,
        seed=987,
        block_index=block_index,
        shard_index=shard_index,
        shard_count=2,
    )


def _sample(cursor: IndexedBlockCursor, count: int) -> list[torch.Tensor]:
    """Collect detached production batches from one explicit cursor."""

    iterator = build_indexed_batch_iterator(
        _config(),
        SimpleNamespace(vocab_size=2**20),
        cursor=cursor,
    )
    return [batch.input_ids.clone() for batch in islice(iterator, count)]


def test_indexed_iterator_replays_and_resumes_the_exact_stream() -> None:
    """Replacement learners must reproduce the same next batch from authority cursor state."""

    uninterrupted = _sample(_cursor(), 5)
    replayed = _sample(_cursor(), 5)
    resumed = _sample(_cursor(block_index=3), 2)

    assert all(torch.equal(left, right) for left, right in zip(uninterrupted, replayed))
    assert all(torch.equal(left, right) for left, right in zip(uninterrupted[3:], resumed))


def test_indexed_iterator_keeps_contributor_shards_distinct() -> None:
    """Current contributors must not consume the same permuted block positions."""

    first = _sample(_cursor(shard_index=0), 8)
    second = _sample(_cursor(shard_index=1), 8)

    assert all(not torch.equal(left, right) for left, right in zip(first, second))
