import pytest

from fs_diloco.core.config import load_config, resolve_config


def test_config_defaults_and_cli_overrides(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_local.yaml",
        run_id="test_run",
        shared_root=str(tmp_path / "run"),
        num_learners=1,
    )
    assert config.run.run_id == "test_run"
    assert config.run.shared_root == str(tmp_path / "run")
    assert config.sync.num_learners == 1
    assert config.sync.quorum_min == 1
    assert config.inner_optimizer.betas == (0.9, 0.95)
    assert config.fragments.enabled is False


def test_fragment_config_and_unknown_keys(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(
        """
run:
  name: fragment
fragments:
  enabled: true
  strategy: balanced_tensor
  num_fragments: 4
""",
        encoding="utf-8",
    )
    config = load_config(good)
    assert config.fragments.enabled is True
    assert config.fragments.num_fragments == 4

    bad_top = tmp_path / "bad_top.yaml"
    bad_top.write_text("unknown_section: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(bad_top)

    bad_nested = tmp_path / "bad_nested.yaml"
    bad_nested.write_text("sync:\n  mode: fragment\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(bad_nested)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("init", "resume_version", "latest"),
        ("init", "resume_db_dump", "null"),
        ("sync", "db_dump_every_versions", "1"),
        ("io", "sqlite_local_dir", "null"),
        ("io", "keep_last_global_versions", "3"),
        ("io", "keep_last_learner_update_versions", "3"),
    ],
)
def test_removed_persistence_and_retention_config_is_rejected(tmp_path, section, key, value):
    path = tmp_path / "legacy.yaml"
    path.write_text(f"{section}:\n  {key}: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(path)


@pytest.mark.parametrize(
    "path",
    [
        "configs/fs_diloco_gpt2_wikitext2_1l_debug.yaml",
        "configs/fs_diloco_gpt2_wikitext2_1l_fragment_debug.yaml",
        "configs/fs_diloco_gpt2_wikitext2_8l_fragment_50x10.yaml",
        "configs/fs_diloco_gpt2_wikitext2_8l_no_fragment_50x10.yaml",
    ],
)
def test_bfloat16_upload_configs(path):
    assert load_config(path).io.tensor_dtype == "bfloat16"


def test_full_5000_config_enables_stepwise_local_delta_rebase():
    config = resolve_config("configs/fs_diloco_gpt2_wikitext2_8l_5000steps.yaml")
    assert config.learner.poll_latest_during_inner_steps is True
    assert config.learner.adopt_global_after_upload is True
    assert config.learner.global_adoption_strategy == "rebase_post_publish_delta"
    assert config.sync.grace_window.mode == "adaptive_fastest_upload_eta"
    assert config.sync.grace_window.initial_seconds == 10.0
    assert config.training.max_local_steps == 5000
    assert config.training.completion_mode == "global_only"


def test_global_only_completion_requires_global_target(tmp_path):
    path = tmp_path / "global_only_without_target.yaml"
    path.write_text(
        """
sync:
  stop_after_outer_steps: null
  stop_after_global_tokens: null
training:
  completion_mode: global_only
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires a configured global stop target"):
        resolve_config(path)


@pytest.mark.parametrize("mode", ["unknown", "adaptive_eta"])
def test_rejects_unsupported_grace_mode(tmp_path, mode):
    path = tmp_path / "bad_grace.yaml"
    path.write_text(f"sync:\n  grace_window:\n    mode: {mode}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported sync.grace_window.mode"):
        resolve_config(path)


def test_fragment_rejects_full_local_delta_rebase(tmp_path):
    path = tmp_path / "fragment_rebase.yaml"
    path.write_text(
        """
fragments:
  enabled: true
learner:
  global_adoption_strategy: rebase_post_publish_delta
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="only supported by the full learner"):
        resolve_config(path)
