"""Global-adoption state machines used by the full learner.

The runner owns the training loop, optimizer, scheduler, and the three common
adoption-finalization events.  A strategy owns every strategy-specific
reference, carried-token counter, and publication hook.  The stable hook order
within a cycle is:

``on_local_tokens`` → optional ``on_newer_latest`` → ``on_stop`` or
``on_cycle_end`` → ``before_publish`` → ``on_after_publish``.

All state is process-local and intentionally ephemeral.  Strategies receive
storage/model operations through :class:`AdoptionContext`, keeping this module
free of imports from the learner runner and safe for config-time validation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import time
from typing import Any, Callable, ClassVar

from ..core.adoption_rules import validate_global_adoption_config


@dataclass(frozen=True)
class PredictionState:
    """Ephemeral state carried while training on a predicted global snapshot."""

    reference_flat: Any | None = None
    carried_tokens: int = 0
    update_id: str | None = None
    base_version: int | None = None

    @property
    def active(self) -> bool:
        return self.reference_flat is not None

    def require_active(self) -> PredictionState:
        complete = (
            self.reference_flat is not None
            and self.update_id is not None
            and self.base_version is not None
            and self.carried_tokens >= 0
        )
        if not complete:
            raise RuntimeError("predicted-global state is incomplete")
        return self

    def add_tokens(self, tokens: int) -> PredictionState:
        self.require_active()
        return PredictionState(
            reference_flat=self.reference_flat,
            carried_tokens=self.carried_tokens + int(tokens),
            update_id=self.update_id,
            base_version=self.base_version,
        )


@dataclass(frozen=True)
class PredictionReconcileResult:
    version: int
    latest: dict[str, Any]
    tokens_since_global_load: int
    state: PredictionState


@dataclass(frozen=True)
class AdoptionOutcome:
    version: int
    latest: dict[str, Any]
    tokens_since_global_load: int
    preserve_inner_state: bool
    reason: str
    load_apply_seconds: float = 0.0


@dataclass(frozen=True)
class StrategyAction:
    adoption: AdoptionOutcome | None = None
    reset_optimizer_reason: str | None = None
    tokens_since_global_load: int | None = None

    def __post_init__(self) -> None:
        if self.adoption is not None and self.reset_optimizer_reason is not None:
            raise ValueError("strategy action cannot adopt and request a standalone reset")


@dataclass(frozen=True)
class PublishResult:
    update_id: str
    base_global_version: int


@dataclass(frozen=True)
class AdoptionContext:
    model: Any
    paths: Any
    param_index: dict[str, Any]
    device: Any
    config: Any
    logger: Any
    last_loaded_global_version: int
    last_loaded_latest: dict[str, Any]
    tokens_since_global_load: int
    read_latest_if_newer_fn: Callable[..., dict[str, Any] | None]
    wait_for_latest_if_newer_fn: Callable[..., tuple[dict[str, Any] | None, float]]
    adopt_global_fn: Callable[..., int]
    rebase_local_delta_fn: Callable[..., tuple[int, float, dict[str, Any]]]
    snapshot_model_fn: Callable[..., tuple[Any, dict[str, Any]]]
    prepare_prediction_fn: Callable[
        ...,
        tuple[Any | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None],
    ]
    load_or_refresh_latest_fn: Callable[..., Any] | None = None
    terminal_published_fn: Callable[[Any], bool] | None = None

    def terminal_published(self) -> bool:
        if self.terminal_published_fn is None:
            return bool(self.paths.stop_json.exists())
        return bool(self.terminal_published_fn(self.paths))

    def read_newer_latest(self) -> dict[str, Any] | None:
        return self.read_latest_if_newer_fn(self.paths, self.last_loaded_global_version)

    def wait_for_newer_latest(self, *, wait_seconds: float) -> tuple[dict[str, Any] | None, float]:
        return self.wait_for_latest_if_newer_fn(
            self.paths,
            self.last_loaded_global_version,
            wait_seconds=wait_seconds,
            poll_seconds=self.config.learner.post_publish_latest_poll_seconds,
        )

    def _load_latest(self, latest: dict[str, Any], load_fn: Callable[..., Any]) -> Any:
        if self.load_or_refresh_latest_fn is None:
            return None, load_fn(latest)
        result = self.load_or_refresh_latest_fn(
            paths=self.paths,
            latest=latest,
            version_field="version",
            load_fn=load_fn,
            wait_seconds=self.config.learner.prediction.reconcile_timeout_seconds,
            poll_seconds=self.config.learner.post_publish_latest_poll_seconds,
        )
        if result.retry_count:
            self.logger.event(
                "latest_load_recovered",
                requested_version=int(latest["version"]),
                loaded_version=int(result.latest["version"]),
                retry_count=result.retry_count,
                waited_seconds=result.waited_seconds,
                missing_path=result.missing_path,
            )
        return result.latest, result.value

    def adopt_global(self, latest: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        loaded_latest, version = self._load_latest(
            latest,
            lambda candidate: self.adopt_global_fn(
                model=self.model,
                latest=candidate,
                param_index=self.param_index,
                device=self.device,
            ),
        )
        return int(version), latest if loaded_latest is None else loaded_latest

    def rebase_local_delta(
        self,
        latest: dict[str, Any],
        reference_flat: Any,
    ) -> tuple[int, float, dict[str, Any], dict[str, Any]]:
        loaded_latest, value = self._load_latest(
            latest,
            lambda candidate: self.rebase_local_delta_fn(
                model=self.model,
                latest=candidate,
                param_index=self.param_index,
                device=self.device,
                reference_flat=reference_flat,
                config=self.config,
            ),
        )
        version, delta_norm, stats = value
        return (
            int(version),
            float(delta_norm),
            stats,
            latest if loaded_latest is None else loaded_latest,
        )

    def snapshot_model(self) -> tuple[Any, dict[str, Any]]:
        return self.snapshot_model_fn(
            model=self.model,
            param_index=self.param_index,
            device=self.device,
            config=self.config,
        )

    def prepare_prediction(
        self,
    ) -> tuple[Any | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        return self.prepare_prediction_fn(
            paths=self.paths,
            model=self.model,
            cached_latest=self.last_loaded_latest,
            param_index=self.param_index,
            device=self.device,
            config=self.config,
            local_tokens=self.tokens_since_global_load,
        )


def reconcile_prediction(
    *,
    ctx: AdoptionContext,
    latest: dict[str, Any],
    state: PredictionState,
    previous_version: int,
    waited_seconds: float | None = None,
) -> PredictionReconcileResult:
    """Rebase post-prediction progress and clear the explicit prediction state."""

    active = state.require_active()
    version, delta_norm, reconcile_compute_stats, loaded_latest = ctx.rebase_local_delta(
        latest,
        active.reference_flat,
    )
    event_fields: dict[str, Any] = {
        "previous_version": previous_version,
        "version": version,
        "prediction_base_version": active.base_version,
        "prediction_update_id": active.update_id,
        "carried_delta_tokens": active.carried_tokens,
        "post_prediction_delta_norm": delta_norm,
    }
    if waited_seconds is not None:
        event_fields["reconcile_waited_seconds"] = waited_seconds
    event_fields.update(reconcile_compute_stats)
    ctx.logger.event("global_prediction_reconciled", **event_fields)
    return PredictionReconcileResult(
        version=version,
        latest=loaded_latest,
        tokens_since_global_load=active.carried_tokens,
        state=PredictionState(),
    )


class GlobalAdoptionStrategy(ABC):
    name: ClassVar[str]

    @classmethod
    def validate(cls, config: Any) -> None:
        """Validate constraints owned by this strategy; replace is a no-op."""
        return None

    @abstractmethod
    def wants_inner_poll(self, config: Any) -> bool:
        raise NotImplementedError

    @abstractmethod
    def on_newer_latest(
        self,
        ctx: AdoptionContext,
        latest: dict[str, Any],
    ) -> StrategyAction:
        raise NotImplementedError

    def on_cycle_end(self, ctx: AdoptionContext) -> StrategyAction:
        return StrategyAction()

    def before_publish(self, ctx: AdoptionContext) -> None:
        del ctx

    @abstractmethod
    def on_after_publish(
        self,
        ctx: AdoptionContext,
        publish_result: PublishResult,
    ) -> StrategyAction:
        raise NotImplementedError

    def on_local_tokens(self, tokens: int) -> None:
        del tokens

    def on_stop(self, ctx: AdoptionContext) -> bool:
        del ctx
        return False

    def _poll_after_publish(self, ctx: AdoptionContext, update_id: str) -> dict[str, Any] | None:
        maybe_latest = ctx.read_newer_latest()
        ctx.logger.event(
            "latest_polled",
            current_version=ctx.last_loaded_global_version,
            found_version=maybe_latest.get("version") if maybe_latest else None,
        )
        wait_seconds = ctx.config.learner.post_publish_latest_wait_seconds
        if maybe_latest is None and wait_seconds > 0.0:
            ctx.logger.event(
                "post_publish_latest_wait_started",
                current_version=ctx.last_loaded_global_version,
                update_id=update_id,
                wait_seconds=wait_seconds,
                poll_seconds=ctx.config.learner.post_publish_latest_poll_seconds,
            )
            maybe_latest, waited_seconds = ctx.wait_for_newer_latest(wait_seconds=wait_seconds)
            ctx.logger.event(
                "post_publish_latest_wait_finished",
                current_version=ctx.last_loaded_global_version,
                found_version=maybe_latest.get("version") if maybe_latest else None,
                update_id=update_id,
                waited_seconds=waited_seconds,
            )
        return maybe_latest

    def _direct_adoption(
        self,
        ctx: AdoptionContext,
        latest: dict[str, Any],
        *,
        update_id: str | None = None,
    ) -> StrategyAction:
        previous_version = ctx.last_loaded_global_version
        adoption_start = time.monotonic()
        version, loaded_latest = ctx.adopt_global(latest)
        load_apply_seconds = time.monotonic() - adoption_start
        if update_id is not None and self.name != ReplaceGlobalAdoptionStrategy.name:
            ctx.logger.event(
                "global_adopted_after_publish",
                previous_version=previous_version,
                version=version,
                update_id=update_id,
            )
        return StrategyAction(
            adoption=AdoptionOutcome(
                version=version,
                latest=loaded_latest,
                tokens_since_global_load=0,
                preserve_inner_state=False,
                reason="global_adopted",
                load_apply_seconds=load_apply_seconds,
            )
        )


class ReplaceGlobalAdoptionStrategy(GlobalAdoptionStrategy):
    name = "replace"

    @classmethod
    def validate(cls, config: Any) -> None:
        del config
        return None

    def wants_inner_poll(self, config: Any) -> bool:
        return bool(config.learner.poll_latest_during_inner_steps)

    def on_newer_latest(
        self,
        ctx: AdoptionContext,
        latest: dict[str, Any],
    ) -> StrategyAction:
        return self._direct_adoption(ctx, latest)

    def on_after_publish(
        self,
        ctx: AdoptionContext,
        publish_result: PublishResult,
    ) -> StrategyAction:
        maybe_latest = self._poll_after_publish(ctx, publish_result.update_id)
        if maybe_latest is None:
            return StrategyAction()
        return self._direct_adoption(ctx, maybe_latest, update_id=publish_result.update_id)


class RebaseGlobalAdoptionStrategy(GlobalAdoptionStrategy):
    name = "rebase_post_publish_delta"

    def __init__(self) -> None:
        self._reference_flat: Any | None = None
        self._carried_tokens = 0
        self._anchor_update_id: str | None = None

    @classmethod
    def validate(cls, config: Any) -> None:
        validate_global_adoption_config(config)

    def wants_inner_poll(self, config: Any) -> bool:
        return bool(
            config.learner.poll_latest_during_inner_steps and self._reference_flat is not None
        )

    def on_local_tokens(self, tokens: int) -> None:
        if self._reference_flat is not None:
            self._carried_tokens += int(tokens)

    def on_newer_latest(
        self,
        ctx: AdoptionContext,
        latest: dict[str, Any],
    ) -> StrategyAction:
        if self._reference_flat is None:
            raise RuntimeError("local-delta rebase reference is unavailable")
        previous_version = ctx.last_loaded_global_version
        adoption_start = time.monotonic()
        version, delta_norm, reconcile_compute_stats, loaded_latest = ctx.rebase_local_delta(
            latest,
            self._reference_flat,
        )
        load_apply_seconds = time.monotonic() - adoption_start
        ctx.logger.event(
            "global_rebased",
            previous_version=previous_version,
            version=version,
            anchor_update_id=self._anchor_update_id,
            carried_delta_tokens=self._carried_tokens,
            local_delta_norm=delta_norm,
            **reconcile_compute_stats,
        )
        carried_tokens = self._carried_tokens
        self._clear_anchor()
        return StrategyAction(
            adoption=AdoptionOutcome(
                version=version,
                latest=loaded_latest,
                tokens_since_global_load=carried_tokens,
                preserve_inner_state=True,
                reason="global_rebased",
                load_apply_seconds=load_apply_seconds,
            )
        )

    def before_publish(self, ctx: AdoptionContext) -> None:
        del ctx
        self._clear_anchor()

    def on_after_publish(
        self,
        ctx: AdoptionContext,
        publish_result: PublishResult,
    ) -> StrategyAction:
        maybe_latest = self._poll_after_publish(ctx, publish_result.update_id)
        if maybe_latest is not None:
            return self._direct_adoption(ctx, maybe_latest, update_id=publish_result.update_id)
        if ctx.terminal_published():
            ctx.logger.event(
                "local_rebase_anchor_skipped_on_stop",
                update_id=publish_result.update_id,
                base_global_version=publish_result.base_global_version,
            )
            return StrategyAction()
        self._reference_flat, reference_compute_stats = ctx.snapshot_model()
        self._carried_tokens = 0
        self._anchor_update_id = publish_result.update_id
        ctx.logger.event(
            "local_rebase_anchor_saved",
            update_id=publish_result.update_id,
            base_global_version=publish_result.base_global_version,
            anchor_bytes=int(self._reference_flat.numel() * self._reference_flat.element_size()),
            **reference_compute_stats,
        )
        return StrategyAction()

    def _clear_anchor(self) -> None:
        self._reference_flat = None
        self._carried_tokens = 0
        self._anchor_update_id = None


class PredictGlobalAdoptionStrategy(GlobalAdoptionStrategy):
    name = "predict_post_publish_global"

    def __init__(self) -> None:
        self._state = PredictionState()

    @classmethod
    def validate(cls, config: Any) -> None:
        validate_global_adoption_config(config)

    def wants_inner_poll(self, config: Any) -> bool:
        return bool(config.learner.poll_latest_during_inner_steps and self._state.active)

    def on_local_tokens(self, tokens: int) -> None:
        if self._state.active:
            self._state = self._state.add_tokens(tokens)

    def on_newer_latest(
        self,
        ctx: AdoptionContext,
        latest: dict[str, Any],
    ) -> StrategyAction:
        adoption_start = time.monotonic()
        result = reconcile_prediction(
            ctx=ctx,
            latest=latest,
            state=self._state,
            previous_version=ctx.last_loaded_global_version,
        )
        load_apply_seconds = time.monotonic() - adoption_start
        self._state = result.state
        return StrategyAction(
            adoption=AdoptionOutcome(
                version=result.version,
                latest=result.latest,
                tokens_since_global_load=result.tokens_since_global_load,
                preserve_inner_state=True,
                reason="global_prediction_reconciled",
                load_apply_seconds=load_apply_seconds,
            )
        )

    def on_cycle_end(self, ctx: AdoptionContext) -> StrategyAction:
        if not self._state.active:
            return StrategyAction()
        active = self._state.require_active()
        ctx.logger.event(
            "global_prediction_reconcile_wait_started",
            current_version=ctx.last_loaded_global_version,
            prediction_base_version=active.base_version,
            prediction_update_id=active.update_id,
            timeout_seconds=ctx.config.learner.prediction.reconcile_timeout_seconds,
        )
        maybe_latest, waited_seconds = ctx.wait_for_newer_latest(
            wait_seconds=ctx.config.learner.prediction.reconcile_timeout_seconds
        )
        if maybe_latest is None:
            if ctx.terminal_published():
                self.on_stop(ctx)
                return StrategyAction()
            raise TimeoutError("timed out waiting to reconcile predicted global before publication")
        adoption_start = time.monotonic()
        result = reconcile_prediction(
            ctx=ctx,
            latest=maybe_latest,
            state=self._state,
            previous_version=ctx.last_loaded_global_version,
            waited_seconds=waited_seconds,
        )
        load_apply_seconds = time.monotonic() - adoption_start
        self._state = result.state
        return StrategyAction(
            adoption=AdoptionOutcome(
                version=result.version,
                latest=result.latest,
                tokens_since_global_load=result.tokens_since_global_load,
                preserve_inner_state=True,
                reason="global_prediction_reconciled",
                load_apply_seconds=load_apply_seconds,
            )
        )

    def before_publish(self, ctx: AdoptionContext) -> None:
        del ctx
        if self._state.active:
            raise RuntimeError("cannot publish a proposal while training on predicted global")

    def on_after_publish(
        self,
        ctx: AdoptionContext,
        publish_result: PublishResult,
    ) -> StrategyAction:
        maybe_latest = self._poll_after_publish(ctx, publish_result.update_id)
        prepared_reference: Any | None = None
        prepared_stats: dict[str, Any] | None = None
        if maybe_latest is None:
            if ctx.terminal_published():
                ctx.logger.event(
                    "global_prediction_start_skipped_on_stop",
                    update_id=publish_result.update_id,
                    base_global_version=publish_result.base_global_version,
                )
                return StrategyAction()
            if int(ctx.last_loaded_latest["version"]) != ctx.last_loaded_global_version:
                raise RuntimeError("cached latest metadata does not match learner base")
            prepared_reference, prepared_stats, maybe_latest, recovery = ctx.prepare_prediction()
            if recovery is not None:
                if maybe_latest is None:
                    raise RuntimeError(
                        "prediction preparation recovery did not return newer latest"
                    )
                ctx.logger.event(
                    "global_prediction_preparation_recovered",
                    update_id=publish_result.update_id,
                    found_version=int(maybe_latest["version"]),
                    **recovery,
                )
        if maybe_latest is not None:
            return self._direct_adoption(ctx, maybe_latest, update_id=publish_result.update_id)
        if prepared_reference is None or prepared_stats is None:
            raise RuntimeError("prediction preparation did not return a snapshot")
        self._state = PredictionState(
            reference_flat=prepared_reference,
            carried_tokens=0,
            update_id=publish_result.update_id,
            base_version=ctx.last_loaded_global_version,
        )
        ctx.logger.event(
            "global_prediction_started",
            update_id=publish_result.update_id,
            reference_bytes=int(prepared_reference.numel() * prepared_reference.element_size()),
            **prepared_stats,
        )
        return StrategyAction(
            reset_optimizer_reason="global_prediction_started",
            tokens_since_global_load=0,
        )

    def on_stop(self, ctx: AdoptionContext) -> bool:
        if not self._state.active:
            return False
        active = self._state.require_active()
        ctx.logger.event(
            "global_prediction_abandoned_on_stop",
            prediction_base_version=active.base_version,
            prediction_update_id=active.update_id,
            carried_delta_tokens=active.carried_tokens,
        )
        self._state = PredictionState()
        return True


STRATEGY_TYPES: dict[str, type[GlobalAdoptionStrategy]] = {
    strategy_type.name: strategy_type
    for strategy_type in (
        ReplaceGlobalAdoptionStrategy,
        RebaseGlobalAdoptionStrategy,
        PredictGlobalAdoptionStrategy,
    )
}


def strategy_type_for_config(config: Any) -> type[GlobalAdoptionStrategy]:
    name = config.learner.global_adoption_strategy
    strategy_type = STRATEGY_TYPES.get(name)
    if strategy_type is None:
        raise ValueError(f"unsupported learner.global_adoption_strategy: {name}")
    return strategy_type


def make_global_adoption_strategy(config: Any) -> GlobalAdoptionStrategy:
    strategy_type = strategy_type_for_config(config)
    strategy_type.validate(config)
    return strategy_type()
