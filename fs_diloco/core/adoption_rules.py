"""Torch-free validation shared by config loading and runtime adoption strategies."""

from __future__ import annotations

from typing import Any


def validate_global_adoption_config(config: Any) -> None:
    strategy = config.learner.global_adoption_strategy
    if strategy == "replace":
        return
    if strategy not in {"rebase_post_publish_delta", "predict_post_publish_global"}:
        raise ValueError(f"unsupported learner.global_adoption_strategy: {strategy}")
    if (
        not config.learner.adopt_global_after_upload
        or not config.learner.poll_latest_during_inner_steps
    ):
        raise ValueError(
            f"{strategy} requires adopt_global_after_upload=true and "
            "poll_latest_during_inner_steps=true"
        )
    if strategy == "predict_post_publish_global":
        if config.outer_optimizer.name.lower() != "nesterov":
            raise ValueError("predict_post_publish_global currently requires outer nesterov")
        if config.outer_optimizer.weight_decay != 0.0:
            raise ValueError("predict_post_publish_global currently requires outer weight_decay=0")
        if config.learner.prediction.reconcile_timeout_seconds <= 0.0:
            raise ValueError("learner.prediction.reconcile_timeout_seconds must be > 0")
