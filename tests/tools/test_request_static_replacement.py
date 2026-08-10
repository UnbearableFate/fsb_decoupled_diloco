from __future__ import annotations

from pathlib import Path

import pytest

from fs_diloco.tools import authorize_static_replacement


def test_authorization_collision_tells_operator_to_issue_a_fresh_attempt_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def collide(*_args: object, **_kwargs: object) -> Path:
        raise FileExistsError("immutable target collision")

    monkeypatch.setattr(
        authorize_static_replacement,
        "publish_static_replacement_authorization",
        collide,
    )
    with pytest.raises(SystemExit) as raised:
        authorize_static_replacement.main(
            [
                "--shared-root",
                str(tmp_path),
                "--run-id",
                "run-1",
                "--descriptor-sha256",
                "a" * 64,
                "--learner-id",
                "learner_000",
                "--old-logical-launch-id",
                "launch-old",
                "--old-attempt-id",
                "attempt-old",
                "--old-binding-generation",
                "1",
                "--new-logical-launch-id",
                "launch-new",
                "--new-attempt-id",
                "attempt-colliding",
                "--reason",
                "operator recovery",
            ]
        )

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "immutable authorization collision" in error
    assert "fresh --new-attempt-id" in error
