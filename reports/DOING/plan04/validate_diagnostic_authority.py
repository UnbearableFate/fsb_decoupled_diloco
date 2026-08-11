"""Exercise the current scenario oracle against one completed diagnostic run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from do_experiments.experiment04.scenario_supervisor import (
    SCENARIOS,
    _final_authority_evidence,
)


def main() -> None:
    """Validate one normal authority and print a compact durable-history projection."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("first_syncer_job_id")
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    evidence = _final_authority_evidence(
        run_root / "control" / "syncer_metadata.sqlite3",
        run_root=run_root,
        scenario=SCENARIOS["normal"],
        first_syncer_job_id=args.first_syncer_job_id,
        second_syncer_job_id=None,
        victim=None,
        replacement=None,
    )
    print(
        json.dumps(
            {
                "final_version": evidence["terminal"]["final_version"],
                "merge_counts": evidence["merge_counts"],
                "syncer_epochs": len(evidence["syncer_epochs"]),
                "terminal_fences": len(evidence["terminal_contributor_fences"]),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
