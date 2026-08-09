from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from fs_diloco.core.config import Config, SyncSection, config_to_dict, load_config, resolve_config
from fs_diloco.core.config_v4 import (
    ConfigProfile,
    ConfigV4,
    LeaderSection,
    MaintenanceSection,
    config_v4_to_dict,
    load_config_v4,
)


FULL_CONFIGS = tuple(sorted(Path("configs").rglob("fs_diloco_*.yaml")))
BASELINE_CONFIGS = tuple(sorted(Path("configs").glob("torch_baseline_*.yaml")))
REPOSITORY_CONFIGS = FULL_CONFIGS + BASELINE_CONFIGS
PRIMARY_RUNS_ROOT = Path("/work/xg24i002/x10041/fsb_decoupled_diloco/runs/fs_diloco")
BASELINE_RUNS_ROOT = Path("/work/xg24i002/x10041/fsb_decoupled_diloco/runs/torch_baselines")


@pytest.mark.parametrize("path", FULL_CONFIGS, ids=lambda path: path.name)
def test_every_full_repository_config_is_strict_v4(path: Path) -> None:
    config = load_config_v4(path, profile=ConfigProfile.FULL_V4)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert config.config_schema_version == 1
    assert payload["config_schema_version"] == 1
    for removed in ("init", "fragments", "failure_sim"):
        assert removed not in payload
    assert set(payload.get("coordination", {})) <= {"leader"}
    assert "stop_after_global_tokens" not in payload.get("sync", {})


@pytest.mark.parametrize("path", BASELINE_CONFIGS, ids=lambda path: path.name)
def test_every_baseline_config_uses_the_shared_schema(path: Path) -> None:
    config = load_config_v4(path, profile=ConfigProfile.TORCH_BASELINE)
    assert config.shared.torch_baseline.enabled is True
    assert config.config_schema_version == 1


@pytest.mark.parametrize("path", REPOSITORY_CONFIGS, ids=lambda path: path.name)
def test_repository_configs_keep_the_primary_worktree_run_root(path: Path) -> None:
    loaded = load_config(path)
    expected = BASELINE_RUNS_ROOT if loaded.torch_baseline.enabled else PRIMARY_RUNS_ROOT
    assert loaded.run.shared_root == str(expected / "{run_id}")
    assert resolve_config(path, run_id="path-check").run.shared_root == str(expected / "path-check")


def test_shared_overrides_are_explicit_and_do_not_reintroduce_runtime_modes(
    tmp_path: Path,
) -> None:
    config = resolve_config(
        "configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml",
        run_id="override",
        shared_root=str(tmp_path / "literal_{run_id}"),
        num_learners=1,
        training_seed=2027,
        scan_interval_seconds=0.2,
        ingest_during_publish=True,
        syncer_device="CPU",
        syncer_publish_dtype="torch.bfloat16",
        staleness_lambda=4.0,
        max_staleness_versions=0,
        global_adoption_strategy="predict_post_publish_global",
        completion_mode="local_or_global",
    )

    assert config.run.shared_root == str(tmp_path / "literal_override")
    assert config.sync.num_learners == config.sync.quorum_min == config.sync.quorum_max == 1
    assert config.training.seed == 2027
    assert config.sync.scan_interval_seconds == 0.2
    assert config.sync.ingest_during_publish is True
    assert config.syncer.device == "cpu"
    assert config.syncer.publish_dtype == "bfloat16"
    assert config.sync.staleness_lambda == 4.0
    assert config.sync.max_staleness_versions == 0
    assert config.learner.global_adoption_strategy == "predict_post_publish_global"
    assert config.training.completion_mode == "local_or_global"
    assert not (
        {"init", "fragments", "failure_sim", "coordination"} & config_to_dict(config).keys()
    )


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("config_schema_version: 2\n", "config_schema_version"),
        ("config_schema_version: 1\ninit: {}\n", "removed v4"),
        ("config_schema_version: 1\nfragments: {}\n", "removed v4"),
        ("config_schema_version: 1\nfailure_sim: {}\n", "removed v4"),
        (
            "config_schema_version: 1\nsync:\n  capture_terminal_predecessor_for_eval: true\n",
            "removed v4",
        ),
        (
            "config_schema_version: 1\ncoordination:\n  recovery_submission: {}\n",
            "removed v4",
        ),
        (
            "config_schema_version: 1\nsync:\n  stop_after_global_tokens: 10\n",
            "removed v4",
        ),
    ],
)
def test_v4_loader_rejects_removed_runtime_keys(
    tmp_path: Path, yaml_text: str, message: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config_v4(path, profile=ConfigProfile.FULL_V4)


@pytest.mark.parametrize("key", ["init", "fragments", "failure_sim", "coordination"])
def test_shared_config_schema_cannot_express_removed_runtime_sections(
    tmp_path: Path, key: str
) -> None:
    path = tmp_path / "partial.yaml"
    path.write_text(f"{key}: {{}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=key):
        load_config(path)
    assert key not in {field.name for field in dataclasses.fields(Config)}


def test_v4_direct_weight_stop_round_trips_without_legacy_token_field(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """config_schema_version: 1
sync:
  stop_after_outer_steps: null
  stop_after_direct_weight_tokens_applied: 100
coordination:
  leader: {}
maintenance: {}
""",
        encoding="utf-8",
    )
    config = load_config_v4(path, profile=ConfigProfile.FULL_V4)
    payload = config_v4_to_dict(config)

    assert config.stop_after_direct_weight_tokens_applied == 100
    assert payload["sync"]["stop_after_direct_weight_tokens_applied"] == 100
    assert "stop_after_global_tokens" not in payload["sync"]


def test_v4_full_profile_validates_shared_leader_and_maintenance() -> None:
    ConfigV4().validate(ConfigProfile.FULL_V4)

    streaming = ConfigV4()
    streaming.shared.data.streaming = True
    with pytest.raises(ValueError, match="streaming=true"):
        streaming.validate(ConfigProfile.FULL_V4)

    short_grace = ConfigV4(
        leader=LeaderSection(lease_duration_seconds=90.0, max_clock_skew_seconds=2.0),
        maintenance=MaintenanceSection(publication_orphan_grace_seconds=93.0),
    )
    with pytest.raises(ValueError, match="orphan_grace"):
        short_grace.validate(ConfigProfile.FULL_V4)


@pytest.mark.parametrize(
    "leader",
    [
        LeaderSection(lease_duration_seconds=0.0),
        LeaderSection(renew_interval_seconds=float("nan")),
        LeaderSection(max_clock_skew_seconds=float("inf")),
        LeaderSection(lease_busy_timeout_ms=True),
    ],
)
def test_v4_leader_rejects_invalid_numeric_values(leader: LeaderSection) -> None:
    with pytest.raises(ValueError):
        ConfigV4(leader=leader).validate(ConfigProfile.FULL_V4)


def test_validation_profiles_cannot_be_spoofed() -> None:
    with pytest.raises(ValueError, match="requires torch_baseline.enabled"):
        ConfigV4().validate(ConfigProfile.TORCH_BASELINE)

    baseline = ConfigV4()
    baseline.shared.torch_baseline.enabled = True
    baseline.shared.training.max_local_steps = 10
    with pytest.raises(ValueError, match=r"cannot .*torch baseline config"):
        baseline.validate(ConfigProfile.FULL_V4)
    baseline.validate(ConfigProfile.TORCH_BASELINE)


def test_structural_validation_rejects_bool_and_nonfinite_numbers() -> None:
    section = SyncSection(scan_interval_seconds=float("nan"))
    with pytest.raises(ValueError, match="must be finite"):
        section.validate(path="sync")

    config = Config()
    config.training.inner_steps = True
    with pytest.raises(ValueError, match="must be an integer"):
        config.validate(profile="full_v4_shared")


def test_static_deadline_terminal_policy_requires_an_explicit_deadline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "static-deadline.yaml"
    path.write_text(
        "membership:\n  mode: static\nterminal:\n  admission_close_policy: deadline\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="deadline_seconds is required"):
        resolve_config(path)


def test_dynamic_scaling_rejects_a_subminimum_learner_walltime(tmp_path: Path) -> None:
    payload = yaml.safe_load(
        Path("configs/fs_diloco_tiny_ha_dynamic_2node.yaml").read_text(encoding="utf-8")
    )
    payload["scaling"]["learner_walltime"] = "00:09:59"
    path = tmp_path / "short-scaling-walltime.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="at least 00:10:00"):
        resolve_config(path)


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("scan_interval_seconds", 0.0, "scan_interval_seconds must be > 0"),
        ("staleness_lambda", -0.1, "staleness_lambda must be >= 0"),
        ("max_staleness_versions", -1, "max_staleness_versions must be >= 0"),
    ],
)
def test_shared_override_validation_is_fail_closed(
    keyword: str, value: float, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_config("configs/fs_diloco_tiny_local.yaml", **{keyword: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("device", "gpu"), ("compute_dtype", "float16"), ("publish_dtype", "int8")],
)
def test_syncer_compute_config_rejects_unsupported_values(
    tmp_path: Path, field: str, value: str
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(f"syncer:\n  {field}: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"unsupported syncer\.{field}"):
        resolve_config(path)


def test_global_only_requires_an_unambiguous_v4_target(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """config_schema_version: 1
sync:
  stop_after_outer_steps: null
training:
  completion_mode: global_only
coordination:
  leader: {}
maintenance: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires an unambiguous global stop target"):
        load_config_v4(path, profile=ConfigProfile.FULL_V4)
