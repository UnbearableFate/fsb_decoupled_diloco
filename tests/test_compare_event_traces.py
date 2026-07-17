import json

import pytest

from fs_diloco.tools.compare_event_traces import (
    TraceInputError,
    compare_traces,
    load_profile,
    main,
)


def _write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def test_equivalent_traces_ignore_runtime_identity_fields(tmp_path):
    left = tmp_path / "left" / "logs" / "learner_000.jsonl"
    right = tmp_path / "right" / "logs" / "learner_000.jsonl"
    _write_events(
        left,
        [
            {
                "timestamp": 1.0,
                "actor": "learner_000",
                "event_type": "update_written",
                "hostname": "mg0001",
                "run_id": "left-run",
                "update_id": "learner_000_00000002_aaaaaaaaaaaa",
                "file_path": "/left/run/update.safetensors",
                "local_step": 2,
                "tokens": 64,
            }
        ],
    )
    _write_events(
        right,
        [
            {
                "timestamp": 9.0,
                "actor": "learner_000",
                "event_type": "update_written",
                "hostname": "mg0007",
                "run_id": "right-run",
                "update_id": "learner_000_00000002_bbbbbbbbbbbb",
                "file_path": "/right/run/update.safetensors",
                "local_step": 2,
                "tokens": 64,
            }
        ],
    )

    result = compare_traces(tmp_path / "left", tmp_path / "right", load_profile("default"))

    assert result.equivalent
    assert result.actors == ("learner_000",)


@pytest.mark.parametrize(
    ("right_events", "expected_index"),
    [
        (
            [
                {"actor": "learner_000", "event_type": "loaded_global", "version": 0},
                {"actor": "learner_000", "event_type": "global_adopted", "version": 2},
            ],
            1,
        ),
        (
            [{"actor": "learner_000", "event_type": "global_adopted", "version": 1}],
            0,
        ),
        (
            [
                {"actor": "learner_000", "event_type": "global_adopted", "version": 1},
                {"actor": "learner_000", "event_type": "loaded_global", "version": 0},
            ],
            0,
        ),
    ],
)
def test_trace_difference_reports_first_divergence(tmp_path, right_events, expected_index):
    left_events = [
        {"actor": "learner_000", "event_type": "loaded_global", "version": 0},
        {"actor": "learner_000", "event_type": "global_adopted", "version": 1},
    ]
    _write_events(tmp_path / "left" / "learner_000.jsonl", left_events)
    _write_events(tmp_path / "right" / "learner_000.jsonl", right_events)

    result = compare_traces(tmp_path / "left", tmp_path / "right", load_profile("default"))

    assert not result.equivalent
    assert result.divergence is not None
    assert result.divergence.actor == "learner_000"
    assert result.divergence.index == expected_index


def test_profile_and_role_filter_exclude_observational_events(tmp_path):
    for side, eligible in (("left", 0), ("right", 1)):
        _write_events(
            tmp_path / side / "logs" / "syncer.jsonl",
            [
                {"actor": "syncer", "event_type": "quorum_wait", "eligible": eligible},
                {"actor": "syncer", "event_type": "global_published", "version": 1},
            ],
        )
        _write_events(
            tmp_path / side / "logs" / "learner_000.jsonl",
            [{"actor": "learner_000", "event_type": "process_exit", "local_step": 4}],
        )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "core-syncer",
                "default_fields": ["version", "eligible"],
                "ignore_events": ["quorum_wait"],
                "event_fields": {},
            }
        ),
        encoding="utf-8",
    )

    result = compare_traces(
        tmp_path / "left",
        tmp_path / "right",
        load_profile(str(profile_path)),
        roles={"syncer"},
    )

    assert result.equivalent
    assert result.actors == ("syncer",)


def test_invalid_input_and_profile_use_cli_exit_two(tmp_path, capsys):
    assert main([str(tmp_path / "missing-left"), str(tmp_path / "missing-right")]) == 2
    assert "does not exist" in capsys.readouterr().err

    profile_path = tmp_path / "bad-profile.json"
    profile_path.write_text('{"name": "bad", "default_fields": "version"}', encoding="utf-8")
    with pytest.raises(TraceInputError, match="default_fields"):
        load_profile(str(profile_path))
