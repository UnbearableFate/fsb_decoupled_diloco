"""Tests for deterministic standalone rank-local data batching."""

from typing import Any

from torch_ddp_baselines.data import batch_blocks, tokenize_blocks


class NumericTokenizer:
    """Turn numeric test strings into one-token sequences."""

    eos_token_id = None  # Tests intentionally omit synthetic EOS tokens.

    def __call__(self, text: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
        """Return the integer encoded by one test row."""

        assert add_special_tokens is False
        return {"input_ids": [int(text)]}


def test_text_rows_form_nonoverlapping_fixed_blocks() -> None:
    """Rank shards must not silently duplicate or overlap token positions."""

    dataset: list[dict[str, Any]] = [{"text": str(value)} for value in range(8)]

    blocks = tokenize_blocks(dataset, NumericTokenizer(), block_size=2)

    assert blocks == [[0, 1], [2, 3], [4, 5], [6, 7]]


def test_unshuffled_batch_epochs_are_deterministic() -> None:
    """Disabling shuffle must repeat the same complete block order every epoch."""

    iterator = batch_blocks(
        [[0, 1], [2, 3]],
        micro_batch_size=1,
        shuffle=False,
        seed=1337,
    )

    observed = [next(iterator).input_ids.tolist() for _ in range(4)]
    assert observed == [[[0, 1]], [[2, 3]], [[0, 1]], [[2, 3]]]
