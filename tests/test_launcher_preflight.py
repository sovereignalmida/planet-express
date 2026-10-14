"""Launcher preflight (contract 22.4, 22.8.6) and the proposal-only gate, without privilege.

The checks that need root (a real helper, real cgroups, real uids) run in tests/test_launcher_privileged.py."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from missions_helpers import advance_to
from supervisor_fakes import Rig, update

from planet_express.missions.containment import ContainmentError
from planet_express.missions.containment_cgroup import CgroupContainment
from planet_express.missions.launcher.preflight import (
    PROBE_SCRIPT,
    PreflightReport,
    check_installation,
    check_sudoers,
    failed_report,
    judge_probe,
    run_preflight,
)
from planet_express.missions.launcher.strategies import Launched
from planet_express.missions.supervisor import PREFLIGHT_RETRY_S, ProposalOnly

ME = os.geteuid()
UID = 61999

GOOD_SEEN = {"ids": [[UID] * 3, [UID] * 3, []],
             "denied": {"cgroup.procs": True, "cgroup.freeze": True, "cgroup.kill": True,
                        "parent cgroup.procs": True, "grandparent cgroup.procs": True},
             "events_readable": True, "sock_connect": True, "workspace_writable": True}


def verdicts(report):
    return {c.name: c.ok for c in report.checks}


# ── the report ──────────────────────────────────────────────────────────────

def test_an_empty_report_is_not_a_pass():
    assert not PreflightReport("sudo").ok


def test_the_report_names_what_failed_and_renders():
    report = PreflightReport("sudo")
    report.add("a", True)
    report.add("b", False, "because")
    assert not report.ok and report.reason == "b: because"
    text = report.render()
    assert "FAIL, host is proposal-only" in text and "[FAIL] b: because" in text and "[ok] a" in text
    assert report.to_dict()["checks"][1] == {"name": "b", "ok": False, "detail": "because"}


# ── what the probe process says about its own powers ───────────────────────

def test_a_probe_that_was_denied_everything_passes():
    report = PreflightReport("direct")
    judge_probe(report, GOOD_SEEN, UID, sock_dir=True)
    assert report.ok, report.reason
    assert "session uid cannot write cgroup.kill" in verdicts(report)
    assert "the session uid can connect to its own socket" in verdicts(report)


@pytest.mark.parametrize("name", ["cgroup.procs", "cgroup.freeze", "cgroup.kill", "parent cgroup.procs",
                                  "grandparent cgroup.procs"])
@pytest.mark.parametrize("value", [False, "errno 2", None])
def test_a_probe_that_could_write_a_control_file_fails_the_host(name, value):
    seen = json.loads(json.dumps(GOOD_SEEN))
    seen["denied"][name] = value
    report = PreflightReport("direct")
    judge_probe(report, seen, UID, sock_dir=False)
    assert not report.ok and verdicts(report)[f"session uid cannot write {name}"] is False


@pytest.mark.parametrize("change", [
    {"ids": [[0, 0, 0], [UID] * 3, []]}, {"ids": [[UID] * 3, [0, 0, 0], []]},
    {"ids": [[UID] * 3, [UID] * 3, [27]]}, {"ids": None},
    {"events_readable": False}, {"workspace_writable": False}, {"sock_connect": "EACCES"},
    {"sock_connect": "errno 2"}, {"sock_connect": None},
])
def test_a_probe_with_the_wrong_identity_or_reach_fails_the_host(change):
    seen = {**GOOD_SEEN, **change}
    report = PreflightReport("direct")
    judge_probe(report, seen, UID, sock_dir=True)
    assert not report.ok


def test_a_probe_that_reported_nothing_fails_the_host():
    report = PreflightReport("direct")
    judge_probe(report, None, UID, sock_dir=False)
    assert not report.ok


def test_the_probe_script_is_valid_python_and_exercises_exactly_the_denied_writes():
    compile(PROBE_SCRIPT, "probe", "exec")
    for needle in ("cgroup.procs", "cgroup.freeze", "cgroup.kill", "parent", "grand", "O_WRONLY", "PermissionError"):
        assert needle in PROBE_SCRIPT


def test_the_probe_script_reports_a_denied_write_when_run_as_a_plain_user(tmp_path):
    cg = tmp_path / "root" / "p" / "cg"
    cg.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    for d in (cg, cg.parent, cg.parent.parent):
        (d / "cgroup.procs").write_text("")
        (d / "cgroup.procs").chmod(0o444)             # not writable by anyone, as for a session uid
    for name in ("cgroup.freeze", "cgroup.kill"):
        (cg / name).write_text("")
        (cg / name).chmod(0o444)
    (cg / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    proc = subprocess.Popen([sys.executable, "-I", "-c", PROBE_SCRIPT, str(cg), str(cg.parent),
                             str(cg.parent.parent), "-", str(ws)], stdout=subprocess.PIPE)
    try:
        seen = json.loads(proc.stdout.readline())
    finally:
        proc.kill()
        proc.wait()
    if ME == 0:
        pytest.skip("root can write read-only files")
    assert all(v is True for v in seen["denied"].values()) and seen["events_readable"] and seen["workspace_writable"]
    assert seen["ids"][0] == [ME] * 3


# ── installation ────────────────────────────────────────────────────────────

def installed(tmp_path):
    tmp_path.chmod(0o755)
    helper = tmp_path / "libexec" / "pe-launch"
    helper.parent.mkdir()
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    conf = tmp_path / "etc" / "launch.conf"
    conf.parent.mkdir()
    conf.write_text("{}")
    conf.chmod(0o600)
    for d in (helper.parent, conf.parent):
        d.chmod(0o755)
    return str(helper), str(conf)


def check(tmp_path, helper, conf):
    report = PreflightReport("sudo")
    return check_installation(report, helper=helper, conf=conf, trusted_uid=ME, below=str(tmp_path)), report


def test_a_clean_installation_passes(tmp_path):
    ok, report = check(tmp_path, *installed(tmp_path))
    assert ok and report.ok


def test_a_group_writable_helper_directory_fails(tmp_path):
    helper, conf = installed(tmp_path)
    Path(helper).parent.chmod(0o775)
    ok, report = check(tmp_path, helper, conf)
    assert not ok and not verdicts(report)["helper install"]


def test_a_world_writable_helper_fails(tmp_path):
    helper, conf = installed(tmp_path)
    Path(helper).chmod(0o757)
    ok, _ = check(tmp_path, helper, conf)
    assert not ok


def test_a_symlinked_helper_directory_fails(tmp_path):
    helper, conf = installed(tmp_path)
    real = Path(helper).parent
    link = tmp_path / "linked"
    link.symlink_to(real)
    ok, report = check(tmp_path, str(link / "pe-launch"), conf)
    assert not ok and not verdicts(report)["helper install"]


def test_a_symlinked_config_fails(tmp_path):
    helper, conf = installed(tmp_path)
    link = tmp_path / "etc" / "link.conf"
    link.symlink_to(conf)
    ok, report = check(tmp_path, helper, str(link))
    assert not ok and not verdicts(report)["launch.conf install"]


def test_a_helper_owned_by_somebody_else_fails(tmp_path):
    helper, conf = installed(tmp_path)
    report = PreflightReport("sudo")
    assert not check_installation(report, helper=helper, conf=conf, trusted_uid=ME + 1, below=str(tmp_path))


def test_a_missing_helper_fails_cleanly(tmp_path):
    helper, conf = installed(tmp_path)
    ok, report = check(tmp_path, helper + "-gone", conf)
    assert not ok and "No such file" in report.reason


# ── sudoers ─────────────────────────────────────────────────────────────────

def fake_run(returncode=1, stdout=b"", raises=None):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, b"")
    run.seen = seen
    return run


def test_sudoers_must_refuse_an_argument():
    run = fake_run(returncode=1)
    report = PreflightReport("sudo")
    assert check_sudoers(report, "/usr/libexec/planetexpress/pe-launch", run=run)
    assert run.seen["argv"] == ["/usr/bin/sudo", "-n", "/usr/libexec/planetexpress/pe-launch", "--x"]
    assert run.seen["env"] == {"PATH": "/usr/bin:/bin"} and run.seen["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("run", [fake_run(returncode=0), fake_run(returncode=1, stdout=b"PE-LAUNCH-STATUS {}"),
                                 fake_run(raises=OSError("no sudo")),
                                 fake_run(raises=subprocess.TimeoutExpired("sudo", 20))])
def test_sudoers_that_allows_an_argument_or_cannot_be_asked_fails(run):
    report = PreflightReport("sudo")
    assert not check_sudoers(report, "/x/pe-launch", run=run)


# ── the whole preflight on a fake cgroup tree ───────────────────────────────

class FsCgroup(CgroupContainment):
    """A cgroup tree on tmpfs where the 'kernel' reflects freeze and kill, like the real one."""

    def _write(self, path, value):
        super()._write(path, value)
        name = Path(path).name
        events = Path(path).with_name("cgroup.events")
        if name == "cgroup.freeze":
            events.write_text(f"populated 1\nfrozen {value}\n")
        if name == "cgroup.kill":
            events.write_text("populated 0\nfrozen 1\n")
            shutil.rmtree(Path(self._proc) / "4242", ignore_errors=True)       # the process is gone

    def _rmdir(self, path):
        for f in Path(path).iterdir():
            f.unlink()
        super()._rmdir(path)


class ProbeLauncher:
    """The helper's effect on the tree plus a 'session' that prints what a real probe would."""

    def __init__(self, root, seen=GOOD_SEEN, pids_max="256", proc=None, socks=None):
        self.root, self.seen, self.pids_max, self.specs = Path(root), seen, pids_max, []
        self.proc, self.socks = proc, socks

    def start(self, session_id, spec):
        self.specs.append((session_id, spec))
        if spec.sock_dir:                                    # Core made and bound the socket before asking
            sockets = Path(self.socks) / session_id
            assert (sockets / "worker.sock").exists(), "the socket must exist BEFORE the launch"
        proc = Path(self.proc) / "4242"
        proc.mkdir(parents=True, exist_ok=True)
        (proc / "status").write_text(f"Name:\tprobe\nUid:\t{spec.uid}\t{spec.uid}\t{spec.uid}\t{spec.uid}\n")
        path = self.root / session_id
        path.mkdir()
        (path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        (path / "cgroup.procs").write_text("4242\n")
        (path / "cgroup.freeze").write_text("0")
        (path / "cgroup.kill").write_text("")
        if self.pids_max is not None:
            (path / "pids.max").write_text(self.pids_max + "\n")
        proc = subprocess.Popen([sys.executable, "-c", f"print({json.dumps(json.dumps(self.seen))}, flush=True)"],
                                stdout=subprocess.PIPE, bufsize=0)
        os.set_blocking(proc.stdout.fileno(), False)
        return Launched(proc, {"ok": True, "session_id": session_id, "uid": spec.uid, "gid": spec.uid,
                               "cgroup": str(path)})


def tree(tmp_path, **launcher_kw):
    helper, conf = installed(tmp_path)
    root = tmp_path / "cgroot"
    root.mkdir()
    (root / "cgroup.subtree_control").write_text("pids\n")
    (root / "cgroup.procs").write_text("")
    ws = tmp_path / "ws"
    ws.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    socks = tmp_path / "socks"
    socks.mkdir()
    launcher = ProbeLauncher(root, proc=proc, socks=socks, **launcher_kw)
    c = FsCgroup(root, launcher=launcher, uid_range=(61000, 61999), require_cgroup2=False, sleep=lambda s: None,
                 proc=str(proc), sock_root=str(socks))
    return c, launcher, helper, conf, ws


def preflight(tmp_path, c, helper, conf, ws, **kw):
    kw.setdefault("getpwall", lambda: [])          # hermetic: this machine's accounts are not the test's business
    kw.setdefault("getgrall", lambda: [])
    return run_preflight(c, strategy="direct", workspace_root=str(ws), sock_root=None, probe_uid=UID, helper=helper,
                         conf=conf, python=sys.executable, trusted_uid=ME, below=str(tmp_path),
                         uid_range=(61000, 61999), **kw)


def preflight_with_socks(tmp_path, c, helper, conf, ws, sock_root):
    return run_preflight(c, strategy="direct", workspace_root=str(ws), sock_root=sock_root, probe_uid=UID,
                         helper=helper, conf=conf, python=sys.executable, trusted_uid=ME, below=str(tmp_path),
                         uid_range=(61000, 61999), getpwall=lambda: [], getgrall=lambda: [])


def test_a_healthy_host_passes_every_step_and_cleans_up_after_itself(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    report = preflight(tmp_path, c, helper, conf, ws)
    assert report.ok, report.render()
    names = verdicts(report)
    for step in ("helper install", "launch.conf install", "launch a throwaway session through the helper",
                 "Core freezes the session and sees frozen 1", "Core thaws the session and sees frozen 0",
                 "Core kills the session", "the killed session is empty (populated 0)",
                 "Core removes the session cgroup", "the session cgroup is gone",
                 "pids controller enabled on the sessions parent", "pids.max set and readable on the session",
                 "cgroup.kill exists", "session uid range"):
        assert names.get(step) is True, step
    (sid, spec), = launcher.specs
    assert sid.startswith("PREFLIGHT-") and spec.uid == UID and spec.cwd.startswith(str(ws))
    assert list(ws.iterdir()) == [] and [p for p in Path(c.root).iterdir() if p.is_dir()] == []


def test_the_probe_uid_is_always_the_top_of_the_range_which_the_allocator_never_hands_out(tmp_path):
    c, *_ = tree(tmp_path)
    assert c.uid_range == (61000, 61998)
    report = PreflightReport("direct")
    report.add("x", True)
    bad = run_preflight(c, strategy="direct", workspace_root="/nope", sock_root=None, probe_uid=61998,
                        helper="/nope", conf="/nope", uid_range=(61000, 61999))
    assert not verdicts(bad)["session uid range"]


def test_a_probe_that_could_write_cgroup_files_makes_the_host_proposal_only(tmp_path):
    seen = json.loads(json.dumps(GOOD_SEEN))
    seen["denied"]["cgroup.kill"] = False
    c, launcher, helper, conf, ws = tree(tmp_path, seen=seen)
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and "session uid cannot write cgroup.kill" in report.reason


def test_a_missing_pids_limit_makes_the_host_proposal_only(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path, pids_max=None)
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and not verdicts(report)["pids.max set and readable on the session"]


def test_the_pids_controller_is_enabled_when_it_is_not(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    (Path(c.root) / "cgroup.subtree_control").write_text("")
    report = preflight(tmp_path, c, helper, conf, ws)
    # It asked for the controller, and (a plain file does not become "pids" like a real cgroupfs) the read-back
    # is what is judged, so a controller that did not actually turn on fails the host.
    assert (Path(c.root) / "cgroup.subtree_control").read_text() == "+pids"
    assert verdicts(report)["pids controller enabled on the sessions parent"] is False and not report.ok


def test_a_controller_that_cannot_be_enabled_stops_before_launching(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    (Path(c.root) / "cgroup.subtree_control").unlink()
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and launcher.specs == []


def test_an_unsafe_installation_skips_the_probe_and_fails(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    Path(helper).chmod(0o777)
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and launcher.specs == [] and "skipped" in report.reason


def test_a_missing_interpreter_fails_without_launching(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    report = run_preflight(c, strategy="direct", workspace_root=str(ws), sock_root=None, probe_uid=UID,
                           helper=helper, conf=conf, python=str(tmp_path / "no-python"), trusted_uid=ME,
                           below=str(tmp_path))
    assert not report.ok and launcher.specs == []


def test_an_unusable_workspace_root_fails(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    report = run_preflight(c, strategy="direct", workspace_root=str(tmp_path / "missing"), sock_root=None,
                           probe_uid=UID, helper=helper, conf=conf, python=sys.executable, trusted_uid=ME,
                           below=str(tmp_path))
    assert not report.ok and launcher.specs == []


def test_a_launch_the_helper_refuses_fails_the_host(tmp_path):
    from planet_express.missions.launcher.strategies import LaunchError
    c, launcher, helper, conf, ws = tree(tmp_path)

    def refuse(session_id, spec):
        raise LaunchError("the launcher refused: uid")
    launcher.start = refuse
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and "the launcher refused: uid" in report.reason
    assert list(ws.iterdir()) == []


def test_the_sudo_strategy_also_checks_sudoers(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    report = preflight(tmp_path, c, helper, conf, ws, sudo=True, run=fake_run(returncode=0))
    assert not report.ok and launcher.specs == [] and not verdicts(report)["sudoers refuses arguments"]
    report = preflight(tmp_path, c, helper, conf, ws, sudo=True, run=fake_run(returncode=1))
    assert report.ok, report.render()


# ── proposal-only: the supervisor refuses to start sessions ─────────────────

def failing(reason="helper install: untrusted"):
    return lambda: failed_report("sudo", "helper install", reason)


def passing():
    report = PreflightReport("sudo")
    report.add("everything", True)
    return report


def gated(tmp_path, preflight_fn, **kw):
    rig = Rig(tmp_path, **kw)
    rig.sup.preflight_fn = preflight_fn
    return rig


def test_a_failed_preflight_means_no_session_is_started(tmp_path):
    rig = gated(tmp_path, failing())
    with pytest.raises(ProposalOnly, match="untrusted"):
        rig.sup.start(rig.mid)
    assert rig.runtimes == [] and rig.sessions() == [] and rig.status() == "queued"


def test_a_failed_preflight_leaves_queued_missions_queued_and_burns_no_start_attempts(tmp_path):
    rig = gated(tmp_path, failing())
    for _ in range(10):
        rig.sup.tick()
    assert rig.status() == "queued" and rig.sessions() == [] and rig.sup._start_failures == {}


def test_a_queued_mission_can_still_be_cancelled_while_the_host_is_proposal_only(tmp_path):
    rig = gated(tmp_path, failing())
    rig.sup.cancel(rig.mid, "operator:me")
    rig.sup.tick()
    assert rig.status() == "cancelled"


def test_the_preflight_is_retried_after_the_interval_and_a_repaired_host_starts_sessions(tmp_path):
    calls = []
    results = [failing()(), passing()]

    def preflight_fn():
        calls.append(1)
        value = results.pop(0)
        return value() if callable(value) else value
    rig = gated(tmp_path, preflight_fn, behaviours=[{"script": [update()]}])
    rig.sup.tick()
    assert len(calls) == 1 and rig.status() == "queued"
    rig.sup.tick()
    assert len(calls) == 1                                    # not asked again within the interval
    rig.clock.t += PREFLIGHT_RETRY_S
    rig.sup.tick()
    assert len(calls) == 2 and rig.status() == "investigating"
    rig.sup.tick()
    assert len(calls) == 2                                    # a pass is remembered


def test_a_preflight_that_raises_is_a_failed_preflight(tmp_path):
    def boom():
        raise RuntimeError("secret detail")
    rig = gated(tmp_path, boom)
    with pytest.raises(ProposalOnly) as exc:
        rig.sup.start(rig.mid)
    assert "secret detail" not in str(exc.value) and "RuntimeError" in str(exc.value)


def test_mutating_tiers_are_still_refused_whatever_the_preflight_says(tmp_path):
    from planet_express.missions.supervisor import UnsupportedTier
    rig = gated(tmp_path, passing, tier="repair")
    advance_to(rig.ms, rig.mid, "queued") if rig.status() == "draft" else None
    with pytest.raises((UnsupportedTier, Exception)):
        rig.sup.start(rig.mid)
    assert rig.runtimes == []


def test_without_a_configured_preflight_the_supervisor_is_ready(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()]}])
    assert rig.sup.launcher_ready()
    rig.sup.start(rig.mid)
    assert rig.status() == "investigating"


def test_a_proc_that_hides_the_sessions_processes_fails_the_host(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    original = launcher.start

    def start(session_id, spec):
        got = original(session_id, spec)
        shutil.rmtree(Path(launcher.proc) / "4242")            # /proc mounted hidepid: Core cannot see the process
        return got
    launcher.start = start
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and not verdicts(report)["Core sees the running session's processes in /proc"]


def test_core_binds_the_per_session_socket_before_the_launch_and_the_probe_connects_to_it(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    socks = Path(launcher.socks)
    report = preflight_with_socks(tmp_path, c, helper, conf, ws, str(socks))
    assert report.ok, report.render()
    names = verdicts(report)
    assert names["Core can make and bind a per-session socket"] and names["the session uid can connect to its own socket"]
    assert launcher.specs[0][1].sock_dir is True
    assert list(socks.iterdir()) == []                         # cleanup removed the directory and the socket


def test_a_socket_root_core_cannot_write_fails_the_host(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    report = preflight_with_socks(tmp_path, c, helper, conf, ws, str(tmp_path / "no" / "such"))
    assert not report.ok and launcher.specs == []


def test_the_probe_script_connects_to_the_socket_in_its_directory(tmp_path):
    import socket
    cg = tmp_path / "root" / "p" / "cg"
    cg.mkdir(parents=True)
    (cg / "cgroup.events").write_text("populated 1\n")
    ws, sockdir = tmp_path / "ws", tmp_path / "s"
    ws.mkdir()
    sockdir.mkdir()
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sockdir / "worker.sock"))
    server.listen(1)
    proc = subprocess.Popen([sys.executable, "-I", "-c", PROBE_SCRIPT, str(cg), str(cg.parent), str(cg.parent.parent),
                             str(sockdir), str(ws)], stdout=subprocess.PIPE)
    try:
        assert json.loads(proc.stdout.readline())["sock_connect"] is True
    finally:
        proc.kill()
        proc.wait()
        server.close()


# ── the reserved range must belong to no account (Codex: an account there would share a session's identity) ───────

def acct(name, uid, gid):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid)


def grp_entry(name, gid):
    return SimpleNamespace(gr_name=name, gr_gid=gid)


@pytest.mark.parametrize("users, groups, hit", [
    ([acct("bob", 61500, 61500)], [], "user bob"),            # an ordinary account with its own private group
    ([acct("svc", 1001, 61007)], [], "user svc"),             # an account whose PRIMARY group is in the range
    ([], [grp_entry("odd", 61001)], "group odd"),
    ([acct("edge-low", 61000, 100)], [], "user edge-low"), ([acct("edge-high", 61999, 100)], [], "user edge-high"),
])
def test_an_account_or_group_in_the_range_fails_the_host(tmp_path, users, groups, hit):
    c, launcher, helper, conf, ws = tree(tmp_path)
    report = preflight(tmp_path, c, helper, conf, ws, getpwall=lambda: users, getgrall=lambda: groups)
    assert not report.ok and hit in report.reason


def test_accounts_just_outside_the_range_are_fine(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    users = [acct("a", 60999, 60999), acct("b", 62000, 62000), acct("root", 0, 0)]
    assert preflight(tmp_path, c, helper, conf, ws, getpwall=lambda: users).ok


def test_a_step_that_raises_after_the_launch_still_kills_and_removes_the_probe(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    real_freeze = c.freeze
    kills = []
    real_kill = c.kill

    def boom(cs):
        raise OSError("cgroup.freeze vanished")

    def kill(cs):
        kills.append(cs.session_id)
        return real_kill(cs)
    c.freeze, c.kill = boom, kill
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok
    assert kills and [p for p in Path(c.root).iterdir() if p.is_dir()] == []     # killed and removed anyway
    assert real_freeze


def test_a_kill_that_failed_is_retried_in_the_cleanup(tmp_path):
    c, launcher, helper, conf, ws = tree(tmp_path)
    real_kill = c.kill
    attempts = []

    def flaky(cs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ContainmentError("cgroup still populated after kill")
        return real_kill(cs)
    c.kill = flaky
    report = preflight(tmp_path, c, helper, conf, ws)
    assert not report.ok and len(attempts) == 2 and [p for p in Path(c.root).iterdir() if p.is_dir()] == []
