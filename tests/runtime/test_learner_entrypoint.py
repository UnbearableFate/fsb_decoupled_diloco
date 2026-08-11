"""Verify Torch-free learner admission and the sole current command line."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from fs_diloco.runtime import learner_entrypoint


def _loaded(tmp_path: Path) -> SimpleNamespace:
    """Build the minimal unique-protocol learner descriptor fixture."""

    return SimpleNamespace(
        paths=SimpleNamespace(
            resolved_config_yaml=tmp_path / "run/control/run_config.resolved.yaml"
        ),
        descriptor={
            "run_id": "run-current",
            "descriptor_sha256": "d" * 64,
            "stream_pool_size": 2,
        },
        config=SimpleNamespace(
            membership=SimpleNamespace(
                initial_membership_deadline_seconds=10.0,
                registration_request_ttl_seconds=10.0,
                registration_scan_interval_seconds=0.1,
            ),
            sync=SimpleNamespace(scan_interval_seconds=0.1),
            leader=SimpleNamespace(max_clock_skew_seconds=1.0),
        ),
    )


def test_bootstrap_admission_is_revalidated_before_runtime_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bootstrap fence is read twice before the Torch-dependent runtime imports."""

    loaded = _loaded(tmp_path)
    admission = SimpleNamespace(
        fence={
            "instance_id": "instance-current",
            "placement_id": "placement-current",
            "placement_epoch": 1,
            "stream_id": 0,
            "stream_epoch": 1,
            "admission_generation": 1,
            "admission_token_sha256": "a" * 64,
        }
    )
    request_calls: list[dict[str, object]] = []
    response_calls: list[dict[str, object]] = []
    runtime_calls: list[tuple[object, object]] = []
    fake_runtime = ModuleType("fs_diloco.runtime.learner")
    fake_runtime.run_admitted_learner = (  # type: ignore[attr-defined]
        lambda loaded_value, admission_value: runtime_calls.append((loaded_value, admission_value))
    )

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setitem(sys.modules, "fs_diloco.runtime.learner", fake_runtime)
    monkeypatch.setattr(learner_entrypoint, "load_run_descriptor", lambda *_a, **_kw: loaded)
    monkeypatch.setattr(learner_entrypoint, "prepare_learner_instance_dir", lambda *_a: None)

    def publish_request(*_args: object, **kwargs: object) -> tuple[Path, str]:
        """Record the bootstrap request without touching the filesystem."""

        request_calls.append(kwargs)
        return tmp_path / "request.json", "r" * 64

    monkeypatch.setattr(
        learner_entrypoint,
        "publish_admission_request_with_sha256",
        publish_request,
    )

    def read_response(*_args: object, **kwargs: object) -> object:
        """Return one stable admission while recording both validation reads."""

        response_calls.append(kwargs)
        return admission

    monkeypatch.setattr(learner_entrypoint, "read_admission_response", read_response)

    learner_entrypoint.main(
        [
            "--config",
            str(loaded.paths.resolved_config_yaml),
            "--shared-root",
            str(tmp_path / "run"),
            "--bootstrap-slot",
            "0",
        ]
    )

    assert request_calls[0]["bootstrap_slot"] == 0
    assert request_calls[0]["stream_id"] == 0
    assert request_calls[0]["launch_request_id"] is None
    assert len(response_calls) == 2
    assert "expected_fence" not in response_calls[0]
    assert response_calls[1]["expected_fence"] == admission.fence
    assert runtime_calls == [(loaded, admission)]


@pytest.mark.parametrize(
    "legacy_option",
    ["--learner-id", "--logical-launch-id", "--attempt-id"],
)
def test_learner_parser_rejects_removed_identity_options(legacy_option: str) -> None:
    """Removed static identity options are absent instead of accepted as aliases."""

    with pytest.raises(SystemExit):
        learner_entrypoint.build_parser().parse_args(
            [
                "--config",
                "resolved.yaml",
                "--shared-root",
                "/run",
                "--bootstrap-slot",
                "0",
                legacy_option,
                "legacy-value",
            ]
        )


@pytest.mark.parametrize(
    "identity_args",
    [
        ["--bootstrap-slot", "0", "--stream-id", "0"],
        ["--bootstrap-slot", "0", "--replace-instance-id", "old"],
        ["--launch-request-id", "launch-1"],
    ],
)
def test_learner_rejects_inconsistent_current_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_args: list[str],
) -> None:
    """Bootstrap and launch authorization fields remain mutually consistent."""

    loaded = _loaded(tmp_path)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(learner_entrypoint, "load_run_descriptor", lambda *_a, **_kw: loaded)

    with pytest.raises(ValueError, match="bootstrap admission|requires --stream-id"):
        learner_entrypoint.main(
            [
                "--config",
                str(loaded.paths.resolved_config_yaml),
                "--shared-root",
                str(tmp_path / "run"),
                *identity_args,
            ]
        )
