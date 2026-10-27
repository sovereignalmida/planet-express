"""Launcher preflight (contract 22.4, 22.8.6): prove on THIS host that sessions can be contained, or say exactly why not.

It runs real operations, never an inference from file modes: it launches a throwaway session (the top uid of the
range, which `start_session` never allocates) through the same helper real sessions use, then as the actual Core
identity freezes it (and confirms `frozen 1`), thaws, kills it, confirms `populated 0` and removes the cgroup, while
the probe process itself, running as the throwaway uid, tries the writes that must be refused. Any check that fails
or cannot be run makes the host PROPOSAL-ONLY: the supervisor then refuses to start sessions at all (there is no
weaker fallback containment, decided 2026-10-13). The report is structured and printable.
"""

import json
import os
import select
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from planet_express.missions.containment import ContainedSession, ContainmentError
from planet_express.missions.launcher import pe_launch
from planet_express.missions.launcher.spec import LaunchSpec

PROBE_SCRIPT = r"""
import json, os, sys, time
cg, parent, grand, sock, ws = sys.argv[1:6]
def denied(path):
    try:
        fd = os.open(path, os.O_WRONLY)
    except PermissionError:
        return True
    except OSError as exc:
        return "errno %d" % exc.errno
    os.close(fd)
    return False
out = {"ids": [list(os.getresuid()), list(os.getresgid()), os.getgroups()],
       "denied": {n: denied(os.path.join(cg, n)) for n in ("cgroup.procs", "cgroup.freeze", "cgroup.kill")}}
out["denied"]["parent cgroup.procs"] = denied(os.path.join(parent, "cgroup.procs"))
out["denied"]["grandparent cgroup.procs"] = denied(os.path.join(grand, "cgroup.procs"))
try:
    out["events_readable"] = "populated" in open(os.path.join(cg, "cgroup.events")).read()
except OSError:
    out["events_readable"] = False
if sock == "-":
    out["sock_connect"] = True
else:
    import socket
    try:
        client = socket.socket(socket.AF_UNIX)
        client.connect(os.path.join(sock, "worker.sock"))
        client.close()
        out["sock_connect"] = True
    except PermissionError:
        out["sock_connect"] = "EACCES"
    except OSError as exc:
        out["sock_connect"] = "errno %d" % exc.errno
try:
    probe = os.path.join(ws, "probe.txt")
    open(probe, "w").write("x")
    os.unlink(probe)
    out["workspace_writable"] = True
except OSError:
    out["workspace_writable"] = False
print(json.dumps(out), flush=True)
time.sleep(120)
"""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PreflightReport:
    strategy: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def reason(self) -> str | None:
        bad = self.failures
        return None if not bad else "; ".join(f"{c.name}: {c.detail}" for c in bad)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "ok": self.ok,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]}

    def render(self) -> str:
        lines = [f"launcher preflight ({self.strategy}): {'PASS' if self.ok else 'FAIL, host is proposal-only'}"]
        lines += [f"  [{'ok' if c.ok else 'FAIL'}] {c.name}" + (f": {c.detail}" if c.detail else "")
                  for c in self.checks]
        return "\n".join(lines)


def failed_report(strategy: str, name: str, detail: str) -> PreflightReport:
    """A report for a host that could not even be set up for the check (for example, no launcher could be built)."""
    report = PreflightReport(strategy)
    report.add(name, False, detail)
    return report


def _trusted(report: PreflightReport, name: str, path: str, trusted_uid: int, below: str) -> bool:
    try:
        pe_launch.check_trusted_path(path, trusted_uid=trusted_uid, below=below)
    except pe_launch.Refused as exc:
        return report.add(name, False, f"{path}: {exc.code}")
    except OSError as exc:
        return report.add(name, False, f"{path}: {exc.strerror}")
    return report.add(name, True, path)


def check_range_free(report: PreflightReport, low: int, high: int, *, getpwall=None, getgrall=None) -> bool:
    """No account and no group (by uid, primary gid or gid) lives in the session range: such an account would share a
    session's identity, files and socket. Reads the same databases the helper reads."""
    import grp
    import pwd
    users = (getpwall or pwd.getpwall)()
    groups = (getgrall or grp.getgrall)()
    taken = sorted({f"user {u.pw_name}" for u in users if low <= u.pw_uid <= high or low <= u.pw_gid <= high}
                   | {f"group {g.gr_name}" for g in groups if low <= g.gr_gid <= high})
    return report.add("no account or group uses the session uid range", not taken,
                      ", ".join(taken[:3]) if taken else f"{low}-{high}")


def check_no_stale_uids(report: PreflightReport, low: int, high: int, held: "list[int]") -> bool:
    """No session still holds a uid OUTSIDE the configured range. After the range is moved (the default moved off
    61000-61999 because it overlapped systemd's DynamicUser range) a live or unreleased session in the old range would be
    orphaned: the launcher and the reaper only know the new range. Refuse, naming the uids, until they are gone."""
    stale = sorted({u for u in held if not low <= u <= high})
    return report.add("no session holds a uid outside the configured range", not stale,
                      (f"uids {', '.join(str(u) for u in stale[:5])} are held by sessions that predate the range {low}-{high}: "
                       "let those missions finish or cancel them, wait for the reaper to release the uids, then restart")
                      if stale else f"{low}-{high}")


def check_installation(report: PreflightReport, *, helper: str, conf: str, trusted_uid: int = 0,
                       below: str = "/") -> bool:
    """The helper and its config are root-owned, not group/world writable, with no symlink anywhere in their chains."""
    first = _trusted(report, "helper install", helper, trusted_uid, below)
    second = _trusted(report, "launch.conf install", conf, trusted_uid, below)
    return first and second


def check_sudoers(report: PreflightReport, helper: str, *, sudo: str = "/usr/bin/sudo", run=subprocess.run) -> bool:
    """`sudo -n pe-launch` is allowed (the real launch proves that below) and any argument is refused by sudoers."""
    try:
        result = run([sudo, "-n", helper, "--x"], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                     env={"PATH": "/usr/bin:/bin"}, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return report.add("sudoers refuses arguments", False, type(exc).__name__)
    ran_helper = b"PE-LAUNCH-STATUS" in (result.stdout or b"")
    if ran_helper:
        return report.add("sudoers refuses arguments", False, "sudo ran the helper with an argument")
    return report.add("sudoers refuses arguments", result.returncode != 0,
                      "" if result.returncode != 0 else "sudo allowed an argument")


def _read_json_line(fd: int, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s
    data = b""
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if not select.select([fd], [], [], remaining)[0]:
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            return None
        data += chunk
        if len(data) > 65536:
            return None
    try:
        obj = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def log_group_gids(names) -> list[int]:
    """The gids of the configured log groups that exist on this host (the helper skips an absent one the same way)."""
    import grp
    out = []
    for name in names:
        try:
            out.append(grp.getgrnam(name).gr_gid)
        except KeyError:
            continue
    return sorted(out)


def judge_probe(report: PreflightReport, seen: dict | None, uid: int, *, sock_dir: bool,
                log_gids: list[int] | None = None) -> None:
    """What the probe process (running as the throwaway uid) reported about its own powers. `log_gids` is None when sessions are
    not set up to read logs (they must then have no supplementary group at all) and the gids the helper must have given
    otherwise: a helper or launch.conf that disagrees with `missions.read_log_groups` fails here, not on the first real session."""
    if seen is None:
        report.add("probe session ran", False, "it reported nothing")
        return
    ids = seen.get("ids") or [[], [], None]
    want = sorted(log_gids or [])
    report.add("session identity", ids[0] == [uid] * 3 and ids[1] == [uid] * 3 and sorted(ids[2] or []) == want and isinstance(ids[2], list),
               (f"uid/gid {uid}, " + (f"log groups {want} only" if want else "no supplementary groups")) if ids[0] == [uid] * 3 else f"ran as {ids}")
    denied = seen.get("denied") or {}
    for name in ("cgroup.procs", "cgroup.freeze", "cgroup.kill", "parent cgroup.procs", "grandparent cgroup.procs"):
        value = denied.get(name)
        report.add(f"session uid cannot write {name}", value is True,
                   "EACCES" if value is True else f"got {value!r}")
    report.add("cgroup.events readable by the session", seen.get("events_readable") is True)
    if sock_dir:
        report.add("the session uid can connect to its own socket", seen.get("sock_connect") is True,
                   "" if seen.get("sock_connect") is True else f"got {seen.get('sock_connect')!r}")
    report.add("workspace writable by the session uid", seen.get("workspace_writable") is True)


def enable_pids(report: PreflightReport, parent: str) -> bool:
    """Enable the `pids` controller on the sessions parent (it must be empty of processes) and read it back."""
    path = os.path.join(parent, "cgroup.subtree_control")
    try:
        with open(path, encoding="ascii") as handle:
            enabled = "pids" in handle.read().split()
        if not enabled:
            with open(path, "w", encoding="ascii") as handle:
                handle.write("+pids")
            with open(path, encoding="ascii") as handle:
                enabled = "pids" in handle.read().split()
    except OSError as exc:
        return report.add("pids controller enabled on the sessions parent", False, exc.strerror or "error")
    return report.add("pids controller enabled on the sessions parent", enabled)


class _ProbeSocket:
    """What Core does before every real launch: make `<sock_root>/<sid>/` and bind (and listen on) `worker.sock`.
    The launcher then hands both to the session. Closing removes nothing: `containment.cleanup` removes the directory."""

    def __init__(self, sock_root: str, sid: str):
        import socket
        self.directory = os.path.join(sock_root, sid)
        os.mkdir(self.directory, 0o700)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(os.path.join(self.directory, "worker.sock"))
        self.server.listen(4)

    def close(self) -> None:
        self.server.close()


def _step(report: PreflightReport, name: str, action, detail: str = "") -> None:
    try:
        action()
        report.add(name, True, detail)
    except (OSError, ContainmentError) as exc:
        report.add(name, False, str(exc) or type(exc).__name__)


def _untraversable(path: str) -> str | None:
    """The first directory on the way to `path` that a session uid could not search (no other-execute), or None."""
    current = os.path.abspath(path)
    while True:
        try:
            if not os.stat(current).st_mode & 0o001:
                return current
        except OSError:
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def run_probe(report: PreflightReport, containment, *, workspace_root: str, sock_root: str | None, probe_uid: int,
              python: str, token: str = "p" * 40, wait_s: float = 10.0, check_reach: bool = False,
              log_groups: tuple[str, ...] = ()) -> None:
    """Launch, freeze (+confirm), thaw, kill (+confirm empty), remove: all as the real Core identity."""
    sid = "PREFLIGHT-" + os.urandom(4).hex()
    workspace = os.path.join(workspace_root, sid)
    cs: ContainedSession | None = None
    blocked = _untraversable(workspace_root) if check_reach else None
    if blocked:
        report.add("workspace root reachable by session users", False,
                   f"{blocked} is not searchable by other users; set missions.workspace_root outside Core's private data dir")
        return
    try:
        os.mkdir(workspace, 0o700)
    except OSError as exc:
        report.add("workspace root usable by Core", False, f"{workspace_root}: {exc.strerror}")
        return
    report.add("workspace root usable by Core", True, workspace_root)
    sock = None
    try:
        cgroup = containment.cgroup_path(sid)
        parent = os.path.dirname(cgroup)
        if not enable_pids(report, parent):
            return
        probe_sock = "-"
        if sock_root:
            try:
                sock = _ProbeSocket(sock_root, sid)
            except OSError as exc:
                report.add("Core can make and bind a per-session socket", False, f"{sock_root}: {exc.strerror}")
                return
            report.add("Core can make and bind a per-session socket", True, sock_root)
            probe_sock = sock.directory
        spec = LaunchSpec(
            uid=probe_uid, cwd=workspace, env={"PATH": "/usr/bin:/bin"}, token=token, sock_dir=bool(sock_root),
            read_logs=bool(log_groups),          # the probe asks exactly as a real Observe/Propose session will
            argv=(python, "-I", "-c", PROBE_SCRIPT, cgroup, parent, os.path.dirname(parent), probe_sock, workspace))
        try:
            cs = containment.launch(sid, spec)
        except ContainmentError as exc:
            report.add("launch a throwaway session through the helper", False, str(exc))
            return
        report.add("launch a throwaway session through the helper", True, f"uid {probe_uid}")
        judge_probe(report, _read_json_line(containment.output_fd(cs), wait_s), probe_uid, sock_dir=bool(sock_root),
                    log_gids=log_group_gids(log_groups) if log_groups else None)
        # Core must be able to SEE a session's processes to ever prove a uid unused: a /proc mounted hidepid would
        # make `uid_in_use` read false while they run, so the probe checks it while the session is alive.
        report.add("Core sees the running session's processes in /proc", containment.uid_in_use(probe_uid))
        report.add("cgroup.kill exists", os.path.exists(os.path.join(cgroup, "cgroup.kill")), "kernel >= 5.14")
        report.add("session cgroup holds the process", bool(containment.pids(cs)))
        try:
            with open(os.path.join(cgroup, "pids.max"), encoding="ascii") as handle:
                limit = handle.read().strip()
            report.add("pids.max set and readable on the session", limit.isdigit(), f"pids.max={limit}")
        except OSError as exc:
            report.add("pids.max set and readable on the session", False, exc.strerror or type(exc).__name__)
        _step(report, "Core freezes the session and sees frozen 1", lambda: containment.freeze(cs))
        _step(report, "Core thaws the session and sees frozen 0", lambda: containment.thaw(cs))
        _step(report, "Core kills the session", lambda: containment.kill(cs))
        report.add("the killed session is empty (populated 0)", containment.is_empty(cs))
        _step(report, "Core removes the session cgroup", lambda: containment.cleanup(cs))
        report.add("the session cgroup is gone", not os.path.exists(cgroup))
        report.add("no process left running as the probe uid", not containment.uid_in_use(probe_uid))
    finally:
        if sock is not None:
            sock.close()
        shutil.rmtree(workspace, ignore_errors=True)
        if cs is not None:
            # A step above may have failed or raised: never leave the probe process or its cgroup behind (the probe
            # uid is reused by the next preflight). Best effort; the next preflight's launch refuses a leftover loudly.
            try:
                containment.kill(cs)
                containment.cleanup(cs)
            except (OSError, ContainmentError):
                pass
            containment.release_output(cs)


def run_preflight(containment, *, strategy: str, workspace_root: str, sock_root: str | None, probe_uid: int,
                  helper: str, conf: str, python: str = "/usr/bin/python3", trusted_uid: int = 0, below: str = "/",
                  uid_range: tuple[int, int] | None = None, sudo: bool = False, check_reach: bool = False,
                  run=subprocess.run, getpwall=None, getgrall=None, held_uids=None,
                  log_groups: tuple[str, ...] = ()) -> PreflightReport:
    report = PreflightReport(strategy)
    if uid_range is not None:
        low, high = uid_range
        report.add("session uid range", 0 < low < high and probe_uid == high
                   and not low <= os.geteuid() <= high, f"{low}-{high}, probe uid {probe_uid}")
    if uid_range is not None:
        check_range_free(report, *uid_range, getpwall=getpwall, getgrall=getgrall)
        if held_uids is not None:
            check_no_stale_uids(report, *uid_range, list(held_uids()))
    if not check_installation(report, helper=helper, conf=conf, trusted_uid=trusted_uid, below=below):
        report.add("probe session", False, "skipped: the helper or its config is not installed safely")
        return report
    if sudo and not check_sudoers(report, helper, run=run):
        report.add("probe session", False, "skipped: sudoers did not behave as required")
        return report
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        report.add("probe interpreter", False, f"{python} is not an executable file")
        return report
    run_probe(report, containment, workspace_root=workspace_root, sock_root=sock_root, probe_uid=probe_uid,
              python=python, check_reach=check_reach, log_groups=tuple(log_groups))
    return report


__all__ = [
    "PROBE_SCRIPT",
    "Check",
    "PreflightReport",
    "check_installation",
    "check_no_stale_uids",
    "check_range_free",
    "check_sudoers",
    "failed_report",
    "judge_probe",
    "run_preflight",
    "run_probe",
]
