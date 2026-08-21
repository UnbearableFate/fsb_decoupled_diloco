"""Admitted Full Protocol learner training loop."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

import torch

from ..core.run_descriptor import LoadedRunDescriptor, write_actor_attestation
from ..core.versions import (
    CYCLE_RECEIPT_FORMAT_VERSION,
    PROPOSAL_FORMAT_VERSION,
    PROPOSAL_POINTER_FORMAT_VERSION,
)
from ..modeling.hf_data import build_indexed_batch_iterator
from ..modeling.hf_model import choose_device, load_causal_lm_and_tokenizer
from ..modeling.outer_optim import outer_optimizer_step
from ..modeling.param_index import (
    build_param_index,
    flatten_trainable_params,
    load_flat_into_model,
    load_param_index,
    validate_matching_index,
)
from ..modeling.training import build_inner_optimizer_and_scheduler
from ..observability.logging_utils import ActorTelemetryWriter
from ..storage.admission import AdmissionContext
from ..storage.control import (
    current_latest_if_newer,
    publish_terminal_ack,
    read_current_control,
    wait_for_current_latest,
    wait_for_receipt_barrier,
)
from ..protocol.cycle_receipt import CycleReceiptV1, canonical_receipt_relative_path
from ..protocol.data_cursor import IndexedBlockCursor
from ..protocol.proposal import FullUpdateProposalV2, canonical_update_relative_path
from ..protocol.token_accounting import TrainingSegmentAccumulator
from .adoption import (
    AdoptionContext,
    PublishResult,
    StrategyAction,
    make_global_adoption_strategy,
)
from .learner_control import completed_local_steps_from_cycle
from ..storage.atomic_io import atomic_write_json, publish_immutable_bytes
from ..storage.object_store import tensor_schema_sha256
from ..storage.tensor_codec import (
    dtype_from_name,
    load_global_weights_flat,
    load_global_weights_into_model,
    load_outer_state,
    publish_safetensors_immutable,
)


def _wait_for_newer(
    loaded: LoadedRunDescriptor,
    version: int,
    *,
    wait_seconds: float,
    poll_seconds: float,
) -> tuple[dict[str, Any] | None, float]:
    started = time.monotonic()
    deadline = started + max(0.0, float(wait_seconds))
    while True:
        current = read_current_control(
            loaded.paths,
            run_id=str(loaded.config.run.run_id),
            max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
        )
        if current is not None:
            if current.terminal is not None:
                return None, time.monotonic() - started
            if current.latest is not None and int(current.latest["version"]) > version:
                return current.latest, time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return None, time.monotonic() - started
        time.sleep(min(float(poll_seconds), remaining))


def _apply_strategy_action(
    action: StrategyAction,
    *,
    model: torch.nn.Module,
    config: Any,
    telemetry: ActorTelemetryWriter,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler | None,
    completed_local_steps: int,
    current_version: int,
) -> tuple[
    torch.optim.Optimizer,
    torch.optim.lr_scheduler.LRScheduler | None,
    int,
    dict[str, Any] | None,
    int | None,
]:
    outcome = action.adoption
    if outcome is not None:
        if not outcome.preserve_inner_state:
            optimizer, scheduler = build_inner_optimizer_and_scheduler(
                model,
                config,
                completed_local_steps=completed_local_steps,
            )
        telemetry.event(
            outcome.reason,
            version=outcome.version,
            preserve_inner_state=outcome.preserve_inner_state,
        )
        return (
            optimizer,
            scheduler,
            outcome.version,
            outcome.latest,
            outcome.tokens_since_global_load,
        )
    if action.reset_optimizer_reason is not None:
        optimizer, scheduler = build_inner_optimizer_and_scheduler(
            model,
            config,
            completed_local_steps=completed_local_steps,
        )
        telemetry.event(
            "inner_optimizer_reset",
            version=current_version,
            reason=action.reset_optimizer_reason,
        )
    return optimizer, scheduler, current_version, None, action.tokens_since_global_load


def run_admitted_learner(
    loaded: LoadedRunDescriptor,
    admission: AdmissionContext,
) -> None:
    """Start model/GPU work only after the torch-free admission gate succeeded."""

    config = loaded.config
    paths = loaded.paths
    actor_id = admission.actor_id
    attempt_id = admission.attempt_id
    telemetry = ActorTelemetryWriter(
        paths.actor_metrics_path("learner", actor_id, attempt_id),
        actor_kind="learner",
        actor_id=actor_id,
        attempt_id=attempt_id,
    )
    device = choose_device()
    model, tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    write_actor_attestation(
        loaded,
        actor_kind="learner",
        actor_id=actor_id,
        attempt_id=attempt_id,
        runtime_evidence={
            "torch_version": torch.__version__,
            "cuda_runtime_version": torch.version.cuda,
            "gpu_driver_version": os.environ.get("NVIDIA_DRIVER_VERSION"),
            "module_environment": os.environ.get("LOADEDMODULES", "").split(":")
            if os.environ.get("LOADEDMODULES")
            else [],
            "resource_allocation": {
                "pbs_job_id": os.environ.get("PBS_JOBID"),
                "device": str(device),
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            },
        },
        scheduler_job_id=os.environ.get("PBS_JOBID"),
        accelerator_identity=os.environ.get("CUDA_VISIBLE_DEVICES") or str(device),
    )
    telemetry.event("admitted", fence=admission.fence.as_dict(), device=str(device))

    initial = wait_for_current_latest(
        paths,
        run_id=str(config.run.run_id),
        timeout_seconds=loaded.config.leader.learner_recovery_wait_seconds,
        poll_seconds=config.sync.scan_interval_seconds,
        max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
    )
    if initial.kind == "terminal":
        telemetry.event("terminal_observed", terminal=initial.payload)
        return
    latest = initial.payload
    param_index = load_param_index(paths.param_index_json)
    local_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    validate_matching_index(param_index, local_index)
    load_global_weights_into_model(
        paths.shared_root,
        str(latest["weight_path"]),
        model,
        param_index,
        expected_size=int(latest["weight_size_bytes"]),
        expected_sha256=str(latest["weight_sha256"]),
        expected_theta_sha256=str(latest["theta_sha256"]),
    )
    base_version = int(latest["version"])
    resumed_local_steps = completed_local_steps_from_cycle(
        next_cycle_seq=admission.resume.next_cycle_seq,
        inner_steps=config.training.inner_steps,
    )
    optimizer, scheduler = build_inner_optimizer_and_scheduler(
        model,
        config,
        completed_local_steps=resumed_local_steps,
    )
    latest_metadata = latest
    tokens_since_global_load = 0

    def read_newer(_paths: Any, version: int) -> dict[str, Any] | None:
        return current_latest_if_newer(
            paths,
            run_id=str(config.run.run_id),
            newer_than=version,
            max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
        )

    def wait_newer(
        _paths: Any,
        version: int,
        *,
        wait_seconds: float,
        poll_seconds: float,
    ) -> tuple[dict[str, Any] | None, float]:
        return _wait_for_newer(
            loaded,
            version,
            wait_seconds=wait_seconds,
            poll_seconds=poll_seconds,
        )

    def adopt_global(
        *,
        model: torch.nn.Module,
        latest: dict[str, Any],
        param_index: dict[str, Any],
        device: torch.device,
    ) -> int:
        load_global_weights_into_model(
            paths.shared_root,
            str(latest["weight_path"]),
            model,
            param_index,
            expected_size=int(latest["weight_size_bytes"]),
            expected_sha256=str(latest["weight_sha256"]),
            expected_theta_sha256=str(latest["theta_sha256"]),
        )
        model.to(device)
        return int(latest["version"])

    def snapshot_model(**_kwargs: Any) -> tuple[torch.Tensor, dict[str, Any]]:
        snapshot = flatten_trainable_params(
            model,
            param_index,
            device=device,
            dtype=dtype_from_name(config.syncer.compute_dtype),
        )
        return snapshot, {"compute_device": str(device)}

    def rebase_local_delta(
        *,
        latest: dict[str, Any],
        reference_flat: torch.Tensor,
        **_kwargs: Any,
    ) -> tuple[int, float, dict[str, Any]]:
        compute_dtype = dtype_from_name(config.syncer.compute_dtype)
        reference = reference_flat.to(device=device, dtype=compute_dtype)
        local = flatten_trainable_params(
            model,
            param_index,
            device=device,
            dtype=compute_dtype,
        )
        global_flat = load_global_weights_flat(
            paths.shared_root,
            str(latest["weight_path"]),
            param_index,
            expected_size=int(latest["weight_size_bytes"]),
            expected_sha256=str(latest["weight_sha256"]),
            expected_theta_sha256=str(latest["theta_sha256"]),
            device=device,
            dtype=compute_dtype,
        )
        local.sub_(reference)
        delta_norm = float(torch.linalg.vector_norm(local, dtype=torch.float32).item())
        local.add_(global_flat)
        load_flat_into_model(model, local, param_index)
        model.to(device)
        return int(latest["version"]), delta_norm, {"compute_device": str(device)}

    def prepare_prediction(
        *,
        cached_latest: dict[str, Any],
        local_tokens: int,
        **_kwargs: Any,
    ) -> tuple[
        torch.Tensor | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
    ]:
        newer = read_newer(paths, int(cached_latest["version"]))
        if newer is not None:
            return None, None, newer, {"reason": "newer_latest_before_prediction"}
        compute_dtype = dtype_from_name(config.syncer.compute_dtype)
        local_flat = flatten_trainable_params(
            model,
            param_index,
            device=device,
            dtype=compute_dtype,
        )
        global_flat = load_global_weights_flat(
            paths.shared_root,
            str(cached_latest["weight_path"]),
            param_index,
            expected_size=int(cached_latest["weight_size_bytes"]),
            expected_sha256=str(cached_latest["weight_sha256"]),
            expected_theta_sha256=str(cached_latest["theta_sha256"]),
            device=device,
            dtype=compute_dtype,
        )
        outer_theta, outer_state = load_outer_state(
            paths.shared_root,
            str(cached_latest["optim_path"]),
            expected_size=int(cached_latest["optim_size_bytes"]),
            expected_sha256=str(cached_latest["optim_sha256"]),
            expected_theta_sha256=str(cached_latest["theta_sha256"]),
            device=device,
            dtype=compute_dtype,
        )
        if not torch.equal(global_flat, outer_theta):
            raise RuntimeError("prediction global and outer theta do not match")
        momentum = outer_state.get("momentum")
        if momentum is None:
            raise RuntimeError("prediction requires outer momentum")
        previous_tokens = int(cached_latest.get("direct_weight_tokens_applied", 0))
        estimated_tokens = previous_tokens or int(local_tokens) * max(
            1, int(config.sync.quorum_min)
        )
        local_weight = min(1.0, float(local_tokens) / float(max(local_tokens, estimated_tokens)))
        local_delta = local_flat.sub(global_flat)
        historical_delta = momentum.mul(-(1.0 - float(config.outer_optimizer.momentum)))
        predicted_delta = historical_delta.mul(1.0 - local_weight).add(
            local_delta,
            alpha=local_weight,
        )
        predicted, _state = outer_optimizer_step(
            global_flat,
            predicted_delta.neg(),
            outer_state,
            config.outer_optimizer,
        )
        predicted = predicted.detach().contiguous()
        load_flat_into_model(model, predicted, param_index)
        model.to(device)
        return (
            predicted,
            {
                "compute_device": str(device),
                "local_weight": local_weight,
                "estimated_total_tokens": estimated_tokens,
            },
            None,
            None,
        )

    def terminal_published(_paths: Any) -> bool:
        current = read_current_control(
            paths,
            run_id=str(config.run.run_id),
            max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
        )
        return current is not None and current.terminal is not None

    def adoption_context() -> AdoptionContext:
        return AdoptionContext(
            model=model,
            paths=paths,
            param_index=param_index,
            device=device,
            config=config,
            logger=telemetry,
            last_loaded_global_version=base_version,
            last_loaded_latest=latest_metadata,
            tokens_since_global_load=tokens_since_global_load,
            read_latest_if_newer_fn=read_newer,
            wait_for_latest_if_newer_fn=wait_newer,
            adopt_global_fn=adopt_global,
            rebase_local_delta_fn=rebase_local_delta,
            snapshot_model_fn=snapshot_model,
            prepare_prediction_fn=prepare_prediction,
            terminal_published_fn=terminal_published,
        )

    strategy = make_global_adoption_strategy(config)
    fence = admission.fence

    def acknowledge_terminal(
        drain: dict[str, Any],
        *,
        final_cycle_seq: int,
        final_update_id: str | None,
    ) -> None:
        ack_path = publish_terminal_ack(
            paths,
            run_id=str(config.run.run_id),
            descriptor_sha256=str(loaded.descriptor["descriptor_sha256"]),
            generation=int(drain["generation"]),
            actor_id=actor_id,
            attempt_id=attempt_id,
            fence=fence,
            final_cycle_seq=final_cycle_seq,
            final_update_id=final_update_id,
        )
        telemetry.event(
            "terminal_ack_published",
            generation=int(drain["generation"]),
            final_cycle_seq=final_cycle_seq,
            final_update_id=final_update_id,
            path=str(ack_path),
        )
        deadline = time.monotonic() + loaded.config.leader.learner_recovery_wait_seconds
        while time.monotonic() < deadline:
            observed = read_current_control(
                paths,
                run_id=str(config.run.run_id),
                max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
            )
            if observed is not None and observed.terminal is not None:
                telemetry.event("terminal_observed", terminal=observed.terminal)
                return
            time.sleep(config.sync.scan_interval_seconds)
        raise TimeoutError("terminal acknowledgement was not finalized by a current leader")

    def wait_for_ingestion(
        receipt: CycleReceiptV1,
        *,
        final_update_id: str | None,
    ) -> bool:
        barrier = wait_for_receipt_barrier(
            paths,
            run_id=str(config.run.run_id),
            descriptor_sha256=str(loaded.descriptor["descriptor_sha256"]),
            receipt=receipt,
            timeout_seconds=loaded.config.leader.learner_recovery_wait_seconds,
            poll_seconds=config.sync.scan_interval_seconds,
            max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
        )
        if barrier.kind == "ack":
            telemetry.event(
                "receipt_ingestion_acknowledged",
                cycle_seq=receipt.cycle_seq,
                receipt_id=receipt.receipt_id,
                leader_epoch=int(barrier.payload["epoch"]),
            )
            return False
        if barrier.kind == "terminal":
            telemetry.event("terminal_observed", terminal=barrier.payload)
            return True
        acknowledge_terminal(
            barrier.payload,
            final_cycle_seq=receipt.cycle_seq,
            final_update_id=final_update_id,
        )
        return True

    cursor = admission.resume.cursor
    shard_index = fence.stream_id
    shard_count = config.membership.stream_pool_size
    dataset_identity_sha256 = hashlib.sha256(
        json.dumps(
            loaded.descriptor["dataset_identity"],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    indexed_cursor = IndexedBlockCursor(
        stable_contributor_key=fence.stable_contributor_key,
        dataset_identity_sha256=dataset_identity_sha256,
        seed=int(config.training.seed),
        block_index=cursor,
        shard_index=shard_index,
        shard_count=shard_count,
    )
    batch_iterator = build_indexed_batch_iterator(
        config,
        tokenizer,
        cursor=indexed_cursor,
    )
    cycle_seq = admission.resume.next_cycle_seq
    previous_receipt_id = admission.resume.last_receipt_id
    previous_receipt_sha256 = admission.resume.last_receipt_sha256
    last_cycle_update_id = admission.resume.last_update_id
    local_step = resumed_local_steps
    while True:
        current = read_current_control(
            paths,
            run_id=str(config.run.run_id),
            max_clock_skew_seconds=loaded.config.leader.max_clock_skew_seconds,
        )
        if current is not None and current.terminal is not None:
            telemetry.event("terminal_observed", terminal=current.terminal)
            return
        if current is not None and current.drain is not None:
            acknowledge_terminal(
                current.drain,
                final_cycle_seq=cycle_seq - 1,
                final_update_id=last_cycle_update_id,
            )
            return
        cycle_id = str(uuid.uuid4())
        update_id = str(uuid.uuid4())
        cursor_start = cursor
        accounting = TrainingSegmentAccumulator(
            base_global_version=base_version,
            interval_start_step=local_step,
        )
        model.train()
        for _ in range(config.training.inner_steps):
            optimizer.zero_grad(set_to_none=True)
            step_tokens = 0
            step_examples = 0
            step_loss = 0.0
            for _micro in range(config.training.gradient_accumulation_steps):
                batch = next(batch_iterator).to(device)
                cursor += 1
                output = model(input_ids=batch.input_ids, labels=batch.labels)
                if output.loss is None:
                    raise RuntimeError("model did not return a training loss")
                loss = output.loss / config.training.gradient_accumulation_steps
                loss.backward()
                step_loss += float(output.loss.detach().cpu())
                step_tokens += batch.num_tokens
                step_examples += batch.num_examples
            grad_norm = None
            if config.training.grad_clip is not None:
                grad_norm = float(
                    torch.nn.utils.clip_grad_norm_(model.parameters(), config.training.grad_clip)
                    .detach()
                    .cpu()
                )
            optimizer.step()
            if scheduler is not None:
                scheduler.step()
            local_step += 1
            accounting.record_step(
                local_step_end=local_step,
                tokens=step_tokens,
                examples=step_examples,
                loss=step_loss / config.training.gradient_accumulation_steps,
                grad_norm=grad_norm,
            )
            tokens_since_global_load += step_tokens
            strategy.on_local_tokens(step_tokens)
            if strategy.wants_inner_poll(config):
                newer = read_newer(paths, base_version)
                if newer is not None:
                    action = strategy.on_newer_latest(adoption_context(), newer)
                    if action.adoption is not None:
                        accounting.replace_base(
                            new_base_global_version=action.adoption.version,
                            retain_effective_work=action.adoption.preserve_inner_state,
                        )
                    (
                        optimizer,
                        scheduler,
                        base_version,
                        adopted_latest,
                        adopted_tokens,
                    ) = _apply_strategy_action(
                        action,
                        model=model,
                        config=config,
                        telemetry=telemetry,
                        optimizer=optimizer,
                        scheduler=scheduler,
                        completed_local_steps=local_step,
                        current_version=base_version,
                    )
                    if adopted_latest is not None:
                        latest_metadata = adopted_latest
                    if adopted_tokens is not None:
                        tokens_since_global_load = adopted_tokens
        cycle_end_action = strategy.on_cycle_end(adoption_context())
        if cycle_end_action.adoption is not None:
            accounting.replace_base(
                new_base_global_version=cycle_end_action.adoption.version,
                retain_effective_work=cycle_end_action.adoption.preserve_inner_state,
            )
        (
            optimizer,
            scheduler,
            base_version,
            adopted_latest,
            adopted_tokens,
        ) = _apply_strategy_action(
            cycle_end_action,
            model=model,
            config=config,
            telemetry=telemetry,
            optimizer=optimizer,
            scheduler=scheduler,
            completed_local_steps=local_step,
            current_version=base_version,
        )
        if adopted_latest is not None:
            latest_metadata = adopted_latest
        if adopted_tokens is not None:
            tokens_since_global_load = adopted_tokens
        cycle_accounting = accounting.finalize_cycle()
        proposal_expected = cycle_accounting.proposal_expected
        payload_relative = None
        payload_publication = None
        schema_digest = None
        local_theta = None
        if proposal_expected:
            strategy.before_publish(adoption_context())
            local_theta = flatten_trainable_params(model, param_index, dtype=torch.float32)
            payload_relative = canonical_update_relative_path(
                fence.stable_contributor_key, update_id
            )
            payload_publication = publish_safetensors_immutable(
                paths.shared_root / payload_relative,
                {"local_params": local_theta.to(dtype=dtype_from_name(config.io.tensor_dtype))},
            )
            schema_digest = tensor_schema_sha256(
                [
                    {
                        "key": "local_params",
                        "dtype": config.io.tensor_dtype,
                        "shape": [int(local_theta.numel())],
                    }
                ]
            )
        receipt = CycleReceiptV1.from_dict(
            {
                "cycle_receipt_format_version": CYCLE_RECEIPT_FORMAT_VERSION,
                "run_id": config.run.run_id,
                "stable_contributor_key": fence.stable_contributor_key,
                "cycle_seq": cycle_seq,
                "cycle_id": cycle_id,
                "receipt_id": f"receipt-{fence.stable_contributor_key}-{cycle_seq}",
                "previous_receipt_id": previous_receipt_id,
                "previous_receipt_sha256": previous_receipt_sha256,
                "processed_tokens_this_cycle": cycle_accounting.processed_tokens,
                "effective_tokens_this_cycle": cycle_accounting.effective_tokens,
                "local_discarded_tokens_this_cycle": (cycle_accounting.local_discarded_tokens),
                "retained_tokens_since_base": (cycle_accounting.retained_tokens_since_base),
                "data_cursor_start": cursor_start,
                "data_cursor_end": cursor,
                "proposal_expected": proposal_expected,
                "planned_update_id": update_id if proposal_expected else None,
                "planned_payload_sha256": (
                    payload_publication.sha256 if payload_publication is not None else None
                ),
                "contributor_fence": fence.as_dict(),
                "created_at": time.time(),
            }
        )
        receipt_path = paths.shared_root / canonical_receipt_relative_path(fence, receipt.cycle_seq)
        publish_immutable_bytes(receipt_path, receipt.canonical_bytes() + b"\n")
        previous_receipt_id = receipt.receipt_id
        previous_receipt_sha256 = receipt.immutable_sha256()
        cycle_seq += 1
        if not proposal_expected:
            last_cycle_update_id = None
            telemetry.event(
                "receipt_only_cycle_published",
                cycle_seq=receipt.cycle_seq,
                processed_tokens=cycle_accounting.processed_tokens,
                local_discarded_tokens=cycle_accounting.local_discarded_tokens,
                data_cursor=cursor,
            )
            if wait_for_ingestion(receipt, final_update_id=None):
                return
            continue
        assert local_theta is not None
        assert payload_relative is not None
        assert payload_publication is not None
        assert schema_digest is not None
        proposal = FullUpdateProposalV2.from_dict(
            {
                "proposal_format_version": PROPOSAL_FORMAT_VERSION,
                "run_id": config.run.run_id,
                "stable_contributor_key": fence.stable_contributor_key,
                "cycle_seq": receipt.cycle_seq,
                "cycle_id": cycle_id,
                "update_id": update_id,
                "cycle_receipt_id": receipt.receipt_id,
                "cycle_receipt_sha256": receipt.immutable_sha256(),
                "base_global_version": base_version,
                "local_step_start": cycle_accounting.segment.local_step_start,
                "local_step_end": local_step,
                "inner_steps": (local_step - cycle_accounting.segment.local_step_start),
                "processed_tokens_this_cycle": cycle_accounting.processed_tokens,
                "effective_tokens_this_update": cycle_accounting.effective_tokens,
                "local_discarded_tokens_this_cycle": (cycle_accounting.local_discarded_tokens),
                "retained_tokens_since_base": (cycle_accounting.retained_tokens_since_base),
                "data_cursor_start": cursor_start,
                "data_cursor_end": cursor,
                "contributor_fence": fence.as_dict(),
                "payload_relative_path": payload_relative,
                "payload_size": payload_publication.size_bytes,
                "payload_sha256": payload_publication.sha256,
                "tensor_schema_sha256": schema_digest,
                "tensor_dtype": config.io.tensor_dtype,
                "tensor_numel": int(local_theta.numel()),
                "created_at": time.time(),
            }
        )
        proposal_path = (
            paths.shared_root
            / "updates"
            / "proposals"
            / fence.stable_contributor_key
            / f"{update_id}.json"
        )
        publish_immutable_bytes(proposal_path, proposal.canonical_bytes() + b"\n")
        atomic_write_json(
            paths.update_pointer_path(fence.stable_contributor_key),
            {
                "format_version": PROPOSAL_POINTER_FORMAT_VERSION,
                "run_id": config.run.run_id,
                "stable_contributor_key": fence.stable_contributor_key,
                "cycle_seq": receipt.cycle_seq,
                "update_id": update_id,
                "proposal_path": proposal_path.relative_to(paths.shared_root).as_posix(),
                "proposal_sha256": proposal.immutable_sha256(),
                "fence": fence.as_dict(),
            },
        )
        telemetry.event(
            "proposal_published",
            cycle_seq=receipt.cycle_seq,
            update_id=update_id,
            base_global_version=base_version,
            processed_tokens=cycle_accounting.processed_tokens,
            processed_examples=cycle_accounting.processed_examples,
            effective_tokens=cycle_accounting.effective_tokens,
            local_discarded_tokens=cycle_accounting.local_discarded_tokens,
            mean_loss=cycle_accounting.segment.mean_loss,
            data_cursor=cursor,
        )
        last_cycle_update_id = update_id
        if wait_for_ingestion(receipt, final_update_id=update_id):
            return
        if config.learner.adopt_global_after_upload:
            action = strategy.on_after_publish(
                adoption_context(),
                PublishResult(update_id=update_id, base_global_version=base_version),
            )
            (
                optimizer,
                scheduler,
                base_version,
                adopted_latest,
                adopted_tokens,
            ) = _apply_strategy_action(
                action,
                model=model,
                config=config,
                telemetry=telemetry,
                optimizer=optimizer,
                scheduler=scheduler,
                completed_local_steps=local_step,
                current_version=base_version,
            )
            if adopted_latest is not None:
                latest_metadata = adopted_latest
            if adopted_tokens is not None:
                tokens_since_global_load = adopted_tokens
