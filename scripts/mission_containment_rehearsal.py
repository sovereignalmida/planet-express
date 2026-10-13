#!/usr/bin/env python3
"""Containment rehearsal for v4 mission sessions. Needs root; prints one JSON document.

Creates a throwaway cgroup, spawns a process tree as a throwaway uid (a leader, a plain child and a
double-fork/setsid escaper), proves every member is in the cgroup and runs as that uid with
no_new_privs, freezes it, kills it, proves `is_empty()`, and removes everything. Exit status 0 only
if every check passed. Run it on the Ubuntu host and on the MOS VM before relying on containment:

    sudo python3 scripts/mission_containment_rehearsal.py [--cgroup-base /sys/fs/cgroup] [--uid 61999]

It touches only `<cgroup-base>/pe-rehearsal-<pid>` and a temp dir, and removes both on the way out.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
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
    parser.add_argument("--cgroup-base", default="/sys/fs/cgroup")
    parser.add_argument("--uid", type=int, default=61999, help="throwaway uid/gid (no passwd entry needed)")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "must run as root"}))
        return 2
    if not Path(args.cgroup_base, "cgroup.controllers").exists():
        print(json.dumps({"ok": False, "error": "cgroup v2 not mounted at --cgroup-base"}))
        return 2
    if args.uid <= 1000:
        print(json.dumps({"ok": False, "error": "refusing a system-range uid"}))
        return 2

    from planet_express.missions.containment_cgroup import CgroupContainment

    report = {"ok": False, "checks": {}, "cgroup": None}
    checks = report["checks"]
    base = Path(args.cgroup_base) / f"pe-rehearsal-{os.getpid()}"
    work = Path(tempfile.mkdtemp(prefix="pe-rehearsal-"))
    os.chmod(work, 0o755)
    cs = None
    base.mkdir()
    try:
        c = CgroupContainment(base, worker_uid=args.uid, worker_gid=args.uid)
        cs = c.create("rehearsal")
        report["cgroup"] = cs.cgroup_path
        c.spawn(cs, [sys.executable, "-c", TREE], {"PATH": "/usr/bin:/bin"}, str(work))
        time.sleep(1.0)
        members = c.pids(cs)
        checks["tree_has_at_least_3_members"] = len(members) >= 3
        checks["all_run_as_throwaway_uid"] = all(status_field(p, "Uid") == str(args.uid) for p in members)
        nnp = [status_field(p, "NoNewPrivs") for p in members]
        checks["all_no_new_privs"] = bool(nnp) and all(v == "1" for v in nnp)
        checks["not_empty_while_running"] = not c.is_empty(cs)
        c.freeze(cs)
        time.sleep(0.2)
        events = Path(cs.cgroup_path, "cgroup.events").read_text()
        checks["frozen"] = "frozen 1" in events
        c.kill(cs)
        checks["empty_after_kill"] = c.is_empty(cs)
        checks["no_members_after_kill"] = c.pids(cs) == []
        c.cleanup(cs)
        checks["cgroup_removed"] = not Path(cs.cgroup_path).exists()
    except Exception as exc:  # noqa: BLE001 -- report, then clean up
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if cs is not None and Path(cs.cgroup_path).exists():
            try:
                CgroupContainment(base, worker_uid=args.uid, worker_gid=args.uid).kill(cs)
                os.rmdir(cs.cgroup_path)
            except Exception:  # noqa: BLE001
                report.setdefault("cleanup_error", "could not remove session cgroup")
        try:
            os.rmdir(base)
        except OSError:
            report.setdefault("cleanup_error", "could not remove rehearsal cgroup")
        shutil.rmtree(work, ignore_errors=True)
    report["ok"] = bool(checks) and all(checks.values()) and "error" not in report
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
