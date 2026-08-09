from __future__ import annotations

import json
from pathlib import Path

import torch

from fs_diloco.modeling.outer_optim import (
    OuterOptimizerConfig,
    init_outer_state,
    outer_optimizer_step,
)
from fs_diloco.protocol.merge import normalized_update_weights, weighted_average_tensors
from fs_diloco.protocol.selection import PersistentFairSelector
from fs_diloco.protocol.token_accounting import TrainingSegmentAccumulator


PLAN03_REQUIREMENTS = frozenset({"P3-REBASE", "P6-QUALITY", "SEL-02", "SEL-06", "TOK-02"})


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/golden"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_unified_no_change_case_is_exactly_anchored_to_both_p0_projections() -> None:
    unified = _load("unified_v4_trace.json")["cases"]["no_replace_full_quorum"]
    classic = _load("classic_full_v1_trace.json")["semantic_projection"]
    static = _load("static_ha_v1_trace.json")["semantic_projection"]

    assert unified["semantic_projection"] == classic == static
    selector = PersistentFairSelector()
    lineage = []
    for version in range(1, 3):
        selected = selector.select(("learner_001", "learner_000"), quorum_max=2)
        lineage.append(list(selected))
        selector.commit(selected, committed_version=version)
    assert lineage == unified["v4_selected_lineage"]


def test_unified_v4_merge_math_generates_the_p0_semantic_projection() -> None:
    expected = _load("classic_full_v1_trace.json")["semantic_projection"]
    candidates = [
        {
            "update_id": "proposal-000",
            "stable_contributor_key": "learner_000",
            "base_global_version": 0,
            "tokens_this_update": 8,
            "vector": torch.tensor([0.5, -1.5, 2.5], dtype=torch.float32),
        },
        {
            "update_id": "proposal-001",
            "stable_contributor_key": "learner_001",
            "base_global_version": 0,
            "tokens_this_update": 8,
            "vector": torch.tensor([1.5, -2.5, 4.0], dtype=torch.float32),
        },
    ]
    selector = PersistentFairSelector()
    selected_keys = selector.select(
        (item["stable_contributor_key"] for item in candidates), quorum_max=2
    )
    selected = [
        next(item for item in candidates if item["stable_contributor_key"] == key)
        for key in selected_keys
    ]
    weights = normalized_update_weights(
        selected,
        current_version=0,
        staleness_lambda=0.5,
    )
    merged = weighted_average_tensors(
        [item["vector"] for item in selected],
        [weights[item["update_id"]] for item in selected],
    )
    theta_v0 = torch.tensor([1.0, -2.0, 3.0], dtype=torch.float32)
    outer = OuterOptimizerConfig(name="nesterov", lr=0.7, momentum=0.9)
    theta_v1, state_v1 = outer_optimizer_step(
        theta_v0,
        theta_v0 - merged,
        init_outer_state(theta_v0, outer),
        outer,
    )

    def tensor_hex(value: torch.Tensor) -> str:
        return bytes(value.detach().cpu().contiguous().view(torch.uint8).tolist()).hex()

    actual = {
        "selected_ids": [item["update_id"] for item in selected],
        "selected_order": list(selected_keys),
        "weights": [weights[item["update_id"]] for item in selected],
        "merged_float32_le_hex": tensor_hex(merged),
        "theta_float32_le_hex": tensor_hex(theta_v1),
        "outer_step": int(state_v1["step"].item()),
        "outer_momentum_float32_le_hex": tensor_hex(state_v1["momentum"]),
        "version": 1,
        "predecessor_version": 0,
    }
    assert actual == expected


def test_unified_replace_cases_are_generated_by_segment_accounting() -> None:
    cases = _load("unified_v4_trace.json")["cases"]
    accumulator = TrainingSegmentAccumulator(base_global_version=0, interval_start_step=0)
    accumulator.record_step(local_step_end=1, tokens=10, examples=2, loss=4.0, grad_norm=2.0)
    accumulator.replace_base(new_base_global_version=1)
    accumulator.record_step(local_step_end=2, tokens=6, examples=1, loss=1.5, grad_norm=1.0)
    result = accumulator.finalize_cycle()
    expected = cases["prepublication_replace"]
    assert {
        "processed_tokens": result.processed_tokens,
        "local_discarded_tokens": result.local_discarded_tokens,
        "effective_tokens": result.effective_tokens,
        "effective_examples": result.segment.examples,
        "mean_loss": result.segment.mean_loss,
        "mean_grad_norm": result.segment.mean_grad_norm,
        "proposal_expected": result.proposal_expected,
    } == {key: value for key, value in expected.items() if key != "attribution"}

    cycle_end = TrainingSegmentAccumulator(base_global_version=0, interval_start_step=0)
    cycle_end.record_step(local_step_end=1, tokens=8, examples=1, loss=2.0)
    cycle_end.replace_base(new_base_global_version=1)
    result = cycle_end.finalize_cycle()
    expected = cases["cycle_end_replace"]
    assert {
        "processed_tokens": result.processed_tokens,
        "local_discarded_tokens": result.local_discarded_tokens,
        "effective_tokens": result.effective_tokens,
        "effective_examples": result.segment.examples,
        "mean_loss": result.segment.mean_loss,
        "proposal_expected": result.proposal_expected,
    } == {key: value for key, value in expected.items() if key != "attribution"}


def test_unified_truncated_selection_lineage_is_generated_and_balanced() -> None:
    expected = _load("unified_v4_trace.json")["cases"]["truncated_fair_selection"]
    keys = tuple(expected["committed_service_counts"])
    selector = PersistentFairSelector()
    lineage = []
    counts = {key: 0 for key in keys}
    waits = {key: 0 for key in keys}
    maximum_wait = 0
    for version in range(1, 9):
        selected = selector.select(keys, quorum_max=expected["quorum_max"])
        lineage.append(list(selected))
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
    assert lineage == expected["selected_lineage"]
    assert [sorted(selected) for selected in lineage] == expected["reduction_lineage"]
    assert counts == expected["committed_service_counts"]
    assert maximum_wait == expected["maximum_wait_rounds"]
    assert jain == expected["jain_index"]
