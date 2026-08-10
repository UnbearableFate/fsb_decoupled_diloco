from __future__ import annotations

import sys
from types import ModuleType

import pytest

from fs_diloco import cli


@pytest.mark.parametrize(
    ("command", "module_name"),
    [
        ("syncer", "fs_diloco.syncer"),
        ("learner", "fs_diloco.learner"),
        ("inspect", "fs_diloco.tools.analysis"),
        ("close", "fs_diloco.tools.request_terminal_close"),
    ],
)
def test_dispatch_forwards_remaining_arguments(
    command: str,
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str] | None] = []
    fake = ModuleType(module_name)
    fake.main = lambda argv=None: received.append(argv)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)

    cli.main([command, "--sentinel", "value"])

    assert received == [["--sentinel", "value"]]
