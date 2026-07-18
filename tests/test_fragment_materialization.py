from pathlib import Path

import pytest
from fs_diloco.core.config import resolve_config
from fs_diloco.modeling.hf_model import TinyCausalLM
from fs_diloco.modeling.param_index import build_param_index, flatten_trainable_params
from fs_diloco.protocol.fragment_codec import extract_fragment
from fs_diloco.protocol.fragment_index import build_fragment_index
from fs_diloco.runtime.syncer import (
    publish_fragment_latest,
    should_materialize_fragment_full,
)
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs


@pytest.mark.parametrize("value", [None, 0, -1])
def test_fragment_materialization_interval_is_required_positive(tmp_path, value):
    path = tmp_path / "fragment.yaml"
    rendered = "null" if value is None else str(value)
    path.write_text(
        "fragments:\n"
        "  enabled: true\n"
        "  strategy: full\n"
        "  num_fragments: 1\n"
        f"  materialize_full_every_events: {rendered}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="materialize_full_every_events.*positive"):
        resolve_config(path)


def test_materialization_schedule_has_no_implicit_none_branch():
    config = resolve_config("configs/fs_diloco_tiny_fragment_local.yaml")
    config.fragments.materialize_full_every_events = 10
    config.sync.stop_after_outer_steps = 50

    assert should_materialize_fragment_full(config, 0)
    assert not should_materialize_fragment_full(config, 1)
    assert should_materialize_fragment_full(config, 10)
    assert should_materialize_fragment_full(config, 50)


def test_force_materialization_captures_nonperiodic_terminal_event(tmp_path):
    config = resolve_config(
        "configs/fs_diloco_tiny_fragment_local.yaml",
        run_id="force-terminal-materialization",
        shared_root=str(tmp_path),
    )
    config.fragments.materialize_full_every_events = 10
    config.sync.stop_after_outer_steps = None
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    model = TinyCausalLM(vocab_size=8, hidden_size=4)
    param_index = build_param_index(model, model_name_or_path="synthetic-tiny")
    flat = flatten_trainable_params(model, param_index)
    fragment_index = build_fragment_index(
        param_index,
        strategy="full",
        num_fragments=1,
        source_param_index_path=paths.param_index_json,
    )
    fragment_thetas = {0: extract_fragment(flat, fragment_index, 0)}

    publication = publish_fragment_latest(
        config=config,
        paths=paths,
        param_index=param_index,
        fragment_index=fragment_index,
        fragment_thetas=fragment_thetas,
        fragment_versions={0: 3},
        fragment_updated_events={0: 3},
        total_seen_tokens=1,
        global_merge_event=3,
        previous_materialized_weight_path=Path("older.safetensors"),
        force_materialize=True,
    )

    assert publication.materialized_this_event is True
    assert publication.materialized_bytes > 0
    assert publication.materialize_full_seconds >= 0.0
    assert publication.materialized_weight_path == paths.global_weight_path(3)
    assert publication.materialized_weight_path.is_file()
