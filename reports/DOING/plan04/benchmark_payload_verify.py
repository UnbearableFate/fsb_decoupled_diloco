"""Measure proposal verification against one immutable formal-size payload."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from fs_diloco.protocol.proposal import FullUpdateProposalV2
from fs_diloco.storage.object_store import verify_proposal_payload


def main() -> None:
    """Verify one latest proposal and print a machine-readable timing record."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    latest_path = sorted((run_root / "updates" / "latest").glob("*.json"))[0]
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    proposal_path = run_root / str(latest["proposal_path"])
    proposal = FullUpdateProposalV2.from_json(proposal_path.read_bytes())
    started = time.perf_counter()
    result = verify_proposal_payload(run_root, proposal)
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "elapsed_seconds": elapsed,
                "payload_size_bytes": proposal.payload_size,
                "proposal_path": str(proposal_path),
                "status": result.status.value,
            },
            sort_keys=True,
        )
    )
    if result.value is None:
        raise RuntimeError(f"payload verification failed: {result.status.value}")


if __name__ == "__main__":
    main()
