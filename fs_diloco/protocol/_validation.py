"""Strict, dependency-free validation primitives for protocol boundaries."""

from __future__ import annotations

import math
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_JSON_BYTES = 256 * 1024


def decode_json_object(
    raw: bytes | str,
    *,
    name: str,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
) -> Mapping[str, Any]:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes):
        raise ValueError(f"{name} must be UTF-8 JSON bytes or text")
    if len(encoded) > max_bytes:
        raise ValueError(f"{name} exceeds the {max_bytes}-byte limit")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        decoded = json.loads(encoded, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not valid UTF-8 JSON") from exc
    return require_mapping(decoded, name=name)


def require_mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def require_exact_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    name: str,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise ValueError(f"{name} is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(unknown)}")


def strict_int(value: Any, *, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def strict_float(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def strict_bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def nonempty_string(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def identity(value: Any, *, name: str) -> str:
    result = nonempty_string(value, name=name)
    if _IDENTITY_RE.fullmatch(result) is None:
        raise ValueError(f"{name} is not a safe protocol identity")
    return result


def sha256(value: Any, *, name: str) -> str:
    result = nonempty_string(value, name=name)
    if _SHA256_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return result


def uuid4_string(value: Any, *, name: str) -> str:
    result = nonempty_string(value, name=name)
    try:
        parsed = uuid.UUID(result)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != result:
        raise ValueError(f"{name} must be a canonical UUID4")
    return result
