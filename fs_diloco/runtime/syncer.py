"""Fenced Full Protocol syncer composition and merge loop."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..core.run_descriptor import LoadedRunDescriptor, write_actor_attestation
from ..observability.logging_utils import ActorTelemetryWriter
from ..storage.admission import (
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
    repair_rejected_admission_control,
    read_static_replacement_authorization,
)
from ..storage.control import ControlPublisher
from ..protocol.contributor import (
    ContributorFence,
    StaticContributorFence,
    decode_contributor_fence,
)
from ..protocol.cycle_receipt import (
    CycleReceiptV1,
    canonical_receipt_relative_path,
    contributor_fence_namespace,
)
from ..protocol.data_cursor import ContributorResumeState
from ..protocol.authority import MergeFenceConflict
from ..protocol.proposal import FullUpdateProposalV2
from ..storage.atomic_io import atomic_write_json, publish_immutable_bytes, safe_read_json
from ..storage.authority import (
    AuthoritySchemaError,
    CommandConflictError,
    LeaderAuthority,
    LeaderSession,
    MembershipFenceError,
)
from ..storage.leader_lease import StaleLeaderTokenError
from .pbs_scheduler import PBSScheduler

if TYPE_CHECKING:
    import torch


class AdmissionInvariantError(RuntimeError):
    """An internal admission invariant failed and must terminate this candidate."""


def _pause_candidate_at_fault_boundary(
    authority: LeaderAuthority,
    leader: LeaderSession,
    renewer: Any,
    *,
    version: int,
) -> None:
    """Expose a deterministic process-fault boundary outside SQLite transactions."""

    raw_target = os.environ.get("FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION")
    if raw_target is None:
        return
    try:
        target = int(raw_target)
    except ValueError as exc:
        raise ValueError(
            "FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION must be a nonnegative integer"
        ) from exc
    if target < 0:
        raise ValueError(
            "FS_DILOCO_FAULT_PAUSE_AFTER_COMMITTED_VERSION must be a nonnegative integer"
        )
    marker_value = os.environ.get("FS_DILOCO_FAULT_PAUSE_MARKER_PATH")
    if not marker_value:
        raise ValueError(
            "FS_DILOCO_FAULT_PAUSE_MARKER_PATH is required with the fault pause boundary"
        )
    marker = Path(marker_value)
    if version < target or marker.exists():
        return
    with renewer.quiesce_for_fault_injection():
        authority.assert_outside_transaction()
        atomic_write_json(
            marker,
            {
                "format_version": 1,
                "pid": os.getpid(),
                "epoch": leader.token.epoch,
                "owner_id": leader.token.owner_id,
                "committed_version": int(version),
                "sqlite_transaction_active": False,
                "lease_renewer_quiesced": True,
            },
        )
        os.kill(os.getpid(), signal.SIGSTOP)
    authority.assert_outside_transaction()


def run_fenced_syncer(
    loaded: LoadedRunDescriptor,
    authority: LeaderAuthority,
    leader: LeaderSession,
    control: ControlPublisher,
    *,
    attempt_id: str,
    renewer: Any,
    telemetry: ActorTelemetryWriter,
) -> None:
    import torch

    from ..storage.tensor_codec import dtype_from_name, load_outer_state
    from .services import (
        DynamicCapacityService,
        MaintenanceService,
        MergeAttemptStatus,
        MergeService,
        TerminalService,
        terminal_close_reason,
    )

    config = loaded.config
    paths = loaded.paths
    token = leader.token
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
    telemetry.event("syncer_runtime_ready", epoch=token.epoch, device=str(device))
    renewer.raise_if_failed()
    leader.reconcile_publications(command_id=f"reconcile-publications-e{token.epoch}")
    latest = authority.read.latest_committed_version()
    if latest is None:
        latest, theta, outer_state, param_index = _initialize_genesis(
            loaded,
            leader,
            device=device,
        )
        telemetry.event("genesis_initialized", publication_id=latest.publication_id)
    else:
        param_index = json.loads(paths.param_index_json.read_text(encoding="utf-8"))
        theta, outer_state = load_outer_state(
            paths.shared_root / latest.optim_relative_path,
            device=device,
            dtype=dtype_from_name(config.syncer.compute_dtype),
        )
        telemetry.event("authority_resumed", version=latest.version)
    control.publish_latest(latest)
    _pause_candidate_at_fault_boundary(
        authority,
        leader,
        renewer,
        version=latest.version,
    )
    terminal = authority.read.terminal_record()
    if terminal is not None:
        control.publish_terminal(terminal)
        return
    poll_seconds = float(config.sync.scan_interval_seconds)
    merge_service = MergeService(
        loaded=loaded,
        authority=authority,
        leader=leader,
        control=control,
        telemetry=telemetry,
        theta=theta,
        outer_state=outer_state,
        param_index=param_index,
        device=device,
    )
    capacity_service = (
        DynamicCapacityService(
            authority=authority,
            leader=leader,
            scheduler=PBSScheduler(),
            scaling=config.scaling,
            membership=config.membership,
            shared_root=paths.shared_root,
            descriptor_sha256=str(loaded.descriptor["descriptor_sha256"]),
        )
        if config.membership.mode == "dynamic" and config.scaling.enabled
        else None
    )
    maintenance_service = MaintenanceService(
        authority=authority,
        leader=leader,
        paths=paths,
        config=loaded.config.maintenance,
        telemetry=telemetry,
    )
    while True:
        renewer.raise_if_failed()
        _admit_requests(loaded, authority, leader, telemetry)
        _ingest_proposals(loaded, authority, leader, control, telemetry)
        latest = authority.read.latest_committed_version()
        assert latest is not None
        close_reason = terminal_close_reason(loaded, authority, version=latest.version)
        if close_reason is not None:
            terminal_service = TerminalService(
                loaded=loaded,
                authority=authority,
                leader=leader,
                control=control,
                telemetry=telemetry,
                merge=merge_service,
                ingest=lambda: _ingest_proposals(loaded, authority, leader, control, telemetry),
                admit_preclose=lambda cutoff: _admit_requests(
                    loaded,
                    authority,
                    leader,
                    telemetry,
                    created_at_lte=cutoff,
                ),
            )
            terminal = terminal_service.finalize(reason=close_reason)
            control.publish_terminal(terminal)
            maintenance_service.tick(force=True)
            telemetry.event("terminal_finalized", terminal=terminal)
            return
        outcome = merge_service.merge_once(
            quorum_min=config.sync.quorum_min,
            quorum_max=config.sync.quorum_max,
            purpose="normal",
        )
        if capacity_service is not None:
            current = authority.read.latest_committed_version()
            assert current is not None
            actions = capacity_service.tick(
                global_version=current.version,
                eligible_contributors=len(authority.read.current_contributor_fences()),
                selected_contributors=merge_service.last_selected_contributors,
            )
            for action in actions:
                telemetry.event("dynamic_capacity_action", action=action)
        if isinstance(outcome, MergeAttemptStatus):
            if outcome is MergeAttemptStatus.NO_BATCH:
                time.sleep(poll_seconds)
        else:
            maintenance_service.tick()
            _pause_candidate_at_fault_boundary(
                authority,
                leader,
                renewer,
                version=outcome.version,
            )


def _initialize_genesis(
    loaded: LoadedRunDescriptor,
    leader: LeaderSession,
    *,
    device: torch.device,
) -> tuple[Any, torch.Tensor, dict[str, torch.Tensor], dict[str, Any]]:
    from ..modeling.hf_model import load_causal_lm_and_tokenizer
    from ..modeling.outer_optim import init_outer_state
    from ..modeling.param_index import build_param_index, flatten_trainable_params
    from ..storage.tensor_codec import (
        dtype_from_name,
        encode_global_weights,
        encode_outer_state,
        load_outer_state,
    )

    config = loaded.config
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
    weight_payload, weight_theta_sha = encode_global_weights(
        theta,
        param_index,
        dtype=dtype_from_name(config.syncer.publish_dtype),
    )
    optim_payload, optim_theta_sha = encode_outer_state(
        theta,
        outer_state,
        dtype=dtype_from_name(config.syncer.publish_dtype),
    )
    leader.prepare_publication(
        command_id=f"initialize-genesis-{publication_id}-prepare",
        publication_id=publication_id,
        target_version=0,
        selection_batch_id=None,
        weight_relative_path=loaded.paths.relative(weight_path),
        weight_size=len(weight_payload),
        weight_sha256=hashlib.sha256(weight_payload).hexdigest(),
        optim_relative_path=loaded.paths.relative(optim_path),
        optim_size=len(optim_payload),
        optim_sha256=hashlib.sha256(optim_payload).hexdigest(),
        weight_theta_sha256=weight_theta_sha,
        optim_theta_sha256=optim_theta_sha,
    )
    publish_immutable_bytes(weight_path, weight_payload)
    publish_immutable_bytes(optim_path, optim_payload)
    committed = leader.commit_merge(
        command_id=f"initialize-genesis-{publication_id}-commit",
        publication_id=publication_id,
    )
    if isinstance(committed, MergeFenceConflict):
        raise RuntimeError("genesis unexpectedly encountered a membership fence conflict")
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
    *,
    created_at_lte: float | None = None,
) -> None:
    authority.committed_leader_lease(leader.token)
    _repair_current_admission_controls(loaded, authority, leader)
    if created_at_lte is not None:
        controller = authority.read.controller_status()
        if (
            controller["state"] != "preclosing"
            or controller["requested_at"] is None
            or float(controller["requested_at"]) != created_at_lte
        ):
            raise AdmissionInvariantError("preclose scan cutoff is not the durable close intent")
    for observation in iter_admission_requests(loaded.paths):
        if observation.original is None:
            telemetry.event(
                "admission_request_deferred",
                request_path=str(observation.path),
                error_type=observation.read_error_type,
                error_errno=observation.read_errno,
            )
            continue
        authority.committed_leader_lease(leader.token)
        try:
            _admit_observations_unprotected(loaded, authority, leader, telemetry, (observation,))
        except (AdmissionInvariantError, AuthoritySchemaError, StaleLeaderTokenError):
            raise
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
            disposition = safe_read_json(disposition_path)
            repair_rejected_admission_control(
                loaded.paths,
                request=request,
                disposition=disposition,
                epoch=leader.token.epoch,
                owner_id=leader.token.owner_id,
            )
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
                    replay_authorization = None
                    prior_was_active = False
                    if prior.binding_generation > 1:
                        history = authority.read.static_binding_history(
                            prior.learner_id,
                            prior.binding_generation - 1,
                        )
                        if history is None:
                            raise AuthoritySchemaError(
                                "committed static binding history is missing"
                            )
                        history_status = history.get("final_status")
                        if history_status not in {"terminal", "replaced"}:
                            raise AuthoritySchemaError(
                                "committed static binding history status is invalid"
                            )
                        previous_fence = StaticContributorFence(
                            kind="static",
                            learner_id=str(history["learner_id"]),
                            logical_launch_id=str(history["logical_launch_id"]),
                            attempt_id=str(history["attempt_id"]),
                            binding_generation=int(history["binding_generation"]),
                        )
                        prior_was_active = history_status == "replaced"
                        if prior_was_active or (
                            previous_fence.logical_launch_id != str(request["logical_launch_id"])
                        ):
                            replay_authorization = read_static_replacement_authorization(
                                loaded.paths,
                                request=request,
                                current_fence=previous_fence,
                            )
                            if replay_authorization is None:
                                raise AdmissionAuthorizationError(
                                    "committed static replacement authorization is missing"
                                )
                    binding = leader.replay_committed_static_binding(
                        command_id=command_id,
                        learner_id=str(request["learner_id"]),
                        logical_launch_id=str(request["logical_launch_id"]),
                        attempt_id=str(request["attempt_id"]),
                        expected_generation=request["expected_generation"],
                        allow_logical_replacement=replay_authorization is not None,
                        replacement_reason=(
                            f"operator_static_replacement:{replay_authorization[1]}"
                            if prior_was_active and replay_authorization is not None
                            else None
                        ),
                        registration_created_at=float(request["created_at"]),
                    )
                    if binding is None or binding != prior:
                        raise MembershipFenceError(
                            "active static attempt ID belongs to another admission request"
                        )
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
                        registration_created_at=float(request["created_at"]),
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
                    pbs_job_id=request.get("pbs_job_id"),
                    bootstrap_slot=request.get("bootstrap_slot"),
                    launch_request_id=request.get("launch_request_id"),
                    replace_instance_id=request.get("replace_instance_id"),
                    replacement_reason=(
                        "explicit_dynamic_replacement"
                        if request.get("replace_instance_id") is not None
                        else None
                    ),
                    registration_created_at=float(request["created_at"]),
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
            authority.committed_leader_lease(leader.token)
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
            raise AdmissionInvariantError(
                "admission command returned a fence that is no longer current"
            )
        authority.committed_leader_lease(leader.token)
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
        / "admissions"
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
    control: ControlPublisher,
    telemetry: ActorTelemetryWriter,
) -> None:
    for fence in authority.read.current_contributor_fences():
        progress = authority.read.contributor_progress(fence.stable_contributor_key)
        sequences = (
            (1,) if progress is None else (progress.last_cycle_seq, progress.last_cycle_seq + 1)
        )
        for sequence in sequences:
            canonical_path = loaded.paths.shared_root / canonical_receipt_relative_path(
                fence, sequence
            )
            path = canonical_path
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
