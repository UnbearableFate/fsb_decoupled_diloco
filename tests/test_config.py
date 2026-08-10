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


ROOT = Path(__file__).resolve().parents[1]
CONFIGS = tuple(sorted((ROOT / "configs").glob("full_protocol_*.yaml")))
assert CONFIGS, "no repository configs discovered"
HUB_COMMIT = "a" * 40


def synthetic_config() -> Config:
    config = Config()
    config.model.name_or_path = "synthetic-tiny"
    config.data.dataset_name = "synthetic"
    return config


@pytest.mark.parametrize("path", CONFIGS, ids=lambda path: path.name)
def test_repository_configs_use_the_one_strict_schema(path: Path) -> None:
    config = load_config(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert config.config_schema_version == 1
    assert payload["config_schema_version"] == 1
    assert set(payload) <= {field.name for field in dataclasses.fields(Config)}
    if config.membership.mode == "static":
        assert "scaling" not in payload
        assert "stream_pool_size" not in payload["membership"]
        assert "bootstrap_instances" not in payload["membership"]
        assert config.membership.stream_pool_size == config.sync.num_learners
        assert config.membership.bootstrap_instances == config.sync.num_learners


@pytest.mark.parametrize(
    "extra",
    [
        "membership:\n  mode: static\n  stream_pool_size: 4\n",
        "membership:\n  mode: static\n  bootstrap_instances: 4\n",
        "membership:\n  mode: static\nscaling:\n  enabled: false\n",
    ],
)
def test_static_config_rejects_repeated_or_inert_capacity_fields(
    tmp_path: Path, extra: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(extra, encoding="utf-8")

    with pytest.raises(ValueError, match="static membership"):
        load_config(path)


@pytest.mark.parametrize("revision", [None, "main", "v1.0", "A" * 40, "a" * 39])
def test_hub_inputs_require_immutable_commit_revisions(revision: str | None) -> None:
    config = Config()
    config.model.revision = revision
    config.data.revision = HUB_COMMIT

    with pytest.raises(ValueError, match="model.revision.*40-character.*commit SHA"):
        config.validate()


def test_hub_inputs_at_immutable_commits_are_valid() -> None:
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
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_direct_weight_stop_round_trips_through_the_only_config_type(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """config_schema_version: 1
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


def test_resolution_owns_run_identity_and_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FS_DILOCO_GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("FS_DILOCO_GIT_DIRTY", "0")
    monkeypatch.setenv("FS_DILOCO_SOURCE_FINGERPRINT", "sha256:" + "b" * 64)
    config = resolve_config(
        "configs/full_protocol_static.yaml",
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
    config = synthetic_config()
    config.leader = leader

    with pytest.raises(ValueError):
        config.validate()


def test_repository_hub_revisions_are_pinned_when_not_synthetic() -> None:
    for path in CONFIGS:
        config = load_config(path)
        if config.model.name_or_path != "synthetic-tiny":
            assert re.fullmatch(r"[0-9a-f]{40}", config.model.revision or "")
        if config.data.dataset_name != "synthetic":
            assert re.fullmatch(r"[0-9a-f]{40}", config.data.revision or "")
