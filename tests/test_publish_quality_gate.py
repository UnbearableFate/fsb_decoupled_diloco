import pytest

from fs_diloco.tools.publish_quality_gate import (
    evaluate_publish_quality_gate,
    roundtrip_trend,
)


def test_publish_quality_gate_passes_within_frozen_epsilon():
    result = evaluate_publish_quality_gate(
        fp32_losses={1337: 1.00, 2027: 1.02, 4049: 0.98},
        bf16_losses={1337: 1.005, 2027: 1.035, 4049: 0.99},
        bf16_trends={seed: roundtrip_trend([0.001] * 50) for seed in (1337, 2027, 4049)},
    )

    assert result["status"] == "PASS"
    assert result["epsilon"] == pytest.approx(0.02)
    assert result["mean_paired_degradation"] == pytest.approx(0.01)
    assert result["worst_seed_degradation"] == pytest.approx(0.015)


def test_publish_quality_gate_is_three_state_and_fails_loss_or_trend():
    insufficient = evaluate_publish_quality_gate(
        fp32_losses={1: 1.0, 2: 1.0},
        bf16_losses={1: 1.0, 2: 1.0},
        bf16_trends={1: roundtrip_trend([0.001] * 4), 2: roundtrip_trend([0.001] * 4)},
    )
    assert insufficient["status"] == "NEEDS_MORE_SEEDS"

    increasing = roundtrip_trend([0.001 * index for index in range(1, 51)])
    failed = evaluate_publish_quality_gate(
        fp32_losses={1: 1.0, 2: 1.0, 3: 1.0},
        bf16_losses={1: 1.03, 2: 1.03, 3: 1.03},
        bf16_trends={1: increasing, 2: increasing, 3: increasing},
    )
    assert failed["status"] == "FAIL"
    assert failed["loss_gate_pass"] is False
    assert failed["trend_gate_pass"] is False


def test_roundtrip_trend_rejects_only_confident_growth_and_large_half_ratio():
    bounded = roundtrip_trend([0.001, 0.0011, 0.0009, 0.001, 0.0011, 0.0009])
    assert bounded["bounded"] is True
    assert bounded["slope_ci95_low"] <= 0.0 <= bounded["slope_ci95_high"]
    assert bounded["second_half_to_first_half_ratio"] <= 1.25

    decreasing = roundtrip_trend(
        [0.001 / float(index) for index in range(1, 21)]
    )
    assert decreasing["slope_ci95_high"] < 0.0
    assert decreasing["bounded"] is True

    increasing = roundtrip_trend([float(index) for index in range(1, 21)])
    assert increasing["bounded"] is False
    assert increasing["slope_ci95_low"] > 0.0
