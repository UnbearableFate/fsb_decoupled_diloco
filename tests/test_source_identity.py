from fs_diloco.core.config import resolve_config
from fs_diloco.runtime.syncer import run_identity


def test_source_identity_is_recorded_from_frozen_launcher_environment(monkeypatch):
    monkeypatch.setenv("FS_DILOCO_GIT_COMMIT", "abc123")
    monkeypatch.setenv("FS_DILOCO_GIT_DIRTY", "1")
    monkeypatch.setenv("FS_DILOCO_SOURCE_FINGERPRINT", "sha256:deadbeef")

    config = resolve_config("configs/fs_diloco_tiny_local.yaml")

    assert config.run.git_commit == "abc123"
    assert config.run.git_dirty is True
    assert config.run.source_fingerprint == "sha256:deadbeef"
    assert run_identity(config) == {
        **run_identity(resolve_config("configs/fs_diloco_tiny_local.yaml")),
        "git_commit": "abc123",
        "git_dirty": True,
        "source_fingerprint": "sha256:deadbeef",
    }


def test_required_source_identity_fails_closed(monkeypatch):
    monkeypatch.setenv("FS_DILOCO_REQUIRE_SOURCE_IDENTITY", "1")
    monkeypatch.delenv("FS_DILOCO_GIT_COMMIT", raising=False)
    monkeypatch.delenv("FS_DILOCO_SOURCE_FINGERPRINT", raising=False)

    try:
        resolve_config("configs/fs_diloco_tiny_local.yaml")
    except ValueError as exc:
        assert "source identity" in str(exc)
    else:
        raise AssertionError("required source identity was silently omitted")
