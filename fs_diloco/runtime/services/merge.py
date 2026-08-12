"""Single merge/publication implementation shared by normal and terminal paths."""

from __future__ import annotations

import hashlib
import uuid
from enum import Enum
from typing import Any

import torch

from ...core.run_descriptor import LoadedRunDescriptor
from ...modeling.outer_optim import outer_optimizer_step
from ...protocol.authority import MergeFenceConflict
from ...protocol.merge import normalized_update_weights, weighted_average_tensors
from ...storage.atomic_io import publish_immutable_bytes
from ...storage.authority import CommittedVersion, LeaderAuthority, LeaderSession
from ...storage.control import ControlPublisher
from ...storage.object_store import ArtifactIdentityError
from ...storage.tensor_codec import (
    dtype_from_name,
    encode_global_weights,
    encode_outer_state,
    load_outer_state,
    load_update_vector,
)


class MergeAttemptStatus(str, Enum):
    NO_BATCH = "no_batch"
    FENCE_CONFLICT = "fence_conflict"


class MergeService:
    """Own selection, merge computation, and recoverable checkpoint publication."""

    def __init__(
        self,
        *,
        loaded: LoadedRunDescriptor,
        authority: LeaderAuthority,
        leader: LeaderSession,
        control: ControlPublisher,
        telemetry: Any,
        theta: torch.Tensor,
        outer_state: dict[str, torch.Tensor],
        param_index: dict[str, Any],
        device: torch.device,
    ) -> None:
        """Bind one leader epoch to its in-memory global and optimizer state."""

        self.loaded = loaded
        self.authority = authority
        self.leader = leader
        self.control = control
        self.telemetry = telemetry
        self.theta = theta
        self.outer_state = outer_state
        self.param_index = param_index
        self.device = device
        self.sequence = 0
        self.last_selected_contributors = 0

    def merge_once(
        self, *, quorum_min: int, quorum_max: int, purpose: str
    ) -> CommittedVersion | MergeAttemptStatus:
        """Commit at most one quorum while releasing any unprepared failed selection."""

        if purpose not in {"normal", "terminal"}:
            raise ValueError("merge purpose must be normal or terminal")
        config = self.loaded.config
        paths = self.loaded.paths
        self.sequence += 1
        selection = self.leader.try_select_batch(
            command_id=f"select-{purpose}-e{self.leader.token.epoch}-n{self.sequence}",
            quorum_min=quorum_min,
            quorum_max=quorum_max,
        )
        if selection.batch is None:
            self.last_selected_contributors = 0
            return MergeAttemptStatus.NO_BATCH
        batch = selection.batch
        self.last_selected_contributors = len(batch.candidates)
        latest = self.authority.read.latest_committed_version()
        assert latest is not None
        invalid_update_ids: tuple[str, ...] = ()
        try:
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
            vectors: list[torch.Tensor] = []
            for item in batch.candidates:
                try:
                    vector = load_update_vector(
                        paths.shared_root,
                        item.proposal,
                        device=self.device,
                        dtype=dtype_from_name(config.syncer.compute_dtype),
                    )
                except (ArtifactIdentityError, ValueError):
                    invalid_update_ids = (item.proposal.update_id,)
                    raise
                vectors.append(vector)
            p_bar = weighted_average_tensors(
                vectors,
                [weights_by_id[item.proposal.update_id] for item in batch.candidates],
            )
            next_theta, next_outer = outer_optimizer_step(
                self.theta, self.theta - p_bar, self.outer_state, config.outer_optimizer
            )
            publication_id = str(uuid.uuid4())
            target_version = batch.target_version
            weight_path = paths.epoch_weight_path(
                self.leader.token.epoch,
                self.leader.token.owner_id,
                target_version,
                publication_id,
            )
            optim_path = paths.epoch_outer_optim_path(
                self.leader.token.epoch,
                self.leader.token.owner_id,
                target_version,
                publication_id,
            )
            weight_payload, weight_theta_sha = encode_global_weights(
                next_theta,
                self.param_index,
                dtype=dtype_from_name(config.syncer.publish_dtype),
            )
            optim_payload, optim_theta_sha = encode_outer_state(
                next_theta,
                next_outer,
                dtype=dtype_from_name(config.syncer.publish_dtype),
            )
            self.leader.prepare_publication(
                command_id=f"prepare-{publication_id}",
                publication_id=publication_id,
                target_version=target_version,
                selection_batch_id=batch.batch_id,
                weight_relative_path=paths.relative(weight_path),
                weight_size=len(weight_payload),
                weight_sha256=hashlib.sha256(weight_payload).hexdigest(),
                optim_relative_path=paths.relative(optim_path),
                optim_size=len(optim_payload),
                optim_sha256=hashlib.sha256(optim_payload).hexdigest(),
                weight_theta_sha256=weight_theta_sha,
                optim_theta_sha256=optim_theta_sha,
            )
        except Exception:
            self.leader.abandon_unprepared_batch(
                command_id=f"abandon-unprepared-{batch.batch_id}",
                batch_id=batch.batch_id,
                reason="merge_preparation_failed",
                invalid_update_ids=invalid_update_ids,
            )
            raise
        publish_immutable_bytes(weight_path, weight_payload)
        publish_immutable_bytes(optim_path, optim_payload)
        terminal_arguments: dict[str, int] = {}
        if purpose == "terminal":
            controller = self.authority.read.controller_status()
            terminal_arguments = {
                "terminal_generation": int(controller["generation"]),
                "terminal_merge_limit": int(config.terminal.max_terminal_merges),
            }
        committed = self.leader.commit_merge(
            command_id=f"commit-{publication_id}",
            publication_id=publication_id,
            **terminal_arguments,
        )
        if isinstance(committed, MergeFenceConflict):
            self.telemetry.event(
                "merge_fence_conflict",
                publication_id=publication_id,
                invalid_update_ids=committed.invalid_update_ids,
            )
            latest = self.authority.read.latest_committed_version()
            assert latest is not None
            self.theta, self.outer_state = load_outer_state(
                paths.shared_root,
                latest.optim_relative_path,
                expected_size=latest.optim_size,
                expected_sha256=latest.optim_sha256,
                expected_theta_sha256=latest.theta_sha256,
                device=self.device,
                dtype=dtype_from_name(config.syncer.compute_dtype),
            )
            return MergeAttemptStatus.FENCE_CONFLICT
        self.theta, self.outer_state = load_outer_state(
            paths.shared_root,
            committed.optim_relative_path,
            expected_size=committed.optim_size,
            expected_sha256=committed.optim_sha256,
            expected_theta_sha256=committed.theta_sha256,
            device=self.device,
            dtype=dtype_from_name(config.syncer.compute_dtype),
        )
        self.control.publish_latest(committed)
        self.telemetry.event(
            "version_committed",
            version=committed.version,
            publication_id=committed.publication_id,
            selected_update_ids=[item.proposal.update_id for item in batch.candidates],
            purpose=purpose,
        )
        return committed
