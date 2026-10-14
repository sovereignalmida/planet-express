#!/usr/bin/env python3
"""Containment rehearsal for v4 mission sessions: drives the REAL launcher and prints one JSON document.

Run it on the host that will run missions, as the identity Core runs as (root on MOS, the Core user on Ubuntu), after
the launcher setup steps (session-uid-range, launcher-helper, launcher-sudoers, cgroup-tree, workspace-root,
socket-root) have been applied:

    python3 scripts/mission_containment_rehearsal.py --workspace-root DIR [--launcher auto|sudo|direct] \\
        [--cgroup-root DIR] [--sock-root DIR] [--uid-min N] [--uid-max N]

(the cgroup root, socket root and uid range default to the `missions.*` defaults; pass what your config says)

Step 1 is the launcher preflight (the same one the daemon runs before it starts any session): installation of the
helper and its config, sudoers, then a throwaway session started through `pe-launch` whose own process proves it
cannot write the cgroup files, while Core freezes it (confirming `frozen 1`), thaws it, kills it and removes it.
Step 2 launches a real process tree as a session uid (a leader, a plain child and a double-fork/setsid escaper) and
proves every member is in the cgroup, runs as that uid with no_new_privs, that Core can freeze and kill the whole
tree, and that the cgroup empties. Exit status 0 only if the preflight passed and every check passed.

It touches only `<cgroup-root>/PREFLIGHT-*` and `<cgroup-root>/REHEARSAL-*` (and the matching workspace and socket
directories) and removes them on the way out. Never run it on a machine whose launcher you have not installed on
purpose: with no helper installed the preflight simply fails, which is the point.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TREE = r"""
import os, time
if os.fork() == 0:            # plain child
    time.sleep(300); os._exit(0)
if os.fork() == 0:            # double-fork escaper that also leaves the session
    os.setsid()
    if os.fork() == 0:
        time.sleep(300)
    os._exit(0)
time.sleep(300)
"""


def status_field(pid, key):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(key + ":"):
                return line.split()[1]
    except OSError:
        return None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--launcher", choices=("auto", "sudo", "direct"), default="auto")
    from config_schema import MissionsConfig
    defaults = MissionsConfig()           # the same values Core and the launcher setup use
    parser.add_argument("--cgroup-root", default=defaults.cgroup_root)
    parser.add_argument("--workspace-root", required=True, help="missions.workspace_root (or <state dir>/mission-workspaces)")
    parser.add_argument("--sock-root", default=defaults.sock_root)
    parser.add_argument("--uid-min", type=int, default=defaults.session_uid_min)
    parser.add_argument("--uid-max", type=int, default=defaults.session_uid_max)
    args = parser.parse_args()
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        print(json.dumps({"ok": False, "error": "cgroup v2 is not mounted at /sys/fs/cgroup"}))
        return 2
    if args.uid_min <= 1000:
        print(json.dumps({"ok": False, "error": "refusing a system-range uid"}))
        return 2

    from types import SimpleNamespace

    from planet_express.missions.launcher.cgroup_tree import ensure_tree
    from planet_express.missions.launcher.config import CONF_PATH, HELPER_PATH
    from planet_express.missions.launcher.preflight import run_preflight
    from planet_express.missions.launcher.spec import LaunchSpec
    from planet_express.missions.runner import build_containment, choose_strategy

    cfg = SimpleNamespace(launcher=args.launcher, cgroup_root=args.cgroup_root, session_uid_min=args.uid_min,
                          session_uid_max=args.uid_max, sock_root=args.sock_root)
    strategy = choose_strategy(args.launcher, os.geteuid())
    report = {"ok": False, "strategy": strategy, "preflight": None, "checks": {}, "cgroup": None}
    try:
        ensure_tree(args.cgroup_root)
    except OSError as exc:
        report["tree_error"] = f"could not prepare the cgroup tree: {exc.strerror}"
    containment = build_containment(cfg)
    preflight = run_preflight(
        containment, strategy=strategy, workspace_root=args.workspace_root, sock_root=args.sock_root,
        probe_uid=args.uid_max, helper=HELPER_PATH, conf=CONF_PATH, uid_range=(args.uid_min, args.uid_max),
        sudo=strategy == "sudo")
    report["preflight"] = preflight.to_dict()
    print(preflight.render(), file=sys.stderr)
    if not preflight.ok:
        print(json.dumps(report, indent=2))
        return 1

    checks = report["checks"]
    sid = f"REHEARSAL-{os.getpid()}"
    workspace = Path(args.workspace_root) / sid
    cs = None
    try:
        workspace.mkdir(mode=0o700)
        spec = LaunchSpec(uid=args.uid_min, cwd=str(workspace), argv=(sys.executable, "-c", TREE),
                          env={"PATH": "/usr/bin:/bin"}, token="r" * 40, sock_dir=False)
        cs = containment.launch(sid, spec)
        report["cgroup"] = cs.cgroup_path
        time.sleep(1.0)
        members = containment.pids(cs)
        checks["tree_has_at_least_3_members"] = len(members) >= 3
        checks["all_run_as_session_uid"] = all(status_field(p, "Uid") == str(args.uid_min) for p in members)
        checks["all_no_new_privs"] = bool(members) and all(status_field(p, "NoNewPrivs") == "1" for p in members)
        checks["not_empty_while_running"] = not containment.is_empty(cs)
        containment.freeze(cs)
        checks["frozen"] = "frozen 1" in Path(cs.cgroup_path, "cgroup.events").read_text()
        containment.thaw(cs)
        containment.kill(cs)
        checks["empty_after_kill"] = containment.is_empty(cs)
        checks["no_members_after_kill"] = containment.pids(cs) == []
        containment.cleanup(cs)
        checks["cgroup_removed"] = not Path(cs.cgroup_path).exists()
        checks["uid_free_after_kill"] = not containment.uid_in_use(args.uid_min)
    except Exception as exc:  # noqa: BLE001 -- report, then clean up
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if cs is not None:
            try:
                containment.kill(cs)
                containment.cleanup(cs)
            except Exception:  # noqa: BLE001
                report.setdefault("cleanup_error", "could not remove the rehearsal session cgroup")
            containment.release_output(cs)
        import shutil
        shutil.rmtree(workspace, ignore_errors=True)
    report["ok"] = bool(checks) and all(checks.values()) and "error" not in report and preflight.ok
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
