"""Verify the repository's sole strict Full Protocol configuration schema."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from fs_diloco.core.config import (
    Config,
    LeaderSection,
    config_to_dict,
    load_config,
    resolve_config,
    resolved_config_bytes,
)
from fs_diloco.core.versions import CONFIG_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
CONFIGS = tuple(sorted((ROOT / "configs").rglob("*.yaml")))
assert CONFIGS, "no repository configs discovered"
HUB_COMMIT = "a" * 40


def synthetic_config() -> Config:
    """Return a config whose model and dataset require no remote revisions."""

    config = Config()
    config.model.name_or_path = "synthetic-tiny"
    config.data.dataset_name = "synthetic"
    return config


@pytest.mark.parametrize("path", CONFIGS, ids=lambda path: path.name)
def test_repository_configs_use_the_one_strict_schema(path: Path) -> None:
    """Every tracked YAML config must use the complete current stream-pool schema."""

    config = load_config(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert config.config_schema_version == CONFIG_SCHEMA_VERSION
    assert payload["config_schema_version"] == CONFIG_SCHEMA_VERSION
    assert set(payload) <= {field.name for field in dataclasses.fields(Config)}
    assert 1 <= config.sync.quorum_min <= config.membership.stream_pool_size
    assert config.membership.bootstrap_instances <= config.membership.stream_pool_size


@pytest.mark.parametrize(
    ("content", "unknown_field"),
    [
        ("membership:\n  mode: static\n", "membership.mode"),
        ("sync:\n  num_learners: 4\n", "sync.num_learners"),
    ],
)
def test_removed_capacity_fields_are_rejected_as_unknown(
    tmp_path: Path, content: str, unknown_field: str
) -> None:
    """Legacy mode and duplicate capacity fields have no compatibility parser."""

    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(unknown_field)):
        load_config(path)


@pytest.mark.parametrize("scaling_enabled", [False, True])
def test_fixed_stream_pool_is_shared_by_both_capacity_policies(scaling_enabled: bool) -> None:
    """Scaling changes automation only and never changes membership capacity semantics."""

    config = synthetic_config()
    config.membership.stream_pool_size = 4
    config.membership.bootstrap_instances = 3
    config.sync.quorum_min = 2
    config.sync.quorum_max = 2
    config.scaling.enabled = scaling_enabled
    config.scaling.desired_contributors = 3
    config.scaling.low_contributor_threshold = 2
    if scaling_enabled:
        config.scaling.learner_walltime = "00:10:00"
        config.scaling.learner_queue = "debug-g"

    config.validate()
    assert config.membership.stream_pool_size == 4


def test_capacity_target_cannot_exceed_the_fixed_stream_pool() -> None:
    """Automatic capacity may exceed merge quorum but cannot invent logical streams."""

    config = synthetic_config()
    config.membership.stream_pool_size = 4
    config.membership.bootstrap_instances = 4
    config.sync.quorum_min = config.sync.quorum_max = 2
    config.scaling.enabled = True
    config.scaling.desired_contributors = 5
    config.scaling.low_contributor_threshold = 4

    with pytest.raises(ValueError, match="desired_contributors.*stream_pool_size"):
        config.validate()


@pytest.mark.parametrize("revision", [None, "main", "v1.0", "A" * 40, "a" * 39])
def test_hub_inputs_require_immutable_commit_revisions(revision: str | None) -> None:
    """Remote model identities require immutable lowercase commit revisions."""

    config = Config()
    config.model.name_or_path = "organization/model"
    config.model.revision = revision
    config.data.revision = HUB_COMMIT

    with pytest.raises(ValueError, match="model.revision.*40-character.*commit SHA"):
        config.validate()


def test_hub_inputs_at_immutable_commits_are_valid() -> None:
    """Pinned model, tokenizer, and dataset revisions form a valid source identity."""

    config = Config()
    config.model.name_or_path = "organization/model"
    config.model.revision = "a" * 40
    config.model.tokenizer_revision = "b" * 40
    config.data.dataset_name = "organization/dataset"
    config.data.revision = "c" * 40

    config.validate()


@pytest.mark.parametrize(
    ("section", "local_reference"),
    [
        ("model", "/models/model"),
        ("model", "./models/model"),
        ("model", "../models/model"),
        ("model", "file:///models/model"),
        ("data", "/datasets/data"),
        ("data", "./datasets/data"),
        ("data", "../datasets/data"),
        ("data", "file:///datasets/data"),
    ],
)
def test_local_inputs_are_rejected_without_content_identity(
    section: str, local_reference: str
) -> None:
    """Local model and dataset paths are rejected without content-addressed identity."""

    config = Config()
    config.model.revision = HUB_COMMIT
    config.data.revision = HUB_COMMIT
    if section == "model":
        config.model.name_or_path = local_reference
    else:
        config.data.dataset_name = local_reference

    with pytest.raises(ValueError, match=rf"local {section}.*content identity"):
        config.validate()


def test_unknown_top_level_keys_are_rejected(tmp_path: Path) -> None:
    """Unknown top-level config sections fail before defaults can mask them."""

    path = tmp_path / "config.yaml"
    path.write_text("unknown_section: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown_section"):
        load_config(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("config_schema_version: true\n", "config_schema_version must be an integer"),
        ("sync:\n  scan_interval_seconds: '0.5'\n", "scan_interval_seconds must be a number"),
        ("run:\n  name: invalid/name\n", "run.name must be a safe"),
        (
            "run:\n  source_fingerprint: sha256:short\n",
            "run.source_fingerprint must be a sha256 digest",
        ),
    ],
)
def test_config_rejects_invalid_current_types_and_identities(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    """Strict config decoding rejects wrong scalar types and unsafe identities."""

    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_direct_weight_stop_round_trips_through_the_only_config_type(tmp_path: Path) -> None:
    """The direct-token stop target survives canonical resolved-config serialization."""

    path = tmp_path / "config.yaml"
    path.write_text(
        f"""config_schema_version: {CONFIG_SCHEMA_VERSION}
model:
  name_or_path: synthetic-tiny
data:
  dataset_name: synthetic
sync:
  stop_after_outer_steps: null
  stop_after_direct_weight_tokens_applied: 100
""",
        encoding="utf-8",
    )

    config = load_config(path)
    payload = config_to_dict(config)

    assert config.sync.stop_after_direct_weight_tokens_applied == 100
    assert payload["sync"]["stop_after_direct_weight_tokens_applied"] == 100
    assert yaml.safe_load(resolved_config_bytes(config))["sync"] == payload["sync"]


@pytest.mark.parametrize(
    ("max_local_steps", "outer_steps", "message"),
    [
        (None, 10, "requires training.max_local_steps"),
        (1999, 10, "whole inner cycles"),
        (2000, None, "requires a global step target"),
    ],
)
def test_local_and_global_completion_requires_two_exact_horizons(
    max_local_steps: int | None,
    outer_steps: int | None,
    message: str,
) -> None:
    """The joint completion mode must describe complete local cycles and a global target."""

    config = synthetic_config()
    config.training.inner_steps = 200
    config.training.max_local_steps = max_local_steps
    config.training.completion_mode = "local_and_global"
    config.sync.stop_after_outer_steps = outer_steps

    with pytest.raises(ValueError, match=message):
        config.validate()


def test_resolution_owns_run_identity_and_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime resolution binds source identity and expands the run-root template once."""

    monkeypatch.setenv("FS_DILOCO_GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("FS_DILOCO_GIT_DIRTY", "0")
    monkeypatch.setenv("FS_DILOCO_SOURCE_FINGERPRINT", "sha256:" + "b" * 64)
    config = resolve_config(
        ROOT / "configs/full_protocol.yaml",
        run_id="identity-test",
        shared_root=str(tmp_path / "{run_id}"),
        project_root=tmp_path,
    )

    assert config.run.run_id == "identity-test"
    assert config.run.shared_root == str(tmp_path / "identity-test")
    assert config.run.git_commit == "a" * 40
    assert config.run.git_dirty is False
    assert config.run.source_fingerprint == "sha256:" + "b" * 64


@pytest.mark.parametrize(
    "leader",
    [
        LeaderSection(lease_duration_seconds=0.0),
        LeaderSection(renew_interval_seconds=0.0),
        LeaderSection(lease_duration_seconds=40.0, renew_interval_seconds=10.0),
        LeaderSection(candidate_acquire_poll_seconds=11.0),
    ],
)
def test_leader_timing_constraints_fail_closed(leader: LeaderSection) -> None:
    """Unsafe lease and polling intervals are rejected before leader startup."""

    config = synthetic_config()
    config.leader = leader

    with pytest.raises(ValueError):
        config.validate()


def test_repository_hub_revisions_are_pinned_when_not_synthetic() -> None:
    """All repository workloads that use Hub inputs pin immutable revisions."""

    for path in CONFIGS:
        config = load_config(path)
        if config.model.name_or_path != "synthetic-tiny":
            assert re.fullmatch(r"[0-9a-f]{40}", config.model.revision or "")
        if config.data.dataset_name != "synthetic":
            assert re.fullmatch(r"[0-9a-f]{40}", config.data.revision or "")
