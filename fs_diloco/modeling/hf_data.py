"""Dataset loading and infinite batch iterators."""

from __future__ import annotations

import itertools
import os
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


def _batched_blocks(blocks: list[list[int]], micro_batch_size: int) -> Iterator[Batch]:
    if not blocks:
        raise ValueError("tokenized dataset produced zero blocks")
    for start in itertools.count(step=micro_batch_size):
        indices = [(start + offset) % len(blocks) for offset in range(micro_batch_size)]
        batch_blocks = [blocks[index] for index in indices]
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
) -> Iterator[Batch]:
    from datasets import load_dataset

    dataset_name = data_config.dataset_name
    if dataset_name == "wikitext" and os.environ.get("FS_DILOCO_HF_WIKITEXT_REPO"):
        dataset_name = os.environ["FS_DILOCO_HF_WIKITEXT_REPO"]
    try:
        dataset = load_dataset(
            dataset_name,
            data_config.dataset_config_name,
            split=data_config.train_split,
            cache_dir=data_config.cache_dir,
            streaming=bool(data_config.streaming),
        )
    except Exception as exc:
        if data_config.dataset_name != "wikitext" or "/" in str(dataset_name):
            raise
        try:
            dataset = load_dataset(
                "Salesforce/wikitext",
                data_config.dataset_config_name,
                split=data_config.train_split,
                cache_dir=data_config.cache_dir,
                streaming=bool(data_config.streaming),
            )
        except Exception:
            raise exc
    dataset = dataset.shard(num_shards=num_learners, index=learner_index, contiguous=True)
    texts = [row["text"] for row in dataset if row.get("text")]
    token_stream: list[int] = []
    for text in texts:
        token_stream.extend(tokenizer(text, add_special_tokens=False)["input_ids"])
        eos_id = getattr(tokenizer, "eos_token_id", None)
        if eos_id is not None:
            token_stream.append(eos_id)
    blocks = _chunks(token_stream, block_size)
    return _batched_blocks(blocks, micro_batch_size)


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
    )
