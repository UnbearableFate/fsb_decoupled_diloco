"""Mandatory fenced Full Protocol v4 syncer composition and merge loop."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

import torch

from ..core.run_descriptor import LoadedRunDescriptor, write_actor_attestation
from ..core.versions import CONTROL_FORMAT_VERSION
from ..modeling.hf_model import load_causal_lm_and_tokenizer
from ..modeling.outer_optim import init_outer_state, outer_optimizer_step
from ..modeling.param_index import build_param_index, flatten_trainable_params
from ..observability.logging_utils import ActorTelemetryWriter
from ..protocol.admission_v4 import (
    ADMISSION_RESPONSE_FORMAT_VERSION,
    AdmissionAuthorizationError,
    AdmissionRequestObservation,
    admission_request_error,
    admission_request_sha256,
    archive_disposed_admission_request,
    dispose_invalid_admission_request,
    iter_admission_requests,
    publish_admission_response,
    publish_admission_disposition,
    publish_admission_rejection,
    read_static_replacement_authorization,
)
from ..protocol.authority import MergeFenceConflict
from ..protocol.control_v4 import V4ControlPublisher, iter_terminal_acks
from ..protocol.contributor import (
    ContributorFence,
    StaticContributorFence,
    decode_contributor_fence,
)
from ..protocol.cycle_receipt import (
    CycleReceiptV1,
    canonical_receipt_id,
    canonical_receipt_relative_path,
    contributor_fence_namespace,
)
from ..protocol.data_cursor import ContributorResumeState
from ..protocol.merge import normalized_update_weights, weighted_average_tensors
from ..protocol.proposal import FullUpdateProposalV2
from ..storage.atomic_io import atomic_write_json, publish_immutable_bytes, safe_read_json
from ..storage.authority import (
    CommandConflictError,
    LeaderAuthority,
    LeaderSession,
    MembershipFenceError,
)
from ..storage.tensor_codec import (
    dtype_from_name,
    load_outer_state,
    load_update_vector,
    publish_global_weights_immutable,
    publish_outer_state_immutable,
)


PLAN03_REQUIREMENTS = frozenset(
    {
        "AUTH-02",
        "AUTH-03",
        "AUTH-04",
        "AUTH-05",
        "AUTH-07",
        "AUTH-09",
        "AUTH-10",
        "MODE-02",
        "P4-MIGRATE",
    }
)


def _raise_injected_candidate_failure(version: int) -> None:
    raw = os.environ.get("FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION")
    if raw is None:
        return
    try:
        target = int(raw)
    except ValueError as exc:
        raise ValueError(
            "FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION must be a nonnegative integer"
        ) from exc
    if target < 0:
        raise ValueError(
            "FS_DILOCO_TEST_FAIL_AFTER_COMMITTED_VERSION must be a nonnegative integer"
        )
    if int(version) >= target:
        raise RuntimeError(f"injected candidate failure after committed version {version}")


def run_fenced_syncer(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
    control: V4ControlPublisher,
    *,
    attempt_id: str,
    renewer: Any,
) -> None:
    config_v4 = loaded.config
    config = config_v4.shared
    paths = loaded.paths
    token = leader.token
    telemetry = ActorTelemetryWriter(
        paths.actor_metrics_path("syncer", token.owner_id, attempt_id),
        actor_kind="syncer",
        actor_id=token.owner_id,
        attempt_id=attempt_id,
    )
    device = torch.device(
        "cuda"
        if config.syncer.device == "cuda"
        else ("cuda" if config.syncer.device == "auto" and torch.cuda.is_available() else "cpu")
    )
    write_actor_attestation(
        loaded,
        actor_kind="syncer",
        actor_id=token.owner_id,
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
            },
        },
        scheduler_job_id=os.environ.get("PBS_JOBID"),
        accelerator_identity=os.environ.get("CUDA_VISIBLE_DEVICES") or str(device),
    )
    telemetry.event("leadership_acquired", epoch=token.epoch, device=str(device))
    renewer.raise_if_failed()
    leader.reconcile_publications(command_id=f"reconcile-publications-e{token.epoch}")
    if config.membership.mode == "dynamic":
        leader.initialize_dynamic_membership(
            command_id=f"initialize-dynamic-membership-e{token.epoch}"
        )
    latest = authority.read.latest_committed_version()
    if latest is None:
        latest, theta, outer_state, param_index = _initialize_v0(loaded, leader, device=device)
        telemetry.event("v0_initialized", publication_id=latest.publication_id)
    else:
        param_index = json.loads(paths.param_index_json.read_text(encoding="utf-8"))
        theta, outer_state = load_outer_state(
            paths.shared_root / latest.optim_relative_path,
            device=device,
            dtype=dtype_from_name(config.syncer.compute_dtype),
        )
        telemetry.event("authority_resumed", version=latest.version)
    control.publish_latest(latest)
    _raise_injected_candidate_failure(latest.version)
    terminal = authority.read.terminal_record()
    if terminal is not None:
        control.publish_terminal(terminal)
        return
    poll_seconds = float(config.sync.scan_interval_seconds)
    selection_sequence = 0
    while True:
        renewer.raise_if_failed()
        _admit_requests(loaded, authority, leader, telemetry)
        _ingest_proposals(loaded, authority, leader, control, telemetry)
        latest = authority.read.latest_committed_version()
        assert latest is not None
        if _stop_reached(loaded, authority, latest.version):
            terminal = _finalize(
                loaded,
                authority,
                leader,
                control=control,
                telemetry=telemetry,
                reason="configured_target",
            )
            control.publish_terminal(terminal)
            telemetry.event("terminal_finalized", terminal=terminal)
            return
        selection_sequence += 1
        selection = leader.try_select_batch(
            command_id=(f"select-e{token.epoch}-n{selection_sequence}"),
            quorum_min=config.sync.quorum_min,
            quorum_max=config.sync.quorum_max,
        )
        if selection.batch is None:
            time.sleep(poll_seconds)
            continue
        batch = selection.batch
        updates = [
            {
                "update_id": item.proposal.update_id,
                "tokens_this_update": item.proposal.effective_tokens_this_update,
                "base_global_version": item.proposal.base_global_version,
            }
            for item in batch.candidates
        ]
        weights_by_id = normalized_update_weights(
            updates,
            current_version=latest.version,
            staleness_lambda=config.sync.staleness_lambda,
        )
        vectors = [
            load_update_vector(
                paths.shared_root / item.proposal.payload_relative_path,
                device=device,
                dtype=dtype_from_name(config.syncer.compute_dtype),
            )
            for item in batch.candidates
        ]
        p_bar = weighted_average_tensors(
            vectors,
            [weights_by_id[item.proposal.update_id] for item in batch.candidates],
        )
        gradient = theta - p_bar
        next_theta, next_outer = outer_optimizer_step(
            theta, gradient, outer_state, config.outer_optimizer
        )
        publication_id = str(uuid.uuid4())
        target_version = batch.target_version
        weight_path = paths.epoch_weight_path(
            token.epoch, token.owner_id, target_version, publication_id
        )
        optim_path = paths.epoch_outer_optim_path(
            token.epoch, token.owner_id, target_version, publication_id
        )
        weight, weight_theta_sha = publish_global_weights_immutable(
            weight_path,
            next_theta,
            param_index,
            dtype=dtype_from_name(config.syncer.publish_dtype),
        )
        optim, optim_theta_sha = publish_outer_state_immutable(
            optim_path,
            next_theta,
            next_outer,
            dtype=dtype_from_name(config.syncer.publish_dtype),
        )
        leader.prepare_publication(
            command_id=f"prepare-{publication_id}",
            publication_id=publication_id,
            target_version=target_version,
            selection_batch_id=batch.batch_id,
            weight_relative_path=paths.relative(weight.path),
            weight_size=weight.size_bytes,
            weight_sha256=weight.sha256,
            optim_relative_path=paths.relative(optim.path),
            optim_size=optim.size_bytes,
            optim_sha256=optim.sha256,
            weight_theta_sha256=weight_theta_sha,
            optim_theta_sha256=optim_theta_sha,
        )
        committed = leader.commit_merge(
            command_id=f"commit-{publication_id}", publication_id=publication_id
        )
        if isinstance(committed, MergeFenceConflict):
            telemetry.event(
                "merge_fence_conflict",
                publication_id=publication_id,
                invalid_update_ids=committed.invalid_update_ids,
            )
            latest = authority.read.latest_committed_version()
            assert latest is not None
            theta, outer_state = load_outer_state(
                paths.shared_root / latest.optim_relative_path,
                device=device,
                dtype=dtype_from_name(config.syncer.compute_dtype),
            )
            continue
        latest = committed
        theta, outer_state = load_outer_state(
            paths.shared_root / committed.optim_relative_path,
            device=device,
            dtype=dtype_from_name(config.syncer.compute_dtype),
        )
        control.publish_latest(committed)
        telemetry.event(
            "version_committed",
            version=committed.version,
            publication_id=committed.publication_id,
            selected_update_ids=[item.proposal.update_id for item in batch.candidates],
        )
        _raise_injected_candidate_failure(committed.version)


def _initialize_v0(
    loaded: LoadedRunDescriptor,
    leader: LeaderSession,
    *,
    device: torch.device,
) -> tuple[Any, torch.Tensor, dict[str, torch.Tensor], dict[str, Any]]:
    config = loaded.config.shared
    model, _tokenizer = load_causal_lm_and_tokenizer(config.model)
    model.to(device)
    param_index = build_param_index(model, model_name_or_path=config.model.name_or_path)
    param_index_data = (
        json.dumps(param_index, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    publish_immutable_bytes(loaded.paths.param_index_json, param_index_data)
    theta = flatten_trainable_params(
        model,
        param_index,
        dtype=dtype_from_name(config.syncer.compute_dtype),
        device=device,
    )
    outer_state = init_outer_state(theta, config.outer_optimizer)
    publication_id = str(uuid.uuid4())
    weight_path = loaded.paths.epoch_weight_path(
        leader.token.epoch, leader.token.owner_id, 0, publication_id
    )
    optim_path = loaded.paths.epoch_outer_optim_path(
        leader.token.epoch, leader.token.owner_id, 0, publication_id
    )
    weight, weight_theta_sha = publish_global_weights_immutable(
        weight_path,
        theta,
        param_index,
        dtype=dtype_from_name(config.syncer.publish_dtype),
    )
    optim, optim_theta_sha = publish_outer_state_immutable(
        optim_path,
        theta,
        outer_state,
        dtype=dtype_from_name(config.syncer.publish_dtype),
    )
    committed = leader.initialize_v0(
        command_id=f"initialize-v0-{publication_id}",
        publication_id=publication_id,
        weight_relative_path=loaded.paths.relative(weight.path),
        weight_size=weight.size_bytes,
        weight_sha256=weight.sha256,
        optim_relative_path=loaded.paths.relative(optim.path),
        optim_size=optim.size_bytes,
        optim_sha256=optim.sha256,
        weight_theta_sha256=weight_theta_sha,
        optim_theta_sha256=optim_theta_sha,
    )
    committed_theta, committed_outer_state = load_outer_state(
        loaded.paths.shared_root / committed.optim_relative_path,
        device=device,
        dtype=dtype_from_name(config.syncer.compute_dtype),
    )
    return committed, committed_theta, committed_outer_state, param_index


def _admit_requests(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
    telemetry: ActorTelemetryWriter,
) -> None:
    _repair_current_admission_controls(loaded, authority, leader)
    for observation in iter_admission_requests(loaded.paths):
        if observation.original is None:
            telemetry.event(
                "admission_request_deferred",
                request_path=str(observation.path),
                error_type=observation.read_error_type,
                error_errno=observation.read_errno,
            )
            continue
        try:
            _admit_observations_unprotected(loaded, authority, leader, telemetry, (observation,))
        except (OSError, RuntimeError) as exc:
            telemetry.event(
                "admission_request_deferred",
                request_path=str(observation.path),
                error_type=type(exc).__name__,
                error=str(exc)[:256],
            )


def _admit_observations_unprotected(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
    telemetry: ActorTelemetryWriter,
    observations: tuple[AdmissionRequestObservation, ...],
) -> None:
    descriptor = loaded.descriptor
    for observation in observations:
        path = observation.path
        request = observation.payload
        original = observation.original
        identity = observation.identity
        if original is None or identity is None:
            raise RuntimeError("readable admission observation lost bytes or identity")
        invalid = (
            ("MalformedAdmissionRequest", "request is not a JSON object")
            if request is None
            else admission_request_error(
                request,
                run_id=str(descriptor["run_id"]),
                descriptor_sha256=str(descriptor["descriptor_sha256"]),
            )
        )
        if invalid is not None:
            error_type, message = invalid
            disposition = dispose_invalid_admission_request(
                loaded.paths,
                request_path=path,
                run_id=str(descriptor["run_id"]),
                descriptor_sha256=str(descriptor["descriptor_sha256"]),
                epoch=leader.token.epoch,
                owner_id=leader.token.owner_id,
                error_type=error_type,
                message=message,
                original=original,
                identity=identity,
            )
            telemetry.event(
                "admission_request_discarded",
                request_path=str(path),
                disposition_path=str(disposition),
                error_type=error_type,
            )
            continue
        request_sha = admission_request_sha256(request)
        disposition_path = loaded.paths.registration_disposition_path(request_sha)
        if disposition_path.is_file():
            archive_disposed_admission_request(
                loaded.paths,
                request_path=path,
                request=request,
                original=original,
                identity=identity,
            )
            continue
        command_id = f"admit-{request_sha}"
        try:
            if request.get("mode") == "static":
                prior = authority.read.static_binding(str(request["learner_id"]))
                if (
                    prior is not None
                    and prior.status == "active"
                    and prior.logical_launch_id == str(request["logical_launch_id"])
                    and prior.attempt_id == str(request["attempt_id"])
                ):
                    binding = prior
                else:
                    authorization = None
                    if prior is not None:
                        prior_fence = StaticContributorFence(
                            kind="static",
                            learner_id=prior.learner_id,
                            logical_launch_id=prior.logical_launch_id,
                            attempt_id=prior.attempt_id,
                            binding_generation=prior.binding_generation,
                        )
                        if prior.status == "active" or (
                            prior.logical_launch_id != str(request["logical_launch_id"])
                        ):
                            authorization = read_static_replacement_authorization(
                                loaded.paths,
                                request=request,
                                current_fence=prior_fence,
                            )
                    binding = leader.bind_or_replace_static_attempt(
                        command_id=command_id,
                        learner_id=str(request["learner_id"]),
                        logical_launch_id=str(request["logical_launch_id"]),
                        attempt_id=str(request["attempt_id"]),
                        expected_generation=request["expected_generation"],
                        allow_logical_replacement=authorization is not None,
                        replacement_reason=(
                            f"operator_static_replacement:{authorization[1]}"
                            if prior is not None
                            and prior.status == "active"
                            and authorization is not None
                            else None
                        ),
                    )
                fence = StaticContributorFence(
                    kind="static",
                    learner_id=binding.learner_id,
                    logical_launch_id=binding.logical_launch_id,
                    attempt_id=binding.attempt_id,
                    binding_generation=binding.binding_generation,
                )
                progress = authority.read.contributor_progress(binding.learner_id)
                resume = ContributorResumeState(
                    cursor=0 if progress is None else progress.data_cursor,
                    last_receipt_id=None if progress is None else progress.last_receipt_id,
                    last_receipt_sha256=(
                        None if progress is None else progress.last_receipt_sha256
                    ),
                    next_cycle_seq=1 if progress is None else progress.last_cycle_seq + 1,
                )
            elif request.get("mode") == "dynamic":
                admission = leader.admit_dynamic_incarnation(
                    command_id=command_id,
                    instance_id=str(request["instance_id"]),
                    placement_id=str(request["placement_id"]),
                    stream_id=int(request["stream_id"]),
                    admission_token_sha256=str(request["admission_token_sha256"]),
                    hostname=str(request["hostname"]),
                    pid=int(request["pid"]),
                    launch_request_id=request.get("launch_request_id"),
                    replace_instance_id=request.get("replace_instance_id"),
                    replacement_reason=(
                        "explicit_dynamic_replacement"
                        if request.get("replace_instance_id") is not None
                        else None
                    ),
                )
                fence = admission.fence
                resume = admission.resume
            else:
                raise ValueError("unknown admission request mode")
        except (
            AdmissionAuthorizationError,
            CommandConflictError,
            MembershipFenceError,
            ValueError,
        ) as exc:
            rejection = publish_admission_rejection(
                loaded.paths,
                epoch=leader.token.epoch,
                owner_id=leader.token.owner_id,
                request=request,
                error_type=type(exc).__name__,
                message=str(exc),
            )
            publish_admission_disposition(
                loaded.paths,
                request=request,
                epoch=leader.token.epoch,
                owner_id=leader.token.owner_id,
                outcome="rejected",
                control_path=rejection,
                error_type=type(exc).__name__,
            )
            archive_disposed_admission_request(
                loaded.paths,
                request_path=path,
                request=request,
                original=original,
                identity=identity,
            )
            telemetry.event(
                "admission_rejected",
                request_path=str(path),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
        if fence not in authority.read.current_contributor_fences():
            raise RuntimeError("admission command returned a fence that is no longer current")
        actor_id = fence.learner_id if fence.kind == "static" else fence.instance_id
        attempt_id = fence.attempt_id if fence.kind == "static" else fence.instance_id
        persisted_resume = _existing_admission_resume(
            loaded,
            leader,
            fence=fence,
            actor_id=actor_id,
            attempt_id=attempt_id,
        )
        if persisted_resume is not None:
            resume = persisted_resume
        response = publish_admission_response(
            loaded.paths,
            epoch=leader.token.epoch,
            owner_id=leader.token.owner_id,
            request=request,
            fence=fence,
            resume=resume,
        )
        publish_admission_disposition(
            loaded.paths,
            request=request,
            epoch=leader.token.epoch,
            owner_id=leader.token.owner_id,
            outcome="admitted",
            control_path=response,
            fence=fence,
        )
        archive_disposed_admission_request(
            loaded.paths,
            request_path=path,
            request=request,
            original=original,
            identity=identity,
        )
        telemetry.event("learner_admitted", request_path=str(path), response_path=str(response))


def _repair_current_admission_controls(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
) -> None:
    descriptor = loaded.descriptor
    current_fences = authority.read.current_contributor_fences()
    current_keys = {fence.stable_contributor_key for fence in current_fences}
    current_directory = (
        loaded.paths.epoch_membership_dir(leader.token.epoch, leader.token.owner_id)
        / "admissions_v4"
        / "current"
    )
    if current_directory.is_dir():
        for pointer_path in current_directory.glob("*.json"):
            if pointer_path.stem not in current_keys:
                tombstone = {
                    "format_version": 1,
                    "kind": "superseded",
                    "run_id": descriptor["run_id"],
                    "leader_epoch": leader.token.epoch,
                    "leader_owner_id": leader.token.owner_id,
                    "stable_contributor_key": pointer_path.stem,
                }
                if safe_read_json(pointer_path) != tombstone:
                    atomic_write_json(pointer_path, tombstone)
    for fence in current_fences:
        if fence.kind == "static":
            actor_id = fence.learner_id
            attempt_id = fence.attempt_id
            request = {
                "mode": "static",
                "run_id": descriptor["run_id"],
                "descriptor_sha256": descriptor["descriptor_sha256"],
                "learner_id": fence.learner_id,
                "attempt_id": fence.attempt_id,
            }
        else:
            actor_id = fence.instance_id
            attempt_id = fence.instance_id
            request = {
                "mode": "dynamic",
                "run_id": descriptor["run_id"],
                "descriptor_sha256": descriptor["descriptor_sha256"],
                "instance_id": fence.instance_id,
            }
        resume = _existing_admission_resume(
            loaded,
            leader,
            fence=fence,
            actor_id=actor_id,
            attempt_id=attempt_id,
        )
        if resume is None:
            progress = authority.read.contributor_progress(fence.stable_contributor_key)
            resume = _resume_from_progress(fence, progress)
        publish_admission_response(
            loaded.paths,
            epoch=leader.token.epoch,
            owner_id=leader.token.owner_id,
            request=request,
            fence=fence,
            resume=resume,
        )


def _existing_admission_resume(
    loaded: LoadedRunDescriptor,
    leader: LeaderSession,
    *,
    fence: ContributorFence,
    actor_id: str,
    attempt_id: str,
) -> ContributorResumeState | None:
    response_path = loaded.paths.epoch_admission_response_path(
        leader.token.epoch,
        leader.token.owner_id,
        actor_id,
        attempt_id,
        contributor_fence_namespace(fence),
    )
    existing = safe_read_json(response_path)
    if existing is None:
        if response_path.exists():
            raise RuntimeError("existing admission response is malformed")
        return None
    expected = {
        "format_version": ADMISSION_RESPONSE_FORMAT_VERSION,
        "run_id": loaded.descriptor["run_id"],
        "descriptor_sha256": loaded.descriptor["descriptor_sha256"],
        "actor_id": actor_id,
        "attempt_id": attempt_id,
        "leader_epoch": leader.token.epoch,
        "leader_owner_id": leader.token.owner_id,
    }
    if set(existing) != {*expected, "fence", "resume"} or any(
        existing.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("existing admission response identity is invalid")
    if decode_contributor_fence(existing.get("fence")) != fence:
        raise RuntimeError("existing admission response does not match current fence")
    resume_payload = existing.get("resume")
    if not isinstance(resume_payload, dict) or set(resume_payload) != {
        "cursor",
        "last_receipt_id",
        "last_receipt_sha256",
        "next_cycle_seq",
        "stream_epoch",
    }:
        raise RuntimeError("existing admission response resume state is invalid")
    try:
        return ContributorResumeState(
            cursor=resume_payload["cursor"],
            last_receipt_id=resume_payload["last_receipt_id"],
            last_receipt_sha256=resume_payload["last_receipt_sha256"],
            next_cycle_seq=resume_payload["next_cycle_seq"],
            stream_epoch=resume_payload["stream_epoch"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("existing admission response resume state is invalid") from exc


def _resume_from_progress(fence: ContributorFence, progress: Any) -> ContributorResumeState:
    return ContributorResumeState(
        cursor=0 if progress is None else progress.data_cursor,
        last_receipt_id=None if progress is None else progress.last_receipt_id,
        last_receipt_sha256=None if progress is None else progress.last_receipt_sha256,
        next_cycle_seq=1 if progress is None else progress.last_cycle_seq + 1,
        stream_epoch=fence.stream_epoch if fence.kind == "dynamic" else None,
    )


def _ingest_proposals(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
    control: V4ControlPublisher,
    telemetry: ActorTelemetryWriter,
) -> None:
    receipts_root = loaded.paths.shared_root / "updates" / "receipts"
    for fence in authority.read.current_contributor_fences():
        progress = authority.read.contributor_progress(fence.stable_contributor_key)
        sequences = (
            (1,) if progress is None else (progress.last_cycle_seq, progress.last_cycle_seq + 1)
        )
        for sequence in sequences:
            canonical_path = loaded.paths.shared_root / canonical_receipt_relative_path(
                fence, sequence
            )
            legacy_path = (
                receipts_root
                / fence.stable_contributor_key
                / f"{canonical_receipt_id(fence.stable_contributor_key, sequence)}.json"
            )
            path = canonical_path if canonical_path.exists() else legacy_path
            if not path.exists():
                continue
            try:
                receipt = CycleReceiptV1.from_json(path.read_bytes())
                if receipt.contributor_fence != fence or receipt.cycle_seq != sequence:
                    raise ValueError("receipt object does not match its current fence path")
                leader.ingest_cycle_receipt(
                    command_id=f"receipt-{receipt.immutable_sha256()}", receipt=receipt
                )
                control.publish_receipt_ack(
                    receipt,
                    descriptor_sha256=str(loaded.descriptor["descriptor_sha256"]),
                )
            except Exception as exc:
                telemetry.event(
                    "receipt_ingest_rejected",
                    path=str(path),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
    proposals_root = loaded.paths.shared_root / "updates" / "proposals"
    if proposals_root.is_dir():
        for path in sorted(proposals_root.glob("*/*.json")):
            try:
                proposal = FullUpdateProposalV2.from_json(path.read_bytes())
                disposition = leader.ingest_proposal(
                    command_id=f"proposal-{proposal.immutable_sha256()}", proposal=proposal
                )
                if disposition.value not in {"accepted", "exact_replay"}:
                    telemetry.event(
                        "proposal_disposition",
                        update_id=proposal.update_id,
                        disposition=disposition.value,
                    )
            except Exception as exc:
                telemetry.event(
                    "proposal_ingest_rejected",
                    path=str(path),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )


def _stop_reached(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    version: int,
) -> bool:
    config = loaded.config
    outer_target = config.shared.sync.stop_after_outer_steps
    if outer_target is not None and version >= outer_target:
        return True
    token_target = config.stop_after_direct_weight_tokens_applied
    if token_target is not None:
        return authority.read.token_ledger_summary().direct_applied >= token_target
    return False


def _finalize(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
    *,
    control: V4ControlPublisher,
    telemetry: ActorTelemetryWriter,
    reason: str,
) -> dict[str, Any]:
    controller = authority.read.controller_status()
    cycle_token_budget = (
        int(loaded.config.shared.training.inner_steps)
        * int(loaded.config.shared.training.gradient_accumulation_steps)
        * int(loaded.config.shared.training.micro_batch_size)
        * int(loaded.config.shared.training.block_size)
    )
    if controller["state"] == "open":
        leader.begin_terminal_close(
            command_id=f"terminal-close-{reason}",
            reason=reason,
            hard_crash_cycle_token_budget=cycle_token_budget,
        )
        controller = authority.read.controller_status()
    control.publish_drain(controller)
    deadline = time.monotonic() + loaded.config.leader.learner_recovery_wait_seconds
    while time.monotonic() < deadline:
        _ingest_proposals(loaded, authority, leader, control, telemetry)
        for path, payload, fence in iter_terminal_acks(loaded.paths):
            try:
                generation = payload.get("generation")
                final_cycle_seq = payload.get("final_cycle_seq")
                if (
                    payload.get("format_version") != CONTROL_FORMAT_VERSION
                    or payload.get("kind") != "terminal_ack"
                    or payload.get("run_id") != loaded.descriptor["run_id"]
                    or payload.get("descriptor_sha256") != loaded.descriptor["descriptor_sha256"]
                    or isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation != int(controller["generation"])
                    or isinstance(final_cycle_seq, bool)
                    or not isinstance(final_cycle_seq, int)
                    or final_cycle_seq < 0
                    or not isinstance(payload.get("actor_id"), str)
                    or not isinstance(payload.get("attempt_id"), str)
                    or not (
                        payload.get("final_update_id") is None
                        or isinstance(payload.get("final_update_id"), str)
                    )
                ):
                    continue
                actor_id = payload["actor_id"]
                attempt_id = payload["attempt_id"]
                if fence.kind == "static":
                    if actor_id != fence.learner_id or attempt_id != fence.attempt_id:
                        continue
                elif actor_id != fence.instance_id or attempt_id != fence.instance_id:
                    continue
                ack_digest = hashlib.sha256(path.read_bytes()).hexdigest()
                leader.acknowledge_terminal_contributor(
                    command_id=f"terminal-ack-{ack_digest}",
                    fence=fence,
                    final_cycle_seq=final_cycle_seq,
                    final_update_id=payload.get("final_update_id"),
                )
                telemetry.event(
                    "terminal_ack_ingested",
                    actor_id=actor_id,
                    final_cycle_seq=final_cycle_seq,
                )
            except Exception as exc:
                telemetry.event(
                    "terminal_ack_rejected",
                    path=str(path),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        awaiting = tuple(
            row
            for row in authority.read.terminal_contributor_fences()
            if row["state"] == "awaiting_ack"
        )
        if not awaiting:
            break
        time.sleep(float(loaded.config.shared.sync.scan_interval_seconds))
    for row in authority.read.terminal_contributor_fences():
        if row["state"] != "awaiting_ack":
            continue
        fence = decode_contributor_fence(json.loads(str(row["fence_json"])))
        ack_digest = hashlib.sha256(
            json.dumps(fence.as_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()
        leader.acknowledge_terminal_contributor(
            command_id=f"terminal-hard-crash-{ack_digest}",
            fence=fence,
            final_cycle_seq=None,
            hard_crash_gap_tokens_upper_bound=cycle_token_budget,
        )
        telemetry.event(
            "terminal_hard_crash_adjudicated",
            stable_contributor_key=fence.stable_contributor_key,
            hard_crash_gap_tokens_upper_bound=cycle_token_budget,
        )
    state = authority.read.controller_status()["state"]
    if state not in {"finalized", "error"}:
        leader.finalize_terminal(command_id=f"terminal-finalize-{reason}", reason=reason)
    terminal = authority.read.terminal_record()
    if terminal is None:
        raise RuntimeError("terminal record was not committed")
    return terminal
