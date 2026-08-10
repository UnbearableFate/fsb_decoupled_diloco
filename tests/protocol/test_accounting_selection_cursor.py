from __future__ import annotations

import math

import pytest

from fs_diloco.core.config import Config
from fs_diloco.protocol.data_cursor import IndexedBlockCursor
from fs_diloco.protocol.merge import normalized_update_weights
from fs_diloco.protocol.selection import PersistentFairSelector
from fs_diloco.protocol.token_accounting import TrainingSegmentAccumulator


def test_direct_weight_stop_is_valid() -> None:
    direct = Config()
    direct.model.name_or_path = "synthetic-tiny"
    direct.data.dataset_name = "synthetic"
    direct.sync.stop_after_direct_weight_tokens_applied = 1
    direct.validate()


@pytest.mark.parametrize("tokens", [0, -1, math.nan, math.inf, -math.inf, True])
def test_merge_weights_reject_each_nonpositive_nonfinite_or_untyped_token(tokens) -> None:
    with pytest.raises(ValueError, match="positive integer direct tokens"):
        normalized_update_weights(
            [
                {
                    "update_id": "update-1",
                    "base_global_version": 0,
                    "tokens_this_update": tokens,
                }
            ],
            current_version=0,
            staleness_lambda=0.25,
        )


@pytest.mark.parametrize("staleness_lambda", [math.nan, math.inf, -math.inf, -1.0, True])
def test_merge_weights_reject_nonfinite_or_negative_weight_parameters(
    staleness_lambda,
) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        normalized_update_weights(
            [
                {
                    "update_id": "update-1",
                    "base_global_version": 0,
                    "tokens_this_update": 1,
                }
            ],
            current_version=0,
            staleness_lambda=staleness_lambda,
        )


def test_merge_weights_use_stable_fsum_and_reject_duplicate_ids() -> None:
    updates = [
        {"update_id": "large", "base_global_version": 0, "tokens_this_update": 10**200},
        {"update_id": "small", "base_global_version": 0, "tokens_this_update": 1},
    ]
    weights = normalized_update_weights(updates, current_version=0, staleness_lambda=0.0)
    assert math.fsum(weights.values()) == 1.0
    with pytest.raises(ValueError, match="unique"):
        normalized_update_weights(
            [updates[0], {**updates[1], "update_id": "large"}],
            current_version=0,
            staleness_lambda=0.0,
        )
    with pytest.raises(ValueError, match="non-finite total"):
        normalized_update_weights(
            [
                {
                    "update_id": "huge-1",
                    "base_global_version": 0,
                    "tokens_this_update": 10**308,
                },
                {
                    "update_id": "huge-2",
                    "base_global_version": 0,
                    "tokens_this_update": 10**308,
                },
            ],
            current_version=0,
            staleness_lambda=0.0,
        )


def test_prepublication_replace_discards_old_segment_and_resets_effective_metrics() -> None:
    accumulator = TrainingSegmentAccumulator(base_global_version=0, interval_start_step=0)
    accumulator.record_step(local_step_end=1, tokens=10, examples=2, loss=4.0, grad_norm=2.0)
    closed = accumulator.replace_base(new_base_global_version=1)
    accumulator.record_step(local_step_end=2, tokens=6, examples=1, loss=1.5, grad_norm=1.0)

    accounting = accumulator.finalize_cycle()

    assert closed.effective_tokens == 10
    assert accounting.processed_tokens == 16
    assert accounting.local_discarded_tokens == 10
    assert accounting.effective_tokens == 6
    assert accounting.segment.local_step_start == 1
    assert accounting.segment.local_step_end == 2
    assert accounting.segment.mean_loss == 1.5
    assert accounting.segment.mean_grad_norm == 1.0
    assert accounting.proposal_expected


def test_cycle_end_replace_produces_receipt_only_balanced_accounting() -> None:
    accumulator = TrainingSegmentAccumulator(base_global_version=0, interval_start_step=0)
    accumulator.record_step(local_step_end=1, tokens=8, examples=1, loss=2.0)
    accumulator.replace_base(new_base_global_version=1)

    accounting = accumulator.finalize_cycle()

    assert accounting.processed_tokens == accounting.local_discarded_tokens == 8
    assert accounting.effective_tokens == 0
    assert accounting.segment.examples == 0
    assert accounting.segment.mean_loss is None
    assert not accounting.proposal_expected


def test_predict_rebase_retains_work_without_loss_or_double_count() -> None:
    accumulator = TrainingSegmentAccumulator(base_global_version=0, interval_start_step=0)
    accumulator.record_step(local_step_end=1, tokens=8, examples=2, loss=3.0)
    accumulator.replace_base(new_base_global_version=1, retain_effective_work=True)

    accounting = accumulator.finalize_cycle()

    assert accounting.processed_tokens == accounting.effective_tokens == 8
    assert accounting.local_discarded_tokens == 0
    assert accounting.retained_tokens_since_base == 8
    assert accounting.segment.mean_loss == 3.0


def test_persistent_fair_selector_meets_frozen_1000_round_gate() -> None:
    keys = tuple(f"learner-{index}" for index in range(8))
    selector = PersistentFairSelector()
    counts = {key: 0 for key in keys}
    waits = {key: 0 for key in keys}
    maximum_wait = 0
    lineage: list[tuple[str, ...]] = []
    for version in range(1, 1001):
        selected = selector.select(keys, quorum_max=3)
        lineage.append(selected)
        for key in keys:
            if key in selected:
                counts[key] += 1
                waits[key] = 0
            else:
                waits[key] += 1
                maximum_wait = max(maximum_wait, waits[key])
        selector.commit(selected, committed_version=version)

    values = tuple(counts.values())
    jain = sum(values) ** 2 / (len(values) * sum(value * value for value in values))
    assert max(values) - min(values) <= 1
    assert maximum_wait <= math.ceil(8 / 3) + 1
    assert jain >= 0.95
    replay = PersistentFairSelector()
    replay_lineage = []
    for version in range(1, 1001):
        selected = replay.select(keys, quorum_max=3)
        replay_lineage.append(selected)
        replay.commit(selected, committed_version=version)
    assert replay_lineage == lineage


def test_persistent_fair_selector_has_deterministic_balanced_prefix() -> None:
    keys = tuple(f"learner-{index}" for index in range(8))
    expected_cycle = (
        ("learner-0", "learner-1", "learner-2"),
        ("learner-3", "learner-4", "learner-5"),
        ("learner-6", "learner-7", "learner-0"),
        ("learner-1", "learner-2", "learner-3"),
        ("learner-4", "learner-5", "learner-6"),
        ("learner-7", "learner-0", "learner-1"),
        ("learner-2", "learner-3", "learner-4"),
        ("learner-5", "learner-6", "learner-7"),
    )
    selector = PersistentFairSelector()
    actual: list[tuple[str, ...]] = []
    for version in range(1, 17):
        selected = selector.select(keys, quorum_max=3)
        actual.append(selected)
        selector.commit(selected, committed_version=version)

    assert tuple(actual) == expected_cycle * 2


def test_persistent_fair_selector_does_not_charge_failed_selection() -> None:
    selector = PersistentFairSelector()
    selected = selector.select(("learner-0", "learner-1"), quorum_max=1)

    assert selected == ("learner-0",)
    assert selector.select(("learner-0", "learner-1"), quorum_max=1) == selected
    assert selector.committed_service_count == {}


def test_full_quorum_keeps_stable_set_and_reduction_order() -> None:
    keys = ("learner-3", "learner-1", "learner-2", "learner-0")
    selector = PersistentFairSelector()
    for version in range(1, 5):
        selected = selector.select(keys, quorum_max=len(keys))
        assert selected == tuple(sorted(keys))
        selector.commit(selected, committed_version=version)


def test_indexed_cursor_is_explicit_stable_and_resumable() -> None:
    cursor = IndexedBlockCursor(
        stable_contributor_key="stream-7",
        dataset_identity_sha256="a" * 64,
        seed=42,
        block_index=10,
    )
    assert cursor.deterministic_sample_index(
        dataset_blocks=997
    ) == cursor.deterministic_sample_index(dataset_blocks=997)
    assert cursor.advance(3) == IndexedBlockCursor(
        stable_contributor_key="stream-7",
        dataset_identity_sha256="a" * 64,
        seed=42,
        block_index=13,
    )


def test_indexed_cursor_uses_one_bijection_for_nonoverlapping_shards() -> None:
    streams = tuple(
        IndexedBlockCursor(
            stable_contributor_key=f"learner-{shard}",
            dataset_identity_sha256="b" * 64,
            seed=7,
            block_index=0,
            shard_index=shard,
            shard_count=3,
        )
        for shard in range(3)
    )
    selections = [
        {
            (stream if block == 0 else stream.advance(block)).deterministic_sample_index(
                dataset_blocks=300
            )
            for block in range(100)
        }
        for stream in streams
    ]

    assert all(len(selection) == 100 for selection in selections)
    assert not (selections[0] & selections[1])
    assert not (selections[0] & selections[2])
    assert not (selections[1] & selections[2])
    assert len({stream.stream_identity_sha256 for stream in streams}) == 3


def test_indexed_cursor_rejects_invalid_or_undersized_shards() -> None:
    with pytest.raises(ValueError, match="shard_index"):
        IndexedBlockCursor(
            stable_contributor_key="learner-1",
            dataset_identity_sha256="c" * 64,
            seed=0,
            block_index=0,
            shard_index=2,
            shard_count=2,
        )
    cursor = IndexedBlockCursor(
        stable_contributor_key="learner-1",
        dataset_identity_sha256="c" * 64,
        seed=0,
        block_index=0,
        shard_index=1,
        shard_count=2,
    )
    with pytest.raises(ValueError, match="at least shard_count"):
        cursor.deterministic_sample_index(dataset_blocks=1)
