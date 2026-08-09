"""Publish one immutable manual-close request for a Full Protocol v4 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..core.run_descriptor import load_run_descriptor
from ..storage.terminal_request import publish_manual_terminal_request


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--expected-descriptor-sha256")
    args = parser.parse_args(argv)
    loaded = load_run_descriptor(
        args.shared_root,
        expected_descriptor_sha256=args.expected_descriptor_sha256,
    )
    if loaded.config.shared.terminal.admission_close_policy != "manual":
        raise RuntimeError("manual close requires terminal.admission_close_policy=manual")
    payload = publish_manual_terminal_request(
        loaded.paths,
        run_id=loaded.identity.run_id,
        descriptor_sha256=str(loaded.descriptor["descriptor_sha256"]),
        reason=args.reason,
    )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
