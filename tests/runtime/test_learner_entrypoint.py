from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from fs_diloco.runtime import learner_entrypoint


def test_static_learner_admission_is_revalidated_before_runtime_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_config = tmp_path / "run/control/run_config.resolved.yaml"
    loaded = SimpleNamespace(
        paths=SimpleNamespace(resolved_config_yaml=resolved_config),
        descriptor={
            "mode": "static",
            "run_id": "run-current",
            "descriptor_sha256": "d" * 64,
            "static_learner_ids": ["learner_000"],
        },
        config=SimpleNamespace(
            membership=SimpleNamespace(
                mode="static",
                initial_membership_deadline_seconds=10.0,
                registration_request_ttl_seconds=10.0,
                registration_scan_interval_seconds=0.1,
            ),
            sync=SimpleNamespace(scan_interval_seconds=0.1),
            leader=SimpleNamespace(
                learner_recovery_wait_seconds=10.0,
                max_clock_skew_seconds=1.0,
            ),
        ),
    )
    admission = SimpleNamespace(fence={"binding_generation": 1})
    response_calls: list[dict[str, object]] = []
    runtime_calls: list[tuple[object, object]] = []
    fake_runtime = ModuleType("fs_diloco.runtime.learner")
    fake_runtime.run_admitted_learner = (  # type: ignore[attr-defined]
        lambda loaded_value, admission_value: runtime_calls.append((loaded_value, admission_value))
    )

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setitem(sys.modules, "fs_diloco.runtime.learner", fake_runtime)
    monkeypatch.setattr(learner_entrypoint, "load_run_descriptor", lambda *_a, **_kw: loaded)
    monkeypatch.setattr(learner_entrypoint, "new_attempt_id", lambda: "attempt-current")
    monkeypatch.setattr(learner_entrypoint, "highest_static_generation", lambda *_a: 0)
    monkeypatch.setattr(learner_entrypoint, "prepare_learner_instance_dir", lambda *_a: None)
    monkeypatch.setattr(
        learner_entrypoint,
        "publish_static_request_with_sha256",
        lambda *_a, **_kw: (tmp_path / "request.json", "r" * 64),
    )

    def read_response(*_args: object, **kwargs: object):
        response_calls.append(kwargs)
        return admission

    monkeypatch.setattr(learner_entrypoint, "read_admission_response", read_response)

    learner_entrypoint.main(
        [
            "--config",
            str(resolved_config),
            "--shared-root",
            str(tmp_path / "run"),
            "--learner-id",
            "learner_000",
            "--logical-launch-id",
            "launch-current",
            "--attempt-id",
            "attempt-current",
        ]
    )

    assert len(response_calls) == 2
    assert "expected_fence" not in response_calls[0]
    assert response_calls[1]["expected_fence"] == admission.fence
    assert runtime_calls == [(loaded, admission)]


def test_learner_parser_rejects_dynamic_and_static_identity_mixing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_config = tmp_path / "run/control/run_config.resolved.yaml"
    loaded = SimpleNamespace(
        paths=SimpleNamespace(resolved_config_yaml=resolved_config),
        descriptor={
            "mode": "dynamic",
            "run_id": "run-current",
            "descriptor_sha256": "d" * 64,
            "stream_pool_size": 2,
        },
        config=SimpleNamespace(membership=SimpleNamespace(mode="dynamic")),
    )
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(learner_entrypoint, "load_run_descriptor", lambda *_a, **_kw: loaded)

    with pytest.raises(ValueError, match="rejects static identity"):
        learner_entrypoint.main(
            [
                "--config",
                str(resolved_config),
                "--shared-root",
                str(tmp_path / "run"),
                "--learner-id",
                "learner_000",
                "--bootstrap-slot",
                "0",
            ]
        )
