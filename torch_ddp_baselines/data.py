"""Deterministic rank-local WikiText batching for standalone baselines."""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from typing import Any, Iterator

import torch

from .config import BaselineConfig


@dataclass(frozen=True)
class Batch:
    """Hold one language-model microbatch and its accounting totals."""

    input_ids: torch.Tensor  # Token IDs supplied to the model.
    labels: torch.Tensor  # Next-token labels aligned with ``input_ids``.
    num_tokens: int  # Number of tokens represented by this microbatch.
    num_examples: int  # Number of fixed-length blocks in this microbatch.

    def to(self, device: torch.device) -> "Batch":
        """Move tensor members to a device while retaining accounting values."""

        return Batch(
            input_ids=self.input_ids.to(device),
            labels=self.labels.to(device),
            num_tokens=self.num_tokens,
            num_examples=self.num_examples,
        )


def load_rank_dataset(config: BaselineConfig, *, rank: int, world_size: int) -> Any:
    """Load the pinned train split and select one deterministic contiguous rank shard."""

    from datasets import load_dataset

    dataset = load_dataset(
        config.data.dataset_name,
        config.data.dataset_config_name,
        revision=config.data.revision,
        split=config.data.train_split,
        cache_dir=None,
    )
    return dataset.shard(num_shards=world_size, index=rank, contiguous=True)


def tokenize_blocks(dataset: Any, tokenizer: Any, *, block_size: int) -> list[list[int]]:
    """Convert text rows into non-overlapping fixed-length token blocks."""

    token_stream: list[int] = []
    eos_token_id = getattr(tokenizer, "eos_token_id", None)
    for row in dataset:
        text = row.get("text") if isinstance(row, dict) else None
        if not text:
            continue
        token_stream.extend(tokenizer(text, add_special_tokens=False)["input_ids"])
        if eos_token_id is not None:
            token_stream.append(int(eos_token_id))
    usable_tokens = len(token_stream) // block_size * block_size
    return [
        token_stream[offset : offset + block_size] for offset in range(0, usable_tokens, block_size)
    ]


def batch_blocks(
    blocks: list[list[int]],
    *,
    micro_batch_size: int,
    shuffle: bool,
    seed: int,
) -> Iterator[Batch]:
    """Yield infinite deterministic epochs over one rank's materialized blocks."""

    if not blocks:
        raise ValueError("tokenized dataset shard produced zero blocks")

    def block_indices() -> Iterator[int]:
        """Yield each epoch's block indexes in the configured deterministic order."""

        for epoch in itertools.count():
            indexes = list(range(len(blocks)))
            if shuffle:
                random.Random(seed + epoch * 100_003).shuffle(indexes)
            yield from indexes

    indexes = block_indices()
    while True:
        examples = [blocks[next(indexes)] for _ in range(micro_batch_size)]
        input_ids = torch.tensor(examples, dtype=torch.long)
        yield Batch(
            input_ids=input_ids,
            labels=input_ids.clone(),
            num_tokens=int(input_ids.numel()),
            num_examples=micro_batch_size,
        )


def build_batch_iterator(
    config: BaselineConfig,
    tokenizer: Any,
    *,
    rank: int,
    world_size: int,
) -> Iterator[Batch]:
    """Build the infinite deterministic microbatch stream for one distributed rank."""

    dataset = load_rank_dataset(config, rank=rank, world_size=world_size)
    blocks = tokenize_blocks(dataset, tokenizer, block_size=config.data.block_size)
    return batch_blocks(
        blocks,
        micro_batch_size=config.training.micro_batch_size,
        shuffle=config.data.shuffle_blocks,
        seed=config.training.seed + rank * 100_003,
    )
