"""Convenience command dispatcher."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["syncer", "learner", "inspect", "close"])
    args, rest = parser.parse_known_args(argv)
    if args.command == "syncer":
        from .syncer import main as syncer_main

        syncer_main(rest)
    elif args.command == "learner":
        from .learner import main as learner_main

        learner_main(rest)
    elif args.command == "inspect":
        from .analysis import main as inspect_main

        inspect_main(rest)
    else:
        from .tools.request_terminal_close import main as close_main

        close_main(rest)


if __name__ == "__main__":
    main()
