import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel

from fs_diloco.baselines.protocol import (
    average_trainable_parameters,
    should_average,
    train_optimizer_step,
)
from fs_diloco.modeling.hf_data import Batch


class RegressionModel(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor([[1.0]]))

    def forward(self, input_ids, labels):
        prediction = input_ids @ self.weight
        return SimpleNamespace(loss=torch.mean((prediction - labels) ** 2))


class TrackingNoSync(torch.nn.Module):
    def __init__(self, module: torch.nn.Module) -> None:
        super().__init__()
        self.module = module
        self.no_sync_entries = 0
        self.sync_disabled = False
        self.forward_sync_states: list[bool] = []

    @contextlib.contextmanager
    def no_sync(self):
        self.no_sync_entries += 1
        self.sync_disabled = True
        try:
            yield
        finally:
            self.sync_disabled = False

    def forward(self, *args, **kwargs):
        self.forward_sync_states.append(self.sync_disabled)
        return self.module(*args, **kwargs)


def _config(accumulation_steps: int = 1):
    return SimpleNamespace(
        training=SimpleNamespace(
            gradient_accumulation_steps=accumulation_steps,
            precision="fp32",
            grad_clip=1.0,
        )
    )


def _batch(x: float, y: float) -> Batch:
    return Batch(
        input_ids=torch.tensor([[x]], dtype=torch.float32),
        labels=torch.tensor([[y]], dtype=torch.float32),
        num_tokens=1,
        num_examples=1,
    )


def test_ddp_accumulation_uses_no_sync_only_before_final_microbatch():
    model = TrackingNoSync(RegressionModel())
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    batches = iter([_batch(1, 2), _batch(2, 4), _batch(3, 6)])

    train_optimizer_step(
        model,
        batches,
        optimizer,
        None,
        device=torch.device("cpu"),
        config=_config(3),
        ddp_gradient_sync=True,
    )

    assert model.no_sync_entries == 2
    assert model.forward_sync_states == [True, True, False]


class NonFiniteModel(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.weight = torch.nn.Parameter(torch.ones(()))

    def forward(self, input_ids, labels):
        return SimpleNamespace(loss=self.weight * torch.tensor(float("nan")))


def test_nonfinite_loss_fails_before_optimizer_step():
    model = NonFiniteModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    before = model.weight.detach().clone()

    with pytest.raises(FloatingPointError, match="non-finite loss"):
        train_optimizer_step(
            model,
            iter([_batch(1, 2)]),
            optimizer,
            None,
            device=torch.device("cpu"),
            config=_config(),
            ddp_gradient_sync=False,
        )

    assert torch.equal(model.weight, before)


def test_periodic_average_is_bf16_arithmetic_mean_and_retains_inner_state():
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

    def add_peer(flat: torch.Tensor) -> None:
        assert flat.dtype == torch.bfloat16
        flat.add_(torch.tensor([3.0, 4.0], dtype=torch.bfloat16))

    _elapsed, numel = average_trainable_parameters(
        model,
        world_size=2,
        all_reduce_fn=add_peer,
    )

    assert numel == 2
    assert torch.equal(parameter, torch.tensor([[2.0, 3.0]]))
    assert torch.equal(optimizer.state[parameter]["exp_avg"], moment_before)
    assert scheduler.state_dict() == scheduler_before


def test_periodic_schedule_is_exactly_100_step_boundaries():
    observed = [step for step in range(1, 5001) if should_average(step, 100)]
    assert observed == list(range(100, 5001, 100))
    assert len(observed) == 50


def _ddp_equivalence_worker(rank: int, init_file: str, output_dir: str) -> None:
    dist.init_process_group(
        "gloo",
        init_method=f"file://{init_file}",
        rank=rank,
        world_size=2,
    )
    model = DistributedDataParallel(RegressionModel())
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    rank_batches = (
        [_batch(1, 2), _batch(2, 4)]
        if rank == 0
        else [_batch(3, 6), _batch(4, 8)]
    )
    train_optimizer_step(
        model,
        iter(rank_batches),
        optimizer,
        None,
        device=torch.device("cpu"),
        config=_config(2),
        ddp_gradient_sync=True,
    )
    Path(output_dir, f"ddp-{rank}.json").write_text(
        json.dumps({"weight": float(model.module.weight.item())}),
        encoding="utf-8",
    )
    dist.destroy_process_group()


def test_two_rank_ddp_update_matches_combined_batch_gradient(tmp_path):
    init_file = tmp_path / "ddp-store"
    output_dir = tmp_path / "ddp-output"
    output_dir.mkdir()
    mp.spawn(
        _ddp_equivalence_worker,
        args=(str(init_file), str(output_dir)),
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
        json.loads((output_dir / f"ddp-{rank}.json").read_text())["weight"]
        for rank in range(2)
    ]
    assert observed == pytest.approx([expected, expected])


def _periodic_worker(rank: int, init_file: str, output_dir: str) -> None:
    dist.init_process_group(
        "gloo",
        init_method=f"file://{init_file}",
        rank=rank,
        world_size=2,
    )
    model = torch.nn.Linear(1, 1, bias=False)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
    model(torch.ones(1, 1)).sum().backward()
    optimizer.step()
    scheduler.step()
    parameter = next(model.parameters())
    moment_before = optimizer.state[parameter]["exp_avg"].detach().clone()
    scheduler_before = scheduler.state_dict()
    with torch.no_grad():
        parameter.fill_(1.0 + 2.0 * rank)
    average_trainable_parameters(model, world_size=2)
    payload = {
        "weight": float(parameter.item()),
        "moment_unchanged": bool(
            torch.equal(moment_before, optimizer.state[parameter]["exp_avg"])
        ),
        "scheduler_unchanged": scheduler.state_dict() == scheduler_before,
    }
    Path(output_dir, f"periodic-{rank}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    dist.destroy_process_group()


def test_two_rank_periodic_average_converges_and_preserves_local_state(tmp_path):
    init_file = tmp_path / "periodic-store"
    output_dir = tmp_path / "periodic-output"
    output_dir.mkdir()
    mp.spawn(
        _periodic_worker,
        args=(str(init_file), str(output_dir)),
        nprocs=2,
        join=True,
    )
    observed = [
        json.loads((output_dir / f"periodic-{rank}.json").read_text())
        for rank in range(2)
    ]
    assert [item["weight"] for item in observed] == pytest.approx([2.0, 2.0])
    assert all(item["moment_unchanged"] for item in observed)
    assert all(item["scheduler_unchanged"] for item in observed)
