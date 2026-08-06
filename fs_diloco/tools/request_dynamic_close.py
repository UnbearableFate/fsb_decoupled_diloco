"""Publish an authenticated manual-close request for a dynamic HA run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..core.run_descriptor import load_run_descriptor
from ..protocol.dynamic_terminal import write_dynamic_close_request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--expected-descriptor-sha256")
    args = parser.parse_args()
    loaded = load_run_descriptor(
        args.shared_root,
        expected_descriptor_sha256=args.expected_descriptor_sha256,
    )
    if loaded.config.membership.mode != "dynamic":
        raise RuntimeError("manual dynamic close requires a dynamic run")
    payload = write_dynamic_close_request(
        loaded.paths,
        run_id=loaded.identity.run_id,
        source_fingerprint=loaded.identity.source_fingerprint,
        config_sha256=loaded.identity.config_sha256,
    )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
