from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fs_diloco.tools import request_terminal_close


def test_manual_close_cli_binds_descriptor_identity_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shared_root = tmp_path / "run"
    loaded = SimpleNamespace(
        config=SimpleNamespace(
            terminal=SimpleNamespace(admission_close_policy="manual"),
        ),
        paths=object(),
        identity=SimpleNamespace(run_id="run-current"),
        descriptor={"descriptor_sha256": "d" * 64},
    )
    load_calls: list[tuple[Path, str | None]] = []
    publish_calls: list[dict[str, object]] = []

    def load(path: Path, *, expected_descriptor_sha256: str | None = None):
        load_calls.append((path, expected_descriptor_sha256))
        return loaded

    def publish(_paths: object, **kwargs: object) -> dict[str, object]:
        publish_calls.append(kwargs)
        return {"kind": "manual_terminal_close", **kwargs}

    monkeypatch.setattr(request_terminal_close, "load_run_descriptor", load)
    monkeypatch.setattr(request_terminal_close, "publish_manual_terminal_request", publish)

    request_terminal_close.main(
        [
            "--shared-root",
            str(shared_root),
            "--reason",
            "operator maintenance",
            "--expected-descriptor-sha256",
            "d" * 64,
        ]
    )

    assert load_calls == [(shared_root, "d" * 64)]
    assert publish_calls == [
        {
            "run_id": "run-current",
            "descriptor_sha256": "d" * 64,
            "reason": "operator maintenance",
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "kind": "manual_terminal_close",
        **publish_calls[0],
    }


def test_manual_close_cli_rejects_a_nonmanual_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = SimpleNamespace(
        config=SimpleNamespace(
            terminal=SimpleNamespace(admission_close_policy="global_target"),
        )
    )
    monkeypatch.setattr(request_terminal_close, "load_run_descriptor", lambda *_args, **_kw: loaded)

    with pytest.raises(RuntimeError, match="admission_close_policy=manual"):
        request_terminal_close.main(
            ["--shared-root", str(tmp_path / "run"), "--reason", "operator maintenance"]
        )
