"""Tests for standalone DDP and periodic-averaging protocol primitives."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel

from torch_ddp_baselines.data import Batch
from torch_ddp_baselines.protocol import (
    average_trainable_parameters,
    should_average,
    train_optimizer_step,
)


class RegressionModel(torch.nn.Module):
    """Expose one scalar linear weight for exact distributed-update checks."""

    def __init__(self) -> None:
        """Initialize the scalar weight at the shared reference value."""

        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor([[1.0]]))  # Sole trainable weight.

    def forward(self, input_ids: torch.Tensor, labels: torch.Tensor) -> Any:
        """Return mean squared error in the causal-LM loss shape."""

        prediction = input_ids @ self.weight
        return SimpleNamespace(loss=torch.mean((prediction - labels) ** 2))


class TrackingNoSync(torch.nn.Module):
    """Record which microbatch forwards execute under ``no_sync``."""

    def __init__(self, module: torch.nn.Module) -> None:
        """Wrap a model with observable synchronization context state."""

        super().__init__()
        self.module = module  # Model that performs the test forward pass.
        self.no_sync_entries = 0  # Number of entered no-sync contexts.
        self.sync_disabled = False  # Whether the current forward suppresses sync.
        self.forward_sync_states: list[bool] = []  # State observed by each forward.

    @contextlib.contextmanager
    def no_sync(self) -> Iterator[None]:
        """Emulate DDP's reducer-suppression context manager."""

        self.no_sync_entries += 1
        self.sync_disabled = True
        try:
            yield
        finally:
            self.sync_disabled = False

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        """Record the sync state before delegating to the wrapped model."""

        self.forward_sync_states.append(self.sync_disabled)
        return self.module(*args, **kwargs)


def _batch(x_value: float, y_value: float) -> Batch:
    """Build one scalar regression microbatch."""

    return Batch(
        input_ids=torch.tensor([[x_value]], dtype=torch.float32),
        labels=torch.tensor([[y_value]], dtype=torch.float32),
        num_tokens=1,
        num_examples=1,
    )


def _scheduler(optimizer: torch.optim.Optimizer) -> torch.optim.lr_scheduler.LambdaLR:
    """Build a neutral scheduler required by the production step API."""

    return torch.optim.lr_scheduler.LambdaLR(optimizer, lambda _step: 1.0)


def test_ddp_accumulation_syncs_only_the_final_microbatch() -> None:
    """Accumulation must trigger exactly one reducer synchronization per optimizer step."""

    model = TrackingNoSync(RegressionModel())
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)

    train_optimizer_step(
        model,
        iter([_batch(1, 2), _batch(2, 4), _batch(3, 6)]),
        optimizer,
        _scheduler(optimizer),
        device=torch.device("cpu"),
        accumulation_steps=3,
        grad_clip=1.0,
        ddp_gradient_sync=True,
    )

    assert model.no_sync_entries == 2
    assert model.forward_sync_states == [True, True, False]


def test_periodic_average_is_bf16_mean_and_preserves_inner_state() -> None:
    """Parameter averaging must leave local AdamW moments and scheduler state untouched."""

    model = torch.nn.Linear(2, 1, bias=False)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
    model(torch.ones(1, 2)).sum().backward()
    optimizer.step()
    scheduler.step()
    parameter = next(model.parameters())
    moment_before = optimizer.state[parameter]["exp_avg"].detach().clone()
    scheduler_before = scheduler.state_dict()
    with torch.no_grad():
        parameter.copy_(torch.tensor([[1.0, 2.0]]))

    def add_peer(flattened: torch.Tensor) -> None:
        """Emulate a second rank's contribution to the sum collective."""

        assert flattened.dtype == torch.bfloat16
        flattened.add_(torch.tensor([3.0, 4.0], dtype=torch.bfloat16))

    _elapsed, numel = average_trainable_parameters(
        model,
        world_size=2,
        all_reduce_fn=add_peer,
    )

    assert numel == 2
    assert torch.equal(parameter, torch.tensor([[2.0, 3.0]]))
    assert torch.equal(optimizer.state[parameter]["exp_avg"], moment_before)
    assert scheduler.state_dict() == scheduler_before


def test_5000_step_schedule_averages_on_all_twenty_five_boundaries() -> None:
    """The baseline must exercise 25 periodic boundaries matching Full Protocol."""

    observed = [step for step in range(1, 5001) if should_average(step, 200)]
    assert observed == list(range(200, 5001, 200))


def _ddp_worker(rank: int, init_file: str, output_dir: str) -> None:
    """Run one real Gloo DDP update and persist the resulting scalar weight."""

    dist.init_process_group(
        "gloo",
        init_method=f"file://{init_file}",
        rank=rank,
        world_size=2,
    )
    model = DistributedDataParallel(RegressionModel())
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    rank_batches = [_batch(1, 2), _batch(2, 4)] if rank == 0 else [_batch(3, 6), _batch(4, 8)]
    train_optimizer_step(
        model,
        iter(rank_batches),
        optimizer,
        _scheduler(optimizer),
        device=torch.device("cpu"),
        accumulation_steps=2,
        grad_clip=1.0,
        ddp_gradient_sync=True,
    )
    Path(output_dir, f"rank-{rank}.json").write_text(
        json.dumps({"weight": float(model.module.weight.item())}),
        encoding="utf-8",
    )
    dist.destroy_process_group()


def test_two_rank_ddp_matches_the_combined_clipped_gradient(tmp_path: Path) -> None:
    """Real two-process DDP must equal the single-process combined-batch reference."""

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    mp.spawn(
        _ddp_worker,
        args=(str(tmp_path / "store"), str(output_dir)),
        nprocs=2,
        join=True,
    )
    reference = RegressionModel()
    optimizer = torch.optim.SGD(reference.parameters(), lr=0.1)
    inputs = torch.tensor([[1.0], [2.0], [3.0], [4.0]])
    labels = 2.0 * inputs
    reference(input_ids=inputs, labels=labels).loss.backward()
    torch.nn.utils.clip_grad_norm_(reference.parameters(), 1.0)
    optimizer.step()
    expected = float(reference.weight.item())
    observed = [
        json.loads((output_dir / f"rank-{rank}.json").read_text(encoding="utf-8"))["weight"]
        for rank in range(2)
    ]
    assert observed == pytest.approx([expected, expected])
