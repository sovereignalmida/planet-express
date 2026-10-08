"""python -m planet_express.setup discover [--stacks-root PATH]
python -m planet_express.setup plan --answers FILE [--discovery FILE]

`discover` prints the discovery report as JSON. `plan` prints the reviewable plan for those answers,
with secrets masked. Both are read-only and safe to run on a live host.
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
    planned = sub.add_parser("plan", help="show the plan for a set of answers, read-only")
    planned.add_argument("--answers", required=True, help="JSON file of wizard answers")
    planned.add_argument("--discovery", help="a saved discover report; defaults to discovering this host")
    args = parser.parse_args(argv)
    if args.command == "discover":
        json.dump(discover(stacks_root=args.stacks_root), sys.stdout, indent=2)
    else:
        # plan needs pydantic and PyYAML; discover deliberately does not, because on a new host it runs
        # before any virtualenv exists.
        from planet_express.setup.answers import SetupAnswers
        from planet_express.setup.plan import plan
        with open(args.answers, encoding="utf-8") as handle:
            answers = SetupAnswers.model_validate(json.load(handle))
        if args.discovery:
            with open(args.discovery, encoding="utf-8") as handle:
                report = json.load(handle)
        else:
            report = discover()
        json.dump(plan(report, answers).to_public(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
