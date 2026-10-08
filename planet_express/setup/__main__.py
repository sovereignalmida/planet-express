"""python -m planet_express.setup discover [--stacks-root PATH]

Prints the discovery report as JSON. Read-only; safe to run on a live host.
"""
from __future__ import annotations

import argparse
import json
import sys

from planet_express.setup.discover import discover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m planet_express.setup")
    sub = parser.add_subparsers(dest="command", required=True)
    found = sub.add_parser("discover", help="report what this host is, read-only")
    found.add_argument("--stacks-root", help="look for compose stacks here instead of the usual places")
    args = parser.parse_args(argv)
    if args.command == "discover":
        json.dump(discover(stacks_root=args.stacks_root), sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
