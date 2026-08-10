"""Fail-closed identity checks shared by Hugging Face producers."""

from __future__ import annotations

from pathlib import Path


_EXPLICIT_LOCAL_PREFIXES = ("/", "./", "../", "~/", "file://")


def reject_local_reference(reference: str, *, kind: str) -> None:
    """Reject references whose bytes are not bound by a Hub commit identity."""

    if reference.startswith(_EXPLICIT_LOCAL_PREFIXES):
        raise ValueError(
            f"local {kind} reference requires descriptor-bound content identity, "
            "which the Full Protocol does not support"
        )
    try:
        path = Path(reference).expanduser()
        path.lstat()
    except FileNotFoundError:
        return
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"cannot prove {kind} reference is non-local: {reference!r}"
        ) from exc
    raise ValueError(
        f"local {kind} reference requires descriptor-bound content identity, "
        "which the Full Protocol does not support"
    )
