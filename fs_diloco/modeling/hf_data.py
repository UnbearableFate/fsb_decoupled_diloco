"""Dataset loading and infinite batch iterators."""

from __future__ import annotations

import itertools
import os
import random
from dataclasses import dataclass
from typing import Any, Iterator

import torch


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
    """Load one configured text split with the same WikiText fallback as training."""
    from datasets import load_dataset

    dataset_name = data_config.dataset_name
    if dataset_name == "wikitext" and os.environ.get("FS_DILOCO_HF_WIKITEXT_REPO"):
        dataset_name = os.environ["FS_DILOCO_HF_WIKITEXT_REPO"]
    try:
        return load_dataset(
            dataset_name,
            data_config.dataset_config_name,
            split=split,
            cache_dir=data_config.cache_dir,
            streaming=bool(data_config.streaming),
        )
    except Exception as exc:
        if data_config.dataset_name != "wikitext" or "/" in str(dataset_name):
            raise
        try:
            return load_dataset(
                "Salesforce/wikitext",
                data_config.dataset_config_name,
                split=split,
                cache_dir=data_config.cache_dir,
                streaming=bool(data_config.streaming),
            )
        except Exception:
            raise exc


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
        block_indices: Iterator[int] = (
            index % len(blocks) for index in itertools.count()
        )
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


def wikitext_batches(
    data_config: Any,
    tokenizer: Any,
    *,
    learner_index: int,
    num_learners: int,
    micro_batch_size: int,
    block_size: int,
    seed: int,
) -> Iterator[Batch]:
    dataset = load_text_split(data_config, data_config.train_split)
    dataset = dataset.shard(num_shards=num_learners, index=learner_index, contiguous=True)
    blocks = text_rows_to_blocks(dataset, tokenizer, block_size)
    return _batched_blocks(
        blocks,
        micro_batch_size,
        shuffle=bool(data_config.shuffle_blocks),
        seed=seed,
        learner_index=learner_index,
    )


def build_batch_iterator(
    config: Any,
    tokenizer: Any,
    *,
    learner_index: int,
    num_learners: int,
) -> Iterator[Batch]:
    if config.data.dataset_name == "synthetic":
        vocab_size = int(getattr(tokenizer, "vocab_size", config.model.synthetic_vocab_size))
        return synthetic_batches(
            vocab_size=vocab_size,
            block_size=config.training.block_size,
            micro_batch_size=config.training.micro_batch_size,
            seed=config.training.seed,
            learner_index=learner_index,
        )
    return wikitext_batches(
        config.data,
        tokenizer,
        learner_index=learner_index,
        num_learners=num_learners,
        micro_batch_size=config.training.micro_batch_size,
        block_size=config.training.block_size,
        seed=config.training.seed,
    )


def build_stream_batch_iterator(
    config: Any,
    tokenizer: Any,
    *,
    stream_id: int,
    stream_pool_size: int,
) -> Iterator[Batch]:
    """Build one fixed virtual stream independent of current member count."""
    stream_id = int(stream_id)
    stream_pool_size = int(stream_pool_size)
    if stream_pool_size < 1:
        raise ValueError("stream_pool_size must be >= 1")
    if not 0 <= stream_id < stream_pool_size:
        raise ValueError("stream_id must be within the fixed stream pool")
    return build_batch_iterator(
        config,
        tokenizer,
        learner_index=stream_id,
        num_learners=stream_pool_size,
    )
