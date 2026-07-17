import pytest
import torch

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.adoption import (
    AdoptionContext,
    PredictGlobalAdoptionStrategy,
    PublishResult,
    RebaseGlobalAdoptionStrategy,
    ReplaceGlobalAdoptionStrategy,
    make_global_adoption_strategy,
)
from fs_diloco.storage.paths import RunPaths


class RecordingLogger:
    def __init__(self):
        self.events = []

    def event(self, event_type, **payload):
        self.events.append((event_type, payload))


def _context(
    config,
    *,
    logger=None,
    read_latest=None,
    wait_latest=None,
    prepare_prediction=None,
    reference=None,
):
    logger = logger or RecordingLogger()
    reference = reference if reference is not None else torch.arange(4, dtype=torch.float32)
    return AdoptionContext(
        model=object(),
        paths=RunPaths("unused"),
        param_index={"total_numel": 4},
        device=torch.device("cpu"),
        config=config,
        logger=logger,
        last_loaded_global_version=1,
        last_loaded_latest={"version": 1},
        tokens_since_global_load=64,
        read_latest_if_newer_fn=read_latest or (lambda *_args: None),
        wait_for_latest_if_newer_fn=wait_latest
        or (lambda *_args, **_kwargs: (None, 0.0)),
        adopt_global_fn=lambda **kwargs: int(kwargs["latest"]["version"]),
        rebase_local_delta_fn=lambda **kwargs: (
            int(kwargs["latest"]["version"]),
            1.25,
            {"reconcile_compute_device": "cpu"},
        ),
        snapshot_model_fn=lambda **_kwargs: (reference, {"reference_compute_device": "cpu"}),
        prepare_prediction_fn=prepare_prediction
        or (lambda **_kwargs: (reference, {"prediction_compute_device": "cpu"}, None, None)),
    )


@pytest.mark.parametrize(
    ("config_path", "expected_type"),
    [
        ("configs/fs_diloco_tiny_local.yaml", ReplaceGlobalAdoptionStrategy),
        ("configs/fs_diloco_tiny_rebase_local.yaml", RebaseGlobalAdoptionStrategy),
        ("configs/fs_diloco_tiny_predict_local.yaml", PredictGlobalAdoptionStrategy),
    ],
)
def test_strategy_factory_dispatches(config_path, expected_type):
    strategy = make_global_adoption_strategy(resolve_config(config_path))

    assert isinstance(strategy, expected_type)


def test_strategy_factory_rejects_unknown_name():
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.learner.global_adoption_strategy = "unknown"

    with pytest.raises(ValueError, match="unsupported learner.global_adoption_strategy"):
        make_global_adoption_strategy(config)


def test_replace_returns_reset_adoption_outcome():
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.learner.poll_latest_during_inner_steps = True
    strategy = ReplaceGlobalAdoptionStrategy()

    action = strategy.on_newer_latest(_context(config), {"version": 2})

    assert strategy.wants_inner_poll(config)
    assert action.adoption is not None
    assert action.adoption.version == 2
    assert action.adoption.tokens_since_global_load == 0
    assert action.adoption.preserve_inner_state is False


def test_rebase_strategy_owns_anchor_tokens_and_clears_after_adoption():
    config = resolve_config("configs/fs_diloco_tiny_rebase_local.yaml")
    logger = RecordingLogger()
    ctx = _context(config, logger=logger)
    strategy = RebaseGlobalAdoptionStrategy()

    start_action = strategy.on_after_publish(
        ctx,
        PublishResult(update_id="u1", base_global_version=1),
    )
    strategy.on_local_tokens(96)
    action = strategy.on_newer_latest(ctx, {"version": 2})

    assert start_action.adoption is None
    assert action.adoption is not None
    assert action.adoption.preserve_inner_state is True
    assert action.adoption.tokens_since_global_load == 96
    assert not strategy.wants_inner_poll(config)
    assert [event for event, _payload in logger.events] == [
        "latest_polled",
        "local_rebase_anchor_saved",
        "global_rebased",
    ]
    assert logger.events[-1][1]["anchor_update_id"] == "u1"
    assert logger.events[-1][1]["carried_delta_tokens"] == 96


def test_prediction_strategy_starts_reconciles_and_abandons_on_stop():
    config = resolve_config("configs/fs_diloco_tiny_predict_local.yaml")
    logger = RecordingLogger()
    ctx = _context(config, logger=logger)
    strategy = PredictGlobalAdoptionStrategy()

    start_action = strategy.on_after_publish(
        ctx,
        PublishResult(update_id="u1", base_global_version=1),
    )
    strategy.on_local_tokens(32)
    reconcile_action = strategy.on_newer_latest(ctx, {"version": 2})

    assert start_action.reset_optimizer_reason == "global_prediction_started"
    assert reconcile_action.adoption is not None
    assert reconcile_action.adoption.preserve_inner_state is True
    assert reconcile_action.adoption.tokens_since_global_load == 32
    assert not strategy.wants_inner_poll(config)

    strategy.on_after_publish(ctx, PublishResult(update_id="u2", base_global_version=1))
    strategy.on_local_tokens(16)
    assert strategy.on_stop(ctx) is True
    assert logger.events[-1] == (
        "global_prediction_abandoned_on_stop",
        {
            "prediction_base_version": 1,
            "prediction_update_id": "u2",
            "carried_delta_tokens": 16,
        },
    )


def test_prediction_strategy_timeout_keeps_state_for_diagnostics():
    config = resolve_config("configs/fs_diloco_tiny_predict_local.yaml")
    logger = RecordingLogger()
    ctx = _context(config, logger=logger)
    strategy = PredictGlobalAdoptionStrategy()
    strategy.on_after_publish(ctx, PublishResult(update_id="u1", base_global_version=1))

    with pytest.raises(TimeoutError, match="timed out waiting to reconcile"):
        strategy.on_cycle_end(ctx)

    assert strategy.wants_inner_poll(config)
    assert logger.events[-1][0] == "global_prediction_reconcile_wait_started"
