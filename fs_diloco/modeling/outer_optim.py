"""Explicit flat-vector outer optimizers."""

from __future__ import annotations

import torch

from ..core.config import OuterOptimizerSection


def init_outer_state(
    theta: torch.Tensor, config: OuterOptimizerSection
) -> dict[str, torch.Tensor]:
    state: dict[str, torch.Tensor] = {"step": torch.tensor(0, dtype=torch.int64)}
    name = config.name
    if name in {"sgd", "momentum", "nesterov"}:
        state["momentum"] = torch.zeros_like(theta)
    elif name == "adamw":
        state["exp_avg"] = torch.zeros_like(theta)
        state["exp_avg_sq"] = torch.zeros_like(theta)
    else:
        raise ValueError(f"unsupported outer optimizer: {config.name}")
    return state


def outer_optimizer_step(
    theta: torch.Tensor,
    grad: torch.Tensor,
    state: dict[str, torch.Tensor],
    config: OuterOptimizerSection,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    name = config.name
    theta = theta.detach().clone()
    grad = grad.detach().to(theta.dtype).clone()
    state_step = state.get("step")
    step = int(state_step.item()) + 1 if state_step is not None else 1
    step_tensor = torch.tensor(step, dtype=torch.int64, device=theta.device)

    if name in {"sgd", "momentum", "nesterov"}:
        if config.weight_decay:
            grad = grad.add(theta, alpha=config.weight_decay)
        momentum_buffer = state.get("momentum")
        if momentum_buffer is None:
            momentum_buffer = torch.zeros_like(theta)
        if name == "sgd" or config.momentum == 0.0:
            update = grad
            momentum_buffer = torch.zeros_like(theta)
        else:
            momentum_buffer = momentum_buffer.mul(config.momentum).add(grad)
            if name == "nesterov":
                update = grad.add(momentum_buffer, alpha=config.momentum)
            else:
                update = momentum_buffer
        theta = theta.add(update, alpha=-config.lr)
        new_state = {"step": step_tensor, "momentum": momentum_buffer}
        return theta, new_state

    if name == "adamw":
        beta1, beta2 = config.betas
        exp_avg = state.get("exp_avg")
        exp_avg_sq = state.get("exp_avg_sq")
        if exp_avg is None:
            exp_avg = torch.zeros_like(theta)
        if exp_avg_sq is None:
            exp_avg_sq = torch.zeros_like(theta)
        if config.weight_decay:
            theta = theta.mul(1.0 - config.lr * config.weight_decay)
        exp_avg = exp_avg.mul(beta1).add(grad, alpha=1.0 - beta1)
        exp_avg_sq = exp_avg_sq.mul(beta2).addcmul(grad, grad, value=1.0 - beta2)
        bias_correction1 = 1.0 - beta1**step
        bias_correction2 = 1.0 - beta2**step
        m_hat = exp_avg / bias_correction1
        v_hat = exp_avg_sq / bias_correction2
        theta = theta.addcdiv(m_hat, v_hat.sqrt().add(config.eps), value=-config.lr)
        new_state = {
            "step": step_tensor,
            "exp_avg": exp_avg,
            "exp_avg_sq": exp_avg_sq,
        }
        return theta, new_state

    raise ValueError(f"unsupported outer optimizer: {config.name}")


def state_to_tensors(
    theta: torch.Tensor,
    state: dict[str, torch.Tensor],
    *,
    dtype: torch.dtype | None = None,
) -> dict[str, torch.Tensor]:
    def prepare(value: torch.Tensor) -> torch.Tensor:
        result = value.detach().cpu()
        if dtype is not None and result.is_floating_point():
            result = result.to(dtype=dtype)
        return result

    tensors = {"theta": prepare(theta)}
    for key, value in state.items():
        tensors[key] = prepare(value)
    return tensors


def state_from_tensors(
    tensors: dict[str, torch.Tensor],
    *,
    device: torch.device | str = "cpu",
    dtype: torch.dtype | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    target_dtype = dtype or torch.float32
    theta = tensors["theta"].detach().to(device=device, dtype=target_dtype)
    state = {}
    for key, value in tensors.items():
        if key == "theta":
            continue
        target = value.detach().to(
            device=device,
            dtype=dtype if dtype is not None and value.is_floating_point() else value.dtype,
        )
        state[key] = target
    if "step" not in state:
        state["step"] = torch.tensor(0, dtype=torch.int64, device=device)
    return theta, state
