"""Single merge/publication implementation shared by normal and terminal paths."""

from __future__ import annotations

import uuid
from typing import Any

import torch

from ...core.run_descriptor import LoadedRunDescriptor
from ...modeling.outer_optim import outer_optimizer_step
from ...protocol.authority import MergeFenceConflict
from ...protocol.merge import normalized_update_weights, weighted_average_tensors
from ...storage.authority import LeaderAuthority, LeaderSession
from ...storage.control import V4ControlPublisher
from ...storage.tensor_codec import (
    dtype_from_name,
    load_outer_state,
    load_update_vector,
    publish_global_weights_immutable,
    publish_outer_state_immutable,
)


PLAN03_REQUIREMENTS = frozenset({"DMB-06", "P5-ARCH", "PUB-01", "TERM-03"})


class MergeService:
    def __init__(
        self,
        *,
        loaded: LoadedRunDescriptor,
        authority: LeaderAuthority,
        leader: LeaderSession,
        control: V4ControlPublisher,
        telemetry: Any,
        theta: torch.Tensor,
        outer_state: dict[str, torch.Tensor],
        param_index: dict[str, Any],
        device: torch.device,
    ) -> None:
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

    def merge_once(self, *, quorum_min: int, quorum_max: int, purpose: str) -> Any | None:
        config = self.loaded.config.shared
        paths = self.loaded.paths
        self.sequence += 1
        selection = self.leader.try_select_batch(
            command_id=(
                f"select-{purpose}-e{self.leader.token.epoch}-n{self.sequence}-"
                f"{uuid.uuid4().hex[:12]}"
            ),
            quorum_min=quorum_min,
            quorum_max=quorum_max,
        )
        if selection.batch is None:
            return None
        batch = selection.batch
        latest = self.authority.read.latest_committed_version()
        assert latest is not None
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
                device=self.device,
                dtype=dtype_from_name(config.syncer.compute_dtype),
            )
            for item in batch.candidates
        ]
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
        weight, weight_theta_sha = publish_global_weights_immutable(
            weight_path,
            next_theta,
            self.param_index,
            dtype=dtype_from_name(config.syncer.publish_dtype),
        )
        optim, optim_theta_sha = publish_outer_state_immutable(
            optim_path,
            next_theta,
            next_outer,
            dtype=dtype_from_name(config.syncer.publish_dtype),
        )
        self.leader.prepare_publication(
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
        committed = self.leader.commit_merge(
            command_id=f"commit-{publication_id}", publication_id=publication_id
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
                paths.shared_root / latest.optim_relative_path,
                device=self.device,
                dtype=dtype_from_name(config.syncer.compute_dtype),
            )
            return None
        self.theta, self.outer_state = load_outer_state(
            paths.shared_root / committed.optim_relative_path,
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
