"""Dataset loading and infinite batch iterators."""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from typing import Any, Iterator

import torch

from ..protocol.data_cursor import IndexedBlockCursor
from .hf_identity import reject_local_reference


@dataclass
class Batch:
    input_ids: torch.Tensor
    labels: torch.Tensor
    num_tokens: int
    num_examples: int

    def to(self, device: torch.device) -> "Batch":
        return Batch(
            input_ids=self.input_ids.to(device),
            labels=self.labels.to(device),
            num_tokens=self.num_tokens,
            num_examples=self.num_examples,
        )


def synthetic_batches(
    *,
    vocab_size: int,
    block_size: int,
    micro_batch_size: int,
    seed: int,
    learner_index: int,
) -> Iterator[Batch]:
    generator = torch.Generator()
    generator.manual_seed(seed + learner_index * 100_003)
    while True:
        input_ids = torch.randint(
            low=0,
            high=vocab_size,
            size=(micro_batch_size, block_size),
            generator=generator,
            dtype=torch.long,
        )
        yield Batch(
            input_ids=input_ids,
            labels=input_ids.clone(),
            num_tokens=int(input_ids.numel()),
            num_examples=micro_batch_size,
        )


def _chunks(tokens: list[int], block_size: int) -> list[list[int]]:
    usable = (len(tokens) // block_size) * block_size
    return [tokens[i : i + block_size] for i in range(0, usable, block_size)]


def load_text_split(data_config: Any, split: str) -> Any:
    """Load one configured text split at its immutable revision."""
    reject_local_reference(str(data_config.dataset_name), kind="dataset")

    from datasets import load_dataset

    return load_dataset(
        data_config.dataset_name,
        data_config.dataset_config_name,
        revision=data_config.revision,
        split=split,
        cache_dir=data_config.cache_dir,
    )


def text_rows_to_blocks(dataset: Any, tokenizer: Any, block_size: int) -> list[list[int]]:
    """Apply the repository text→tokens+EOS→non-overlap block protocol."""
    token_stream: list[int] = []
    eos_id = getattr(tokenizer, "eos_token_id", None)
    for row in dataset:
        text = row.get("text") if isinstance(row, dict) else None
        if not text:
            continue
        token_stream.extend(tokenizer(text, add_special_tokens=False)["input_ids"])
        if eos_id is not None:
            token_stream.append(eos_id)
    return _chunks(token_stream, block_size)


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return value ^ (value >> 31)


def _batched_blocks(
    blocks: list[list[int]],
    micro_batch_size: int,
    *,
    shuffle: bool,
    seed: int,
    learner_index: int,
) -> Iterator[Batch]:
    if not blocks:
        raise ValueError("tokenized dataset produced zero blocks")

    if not shuffle:
        block_indices: Iterator[int] = (index % len(blocks) for index in itertools.count())
    else:
        base_seed = _splitmix64(int(seed) ^ _splitmix64(int(learner_index)))

        def shuffled_indices() -> Iterator[int]:
            for epoch in itertools.count():
                indices = list(range(len(blocks)))
                random.Random(_splitmix64(base_seed + epoch)).shuffle(indices)
                yield from indices

        block_indices = shuffled_indices()

    while True:
        batch_blocks = [blocks[next(block_indices)] for _ in range(micro_batch_size)]
        input_ids = torch.tensor(batch_blocks, dtype=torch.long)
        yield Batch(
            input_ids=input_ids,
            labels=input_ids.clone(),
            num_tokens=int(input_ids.numel()),
            num_examples=len(batch_blocks),
        )


def build_indexed_batch_iterator(
    config: Any,
    tokenizer: Any,
    *,
    cursor: IndexedBlockCursor,
) -> Iterator[Batch]:
    """Build a random-access resumable stream from the explicit cursor.

    The authority cursor counts optimizer micro-batches.  Each batch expands
    into ``micro_batch_size`` adjacent logical block positions before the
    shared keyed permutation is applied, so shards cannot overlap before the
    finite block set wraps.
    """

    micro_batch_size = int(config.training.micro_batch_size)
    block_size = int(config.data.block_size)
    if config.data.dataset_name == "synthetic":
        vocab_size = int(getattr(tokenizer, "vocab_size", config.model.synthetic_vocab_size))
        dataset_blocks = 2**31 - 1
        materialized_blocks: list[list[int]] | None = None
    else:
        dataset = load_text_split(config.data, config.data.train_split)
        materialized_blocks = text_rows_to_blocks(dataset, tokenizer, block_size)
        dataset_blocks = len(materialized_blocks)
        if dataset_blocks < cursor.shard_count:
            raise ValueError("tokenized dataset has fewer blocks than configured data shards")

    next_cursor = cursor
    while True:
        batch_blocks: list[list[int]] = []
        for micro_index in range(micro_batch_size):
            block_cursor = IndexedBlockCursor(
                stable_contributor_key=next_cursor.stable_contributor_key,
                dataset_identity_sha256=next_cursor.dataset_identity_sha256,
                seed=next_cursor.seed,
                block_index=next_cursor.block_index * micro_batch_size + micro_index,
                shard_index=next_cursor.shard_index,
                shard_count=next_cursor.shard_count,
            )
            sample_index = block_cursor.deterministic_sample_index(dataset_blocks=dataset_blocks)
            if materialized_blocks is None:
                generator = torch.Generator()
                generator.manual_seed(_splitmix64(int(config.training.seed) ^ sample_index))
                block = torch.randint(
                    low=0,
                    high=vocab_size,
                    size=(block_size,),
                    generator=generator,
                    dtype=torch.long,
                ).tolist()
            else:
                block = materialized_blocks[sample_index]
            batch_blocks.append(block)
        input_ids = torch.tensor(batch_blocks, dtype=torch.long)
        yield Batch(
            input_ids=input_ids,
            labels=input_ids.clone(),
            num_tokens=int(input_ids.numel()),
            num_examples=micro_batch_size,
        )
        next_cursor = next_cursor.advance()
