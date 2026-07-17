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
    assert config.syncer.device == "auto"
    assert config.syncer.compute_dtype == "float32"
    assert config.syncer.publish_dtype == "float32"


def test_syncer_runtime_config_accepts_cpu_bfloat16_aliases(tmp_path):
    path = tmp_path / "syncer_bf16.yaml"
    path.write_text(
        """
syncer:
  device: CPU
  compute_dtype: bf16
  publish_dtype: torch.bfloat16
""",
        encoding="utf-8",
    )
    config = resolve_config(path)
    assert config.syncer.device == "cpu"
    assert config.syncer.compute_dtype == "bfloat16"
    assert config.syncer.publish_dtype == "bfloat16"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device", "gpu"),
        ("compute_dtype", "float16"),
        ("publish_dtype", "int8"),
    ],
)
def test_syncer_runtime_config_rejects_unsupported_values(tmp_path, field, value):
    path = tmp_path / "bad_syncer.yaml"
    path.write_text(f"syncer:\n  {field}: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"unsupported syncer.{field}"):
        resolve_config(path)


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


def test_wait_only_5000_config_uses_post_publish_grace_without_rebase():
    config = resolve_config(
        "configs/fs_diloco_gpt2_wikitext2_8l_5000steps_wait2p5.yaml"
    )
    assert config.training.max_local_steps == 5000
    assert config.model.dtype == "bfloat16"
    assert config.io.tensor_dtype == "bfloat16"
    assert config.sync.max_staleness_versions == 2
    assert config.learner.poll_latest_during_inner_steps is False
    assert config.learner.global_adoption_strategy == "replace"
    assert config.learner.post_publish_latest_wait_seconds == 2.5
    assert config.learner.post_publish_latest_poll_seconds == 0.2


@pytest.mark.parametrize(
    ("path", "wait_seconds"),
    [
        ("configs/fs_diloco_gpt2_wikitext2_8l_5000steps_predict.yaml", 0.0),
        (
            "configs/fs_diloco_gpt2_wikitext2_8l_5000steps_wait2p5_predict.yaml",
            2.5,
        ),
    ],
)
def test_predict_5000_configs_differ_only_in_post_publish_wait(path, wait_seconds):
    config = resolve_config(path)
    assert config.training.max_local_steps == 5000
    assert config.model.dtype == "bfloat16"
    assert config.io.tensor_dtype == "bfloat16"
    assert config.sync.max_staleness_versions == 2
    assert config.learner.poll_latest_during_inner_steps is True
    assert config.learner.global_adoption_strategy == "predict_post_publish_global"
    assert config.learner.post_publish_latest_wait_seconds == wait_seconds
    assert config.learner.prediction_reconcile_timeout_seconds == 60.0


def test_predict_wait_zero_5000_config_stops_only_at_global_target():
    config = resolve_config(
        "configs/fs_diloco_gpt2_wikitext2_8l_5000steps_predict.yaml"
    )
    assert config.training.max_local_steps == 5000
    assert config.training.completion_mode == "global_only"
    assert config.sync.stop_after_outer_steps == 50
    assert config.sync.grace_window.mode == "fixed"
    assert config.sync.grace_window.fixed_seconds == 20.0
    assert config.learner.global_adoption_strategy == "predict_post_publish_global"
    assert config.learner.post_publish_latest_wait_seconds == 0.0


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


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("post_publish_latest_wait_seconds", -0.1, "must be >= 0"),
        ("post_publish_latest_poll_seconds", 0.0, "must be > 0"),
    ],
)
def test_post_publish_wait_config_rejects_invalid_values(tmp_path, key, value, message):
    path = tmp_path / "invalid_wait.yaml"
    path.write_text(f"learner:\n  {key}: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        resolve_config(path)


def test_predict_global_requires_stepwise_polling(tmp_path):
    path = tmp_path / "invalid_predict.yaml"
    path.write_text(
        """
learner:
  global_adoption_strategy: predict_post_publish_global
  poll_latest_during_inner_steps: false
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires adopt_global_after_upload"):
        resolve_config(path)
