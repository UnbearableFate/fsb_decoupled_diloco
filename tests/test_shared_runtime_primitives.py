from pathlib import Path

import pytest

from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.learner import apply_fragment_adoption
from fs_diloco.runtime.syncer import (
    collect_with_grace_window,
    drop_missing_update_files,
    fragment_update_proposal_source,
    full_update_proposal_source,
)


class RecordingLogger:
    def __init__(self):
        self.events = []

    def event(self, event_type, **payload):
        self.events.append((event_type, payload))


class FakeProposalStore:
    def __init__(self, rows):
        self.rows = rows
        self.dropped = []

    def eligible_updates(self, current_version, max_staleness_versions):
        assert (current_version, max_staleness_versions) == (3, 2)
        return list(self.rows)

    def eligible_fragment_updates(
        self, *, fragment_id, current_fragment_version, max_staleness_versions
    ):
        assert (fragment_id, current_fragment_version, max_staleness_versions) == (1, 4, 2)
        return list(self.rows)

    def drop_updates(self, update_ids, reason):
        self.dropped.append(("full", list(update_ids), reason))

    def drop_fragment_updates(self, update_ids, reason):
        self.dropped.append(("fragment", list(update_ids), reason))


@pytest.mark.parametrize(
    ("source_factory", "source_kwargs", "source_kind", "missing_event", "context_fields"),
    [
        (
            full_update_proposal_source,
            {"current_version": 3, "max_staleness_versions": 2},
            "full",
            "updates_dropped_missing_files",
            {"version": 3},
        ),
        (
            fragment_update_proposal_source,
            {
                "fragment_id": 1,
                "current_fragment_version": 4,
                "max_staleness_versions": 2,
            },
            "fragment",
            "fragment_updates_dropped_missing_files",
            {"fragment_id": 1, "fragment_version": 4},
        ),
    ],
)
def test_parameterized_proposal_source_preserves_selection_and_missing_file_behavior(
    tmp_path,
    source_factory,
    source_kwargs,
    source_kind,
    missing_event,
    context_fields,
):
    present = tmp_path / "present.safetensors"
    present.write_bytes(b"payload")
    rows = [
        {
            "update_id": "present",
            "learner_id": "learner_000",
            "local_step_end": 1,
            "file_path": str(present),
        },
        {
            "update_id": "missing",
            "learner_id": "learner_001",
            "local_step_end": 1,
            "file_path": str(tmp_path / "missing.safetensors"),
        },
    ]
    store = FakeProposalStore(rows)
    logger = RecordingLogger()
    source = source_factory(**source_kwargs)
    config = resolve_config("configs/fs_diloco_tiny_local.yaml")
    config.sync.grace_window.fixed_seconds = 0.0
    config.sync.quorum_max = 2

    selected = collect_with_grace_window(
        store,
        Path("unused"),
        config,
        logger,
        source=source,
    )

    assert [row["update_id"] for row in selected] == ["present"]
    assert store.dropped == [(source_kind, ["missing"], "missing_file")]
    assert logger.events[0] == (
        "grace_window_started",
        {
            "mode": "fixed",
            "initial_seconds": 0.0,
            **context_fields,
        },
    )
    assert logger.events[1] == (
        missing_event,
        {"count": 1, "update_ids": ["missing"]},
    )
    assert logger.events[-1][0] == "grace_window_completed"
    assert {key: logger.events[-1][1][key] for key in context_fields} == context_fields


def test_parameterized_missing_file_helper_can_be_used_before_grace_window(tmp_path):
    row = {
        "update_id": "missing",
        "learner_id": "learner_000",
        "local_step_end": 1,
        "file_path": str(tmp_path / "missing.safetensors"),
    }
    store = FakeProposalStore([row])
    logger = RecordingLogger()
    source = full_update_proposal_source(current_version=3, max_staleness_versions=2)

    assert drop_missing_update_files(store, [row], logger, source=source) == []
    assert store.dropped == [("full", ["missing"], "missing_file")]


@pytest.mark.parametrize(
    (
        "event_type",
        "reset_tokens",
        "reset_optimizer",
        "include_fragment_versions",
        "expected_event_types",
        "expected_tokens",
        "optimizer_rebuilt",
    ),
    [
        (
            "fragments_adopted",
            True,
            True,
            True,
            ["inner_optimizer_reset", "fragments_adopted"],
            {0: 0, 1: 0},
            True,
        ),
        (
            "fragments_adopted",
            True,
            True,
            True,
            ["inner_optimizer_reset", "fragments_adopted"],
            {0: 0, 1: 0},
            True,
        ),
        (
            "final_wait_fragments_adopted",
            True,
            False,
            False,
            ["final_wait_fragments_adopted"],
            {0: 0, 1: 0},
            False,
        ),
        (
            "final_fragments_adopted",
            False,
            False,
            False,
            ["final_fragments_adopted"],
            {0: 10, 1: 20},
            False,
        ),
    ],
    ids=["inner-poll", "after-upload", "final-wait", "final-latest"],
)
def test_fragment_adoption_helper_preserves_all_four_call_contexts(
    event_type,
    reset_tokens,
    reset_optimizer,
    include_fragment_versions,
    expected_event_types,
    expected_tokens,
    optimizer_rebuilt,
):
    config = resolve_config("configs/fs_diloco_tiny_fragment_local.yaml")
    config.fragments.reset_inner_optimizer_on_fragment_adopt = True
    logger = RecordingLogger()
    old_optimizer = object()
    old_scheduler = object()

    class FakeOptimizer:
        state = {}
        param_groups = [{"lr": 0.25}]

    class FakeScheduler:
        last_epoch = 6

    new_optimizer = FakeOptimizer()
    new_scheduler = FakeScheduler()

    def adopt_fn(**kwargs):
        versions = dict(kwargs["last_loaded_fragment_versions"])
        versions[0] = 3
        versions[1] = 4
        return 7, versions, [0, 1]

    result = apply_fragment_adoption(
        model=object(),
        latest={"global_merge_event": 7},
        param_index={},
        fragment_index={},
        last_loaded_fragment_versions={0: 2, 1: 3},
        tokens_since_fragment_load={0: 10, 1: 20},
        fragment_adopt_count=5,
        last_adopted_fragments=[1],
        optimizer=old_optimizer,
        scheduler=old_scheduler,
        device=object(),
        config=config,
        logger=logger,
        event_type=event_type,
        reset_tokens=reset_tokens,
        reset_optimizer=reset_optimizer,
        include_fragment_versions=include_fragment_versions,
        completed_local_steps=6,
        adopt_fn=adopt_fn,
        rebuild_inner_state_fn=lambda *_args, **_kwargs: (new_optimizer, new_scheduler),
    )

    assert result.global_merge_event == 7
    assert result.fragment_versions == {0: 3, 1: 4}
    assert result.fragment_adopt_count == 7
    assert result.last_adopted_fragments == [0, 1]
    assert result.tokens_since_fragment_load == expected_tokens
    assert (result.optimizer is new_optimizer) is optimizer_rebuilt
    assert (result.scheduler is new_scheduler) is optimizer_rebuilt
    assert [event for event, _payload in logger.events] == expected_event_types
    adoption_payload = logger.events[-1][1]
    assert adoption_payload["global_merge_event"] == 7
    assert adoption_payload["fragments"] == [0, 1]
    assert ("fragment_versions" in adoption_payload) is include_fragment_versions
