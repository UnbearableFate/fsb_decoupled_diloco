"""Explicit flat-vector outer optimizers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import torch


@dataclass
class OuterOptimizerConfig:
    name: str = "nesterov"
    lr: float = 0.7
    momentum: float = 0.9
    weight_decay: float = 0.0
    betas: tuple[float, float] = (0.9, 0.999)
    eps: float = 1.0e-8


def config_from_any(config: Any) -> OuterOptimizerConfig:
    if isinstance(config, OuterOptimizerConfig):
        return config
    betas = getattr(config, "betas", (0.9, 0.999))
    if isinstance(betas, list):
        betas = tuple(betas)
    return OuterOptimizerConfig(
        name=getattr(config, "name", "nesterov"),
        lr=float(getattr(config, "lr", 0.7)),
        momentum=float(getattr(config, "momentum", 0.9)),
        weight_decay=float(getattr(config, "weight_decay", 0.0)),
        betas=betas,
        eps=float(getattr(config, "eps", 1.0e-8)),
    )


def init_outer_state(theta: torch.Tensor, config: Any) -> dict[str, torch.Tensor]:
    cfg = config_from_any(config)
    state: dict[str, torch.Tensor] = {"step": torch.tensor(0, dtype=torch.int64)}
    name = cfg.name.lower()
    if name in {"sgd", "momentum", "nesterov"}:
        state["momentum"] = torch.zeros_like(theta)
    elif name == "adamw":
        state["exp_avg"] = torch.zeros_like(theta)
        state["exp_avg_sq"] = torch.zeros_like(theta)
    else:
        raise ValueError(f"unsupported outer optimizer: {cfg.name}")
    return state


def outer_optimizer_step(
    theta: torch.Tensor,
    grad: torch.Tensor,
    state: dict[str, torch.Tensor],
    config: Any,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    cfg = config_from_any(config)
    name = cfg.name.lower()
    theta = theta.detach().clone()
    grad = grad.detach().to(theta.dtype).clone()
    state_step = state.get("step")
    step = int(state_step.item()) + 1 if state_step is not None else 1
    step_tensor = torch.tensor(step, dtype=torch.int64, device=theta.device)

    if name in {"sgd", "momentum", "nesterov"}:
        if cfg.weight_decay:
            grad = grad.add(theta, alpha=cfg.weight_decay)
        momentum_buffer = state.get("momentum")
        if momentum_buffer is None:
            momentum_buffer = torch.zeros_like(theta)
        if name == "sgd" or cfg.momentum == 0.0:
            update = grad
            momentum_buffer = torch.zeros_like(theta)
        else:
            momentum_buffer = momentum_buffer.mul(cfg.momentum).add(grad)
            if name == "nesterov":
                update = grad.add(momentum_buffer, alpha=cfg.momentum)
            else:
                update = momentum_buffer
        theta = theta.add(update, alpha=-cfg.lr)
        new_state = {"step": step_tensor, "momentum": momentum_buffer}
        return theta, new_state

    if name == "adamw":
        beta1, beta2 = cfg.betas
        exp_avg = state.get("exp_avg")
        exp_avg_sq = state.get("exp_avg_sq")
        if exp_avg is None:
            exp_avg = torch.zeros_like(theta)
        if exp_avg_sq is None:
            exp_avg_sq = torch.zeros_like(theta)
        if cfg.weight_decay:
            theta = theta.mul(1.0 - cfg.lr * cfg.weight_decay)
        exp_avg = exp_avg.mul(beta1).add(grad, alpha=1.0 - beta1)
        exp_avg_sq = exp_avg_sq.mul(beta2).addcmul(grad, grad, value=1.0 - beta2)
        bias_correction1 = 1.0 - beta1**step
        bias_correction2 = 1.0 - beta2**step
        m_hat = exp_avg / bias_correction1
        v_hat = exp_avg_sq / bias_correction2
        theta = theta.addcdiv(m_hat, v_hat.sqrt().add(cfg.eps), value=-cfg.lr)
        new_state = {
            "step": step_tensor,
            "exp_avg": exp_avg,
            "exp_avg_sq": exp_avg_sq,
        }
        return theta, new_state

    raise ValueError(f"unsupported outer optimizer: {cfg.name}")


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
        target = value.detach().to(device=device)
        if dtype is not None and target.is_floating_point():
            target = target.to(dtype=dtype)
        state[key] = target
    if "step" not in state:
        state["step"] = torch.tensor(0, dtype=torch.int64, device=device)
    return theta, state
