import math

import pytest
import torch

from fs_diloco.core.config import Config, resolve_config
from fs_diloco.modeling.training import (
    build_inner_optimizer_and_scheduler,
    inner_lr_multiplier,
)


def _cosine_config(*, completion_mode: str = "local_or_global") -> Config:
    config = Config()
    config.training.completion_mode = completion_mode
    config.inner_optimizer.scheduler = "cosine"
    config.inner_optimizer.lr = 1.0
    config.inner_optimizer.warmup_steps = 2
    config.inner_optimizer.scheduler_total_steps = 6
    config.inner_optimizer.min_lr_ratio = 0.1
    return config


@pytest.mark.parametrize(
    ("completed_steps", "expected"),
    [
        (0, 0.5),
        (1, 1.0),
        (2, 1.0),
        (4, 0.5),
        (6, 0.1),
        (7, 0.1),
    ],
)
def test_inner_lr_multiplier_matches_golden_curve(completed_steps, expected):
    assert inner_lr_multiplier(_cosine_config(), completed_steps) == pytest.approx(expected)


def _advance_one_step(optimizer, scheduler):
    used_lr = float(optimizer.param_groups[0]["lr"])
    optimizer.step()
    scheduler.step()
    return used_lr


def test_rebuilding_inner_state_restores_cumulative_scheduler_phase():
    reference_model = torch.nn.Linear(1, 1)
    rebuilt_model = torch.nn.Linear(1, 1)
    config = _cosine_config()
    reference_optimizer, reference_scheduler = build_inner_optimizer_and_scheduler(
        reference_model, config, completed_local_steps=0
    )
    rebuilt_optimizer, rebuilt_scheduler = build_inner_optimizer_and_scheduler(
        rebuilt_model, config, completed_local_steps=0
    )

    reference_lrs = []
    rebuilt_lrs = []
    for completed_steps in range(8):
        reference_lrs.append(_advance_one_step(reference_optimizer, reference_scheduler))
        rebuilt_lrs.append(_advance_one_step(rebuilt_optimizer, rebuilt_scheduler))
        if completed_steps + 1 in {1, 2, 5}:
            rebuilt_optimizer, rebuilt_scheduler = build_inner_optimizer_and_scheduler(
                rebuilt_model,
                config,
                completed_local_steps=completed_steps + 1,
            )

    assert rebuilt_lrs == pytest.approx(reference_lrs)
    assert reference_lrs == pytest.approx(
        [
            0.5,
            1.0,
            1.0,
            0.5 * (1.0 + math.cos(math.pi * 0.25)),
            0.5,
            max(0.1, 0.5 * (1.0 + math.cos(math.pi * 0.75))),
            0.1,
            0.1,
        ]
    )


def test_completion_mode_does_not_change_scheduler_curve():
    local = _cosine_config(completion_mode="local_or_global")
    global_only = _cosine_config(completion_mode="global_only")
    assert [inner_lr_multiplier(local, step) for step in range(9)] == [
        inner_lr_multiplier(global_only, step) for step in range(9)
    ]


def test_cosine_scheduler_requires_independent_horizon(tmp_path):
    path = tmp_path / "missing_horizon.yaml"
    path.write_text(
        "inner_optimizer:\n  scheduler: cosine\n  warmup_steps: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="scheduler_total_steps"):
        resolve_config(path)


@pytest.mark.parametrize(
    ("scheduler", "warmup", "total", "min_ratio", "message"),
    [
        ("wsd", 0, None, 0.1, "unsupported inner_optimizer.scheduler"),
        ("cosine", -1, 4, 0.1, "warmup_steps"),
        ("cosine", 4, 4, 0.1, "scheduler_total_steps"),
        ("cosine", 1, 4, 0.0, "min_lr_ratio"),
        ("cosine", 1, 4, 1.1, "min_lr_ratio"),
    ],
)
def test_inner_scheduler_config_rejects_invalid_values(
    tmp_path, scheduler, warmup, total, min_ratio, message
):
    total_line = "" if total is None else f"  scheduler_total_steps: {total}\n"
    path = tmp_path / "invalid_scheduler.yaml"
    path.write_text(
        "inner_optimizer:\n"
        f"  scheduler: {scheduler}\n"
        f"  warmup_steps: {warmup}\n"
        f"  min_lr_ratio: {min_ratio}\n"
        f"{total_line}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        resolve_config(path)
