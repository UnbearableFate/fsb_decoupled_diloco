import pytest

from fs_diloco.observability.logging_utils import JsonlLogger
from fs_diloco.runtime import learner
from fs_diloco.storage.paths import RunPaths, prepare_run_dirs


def test_final_fragment_wait_heartbeats_until_timeout(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    clock = [0.0]
    heartbeat_times = []

    monkeypatch.setattr(learner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        learner.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    result = learner.wait_for_final_fragment_progress(
        paths=paths,
        target_global_merge_event=1,
        current_global_merge_event_fn=lambda: 0,
        no_progress_timeout_seconds=5.0,
        heartbeat_interval_seconds=2.0,
        poll_seconds=1.0,
        handle_latest_fn=lambda _latest: None,
        write_heartbeat_fn=lambda: heartbeat_times.append(clock[0]),
        logger=logger,
        read_latest_fn=lambda _paths, _version: None,
    )

    assert result == "timeout"
    assert heartbeat_times == [0.0, 2.0, 4.0]
    assert max(
        right - left for left, right in zip(heartbeat_times, heartbeat_times[1:])
    ) == 2.0
    assert clock[0] == 5.0


def test_final_fragment_wait_heartbeat_reflects_adopted_progress(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    clock = [0.0]
    current_event = [0]
    heartbeat_events = []
    delivered = [False]

    monkeypatch.setattr(learner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        learner.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    def read_latest(_paths, _version):
        if clock[0] >= 2.0 and not delivered[0]:
            delivered[0] = True
            return {"global_merge_event": 1}
        return None

    def handle_latest(payload):
        current_event[0] = int(payload["global_merge_event"])
        clock[0] += 2.5

    result = learner.wait_for_final_fragment_progress(
        paths=paths,
        target_global_merge_event=1,
        current_global_merge_event_fn=lambda: current_event[0],
        no_progress_timeout_seconds=10.0,
        heartbeat_interval_seconds=2.0,
        poll_seconds=1.0,
        handle_latest_fn=handle_latest,
        write_heartbeat_fn=lambda: heartbeat_events.append(
            (clock[0], current_event[0])
        ),
        logger=logger,
        read_latest_fn=read_latest,
    )

    assert result == "target_reached"
    assert heartbeat_events == [(0.0, 0), (2.0, 0), (4.5, 1)]
    assert heartbeat_events[-1][1] == 1


def test_final_fragment_wait_stops_without_extra_heartbeat(tmp_path):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    paths.stop_json.write_text("{}", encoding="utf-8")
    logger = JsonlLogger(paths.logs / "test.jsonl", "test", mirror_stdout=False)
    heartbeat_count = [0]

    result = learner.wait_for_final_fragment_progress(
        paths=paths,
        target_global_merge_event=1,
        current_global_merge_event_fn=lambda: 0,
        no_progress_timeout_seconds=10.0,
        heartbeat_interval_seconds=2.0,
        poll_seconds=1.0,
        handle_latest_fn=lambda _latest: None,
        write_heartbeat_fn=lambda: heartbeat_count.__setitem__(
            0, heartbeat_count[0] + 1
        ),
        logger=logger,
        read_latest_fn=lambda _paths, _version: None,
    )

    assert result == "stop_seen"
    assert heartbeat_count[0] == 0


def test_final_fragment_adoption_failure_still_attempts_stopped_heartbeat(tmp_path):
    paths = RunPaths(tmp_path)
    prepare_run_dirs(paths, 1)
    log_path = paths.logs / "test.jsonl"
    logger = JsonlLogger(log_path, "test", mirror_stdout=False)
    calls = []

    def fail_adoption():
        calls.append("adoption")
        raise RuntimeError("injected final adoption failure")

    with pytest.raises(RuntimeError, match="injected final adoption failure"):
        learner.finalize_fragment_adoption_and_heartbeat(
            final_adoption_fn=fail_adoption,
            write_stopped_heartbeat_fn=lambda: calls.append("stopped_heartbeat"),
            logger=logger,
        )

    assert calls == ["adoption", "stopped_heartbeat"]
    assert '"event_type": "final_fragment_adoption_failed"' in log_path.read_text()
    assert "injected final adoption failure" in log_path.read_text()
