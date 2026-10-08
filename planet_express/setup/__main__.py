"""python -m planet_express.setup discover [--stacks-root PATH]
python -m planet_express.setup plan   --answers FILE [--discovery FILE]
python -m planet_express.setup apply  --answers FILE --plan-id ID [--dry-run] [--journal-dir DIR]
python -m planet_express.setup undo   --plan-id ID [--journal-dir DIR | --answers FILE]
python -m planet_express.setup serve  [--port N] [--bind ADDR ...]

`discover` prints the discovery report as JSON. `plan` prints the reviewable plan for those answers, with
secrets masked. `apply` runs the plan whose id you reviewed; `--dry-run` only runs each step's read-only
check and changes nothing. `undo` reverts what that apply did, from its journal, and stops at the first thing that
has changed since. Run `plan` and `apply` as the same user: root sees files (sudoers) that others
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


def _undo_command(args) -> int:
    from planet_express.setup.host import RealHost
    from planet_express.setup.plan import Plan, Step
    from planet_express.setup.undo import undo

    if os.geteuid() != 0:
        print("undo changes system files and has to run as root.", file=sys.stderr)
        return 2
    journal_dir = args.journal_dir
    if not journal_dir:
        if not args.answers:
            print("say where the journal is: --journal-dir DIR, or --answers FILE to use the default for this host.",
                  file=sys.stderr)
            return 2
        from planet_express.setup.answers import SetupAnswers
        with open(args.answers, encoding="utf-8") as handle:
            install_dir = SetupAnswers.model_validate(json.load(handle)).install_dir
        journal_dir = default_journal_root(discover(), install_dir)
    saved = Path(journal_dir) / args.plan_id / "plan.json"
    try:
        public = json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read the saved plan {saved}: {exc}", file=sys.stderr)
        return 2
    steps = tuple(Step(s["id"], s["kind"], s["title"], s["target"], s["risk"], s["reversible"], s["needs_root"],
                       tuple(s["depends_on"]), s["params"], s["preview"]) for s in public["steps"])
    saved_plan = Plan(public["story"], steps, tuple(public["warnings"]), tuple(public["will_not_touch"]),
                      tuple(public["blocked"]))
    if saved_plan.to_public()["plan_id"] != args.plan_id:
        print("the saved plan does not match its id; it was changed. Not undoing from it.", file=sys.stderr)
        return 2
    uids = _trusted_uids(saved_plan)
    result = undo(saved_plan, host=RealHost(uids), journal_root=journal_dir, trusted_uids=uids)
    json.dump({"status": result.status, "plan_id": result.plan_id, "step": result.step, "reason": result.reason,
               "undone": result.undone, "not_undone": result.not_undone, "remaining": result.remaining},
              sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result.status == "done" else 1


def _serve_command(args) -> int:
    import socket

    from planet_express.setup.server import Sessions, serve
    addresses = args.bind or (discover()["network"]["lan_addresses"] + ["127.0.0.1"])
    return serve(addresses=list(dict.fromkeys(addresses)), port=args.port,
                 names=[socket.gethostname(), *(args.name or [])], sessions=Sessions())


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
    undoing = sub.add_parser("undo", help="revert what a past apply did, from its journal")
    undoing.add_argument("--plan-id", required=True, help="the plan_id that was applied")
    undoing.add_argument("--journal-dir", help="the journal root the apply used")
    undoing.add_argument("--answers", help="the answers file, to find this host's default journal root")
    serving = sub.add_parser("serve", help="the browser wizard (HTTPS, private addresses only)")
    serving.add_argument("--port", type=int, default=8443)
    serving.add_argument("--bind", action="append", help="an address to listen on (repeatable); default: this host's LAN addresses")
    serving.add_argument("--name", action="append", help="an extra host name the page may be reached by")
    args = parser.parse_args(argv)

    if args.command == "discover":
        json.dump(discover(stacks_root=args.stacks_root), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.command == "apply":
        return _apply_command(args)
    if args.command == "undo":
        return _undo_command(args)
    if args.command == "serve":
        return _serve_command(args)
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
