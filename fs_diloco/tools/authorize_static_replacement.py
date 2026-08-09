"""Publish an explicit operator request to replace one active static attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..protocol.admission_v4 import publish_static_replacement_authorization
from ..protocol.contributor import StaticContributorFence
from ..storage.paths import RunPaths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--descriptor-sha256", required=True)
    parser.add_argument("--learner-id", required=True)
    parser.add_argument("--old-logical-launch-id", required=True)
    parser.add_argument("--old-attempt-id", required=True)
    parser.add_argument("--old-binding-generation", required=True, type=int)
    parser.add_argument("--new-logical-launch-id", required=True)
    parser.add_argument("--new-attempt-id", required=True)
    parser.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    paths = RunPaths(Path(args.shared_root).resolve())
    old_fence = StaticContributorFence(
        kind="static",
        learner_id=args.learner_id,
        logical_launch_id=args.old_logical_launch_id,
        attempt_id=args.old_attempt_id,
        binding_generation=args.old_binding_generation,
    )
    target = publish_static_replacement_authorization(
        paths,
        run_id=args.run_id,
        descriptor_sha256=args.descriptor_sha256,
        old_fence=old_fence,
        new_logical_launch_id=args.new_logical_launch_id,
        new_attempt_id=args.new_attempt_id,
        reason=args.reason,
    )
    print(json.dumps({"authorization_path": str(target)}, sort_keys=True))


if __name__ == "__main__":
    main()
