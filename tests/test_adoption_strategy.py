"""Verify global-adoption strategy state transitions and waiting boundaries."""

from pathlib import Path

import pytest
import torch

from fs_diloco.core.config import Config
from fs_diloco.runtime.adoption import (
    AdoptionContext,
    PredictGlobalAdoptionStrategy,
    PublishResult,
    RebaseGlobalAdoptionStrategy,
    ReplaceGlobalAdoptionStrategy,
    make_global_adoption_strategy,
)
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.atomic_io import atomic_write_json


class RecordingLogger:
    def __init__(self):
        self.events = []

    def event(self, event_type, **payload):
        self.events.append((event_type, payload))


def _config(strategy: str = "replace") -> Config:
    config = Config()
    config.model.name_or_path = "synthetic-tiny"
    config.data.dataset_name = "synthetic"
    config.learner.global_adoption_strategy = strategy
    if strategy != "replace":
        config.learner.poll_latest_during_inner_steps = True
    config.validate()
    return config


def _context(
    config,
    *,
    logger=None,
    read_latest=None,
    wait_latest=None,
    prepare_prediction=None,
    reference=None,
    load_or_refresh_latest=None,
    paths=None,
    snapshot_model=None,
    terminal_published=None,
    last_loaded_global_version=1,
):
    """Build one strategy context with injectable storage and model boundaries."""

    logger = logger or RecordingLogger()
    reference = reference if reference is not None else torch.arange(4, dtype=torch.float32)
    return AdoptionContext(
        model=object(),
        paths=paths or RunPaths(Path("unused")),
        param_index={"total_numel": 4},
        device=torch.device("cpu"),
        config=config,
        logger=logger,
        last_loaded_global_version=last_loaded_global_version,
        last_loaded_latest={"version": last_loaded_global_version},
        tokens_since_global_load=64,
        read_latest_if_newer_fn=read_latest or (lambda *_args: None),
        wait_for_latest_if_newer_fn=wait_latest or (lambda *_args, **_kwargs: (None, 0.0)),
        adopt_global_fn=lambda **kwargs: int(kwargs["latest"]["version"]),
        rebase_local_delta_fn=lambda **kwargs: (
            int(kwargs["latest"]["version"]),
            1.25,
            {"reconcile_compute_device": "cpu"},
        ),
        snapshot_model_fn=snapshot_model
        or (lambda **_kwargs: (reference, {"reference_compute_device": "cpu"})),
        prepare_prediction_fn=prepare_prediction
        or (lambda **_kwargs: (reference, {"prediction_compute_device": "cpu"}, None, None)),
        load_or_refresh_latest_fn=load_or_refresh_latest,
        terminal_published_fn=terminal_published,
    )


@pytest.mark.parametrize(
    ("strategy_name", "expected_type"),
    [
        ("replace", ReplaceGlobalAdoptionStrategy),
        ("rebase_post_publish_delta", RebaseGlobalAdoptionStrategy),
        ("predict_post_publish_global", PredictGlobalAdoptionStrategy),
    ],
)
def test_strategy_factory_dispatches(strategy_name, expected_type):
    strategy = make_global_adoption_strategy(_config(strategy_name))

    assert isinstance(strategy, expected_type)


def test_strategy_factory_rejects_unknown_name():
    config = _config()
    config.learner.global_adoption_strategy = "unknown"

    with pytest.raises(ValueError, match="unsupported learner.global_adoption_strategy"):
        make_global_adoption_strategy(config)


def test_replace_returns_reset_adoption_outcome():
    config = _config()
    config.learner.poll_latest_during_inner_steps = True
    strategy = ReplaceGlobalAdoptionStrategy()

    action = strategy.on_newer_latest(_context(config), {"version": 2})

    assert strategy.wants_inner_poll(config)
    assert action.adoption is not None
    assert action.adoption.version == 2
    assert action.adoption.tokens_since_global_load == 0
    assert action.adoption.preserve_inner_state is False


def test_joint_horizon_skips_impossible_post_target_latest_wait() -> None:
    """Version 10 has no successor while learners finish the remaining local cycles."""

    config = _config()
    config.training.completion_mode = "local_and_global"
    config.training.max_local_steps = 2000
    config.training.inner_steps = 200
    config.sync.stop_after_outer_steps = 10
    calls: list[float] = []

    def wait_latest(*, wait_seconds: float, **_kwargs):
        """Record any forbidden attempt to wait for version 11."""

        calls.append(wait_seconds)
        return None, wait_seconds

    logger = RecordingLogger()
    action = ReplaceGlobalAdoptionStrategy().on_after_publish(
        _context(
            config,
            logger=logger,
            wait_latest=wait_latest,
            last_loaded_global_version=10,
        ),
        PublishResult(update_id="update-at-v10", base_global_version=10),
    )

    assert action.adoption is None
    assert calls == []
    assert [event for event, _payload in logger.events] == [
        "latest_polled",
        "post_publish_latest_wait_skipped_at_global_horizon",
    ]


@pytest.mark.parametrize(
    ("strategy_type", "strategy_name", "skip_event"),
    [
        (
            RebaseGlobalAdoptionStrategy,
            "rebase_post_publish_delta",
            "local_rebase_anchor_skipped_at_global_horizon",
        ),
        (
            PredictGlobalAdoptionStrategy,
            "predict_post_publish_global",
            "global_prediction_start_skipped_at_global_horizon",
        ),
    ],
)
def test_joint_horizon_does_not_start_state_requiring_version_eleven(
    strategy_type,
    strategy_name: str,
    skip_event: str,
) -> None:
    """Stateful adoption modes must not create work that can only reconcile with v11."""

    config = _config(strategy_name)
    config.training.completion_mode = "local_and_global"
    config.training.max_local_steps = 2000
    config.training.inner_steps = 200
    config.sync.stop_after_outer_steps = 10
    calls: list[str] = []

    def snapshot(**_kwargs):
        """Record a forbidden rebase snapshot at the final global horizon."""

        calls.append("snapshot")
        return torch.zeros(4), {}

    def prepare(**_kwargs):
        """Record a forbidden predicted-global preparation at the final horizon."""

        calls.append("prepare")
        return torch.zeros(4), {}, None, None

    logger = RecordingLogger()
    strategy = strategy_type()
    action = strategy.on_after_publish(
        _context(
            config,
            logger=logger,
            snapshot_model=snapshot,
            prepare_prediction=prepare,
            last_loaded_global_version=10,
        ),
        PublishResult(update_id="update-at-v10", base_global_version=10),
    )

    assert action.adoption is None
    assert action.reset_optimizer_reason is None
    assert calls == []
    assert [event for event, _payload in logger.events][-1] == skip_event
    assert not strategy.wants_inner_poll(config)


def test_replace_adoption_uses_actual_latest_returned_by_retry_helper():
    config = _config()
    logger = RecordingLogger()

    def recovered_load(**kwargs):
        recovered = {"version": 3, "weight_path": "v3"}
        value = kwargs["load_fn"](recovered)
        return type(
            "Result",
            (),
            {
                "latest": recovered,
                "value": value,
                "retry_count": 1,
                "waited_seconds": 0.1,
                "missing_path": "v2",
            },
        )()

    action = ReplaceGlobalAdoptionStrategy().on_newer_latest(
        _context(config, logger=logger, load_or_refresh_latest=recovered_load),
        {"version": 2, "weight_path": "v2"},
    )

    assert action.adoption is not None
    assert action.adoption.version == 3
    assert action.adoption.latest["version"] == 3
    assert logger.events[0] == (
        "latest_load_recovered",
        {
            "requested_version": 2,
            "loaded_version": 3,
            "retry_count": 1,
            "waited_seconds": 0.1,
            "missing_path": "v2",
        },
    )


def test_rebase_context_returns_actual_latest_from_retry_helper():
    config = _config("rebase_post_publish_delta")

    def recovered_load(**kwargs):
        recovered = {"version": 4, "weight_path": "global-4"}
        value = kwargs["load_fn"](recovered)
        return type(
            "Result",
            (),
            {
                "latest": recovered,
                "value": value,
                "retry_count": 1,
                "waited_seconds": 0.1,
                "missing_path": "v3",
            },
        )()

    version, delta_norm, stats, latest = _context(
        config,
        load_or_refresh_latest=recovered_load,
    ).rebase_local_delta({"version": 3, "weight_path": "v3"}, torch.zeros(4))

    assert version == 4
    assert delta_norm == 1.25
    assert stats == {"reconcile_compute_device": "cpu"}
    assert latest["version"] == 4


def test_rebase_strategy_owns_anchor_tokens_and_clears_after_adoption():
    config = _config("rebase_post_publish_delta")
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
    config = _config("predict_post_publish_global")
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
    assert not strategy.wants_inner_poll(config)


def test_prediction_strategy_timeout_keeps_state_for_diagnostics():
    config = _config("predict_post_publish_global")
    logger = RecordingLogger()
    ctx = _context(config, logger=logger)
    strategy = PredictGlobalAdoptionStrategy()
    strategy.on_after_publish(ctx, PublishResult(update_id="u1", base_global_version=1))

    with pytest.raises(TimeoutError, match="timed out waiting to reconcile"):
        strategy.on_cycle_end(ctx)

    assert strategy.wants_inner_poll(config)
    assert logger.events[-1][0] == "global_prediction_reconcile_wait_started"


def test_prediction_reconcile_stop_during_wait_abandons_without_timeout(tmp_path):
    config = _config("predict_post_publish_global")
    paths = RunPaths(tmp_path)
    logger = RecordingLogger()

    def stop_during_wait(*_args, **_kwargs):
        atomic_write_json(paths.stop_json, {"reason": "stop_after_outer_steps"})
        return None, 0.25

    ctx = _context(
        config,
        paths=paths,
        logger=logger,
        wait_latest=stop_during_wait,
    )
    strategy = PredictGlobalAdoptionStrategy()
    strategy.on_after_publish(ctx, PublishResult(update_id="u1", base_global_version=1))
    strategy.on_local_tokens(32)

    action = strategy.on_cycle_end(ctx)

    assert action.adoption is None
    assert action.reset_optimizer_reason is None
    assert not strategy.wants_inner_poll(config)
    assert [event for event, _payload in logger.events][-2:] == [
        "global_prediction_reconcile_wait_started",
        "global_prediction_abandoned_on_stop",
    ]
    assert logger.events[-1][1]["carried_delta_tokens"] == 32


def test_predict_after_publish_stop_skips_prediction_preparation(tmp_path):
    config = _config("predict_post_publish_global")
    paths = RunPaths(tmp_path)
    atomic_write_json(paths.stop_json, {"reason": "stop_after_outer_steps"})
    calls = []

    def prepare(**_kwargs):
        calls.append("prepare")
        return torch.zeros(4), {}, None, None

    logger = RecordingLogger()
    strategy = PredictGlobalAdoptionStrategy()
    action = strategy.on_after_publish(
        _context(
            config,
            paths=paths,
            logger=logger,
            prepare_prediction=prepare,
        ),
        PublishResult(update_id="u1", base_global_version=1),
    )

    assert calls == []
    assert action.adoption is None
    assert action.reset_optimizer_reason is None
    assert not strategy.wants_inner_poll(config)
    assert logger.events[-1][0] == "global_prediction_start_skipped_on_stop"


def test_rebase_after_publish_stop_skips_anchor_snapshot(tmp_path):
    config = _config("rebase_post_publish_delta")
    paths = RunPaths(tmp_path)
    atomic_write_json(paths.stop_json, {"reason": "stop_after_outer_steps"})
    calls = []

    def snapshot(**_kwargs):
        calls.append("snapshot")
        return torch.zeros(4), {}

    logger = RecordingLogger()
    strategy = RebaseGlobalAdoptionStrategy()
    action = strategy.on_after_publish(
        _context(
            config,
            paths=paths,
            logger=logger,
            snapshot_model=snapshot,
        ),
        PublishResult(update_id="u1", base_global_version=1),
    )

    assert calls == []
    assert action.adoption is None
    assert not strategy.wants_inner_poll(config)
    assert logger.events[-1][0] == "local_rebase_anchor_skipped_on_stop"


@pytest.mark.parametrize(
    "strategy_type,strategy_name",
    [
        (PredictGlobalAdoptionStrategy, "predict_post_publish_global"),
        (RebaseGlobalAdoptionStrategy, "rebase_post_publish_delta"),
    ],
)
def test_ha_adoption_ignores_polluted_fixed_stop_without_authoritative_terminal(
    tmp_path, strategy_type, strategy_name
):
    config = _config(strategy_name)
    paths = RunPaths(tmp_path)
    atomic_write_json(paths.stop_json, {"reason": "polluted-lower-epoch-cache"})
    calls = []

    def prepare(**_kwargs):
        calls.append("prepare")
        return torch.zeros(4), {}, None, None

    def snapshot(**_kwargs):
        calls.append("snapshot")
        return torch.zeros(4), {}

    strategy = strategy_type()
    action = strategy.on_after_publish(
        _context(
            config,
            paths=paths,
            prepare_prediction=prepare,
            snapshot_model=snapshot,
            terminal_published=lambda _paths: False,
        ),
        PublishResult(update_id="u1", base_global_version=1),
    )

    assert calls
    assert action.reset_optimizer_reason == (
        "global_prediction_started" if strategy_type is PredictGlobalAdoptionStrategy else None
    )


@pytest.mark.parametrize(
    "strategy_type,strategy_name",
    [
        (PredictGlobalAdoptionStrategy, "predict_post_publish_global"),
        (RebaseGlobalAdoptionStrategy, "rebase_post_publish_delta"),
    ],
)
def test_stop_does_not_skip_already_visible_direct_adoption(tmp_path, strategy_type, strategy_name):
    config = _config(strategy_name)
    paths = RunPaths(tmp_path)
    atomic_write_json(paths.stop_json, {"reason": "stop_after_outer_steps"})
    strategy = strategy_type()

    action = strategy.on_after_publish(
        _context(
            config,
            paths=paths,
            read_latest=lambda *_args: {"version": 2},
        ),
        PublishResult(update_id="u1", base_global_version=1),
    )

    assert action.adoption is not None
    assert action.adoption.version == 2
    assert action.adoption.preserve_inner_state is False
