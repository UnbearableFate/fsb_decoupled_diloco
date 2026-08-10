import itertools

from fs_diloco.modeling.hf_data import _batched_blocks


def _flatten_block_ids(iterator, batch_count):
    return [
        int(row[0])
        for batch in itertools.islice(iterator, batch_count)
        for row in batch.input_ids.tolist()
    ]


def test_shuffled_block_stream_is_complete_across_nondivisible_batch_boundaries():
    blocks = [[index, index] for index in range(5)]
    stream = _batched_blocks(
        blocks,
        micro_batch_size=2,
        shuffle=True,
        seed=123,
        learner_index=0,
    )

    flattened = _flatten_block_ids(stream, batch_count=10)
    epochs = [flattened[start : start + 5] for start in range(0, 20, 5)]

    assert all(sorted(epoch) == list(range(5)) for epoch in epochs)
    assert all(left != right for left, right in zip(epochs, epochs[1:]))


def test_shuffled_block_stream_is_deterministic_and_learner_specific():
    blocks = [[index] for index in range(7)]

    def sample(learner_index):
        return _flatten_block_ids(
            _batched_blocks(
                blocks,
                micro_batch_size=3,
                shuffle=True,
                seed=987,
                learner_index=learner_index,
            ),
            batch_count=7,
        )

    assert sample(0) == sample(0)
    assert sample(0) != sample(1)


def test_shuffle_disabled_cycles_in_source_order():
    blocks = [[index] for index in range(5)]
    stream = _batched_blocks(
        blocks,
        micro_batch_size=2,
        shuffle=False,
        seed=123,
        learner_index=9,
    )

    batches = [batch.input_ids.flatten().tolist() for batch in itertools.islice(stream, 4)]

    assert batches == [[0, 1], [2, 3], [4, 0], [1, 2]]
