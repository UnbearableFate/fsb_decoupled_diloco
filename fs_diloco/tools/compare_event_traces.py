"""Compare normalized per-actor JSONL event traces.

The comparator intentionally does not impose a total order across processes.
Each actor is compared independently, and only fields selected by an explicit
profile participate.  Before a profile is used as a regression gate for an
asynchronous pipeline, run the unchanged workload twice and verify that the
selected projection is itself repeatable.

Exit status is 0 for equivalent traces, 1 for a behavioral difference, and 2
for invalid input or profile data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class TraceInputError(ValueError):
    """Raised when trace or profile input cannot be interpreted safely."""


DEFAULT_STABLE_FIELDS = (
    "version",
    "previous_version",
    "current_version",
    "found_version",
    "global_version",
    "global_merge_event",
    "current_global_merge_event",
    "found_global_merge_event",
    "base_global_version",
    "base_global_merge_event",
    "fragment_id",
    "fragment_version",
    "fragment_versions",
    "fragments",
    "local_step",
    "local_step_start",
    "local_step_end",
    "inner_steps",
    "tokens",
    "tokens_this_update",
    "tokens_since_global_load",
    "carried_delta_tokens",
    "total_update_tokens",
    "total_seen_tokens",
    "prediction_base_version",
    "predicted_version",
    "prediction_update_id",
    "anchor_update_id",
    "update_id",
    "update_ids",
    "learner_id",
    "learners",
    "reason",
    "selected",
    "selected_count",
    "quorum_min",
    "quorum_max",
    "awaiting_global_stop",
    "optimizer_state_preserved",
    "scheduler_state_preserved",
    "bootstrapped_total_tokens",
)

OBSERVATIONAL_EVENTS = frozenset(
    {
        "heartbeat_written",
        "heartbeats_ingested",
        "learner_liveness_updated",
        "metadata_ingested",
        "quorum_wait",
        "fragment_quorum_wait",
    }
)


@dataclass(frozen=True)
class TraceProfile:
    name: str
    default_fields: tuple[str, ...]
    event_fields: Mapping[str, tuple[str, ...]]
    ignore_events: frozenset[str] = frozenset()

    def fields_for(self, event_type: str) -> tuple[str, ...]:
        return self.event_fields.get(event_type, self.default_fields)


@dataclass(frozen=True)
class NormalizedEvent:
    actor: str
    event_type: str
    fields: tuple[tuple[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "event_type": self.event_type,
            **dict(self.fields),
        }


@dataclass(frozen=True)
class TraceDivergence:
    actor: str
    index: int
    left: NormalizedEvent | None
    right: NormalizedEvent | None
    left_context: tuple[NormalizedEvent, ...]
    right_context: tuple[NormalizedEvent, ...]


@dataclass(frozen=True)
class TraceComparison:
    profile_name: str
    actors: tuple[str, ...]
    divergence: TraceDivergence | None = None

    @property
    def equivalent(self) -> bool:
        return self.divergence is None


BUILTIN_PROFILES: Mapping[str, TraceProfile] = {
    "default": TraceProfile(
        name="default-v1",
        default_fields=DEFAULT_STABLE_FIELDS,
        event_fields={},
    ),
    "learner-adoption": TraceProfile(
        name="learner-adoption-v1",
        default_fields=DEFAULT_STABLE_FIELDS,
        event_fields={},
        ignore_events=OBSERVATIONAL_EVENTS,
    ),
    "core-pipeline": TraceProfile(
        name="core-pipeline-v1",
        default_fields=DEFAULT_STABLE_FIELDS,
        event_fields={},
        ignore_events=OBSERVATIONAL_EVENTS,
    ),
}

_RANDOM_ID_SUFFIX = re.compile(
    r"^(?P<prefix>.+_\d{8}(?:_f\d{3})?)_[0-9a-fA-F]{8,}$"
)


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TraceInputError(f"profile {field_name} must be a list of strings")
    if len(set(value)) != len(value):
        raise TraceInputError(f"profile {field_name} contains duplicate entries")
    return tuple(value)


def load_profile(name_or_path: str) -> TraceProfile:
    if name_or_path in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name_or_path]
    path = Path(name_or_path)
    if not path.is_file():
        raise TraceInputError(f"unknown profile or profile file does not exist: {name_or_path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceInputError(f"could not read profile {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TraceInputError("profile must contain a JSON object")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise TraceInputError("profile name must be a non-empty string")
    default_fields = _string_list(payload.get("default_fields"), field_name="default_fields")
    raw_event_fields = payload.get("event_fields", {})
    if not isinstance(raw_event_fields, dict):
        raise TraceInputError("profile event_fields must be an object")
    event_fields: dict[str, tuple[str, ...]] = {}
    for event_type, fields in raw_event_fields.items():
        if not isinstance(event_type, str) or not event_type:
            raise TraceInputError("profile event_fields keys must be non-empty strings")
        event_fields[event_type] = _string_list(
            fields,
            field_name=f"event_fields.{event_type}",
        )
    ignore_events = frozenset(
        _string_list(payload.get("ignore_events", []), field_name="ignore_events")
    )
    return TraceProfile(
        name=name,
        default_fields=default_fields,
        event_fields=event_fields,
        ignore_events=ignore_events,
    )


def _normalize_identifier(value: str) -> str:
    match = _RANDOM_ID_SUFFIX.match(value)
    if match is not None:
        return f"{match.group('prefix')}_<id>"
    return value


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalize_identifier(value)
    if isinstance(value, list):
        return tuple(_normalize_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (str(key), _normalize_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _trace_files(root: str | Path) -> list[Path]:
    path = Path(root)
    if not path.exists():
        raise TraceInputError(f"trace input does not exist: {path}")
    if path.is_file():
        if path.suffix != ".jsonl":
            raise TraceInputError(f"trace file must use .jsonl: {path}")
        return [path]
    logs = path / "logs"
    search_root = logs if logs.is_dir() else path
    files = sorted(search_root.glob("*.jsonl"))
    if not files:
        raise TraceInputError(f"no JSONL event files found under: {search_root}")
    return files


def _actor_role(actor: str) -> str:
    if actor == "syncer":
        return "syncer"
    if actor.startswith("learner"):
        return "learner"
    return actor


def normalize_trace(
    root: str | Path,
    profile: TraceProfile,
    *,
    roles: set[str] | None = None,
    actors: set[str] | None = None,
) -> dict[str, tuple[NormalizedEvent, ...]]:
    normalized: dict[str, list[NormalizedEvent]] = {}
    for path in _trace_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TraceInputError(f"could not read trace file {path}: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TraceInputError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise TraceInputError(f"trace row must be an object in {path}:{line_number}")
            actor = row.get("actor")
            event_type = row.get("event_type")
            if not isinstance(actor, str) or not actor:
                raise TraceInputError(f"missing actor in {path}:{line_number}")
            if not isinstance(event_type, str) or not event_type:
                raise TraceInputError(f"missing event_type in {path}:{line_number}")
            if roles is not None and _actor_role(actor) not in roles:
                continue
            if actors is not None and actor not in actors:
                continue
            if event_type in profile.ignore_events:
                continue
            fields = tuple(
                (field, _normalize_value(row[field]))
                for field in profile.fields_for(event_type)
                if field in row
            )
            normalized.setdefault(actor, []).append(
                NormalizedEvent(actor=actor, event_type=event_type, fields=fields)
            )
    return {actor: tuple(events) for actor, events in sorted(normalized.items())}


def _first_difference(
    left: Sequence[NormalizedEvent],
    right: Sequence[NormalizedEvent],
) -> int | None:
    for index, (left_event, right_event) in enumerate(zip(left, right)):
        if left_event != right_event:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def compare_traces(
    left_root: str | Path,
    right_root: str | Path,
    profile: TraceProfile,
    *,
    roles: set[str] | None = None,
    actors: set[str] | None = None,
    context: int = 2,
) -> TraceComparison:
    if context < 0:
        raise TraceInputError("context must be >= 0")
    left = normalize_trace(left_root, profile, roles=roles, actors=actors)
    right = normalize_trace(right_root, profile, roles=roles, actors=actors)
    all_actors = tuple(sorted(set(left) | set(right)))
    for actor in all_actors:
        left_events = left.get(actor, ())
        right_events = right.get(actor, ())
        index = _first_difference(left_events, right_events)
        if index is None:
            continue
        start = max(0, index - context)
        end = index + context + 1
        return TraceComparison(
            profile_name=profile.name,
            actors=all_actors,
            divergence=TraceDivergence(
                actor=actor,
                index=index,
                left=left_events[index] if index < len(left_events) else None,
                right=right_events[index] if index < len(right_events) else None,
                left_context=tuple(left_events[start:end]),
                right_context=tuple(right_events[start:end]),
            ),
        )
    return TraceComparison(profile_name=profile.name, actors=all_actors)


def _event_dicts(events: Iterable[NormalizedEvent]) -> list[dict[str, Any]]:
    return [event.as_dict() for event in events]


def format_comparison(result: TraceComparison) -> str:
    if result.equivalent:
        actors = ",".join(result.actors) or "<none>"
        return f"equivalent profile={result.profile_name} actors={actors}"
    assert result.divergence is not None
    divergence = result.divergence
    return "\n".join(
        (
            f"different profile={result.profile_name} actor={divergence.actor} "
            f"index={divergence.index}",
            "left=" + json.dumps(
                divergence.left.as_dict() if divergence.left is not None else None,
                sort_keys=True,
            ),
            "right=" + json.dumps(
                divergence.right.as_dict() if divergence.right is not None else None,
                sort_keys=True,
            ),
            "left_context=" + json.dumps(_event_dicts(divergence.left_context), sort_keys=True),
            "right_context=" + json.dumps(_event_dicts(divergence.right_context), sort_keys=True),
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", help="left run, logs directory, or JSONL file")
    parser.add_argument("right", help="right run, logs directory, or JSONL file")
    parser.add_argument(
        "--profile",
        default="default",
        help="built-in profile name or JSON profile path",
    )
    parser.add_argument("--role", action="append", choices=("learner", "syncer"))
    parser.add_argument("--actor", action="append")
    parser.add_argument("--context", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profile = load_profile(args.profile)
        result = compare_traces(
            args.left,
            args.right,
            profile,
            roles=set(args.role) if args.role else None,
            actors=set(args.actor) if args.actor else None,
            context=args.context,
        )
    except TraceInputError as exc:
        print(f"trace input error: {exc}", file=sys.stderr)
        return 2
    print(format_comparison(result))
    return 0 if result.equivalent else 1


if __name__ == "__main__":
    raise SystemExit(main())
