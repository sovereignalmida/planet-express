"""python -m planet_express.setup discover [--stacks-root PATH]
python -m planet_express.setup plan   --answers FILE [--discovery FILE]
python -m planet_express.setup apply  --answers FILE --plan-id ID [--dry-run] [--journal-dir DIR]

`discover` prints the discovery report as JSON. `plan` prints the reviewable plan for those answers, with
secrets masked. `apply` runs the plan whose id you reviewed; `--dry-run` only runs each step's read-only
check and changes nothing. Run `plan` and `apply` as the same user: root sees files (sudoers) that others
cannot, so their plan ids differ.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from planet_express.setup.discover import discover


def default_journal_root(report: dict, install_dir: str) -> str:
    """Where the journal lives. MOS keeps / in RAM, so it goes on the pool beside the install."""
    if report["host"]["init_system"] == "mos":
        return str(Path(install_dir).parent / "setup")
    return "/var/lib/planetexpress-setup"


def _trusted_uids(plan) -> set[int]:
    """Root, plus every account the plan makes an owner. Only these may own a directory on the way to a file
    setup writes."""
    import pwd
    uids = {0}
    for step in plan.steps:
        for key in ("owner", "run_user", "run_as"):
            name = step.params.get(key)
            if name:
                try:
                    uids.add(pwd.getpwnam(name).pw_uid)
                except KeyError:
                    pass
    return uids


def _apply_command(args) -> int:
    from planet_express.setup.answers import SetupAnswers
    from planet_express.setup.apply import apply
    from planet_express.setup.host import RealHost
    from planet_express.setup.plan import plan

    with open(args.answers, encoding="utf-8") as handle:
        raw = json.load(handle)
    answers = SetupAnswers.model_validate(raw)
    holds_secrets = any(raw.get(key) for key in ("telegram", "llm", "operator"))
    if holds_secrets and os.stat(args.answers).st_mode & 0o077:
        print(f"{args.answers} holds secrets and other users can read it. chmod 600 it and run again.", file=sys.stderr)
        return 2
    if not args.dry_run and os.geteuid() != 0:
        print("apply changes system files and has to run as root. Use --dry-run to check without changing anything.",
              file=sys.stderr)
        return 2

    report = discover()
    reviewed = plan(report, answers, repo_root=args.repo_root)
    reviewed_id = reviewed.to_public()["plan_id"]
    if reviewed_id != args.plan_id:
        print(f"plan {reviewed_id} is what this host would run now, not {args.plan_id}. Review `plan` again "
              "(run it as the same user as apply).", file=sys.stderr)
        return 2

    result = apply(
        reviewed, host=RealHost(_trusted_uids(reviewed)),
        journal_root=args.journal_dir or default_journal_root(report, answers.install_dir),
        replan=lambda: plan(discover(), answers, repo_root=args.repo_root), dry_run=args.dry_run,
        trusted_uids=_trusted_uids(reviewed))
    json.dump({"status": result.status, "plan_id": result.plan_id, "step": result.step, "reason": result.reason,
               "checks": result.checks}, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result.status in ("done", "dry_run") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m planet_express.setup")
    sub = parser.add_subparsers(dest="command", required=True)
    found = sub.add_parser("discover", help="report what this host is, read-only")
    found.add_argument("--stacks-root", help="look for compose stacks here instead of the usual places")
    planned = sub.add_parser("plan", help="show the plan for a set of answers, read-only")
    planned.add_argument("--answers", required=True, help="JSON file of wizard answers")
    planned.add_argument("--discovery", help="a saved discover report; defaults to discovering this host")
    planned.add_argument("--repo-root", help="the checkout holding the templates and scripts; defaults to this one")
    applied = sub.add_parser("apply", help="run the plan you reviewed")
    applied.add_argument("--answers", required=True, help="JSON file of wizard answers (chmod 600 if it has secrets)")
    applied.add_argument("--plan-id", required=True, help="the plan_id printed by `plan`")
    applied.add_argument("--dry-run", action="store_true", help="run each step's read-only check; change nothing")
    applied.add_argument("--journal-dir", help="where to keep the journal; defaults per host")
    applied.add_argument("--repo-root", help="the checkout holding the templates and scripts; defaults to this one")
    args = parser.parse_args(argv)

    if args.command == "discover":
        json.dump(discover(stacks_root=args.stacks_root), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.command == "apply":
        return _apply_command(args)
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
    json.dump(plan(report, answers, repo_root=args.repo_root).to_public(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
