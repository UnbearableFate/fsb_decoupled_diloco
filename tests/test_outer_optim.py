import torch

from fs_diloco.core.config import OuterOptimizerSection
from fs_diloco.modeling.outer_optim import init_outer_state, outer_optimizer_step


def test_sgd_momentum_step():
    theta = torch.tensor([1.0, 2.0])
    grad = torch.tensor([0.5, -0.5])
    cfg = OuterOptimizerSection(name="momentum", lr=0.1, momentum=0.9)
    state = init_outer_state(theta, cfg)
    theta2, state2 = outer_optimizer_step(theta, grad, state, cfg)
    assert torch.allclose(theta2, torch.tensor([0.95, 2.05]))
    theta3, _ = outer_optimizer_step(theta2, grad, state2, cfg)
    assert torch.allclose(theta3, torch.tensor([0.855, 2.145]), atol=1e-6)


def test_nesterov_step_matches_plan_formula():
    theta = torch.tensor([1.0])
    grad = torch.tensor([0.5])
    cfg = OuterOptimizerSection(name="nesterov", lr=0.1, momentum=0.9)
    state = init_outer_state(theta, cfg)
    theta2, _ = outer_optimizer_step(theta, grad, state, cfg)
    assert torch.allclose(theta2, torch.tensor([0.905]))


def test_adamw_step_known_vector():
    theta = torch.tensor([1.0])
    grad = torch.tensor([0.25])
    cfg = OuterOptimizerSection(name="adamw", lr=0.001, betas=(0.9, 0.999), weight_decay=0.0)
    state = init_outer_state(theta, cfg)
    theta2, state2 = outer_optimizer_step(theta, grad, state, cfg)
    assert torch.allclose(theta2, torch.tensor([0.999]), atol=1e-7)
    assert int(state2["step"].item()) == 1


def test_nesterov_step_preserves_bfloat16_compute_dtype_on_cpu():
    theta = torch.tensor([1.0, 2.0], dtype=torch.bfloat16)
    grad = torch.tensor([0.5, -0.5], dtype=torch.bfloat16)
    cfg = OuterOptimizerSection(name="nesterov", lr=0.1, momentum=0.9)
    state = init_outer_state(theta, cfg)

    updated, updated_state = outer_optimizer_step(theta, grad, state, cfg)

    assert updated.dtype == torch.bfloat16
    assert updated_state["momentum"].dtype == torch.bfloat16
    assert updated_state["step"].dtype == torch.int64
