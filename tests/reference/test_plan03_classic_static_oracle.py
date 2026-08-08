from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import torch

from fs_diloco.modeling.outer_optim import (
    OuterOptimizerConfig,
    init_outer_state,
    outer_optimizer_step,
)
from fs_diloco.protocol.merge import (
    normalized_update_weights,
    select_one_per_learner,
    weighted_average_tensors,
)
from fs_diloco.storage.fenced_store import FencedSQLiteStore
from fs_diloco.storage.leader_lease import LeaderLeaseStore, LeaseSafetyTracker
from fs_diloco.storage.paths import RunPaths, prepare_authority_dirs
from fs_diloco.storage.schema_bootstrap import BootstrapIdentity, initialize_new_run
from fs_diloco.storage.sqlite_store import SQLiteStore
from fs_diloco.storage.atomic_io import sha256_file
from fs_diloco.storage.tensor_codec import (
    load_outer_state,
    load_update_vector,
    save_outer_state,
    save_update_vector,
)


@dataclass(frozen=True)
class EventTape:
    theta_v0: torch.Tensor
    proposals: tuple[tuple[dict[str, Any], torch.Tensor], ...]
    outer: OuterOptimizerConfig


def _metadata(learner_id: str, update_id: str, *, committed_at: float) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "learner_id": learner_id,
        "hostname": "host",
        "base_global_version": 0,
        "local_step_start": 0,
        "local_step_end": 2,
        "inner_steps": 2,
        "tokens_this_update": 8,
        "tokens_since_global_load": 8,
        "file_path": f"updates/{update_id}.safetensors",
        "file_size_bytes": 16,
        "sha256": f"digest-{update_id}",
        "created_at": committed_at - 0.5,
        "committed_at": committed_at,
    }


def _event_tape() -> EventTape:
    return EventTape(
        theta_v0=torch.tensor([1.0, -2.0, 3.0], dtype=torch.float32),
        proposals=(
            (
                _metadata("learner_000", "proposal-000", committed_at=10.0),
                torch.tensor([0.5, -1.5, 2.5], dtype=torch.float32),
            ),
            (
                _metadata("learner_001", "proposal-001", committed_at=11.0),
                torch.tensor([1.5, -2.5, 4.0], dtype=torch.float32),
            ),
        ),
        outer=OuterOptimizerConfig(name="nesterov", lr=0.7, momentum=0.9),
    )


def _semantic_projection(
    store: Any,
    tape: EventTape,
    *,
    ha: bool,
    artifact_root: Path,
) -> dict[str, Any]:
    weight_v0 = save_update_vector(artifact_root / "weights/v0.safetensors", tape.theta_v0)
    state_v0 = init_outer_state(tape.theta_v0, tape.outer)
    optim_v0 = save_outer_state(
        artifact_root / "optim/v0.safetensors",
        tape.theta_v0,
        state_v0,
    )
    initialization = (
        {
            "publication_id": "publication-v0",
            "weight_size_bytes": weight_v0.stat().st_size,
            "optim_size_bytes": optim_v0.stat().st_size,
        }
        if ha
        else {}
    )
    store.initialize_full_run(
        weight_path=str(weight_v0),
        optim_path=str(optim_v0),
        outer_optimizer="nesterov",
        identity={"run_id": "oracle"},
        config_snapshot={"oracle": True},
        **initialization,
    )
    for metadata, vector in tape.proposals:
        path = save_update_vector(artifact_root / str(metadata["file_path"]), vector)
        persisted_metadata = {
            **metadata,
            "file_path": str(path),
            "file_size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        assert store.insert_update_metadata(persisted_metadata)
    eligible = store.eligible_updates(current_version=0, max_staleness_versions=0)
    selected = select_one_per_learner(eligible, quorum_max=2)
    weights = normalized_update_weights(
        selected,
        current_version=0,
        staleness_lambda=0.5,
    )
    merged = weighted_average_tensors(
        [load_update_vector(str(row["file_path"])) for row in selected],
        [weights[row["update_id"]] for row in selected],
    )
    predecessor = store.latest_global_version()
    assert predecessor is not None
    predecessor_version = int(predecessor["version"])
    theta_v0 = load_update_vector(str(predecessor["weight_path"]))
    outer_theta_v0, state0 = load_outer_state(str(predecessor["optim_path"]))
    assert torch.equal(theta_v0, outer_theta_v0)
    theta_v1, state1 = outer_optimizer_step(
        theta_v0,
        theta_v0 - merged,
        state0,
        tape.outer,
    )
    store.mark_updates_selected(
        [str(row["update_id"]) for row in selected],
        "oracle-v1",
    )
    weight_v1 = save_update_vector(artifact_root / "weights/v1.safetensors", theta_v1)
    optim_v1 = save_outer_state(
        artifact_root / "optim/v1.safetensors",
        theta_v1,
        state1,
    )
    publication = (
        {
            "publication_id": "publication-v1",
            "weight_size_bytes": weight_v1.stat().st_size,
            "optim_size_bytes": optim_v1.stat().st_size,
        }
        if ha
        else {}
    )
    committed = store.commit_full_merge(
        predecessor_version=predecessor_version,
        target_version=predecessor_version + 1,
        weight_path=str(weight_v1),
        optim_path=str(optim_v1),
        selected_updates=selected,
        effective_weights=weights,
        total_update_tokens=sum(int(row["tokens_this_update"]) for row in selected),
        total_seen_tokens=sum(int(row["tokens_this_update"]) for row in selected),
        outer_optimizer="nesterov",
        max_staleness_versions=0,
        **publication,
    )
    latest = store.latest_global_version()
    assert latest is not None
    assert int(latest["version"]) == int(committed["version"])
    committed_theta = load_update_vector(str(latest["weight_path"]))
    committed_outer_theta, committed_state = load_outer_state(str(latest["optim_path"]))
    assert torch.equal(committed_theta, committed_outer_theta)
    return {
        "selected_ids": tuple(str(row["update_id"]) for row in selected),
        "selected_order": tuple(str(row["learner_id"]) for row in selected),
        "weights": tuple(weights[str(row["update_id"])] for row in selected),
        "merged": merged,
        "theta": committed_theta,
        "outer_step": committed_state["step"],
        "outer_momentum": committed_state["momentum"],
        "version": int(latest["version"]),
        "predecessor_version": predecessor_version,
    }


def _fixture_projection(projection: dict[str, Any]) -> dict[str, Any]:
    def tensor_hex(value: torch.Tensor) -> str:
        octets = value.detach().cpu().contiguous().view(torch.uint8).tolist()
        return bytes(octets).hex()

    return {
        "selected_ids": list(projection["selected_ids"]),
        "selected_order": list(projection["selected_order"]),
        "weights": list(projection["weights"]),
        "merged_float32_le_hex": tensor_hex(projection["merged"]),
        "theta_float32_le_hex": tensor_hex(projection["theta"]),
        "outer_step": int(projection["outer_step"].item()),
        "outer_momentum_float32_le_hex": tensor_hex(projection["outer_momentum"]),
        "version": projection["version"],
        "predecessor_version": projection["predecessor_version"],
    }


def test_classic_and_static_ha_share_exact_semantic_projection(tmp_path: Path) -> None:
    tape = _event_tape()
    classic = SQLiteStore(tmp_path / "classic.sqlite")
    paths = RunPaths(tmp_path / "static-ha")
    prepare_authority_dirs(paths)
    identity = BootstrapIdentity(
        run_id="oracle",
        source_fingerprint="sha256:oracle",
        config_sha256="oracle-config",
        mode="full",
    )
    initialize_new_run(paths.sqlite_db, identity, marker_path=paths.bootstrap_complete_json)
    lease = LeaderLeaseStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        lease_duration_seconds=90.0,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
    )
    token = lease.acquire(owner_id="oracle-owner", hostname="host", pid=1)
    tracker = LeaseSafetyTracker(token, lease_duration_seconds=90.0, max_clock_skew_seconds=2.0)
    fenced = FencedSQLiteStore(
        paths.sqlite_db,
        identity,
        marker_path=paths.bootstrap_complete_json,
        max_clock_skew_seconds=2.0,
        wall_clock=lambda: 100.0,
        lease_safety_check=tracker.assert_safe,
    )
    try:
        classic_projection = _semantic_projection(
            classic,
            tape,
            ha=False,
            artifact_root=tmp_path / "classic-artifacts",
        )
        ha_projection = _semantic_projection(
            fenced.bind(token),
            tape,
            ha=True,
            artifact_root=tmp_path / "ha-artifacts",
        )
        assert classic_projection.keys() == ha_projection.keys()
        for key in classic_projection:
            classic_value = classic_projection[key]
            ha_value = ha_projection[key]
            if isinstance(classic_value, torch.Tensor):
                assert torch.equal(classic_value, ha_value), key
            else:
                assert classic_value == ha_value, key
        fixture_root = Path(__file__).resolve().parents[1] / "fixtures/golden"
        classic_fixture = json.loads(
            (fixture_root / "classic_full_v1_trace.json").read_text(encoding="utf-8")
        )["semantic_projection"]
        ha_fixture = json.loads(
            (fixture_root / "static_ha_v1_trace.json").read_text(encoding="utf-8")
        )["semantic_projection"]
        assert classic_fixture == ha_fixture
        assert _fixture_projection(classic_projection) == classic_fixture
        assert _fixture_projection(ha_projection) == ha_fixture
    finally:
        classic.close()
        fenced.close()
        lease.close()


def test_oracle_fixture_comparison_detects_semantic_mutation() -> None:
    fixture_root = Path(__file__).resolve().parents[1] / "fixtures/golden"
    fixture = json.loads((fixture_root / "classic_full_v1_trace.json").read_text(encoding="utf-8"))[
        "semantic_projection"
    ]
    mutated = {**fixture, "predecessor_version": fixture["predecessor_version"] + 1}

    with pytest.raises(AssertionError):
        assert mutated == fixture
