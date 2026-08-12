"""Verify merge math and recoverable selected-to-prepared composition."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from fs_diloco.core.config import Config
from fs_diloco.protocol.authority import SelectionBatch, SelectionCandidate, SelectionAttempt
from fs_diloco.protocol.merge import normalized_update_weights, weighted_average_tensors
from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.runtime.services import merge as merge_module
from fs_diloco.runtime.services.merge import MergeService
from fs_diloco.storage.object_store import ArtifactIdentityError, tensor_schema_sha256
from fs_diloco.storage.paths import RunPaths
from fs_diloco.storage.tensor_codec import publish_safetensors_immutable
from tests.support.protocol import proposal_payload


def test_token_staleness_weighting_and_average() -> None:
    """Token weighting must prefer fresher work while preserving tensor shape."""

    updates = [
        {"update_id": "a", "base_global_version": 4, "tokens_this_update": 100},
        {"update_id": "b", "base_global_version": 2, "tokens_this_update": 100},
    ]
    weights = normalized_update_weights(updates, current_version=4, staleness_lambda=0.5)
    assert weights["a"] > weights["b"]
    result = weighted_average_tensors(
        [torch.tensor([1.0, 3.0]), torch.tensor([3.0, 5.0])],
        [weights["a"], weights["b"]],
    )
    assert result.shape == (2,)
    assert torch.all(result > torch.tensor([1.0, 3.0]))


def _published_proposal(run_root: Path) -> FullUpdateProposalV2:
    """Publish one exact update used by pre-publication failure tests."""

    payload = proposal_payload()
    publication = publish_safetensors_immutable(
        run_root / str(payload["payload_relative_path"]),
        {"local_params": torch.tensor([1.0], dtype=torch.float32)},
    )
    payload.update(
        {
            "payload_size": publication.size_bytes,
            "payload_sha256": publication.sha256,
            "tensor_schema_sha256": tensor_schema_sha256(
                [{"key": "local_params", "dtype": "float32", "shape": [1]}]
            ),
        }
    )
    return FullUpdateProposalV2.from_dict(payload)


class _Leader:
    """Record selected-batch cleanup while injecting prepare failures."""

    def __init__(self, batch: SelectionBatch, *, fail_prepare: bool) -> None:
        """Bind the selected batch and selected prepare behavior."""

        self.token = SimpleNamespace(epoch=1, owner_id="owner")  # Current leader identity.
        self.batch = batch  # The single batch returned by selection.
        self.fail_prepare = fail_prepare  # Whether intent creation raises.
        self.abandon_calls: list[dict[str, object]] = []  # Observed cleanup commands.

    def try_select_batch(self, **_kwargs: object) -> SelectionAttempt:
        """Return the fixed selected batch once per service attempt."""

        return SelectionAttempt(batch=self.batch, invalid_update_ids=(), eligible_contributors=1)

    def prepare_publication(self, **_kwargs: object) -> object:
        """Inject the intent-creation boundary or return a placeholder intent."""

        if self.fail_prepare:
            raise RuntimeError("injected prepare failure")
        return SimpleNamespace()

    def abandon_unprepared_batch(self, **kwargs: object) -> tuple[str, ...]:
        """Record the same-epoch cleanup that must precede process failure."""

        self.abandon_calls.append(kwargs)
        return ()


@pytest.mark.parametrize(
    "failure_phase",
    ("load", "reduction", "optimizer", "weight_encode", "outer_encode", "prepare"),
)
def test_every_selected_to_prepared_failure_releases_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    """No same-epoch failure before a durable intent may strand selected updates."""

    proposal = _published_proposal(tmp_path)
    batch = SelectionBatch(
        batch_id="batch-current",
        command_id="select-current",
        owner_epoch=1,
        target_version=1,
        candidates=(SelectionCandidate(proposal=proposal, selection_credit=0),),
    )
    leader = _Leader(batch, fail_prepare=failure_phase == "prepare")
    config = Config()
    loaded = SimpleNamespace(config=config, paths=RunPaths(tmp_path))
    authority = SimpleNamespace(
        read=SimpleNamespace(latest_committed_version=lambda: SimpleNamespace(version=0))
    )
    param_index = {
        "total_numel": 1,
        "params": [{"name": "parameter", "shape": [1], "numel": 1, "offset": 0}],
    }
    service = MergeService(
        loaded=loaded,
        authority=authority,
        leader=leader,
        control=SimpleNamespace(),
        telemetry=SimpleNamespace(event=lambda *_args, **_kwargs: None),
        theta=torch.tensor([0.0]),
        outer_state={"step": torch.tensor(0), "momentum": torch.tensor([0.0])},
        param_index=param_index,
        device=torch.device("cpu"),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        """Raise at the currently parameterized merge preparation boundary."""

        raise RuntimeError(f"injected {failure_phase} failure")

    if failure_phase == "load":
        monkeypatch.setattr(
            merge_module,
            "load_update_vector",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ArtifactIdentityError("injected identity mismatch")
            ),
        )
    elif failure_phase == "reduction":
        monkeypatch.setattr(merge_module, "weighted_average_tensors", fail)
    elif failure_phase == "optimizer":
        monkeypatch.setattr(merge_module, "outer_optimizer_step", fail)
    elif failure_phase == "weight_encode":
        monkeypatch.setattr(merge_module, "encode_global_weights", fail)
    elif failure_phase == "outer_encode":
        monkeypatch.setattr(merge_module, "encode_outer_state", fail)

    with pytest.raises((ArtifactIdentityError, RuntimeError), match="injected"):
        service.merge_once(quorum_min=1, quorum_max=1, purpose="normal")

    assert len(leader.abandon_calls) == 1
    assert leader.abandon_calls[0]["batch_id"] == batch.batch_id
    assert leader.abandon_calls[0]["invalid_update_ids"] == (
        (proposal.update_id,) if failure_phase == "load" else ()
    )
