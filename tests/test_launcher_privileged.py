"""The launcher against a REAL kernel: real cgroup v2, real uids, the real pe-launch, both strategies.

ROOT ONLY, and only inside a disposable container or VM. These tests install files under /usr/libexec,
/etc/planetexpress and /etc/sudoers.d, create accounts, and move every process out of the cgroup namespace root.
They refuse to run unless PE_PRIVILEGED_TESTS_OK=1, which scripts/run_privileged_tests.sh sets only inside the
container (or on the MOS VM, when told to). Everywhere else they are skipped, so the normal suite never runs them.

Each test runs twice: `direct` (Core is root, as on MOS) and `sudo` (Core is an unprivileged uid with no groups,
`sudo -n pe-launch`, as on Ubuntu).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from launcher_core import Core

from planet_express.missions.launcher.config import HELPER_PATH
from planet_express.missions.launcher.setup_steps import (
    MemoryJournal,
    Paths,
    launcher_steps,
    repair_all,
    undo_all,
)

CGROUP2 = Path("/sys/fs/cgroup")
ENABLED = (os.geteuid() == 0 and os.environ.get("PE_PRIVILEGED_TESTS_OK") == "1"
           and (CGROUP2 / "cgroup.controllers").exists())
pytestmark = [pytest.mark.skipif(
    not ENABLED, reason="root-only: run scripts/run_privileged_tests.sh (disposable container or the MOS VM)")]

CORE_UID, CORE_GID = 1500, 1500
UIDS = (61001, 61002, 61003)
INTERP = os.path.realpath(sys.executable)
TEST_CG = CGROUP2 / "pe-launcher-test"
SESSIONS = TEST_CG / "sessions"
WS = "/var/lib/pe-launcher-test/ws"
SOCKS = "/run/pe-launcher-test-socks"
RANGE = (61000, 61999)


def sh(*argv, check=True):
    return subprocess.run(argv, check=check, capture_output=True, text=True)


def move_everything_out_of(root: Path):
    """Inside a container the cgroup namespace root holds our own processes, which forbids delegating controllers."""
    init = root / "pe-test-init"
    init.mkdir(exist_ok=True)
    for pid in (root / "cgroup.procs").read_text().split():
        try:
            (init / "cgroup.procs").write_text(pid)
        except OSError:
            pass                                    # kernel threads and exited processes
    try:
        (root / "cgroup.subtree_control").write_text("+pids")
    except OSError:
        pass


def prepare_cgroups(core_uid, core_gid):
    move_everything_out_of(CGROUP2)
    for leaf in (TEST_CG, TEST_CG / "core", SESSIONS):
        leaf.mkdir(exist_ok=True)
        os.chown(leaf, core_uid, core_gid)
        for name in ("cgroup.procs", "cgroup.subtree_control", "cgroup.threads"):
            os.chown(leaf / name, core_uid, core_gid)
    (TEST_CG / "cgroup.subtree_control").write_text("+pids")
    (SESSIONS / "cgroup.subtree_control").write_text("+pids")


def remove_cgroups():
    if not TEST_CG.exists():
        return
    for path in sorted(TEST_CG.rglob("*"), reverse=True) if TEST_CG.exists() else []:
        if path.is_dir():
            try:
                (path / "cgroup.kill").write_text("1")
            except OSError:
                pass
    for _ in range(50):
        try:
            for path in sorted((p for p in TEST_CG.rglob("*") if p.is_dir()), reverse=True):
                path.rmdir()
            TEST_CG.rmdir()
            return
        except OSError:
            time.sleep(0.1)


class Host:
    def __init__(self, strategy, core, paths):
        self.strategy, self.core, self.paths = strategy, core, paths
        self.core_uid = CORE_UID if strategy == "sudo" else 0
        self.n = 0

    def sid(self, label="s"):
        self.n += 1
        return f"T-{label}-{os.getpid()}-{self.n}"

    def cwd(self, sid):
        return f"{WS}/{sid}"

    def launch(self, argv, uid=UIDS[0], sid=None, **kw):
        sid = sid or self.sid()
        got = self.core.call("launch", session_id=sid, uid=uid, cwd=self.cwd(sid), argv=list(argv), **kw)
        return sid, got

    def finish(self, sid):
        """Kill what is left, check it is empty and remove the cgroup, as Core would."""
        self.core.call("kill", session_id=sid)
        self.core.call("cleanup", session_id=sid)
        shutil.rmtree(self.cwd(sid), ignore_errors=True)


@pytest.fixture(scope="module", params=["direct", "sudo"])
def host(request):
    strategy = request.param
    core_uid, core_gid = (CORE_UID, CORE_GID) if strategy == "sudo" else (0, 0)
    if strategy == "sudo":
        sh("groupadd", "-g", str(CORE_GID), "pe-core-test", check=False)
        sh("useradd", "-M", "-N", "-g", str(CORE_GID), "-u", str(CORE_UID), "-s", "/usr/sbin/nologin", "pe-core-test",
           check=False)
        if shutil.which("sudo") is None:
            pytest.skip("sudo is not installed in this environment")
    remove_cgroups()
    prepare_cgroups(core_uid, core_gid)
    paths = Paths(core_uid=core_uid, core_gid=core_gid, core_user="pe-core-test", uid_min=RANGE[0], uid_max=RANGE[1],
                  cgroup_root=str(SESSIONS), workspace_root=WS, sock_root=SOCKS, interpreter=INTERP,
                  flavour="systemd" if strategy == "sudo" else "mos", unit_dropin="/tmp/pe-launcher-test-dropin.conf",
                  boot_script="/tmp/pe-launcher-test-boot.sh", systemctl="/bin/true")
    journal = MemoryJournal()
    steps = launcher_steps(paths)
    done = repair_all(steps, journal)
    assert not any(v.startswith("FAILED") for v in done.values()), done
    cfg = dict(cgroup_root=str(SESSIONS), sock_root=SOCKS, uid_range=RANGE)
    core = Core.as_user(CORE_UID, **cfg) if strategy == "sudo" else Core.direct(**cfg)
    yield Host(strategy, core, paths)
    core.close()
    remove_cgroups()
    undo_all(steps, journal)
    shutil.rmtree("/var/lib/pe-launcher-test", ignore_errors=True)
    shutil.rmtree(SOCKS, ignore_errors=True)


def py(code, *args):
    return [INTERP, "-I", "-c", code, *map(str, args)]


SLEEPER = "import time; time.sleep(300)"


def first_json(host, sid):
    line = host.core.call("first_line", session_id=sid)["line"]
    return json.loads(line)


# ── who is Core, and who is the session ─────────────────────────────────────────────────────────

def test_core_is_what_this_strategy_says_it_is(host):
    who = host.core.call("whoami")
    assert who["euid"] == host.core_uid and (host.strategy == "direct" or who["groups"] == [])


def test_the_session_runs_as_its_own_uid_and_gid_with_no_groups_and_no_new_privs(host):
    code = ("import json,os\nprint(json.dumps({'ids':[os.getresuid(),os.getresgid(),os.getgroups()],"
            "'nnp':[l for l in open('/proc/self/status') if l.startswith('NoNewPrivs')][0].split()[1],"
            "'sid':os.getsid(0)==os.getpid(),'cwd':os.getcwd()}),flush=True)\nimport time;time.sleep(300)")
    sid, got = host.launch(py(code))
    try:
        seen = first_json(host, sid)
        assert seen["ids"] == [[UIDS[0]] * 3, [UIDS[0]] * 3, []] and seen["nnp"] == "1" and seen["sid"] is True
        assert seen["cwd"] == host.cwd(sid) and got["session_uid"] == UIDS[0]
    finally:
        host.finish(sid)


def test_the_session_cannot_regain_root(host):
    code = ("import os,json\nr=[]\nfor f in (lambda: os.setuid(0), lambda: os.setgid(0), lambda: os.setgroups([0])):\n"
            "  try: f(); r.append('ok')\n  except PermissionError: r.append('EPERM')\n"
            "print(json.dumps(r),flush=True)\nimport time;time.sleep(300)")
    sid, _ = host.launch(py(code))
    try:
        assert first_json(host, sid) == ["EPERM", "EPERM", "EPERM"]
    finally:
        host.finish(sid)


# ── the fd map and the token ────────────────────────────────────────────────────────────────────

def test_exactly_fds_0_1_2_3_are_open_and_the_token_is_on_fd_3_only(host):
    token = "TOK" + "x" * 50
    sid, got = host.launch(py(SLEEPER), token=token, env={"PATH": "/usr/bin:/bin", "PE_SESSION_TOKEN_FD": "3"})
    try:
        pid = got["pids"][0]
        assert sorted(os.listdir(f"/proc/{pid}/fd"), key=int) == ["0", "1", "2", "3"]
        assert Path(f"/proc/{pid}/cmdline").read_bytes().find(token.encode()) == -1
        assert Path(f"/proc/{pid}/environ").read_bytes().find(token.encode()) == -1
        assert os.readlink(f"/proc/{pid}/fd/0") == "/dev/null"
        assert os.readlink(f"/proc/{pid}/fd/1") == os.readlink(f"/proc/{pid}/fd/2")          # one merged output pipe
        with open(f"/proc/{pid}/fd/3", "rb") as handle:
            assert handle.read(100) == token.encode()                                      # and the token is there
    finally:
        host.finish(sid)


def test_the_token_never_appears_in_any_process_argv_while_launching(host):
    token = "SECRETTOKEN" + "y" * 40
    seen, stop = set(), threading.Event()

    def watch():
        while not stop.is_set():
            for entry in os.listdir("/proc"):
                if entry.isdigit():
                    try:
                        for name in ("cmdline", "environ"):
                            if token.encode() in Path(f"/proc/{entry}/{name}").read_bytes():
                                seen.add((entry, name))
                    except OSError:
                        pass
    watcher = threading.Thread(target=watch)
    watcher.start()
    sid, _ = host.launch(py(SLEEPER), token=token)
    time.sleep(0.3)
    stop.set()
    watcher.join()
    host.finish(sid)
    assert seen == set()


def test_the_output_pipe_carries_the_session_output_after_the_status_line(host):
    sid, _ = host.launch(py("import sys; print('out', flush=True); print('err', file=sys.stderr, flush=True); "
                            "import time; time.sleep(300)"))
    try:
        assert host.core.call("first_line", session_id=sid)["line"] == "out"          # the status line was stripped
        assert host.core.call("first_line", session_id=sid)["line"] == "err"          # stderr is the same pipe
    finally:
        host.finish(sid)


# ── Core's authority over the cgroup, and the session's lack of it ───────────────────────────────

def test_core_can_freeze_and_kill_and_the_session_gets_eacces_on_writes(host):
    code = ("import json,os,sys\ncg=sys.argv[1]\ndef den(p):\n  try: os.close(os.open(p, os.O_WRONLY)); return False\n"
            "  except PermissionError: return True\n  except OSError as e: return str(e)\n"
            "r={n:den(cg+'/'+n) for n in ('cgroup.procs','cgroup.freeze','cgroup.kill')}\n"
            "r['parent procs']=den(os.path.dirname(cg)+'/cgroup.procs')\n"
            "r['grandparent procs']=den(os.path.dirname(os.path.dirname(cg))+'/cgroup.procs')\n"
            "r['events readable']='populated' in open(cg+'/cgroup.events').read()\n"
            "print(json.dumps(r),flush=True)\nimport time;time.sleep(300)")
    sid = host.sid()
    cg = f"{SESSIONS}/{sid}"
    sid, got = host.launch(py(code, cg), sid=sid)
    try:
        seen = first_json(host, sid)
        assert seen == {"cgroup.procs": True, "cgroup.freeze": True, "cgroup.kill": True, "parent procs": True,
                        "grandparent procs": True, "events readable": True}
        assert "frozen 1" in host.core.call("freeze", session_id=sid)["events"]
        assert "frozen 0" in host.core.call("thaw", session_id=sid)["events"]
        assert host.core.call("empty", session_id=sid)["empty"] is False
        assert host.core.call("kill", session_id=sid)["empty"] is True
        assert host.core.call("cleanup", session_id=sid)["gone"] is True
    finally:
        shutil.rmtree(host.cwd(sid), ignore_errors=True)


def test_the_cgroup_files_are_owned_by_core_and_the_directory_by_core(host):
    sid, _ = host.launch(py(SLEEPER))
    try:
        cg = Path(SESSIONS) / sid
        for name in ("cgroup.procs", "cgroup.freeze", "cgroup.kill"):
            info = os.stat(cg / name)
            assert (info.st_uid, info.st_gid) == (host.core_uid, host.core_uid if host.strategy == "sudo" else 0), name
            assert info.st_mode & 0o022 == 0
        assert (cg / "pids.max").read_text().strip().isdigit()
    finally:
        host.finish(sid)


def test_a_double_fork_escaper_stays_inside_the_cgroup_and_dies_with_it(host):
    code = ("import os,time\nif os.fork()==0:\n os.setsid()\n if os.fork()==0:\n  time.sleep(300)\n os._exit(0)\n"
            "print('up',flush=True)\ntime.sleep(300)")
    sid, _ = host.launch(py(code))
    try:
        assert host.core.call("first_line", session_id=sid)["line"] == "up"
        time.sleep(0.5)
        assert len(host.core.call("pids", session_id=sid)["pids"]) >= 2
        assert host.core.call("kill", session_id=sid)["empty"] is True
        assert host.core.call("pids", session_id=sid)["pids"] == []
    finally:
        host.finish(sid)


def test_a_fork_bomb_is_stopped_by_pids_max(host):
    code = ("import os,json,time\nn=0\nkids=[]\nwhile True:\n try:\n  pid=os.fork()\n except OSError:\n  break\n"
            " if pid==0:\n  time.sleep(60); os._exit(0)\n kids.append(pid); n+=1\n"
            "print(json.dumps({'forked':n}),flush=True)\ntime.sleep(60)")
    sid, _ = host.launch(py(code))
    try:
        forked = first_json(host, sid)["forked"]
        assert 1 <= forked < 2000                         # bounded by pids.max, not by the host
    finally:
        host.finish(sid)


# ── isolation between two real sessions ─────────────────────────────────────────────────────────

def test_one_session_cannot_ptrace_read_proc_or_signal_another(host):
    a, got_a = host.launch(py(SLEEPER), uid=UIDS[0])
    code = ("import ctypes,json,os,signal,sys\npid=int(sys.argv[1])\nr={}\n"
            "def t(name,f):\n  try: f(); r[name]='allowed'\n  except PermissionError: r[name]='EACCES'\n"
            "  except OSError as e: r[name]=e.errno\n"
            "t('fd dir',lambda: os.listdir(f'/proc/{pid}/fd'))\n"
            "t('environ',lambda: open(f'/proc/{pid}/environ','rb').read())\n"
            "t('mem',lambda: open(f'/proc/{pid}/mem','rb'))\n"
            "t('cwd link',lambda: os.readlink(f'/proc/{pid}/cwd'))\n"
            "t('kill',lambda: os.kill(pid, signal.SIGKILL))\n"
            "libc=ctypes.CDLL(None,use_errno=True)\nr['ptrace attach']=libc.ptrace(16,pid,0,0)\n"
            "r['ptrace errno']=ctypes.get_errno()\nprint(json.dumps(r),flush=True)\nimport time;time.sleep(300)")
    b, _ = host.launch(py(code, got_a["pids"][0]), uid=UIDS[1])
    try:
        seen = first_json(host, b)
        assert seen["fd dir"] == "EACCES" and seen["environ"] == "EACCES" and seen["mem"] == "EACCES"
        assert seen["cwd link"] == "EACCES" and seen["kill"] == "EACCES"
        assert seen["ptrace attach"] == -1 and seen["ptrace errno"] == 1            # EPERM
        assert host.core.call("pids", session_id=a)["pids"] == got_a["pids"]       # A is untouched
    finally:
        host.finish(b)
        host.finish(a)


def test_one_session_cannot_see_into_another_sessions_cgroup_or_workspace_or_socket_directory(host):
    a, _ = host.launch(py(SLEEPER), uid=UIDS[0], sock_dir=True)
    other = host.sid("b")
    code = ("import json,os,sys\nr={}\n"
            "for name,path in (('workspace',sys.argv[1]),('sockdir',sys.argv[2])):\n"
            "  try: os.listdir(path); r[name]='allowed'\n  except PermissionError: r[name]='EACCES'\n"
            "  except OSError as e: r[name]=e.errno\n"
            "print(json.dumps(r),flush=True)\nimport time;time.sleep(300)")
    b, _ = host.launch(py(code, host.cwd(a), f"{SOCKS}/{a}"), uid=UIDS[1], sid=other)
    try:
        assert first_json(host, b) == {"workspace": "EACCES", "sockdir": "EACCES"}
    finally:
        host.finish(b)
        host.finish(a)


# ── workspace and socket directory hand-over ────────────────────────────────────────────────────

def test_the_workspace_is_handed_to_the_session_uid_and_core_keeps_its_group(host):
    sid = host.sid()
    ws = host.cwd(sid)
    os.makedirs(ws, mode=0o700)
    os.chown(ws, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    Path(ws, "BRIEF.md").write_text("brief")
    os.chown(Path(ws, "BRIEF.md"), host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    code = "open('result.json','w').write('{}')\nprint('done',flush=True)\nimport time;time.sleep(300)"
    sid, _ = host.launch(py(code), sid=sid, make_cwd=False)
    try:
        assert host.core.call("first_line", session_id=sid)["line"] == "done"
        info = os.stat(ws)
        assert info.st_uid == UIDS[0] and info.st_mode & 0o7777 == 0o2770
        assert info.st_gid == (CORE_GID if host.strategy == "sudo" else 0)
        assert os.stat(f"{ws}/BRIEF.md").st_uid == UIDS[0]
        assert host.core.call("read", path=f"{ws}/result.json")["text"] == "{}"     # Core reads it through its group
        assert os.stat(f"{ws}/result.json").st_gid == info.st_gid                   # inherited from the setgid directory
    finally:
        host.finish(sid)


def test_the_socket_directory_and_socket_are_handed_to_the_session_and_only_it_can_connect(host):
    code = ("import json,socket,sys\ns=socket.socket(socket.AF_UNIX)\ns.connect(sys.argv[1]+'/worker.sock')\n"
            "print(json.dumps('connected'),flush=True)\nimport time;time.sleep(300)")
    sid = host.sid()
    directory = f"{SOCKS}/{sid}"
    sid, _ = host.launch(py(code, directory), uid=UIDS[0], sock_dir=True, sid=sid)
    try:
        assert first_json(host, sid) == "connected"                            # the session reaches its own socket
        assert host.core.call("accept_on", session_id=sid)["uid"] == UIDS[0]  # and Core sees WHO connected
        info = os.stat(directory)
        assert info.st_uid == host.core_uid and info.st_gid == UIDS[0] and info.st_mode & 0o7777 == 0o750
        sock = os.stat(f"{directory}/worker.sock")
        assert (sock.st_uid, sock.st_gid) == (UIDS[0], UIDS[0])               # handed over by the helper before exec
        assert sock.st_mode & 0o7777 == 0o600                                  # and no group access, whatever Core made
        assert os.stat(SOCKS).st_mode & 0o777 == 0o711
        other, _ = host.launch(py("import socket,sys,json\ntry:\n s=socket.socket(socket.AF_UNIX)\n"
                                  " s.connect(sys.argv[1]+'/worker.sock'); r='connected'\n"
                                  "except PermissionError: r='EACCES'\nexcept OSError as e: r=e.errno\n"
                                  "print(json.dumps(r),flush=True)\nimport time;time.sleep(300)", directory),
                               uid=UIDS[1])
        try:
            assert first_json(host, other) == "EACCES"                         # another session cannot
        finally:
            host.finish(other)
        assert host.core.call("kill", session_id=sid)["empty"] is True
        host.core.call("cleanup", session_id=sid)
        assert not os.path.exists(directory)                                   # cleanup removes the directory and socket
    finally:
        shutil.rmtree(host.cwd(sid), ignore_errors=True)


# ── what the helper refuses, end to end ─────────────────────────────────────────────────────────
# These talk to the REAL helper with a hand-made spec, bypassing Core's own pre-checks, so they prove the helper's
# own validation. A spec the helper wrongly accepted would run /bin/true and print an ok status: the test fails.

def helper_argv(host):
    return [HELPER_PATH] if host.strategy == "direct" else ["/usr/bin/sudo", "-n", HELPER_PATH]


def raw_spec(host, **over):
    sid = host.sid("raw")
    cwd = host.cwd(sid)
    os.makedirs(cwd, mode=0o700, exist_ok=True)
    os.chown(cwd, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    spec = {"session_id": sid, "worker_uid": UIDS[0], "cwd": cwd, "argv": ["/bin/true"],
            "env": {"PATH": "/usr/bin:/bin"}, "token": "t" * 40}
    spec.update(over)
    return spec


def run_raw(host, spec, extra_env=None, data=None):
    kw = {} if host.strategy == "direct" else {"user": CORE_UID, "group": CORE_GID, "extra_groups": []}
    payload = data if data is not None else json.dumps(spec).encode()
    result = subprocess.run(helper_argv(host), input=payload, capture_output=True, timeout=30, check=False,
                            env={"PATH": "/usr/bin:/bin", **(extra_env or {})}, **kw)
    line = result.stdout.decode(errors="replace").splitlines()[0] if result.stdout else ""
    assert line.startswith("PE-LAUNCH-STATUS "), (result.stdout[:200], result.stderr[:200])
    return json.loads(line[len("PE-LAUNCH-STATUS "):])


def assert_refused(host, spec, why, **kw):
    status = run_raw(host, spec, **kw)
    assert status["ok"] is False and __import__("re").search(why, status["error"]), status
    assert not (Path(SESSIONS) / spec.get("session_id", "-")).exists(), "a refused launch must not leave a cgroup"
    assert not Path(f"{SOCKS}/{spec.get('session_id', '-')}").exists()


def test_the_helper_accepts_a_good_spec(host):
    spec = raw_spec(host)
    status = run_raw(host, spec)
    assert status["ok"] is True and status["uid"] == UIDS[0] and status["gid"] == UIDS[0]
    assert status["cgroup"] == f"{SESSIONS}/{spec['session_id']}"
    for _ in range(50):                           # /bin/true exits; the cgroup empties and Core removes it
        try:
            os.rmdir(status["cgroup"])
            break
        except OSError:
            time.sleep(0.1)


@pytest.mark.parametrize("uid", [0, 1, 1000, RANGE[0] - 1, RANGE[1] + 1, 100000, 2**31, -5])
def test_a_uid_that_is_root_or_outside_the_range_is_refused_by_the_helper(host, uid):
    assert_refused(host, raw_spec(host, worker_uid=uid), "uid|worker_uid")


def test_the_core_uid_is_refused_by_the_helper_as_a_session_uid(host):
    if host.core_uid == 0:
        pytest.skip("Core is root here; uid 0 is covered above")
    assert_refused(host, raw_spec(host, worker_uid=host.core_uid), "uid")


@pytest.mark.parametrize("field, value", [("gid", 0), ("group", "root"), ("cgroup_root", "/sys/fs/cgroup"),
                                          ("workspace_root", "/"), ("uid_min", 0), ("gids", [0]), ("user", "root")])
def test_a_spec_field_cannot_select_a_gid_a_root_or_a_path(host, field, value):
    assert_refused(host, raw_spec(host, **{field: value}), "spec keys")


@pytest.mark.parametrize("cwd", ["/tmp", "/", "/etc", "/root", WS, WS + "/../x", WS + "-evil/s", "relative/path"])
def test_a_cwd_outside_the_workspace_root_is_refused_by_the_helper(host, cwd):
    assert_refused(host, raw_spec(host, cwd=cwd), "cwd|path")


def test_a_symlinked_cwd_and_a_symlink_in_the_middle_are_refused_by_the_helper(host):
    target = Path("/tmp") / f"pe-link-target-{os.getpid()}"
    (target / "inner").mkdir(parents=True, exist_ok=True)
    os.makedirs(WS, exist_ok=True)
    link, middle = Path(WS) / f"link-{os.getpid()}", Path(WS) / f"mid-{os.getpid()}"
    link.symlink_to(target)
    middle.symlink_to(target)
    try:
        assert_refused(host, raw_spec(host, cwd=str(link)), "path component")
        assert_refused(host, raw_spec(host, cwd=str(middle / "inner")), "path component")
    finally:
        link.unlink()
        middle.unlink()
        shutil.rmtree(target, ignore_errors=True)


def test_a_cwd_owned_by_a_third_party_is_refused_by_the_helper(host):
    spec = raw_spec(host)
    os.chown(spec["cwd"], 4242, 4242)
    assert_refused(host, spec, "cwd owner")


@pytest.mark.parametrize("argv, why", [(["true"], "absolute|argv"), (["./true"], "absolute|argv"), ([], "argv"),
                                       (["/nonexistent/prog"], "argv"), (["/etc"], "argv"), (["/etc/passwd"], "argv"),
                                       (["/bin/true"] + ["x"] * 70, "argv")])
def test_a_bad_argv_is_refused_by_the_helper(host, argv, why):
    assert_refused(host, raw_spec(host, argv=argv), why)


def test_an_oversize_spec_and_oversize_fields_are_refused_by_the_helper(host):
    assert_refused(host, raw_spec(host), "spec too large", data=b"{" + b" " * 70000 + b"}")
    assert_refused(host, raw_spec(host, env={f"V{i}": "x" * 4000 for i in range(9)}), "env")
    assert_refused(host, raw_spec(host, token="t" * 5000), "token")
    assert_refused(host, raw_spec(host, env={"LD_PRELOAD": "/x.so"}), "env")


def test_a_garbage_or_duplicate_key_spec_is_refused_by_the_helper(host):
    for data in (b"", b"not json", b"[]", b'{"session_id": "a", "session_id": "b"}', b"\xff\xfe"):
        status = run_raw(host, {}, data=data)
        assert status["ok"] is False


def test_a_session_that_already_has_a_cgroup_is_refused_and_the_first_is_untouched(host):
    sid, got = host.launch(py(SLEEPER))
    try:
        spec = raw_spec(host, session_id=sid, worker_uid=UIDS[1])
        status = run_raw(host, spec)
        assert status["ok"] is False and "cgroup" in status["error"]
        assert host.core.call("pids", session_id=sid)["pids"] == got["pids"]
    finally:
        host.finish(sid)


def test_a_uid_with_a_group_that_has_members_is_refused_by_the_helper(host):
    sh("groupadd", "-g", str(UIDS[2]), "pe-test-shared", check=False)
    sh("usermod", "-a", "-G", "pe-test-shared", "nobody", check=False)
    try:
        assert_refused(host, raw_spec(host, worker_uid=UIDS[2]), "group|members")
    finally:
        sh("gpasswd", "-d", "nobody", "pe-test-shared", check=False)
        sh("groupdel", "pe-test-shared", check=False)


def test_a_real_account_in_the_range_is_refused_by_the_helper_and_by_preflight(host):
    sh("groupadd", "-g", str(UIDS[2]), "pe-test-acct", check=False)
    sh("useradd", "-M", "-N", "-u", str(UIDS[2]), "-g", str(UIDS[2]), "-s", "/usr/sbin/nologin", "pe-test-acct",
       check=False)
    try:
        assert_refused(host, raw_spec(host, worker_uid=UIDS[2]), "belongs to an account")
        report = host.core.call("preflight", workspace_root=WS, helper=HELPER_PATH, conf=host.paths.conf, python=INTERP,
                                strategy=host.strategy)
        assert report["ok"] is False and any(c["name"] == "no account or group uses the session uid range"
                                             and not c["ok"] for c in report["checks"])
    finally:
        sh("userdel", "pe-test-acct", check=False)
        sh("groupdel", "pe-test-acct", check=False)


def test_a_workspace_with_a_subdirectory_is_refused_by_the_helper_and_nothing_is_left_behind(host):
    spec = raw_spec(host)
    os.makedirs(f"{spec['cwd']}/sub", mode=0o700)
    assert_refused(host, spec, "workspace entry")         # refused AFTER the cgroup was made: it must be removed again


def test_a_socket_directory_that_is_missing_or_has_the_wrong_contents_is_refused_by_the_helper(host):
    missing = raw_spec(host, sock_dir=True)
    assert_refused(host, missing, "socket dir")
    wrong = raw_spec(host, sock_dir=True)
    directory = Path(SOCKS) / wrong["session_id"]
    directory.mkdir(parents=True)
    os.chown(directory, host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    (directory / "worker.sock").write_text("a plain file, not a socket")
    os.chown(directory / "worker.sock", host.core_uid, host.core_uid if host.strategy == "sudo" else 0)
    try:
        status = run_raw(host, wrong)
        assert status["ok"] is False and "socket" in status["error"]
        assert not (Path(SESSIONS) / wrong["session_id"]).exists()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_hostile_core_environment_is_ignored_by_the_helper(host):
    marker = Path("/tmp/pe-evil-marker")
    marker.unlink(missing_ok=True)
    evil = Path("/tmp/pe-evil-path")
    evil.mkdir(exist_ok=True)
    body = f"open({str(marker)!r}, 'w').write('pwned')\n"
    for name in ("json.py", "ctypes.py", "sitecustomize.py", "re.py", "os.py"):
        (evil / name).write_text(body)
    hostile = {"PYTHONPATH": str(evil), "PYTHONHOME": str(evil), "PYTHONSTARTUP": str(evil / "sitecustomize.py"),
               "PYTHONINSPECT": "1", "PYTHONUSERBASE": str(evil)}
    try:
        assert run_raw(host, raw_spec(host, worker_uid=0), extra_env=hostile)["ok"] is False
        assert not marker.exists(), "an attacker-controlled variable changed what the helper imported"
    finally:
        shutil.rmtree(evil, ignore_errors=True)


def test_a_hostile_core_environment_never_reaches_the_session_either(host, monkeypatch):
    for name, value in (("LD_PRELOAD", "/nonexistent/evil.so"), ("PYTHONPATH", "/nonexistent"), ("EVIL", "1")):
        monkeypatch.setenv(name, value)
    sid, got = host.launch(py(SLEEPER), env={"PATH": "/usr/bin:/bin", "ONLY_THIS": "yes"})
    try:
        environ = []
        for _ in range(50):  # the pid is listed before the exec lands; /proc/<pid>/environ is empty until then
            environ = Path(f"/proc/{got['pids'][0]}/environ").read_bytes().split(b"\0")
            if any(environ):
                break
            time.sleep(0.1)
        assert sorted(e for e in environ if e) == [b"ONLY_THIS=yes", b"PATH=/usr/bin:/bin"]
    finally:
        host.finish(sid)


@pytest.mark.parametrize("extra", [["--x"], [""], ["a", "b"]])
def test_the_helper_refuses_any_argument_even_directly_as_root(host, extra):
    result = subprocess.run([HELPER_PATH, *extra], input=b"{}", capture_output=True, timeout=30, check=False)
    assert result.returncode == 1 and b'"ok":false' in result.stdout


def test_sudo_refuses_the_helper_with_an_argument(host):
    if host.strategy != "sudo":
        pytest.skip("sudo is only the sudo strategy's door")
    result = subprocess.run(["/usr/bin/sudo", "-n", HELPER_PATH, "--x"], input=b"{}", capture_output=True, timeout=30,
                            check=False, user=CORE_UID, group=CORE_GID, extra_groups=[])
    assert result.returncode != 0 and b"PE-LAUNCH-STATUS" not in result.stdout


def test_sudo_does_not_let_core_run_anything_else_as_root(host):
    if host.strategy != "sudo":
        pytest.skip("sudo only")
    for argv in (["/usr/bin/sudo", "-n", "/bin/true"], ["/usr/bin/sudo", "-n", "-i"],
                 ["/usr/bin/sudo", "-n", "-u", "root", "id"]):
        result = subprocess.run(argv, capture_output=True, timeout=30, check=False, user=CORE_UID, group=CORE_GID,
                                extra_groups=[])
        assert result.returncode != 0, argv


# ── two real sessions, sequential and concurrent (the carried-in Codex P1) ───────────────────────

def test_two_sequential_sessions_with_different_uids_both_start(host):
    results = []
    for uid in UIDS[:2]:
        sid, got = host.launch(py(SLEEPER), uid=uid)
        results.append(got["session_uid"])
        host.finish(sid)
    assert results == list(UIDS[:2])


def test_two_concurrent_sessions_both_start_and_run_as_their_own_uid(host):
    a, got_a = host.launch(py(SLEEPER), uid=UIDS[0])
    b, got_b = host.launch(py(SLEEPER), uid=UIDS[1])
    try:
        uid_of = lambda pid: Path(f"/proc/{pid}/status").read_text().split("Uid:")[1].split()[0]    # noqa: E731
        assert uid_of(got_a["pids"][0]) == str(UIDS[0]) and uid_of(got_b["pids"][0]) == str(UIDS[1])
    finally:
        host.finish(a)
        host.finish(b)


def test_a_uid_is_in_use_until_the_last_process_running_as_it_is_gone(host):
    sid, _ = host.launch(py(SLEEPER), uid=UIDS[2])
    assert host.core.call("uid_in_use", uid=UIDS[2])["in_use"] is True
    host.finish(sid)
    deadline = time.time() + 5
    while time.time() < deadline and host.core.call("uid_in_use", uid=UIDS[2])["in_use"]:
        time.sleep(0.1)
    assert host.core.call("uid_in_use", uid=UIDS[2])["in_use"] is False


def test_a_process_outside_the_cgroup_running_as_the_uid_keeps_it_in_use(host):
    stray = subprocess.Popen(["/bin/sleep", "300"], user=UIDS[2], group=UIDS[2], extra_groups=[])
    try:
        assert host.core.call("uid_in_use", uid=UIDS[2])["in_use"] is True
    finally:
        stray.kill()
        stray.wait()


# ── installation hardening, with the real helper ───────────────────────────────────────────────

def run_copy_of_helper(directory: Path, via: Path | None = None):
    target = directory / "pe-launch"
    shutil.copy(HELPER_PATH, target)
    target.chmod(0o755)
    spec = json.dumps({"session_id": "T-x", "worker_uid": 61001, "cwd": f"{WS}/nope", "argv": ["/bin/true"], "env": {},
                       "token": "t" * 40}).encode()
    run = (via or directory) / "pe-launch"
    return subprocess.run([str(run)], input=spec, capture_output=True, timeout=30, check=False,
                          env={"PATH": "/usr/bin:/bin"})


def error_of(result):
    return json.loads(result.stdout.decode().splitlines()[0][len("PE-LAUNCH-STATUS "):]).get("error")


def test_a_helper_in_a_group_writable_directory_refuses_to_run(host, tmp_path):
    base = Path("/opt/pe-test-writable")
    base.mkdir(exist_ok=True)
    os.chmod(base, 0o775)
    try:
        assert error_of(run_copy_of_helper(base)) == "untrusted ownership or mode"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_helper_reached_through_a_symlinked_directory_refuses_to_run(host):
    real = Path("/opt/pe-test-real")
    link = Path("/opt/pe-test-link")
    real.mkdir(exist_ok=True)
    os.chmod(real, 0o755)
    link.symlink_to(real)
    try:
        assert error_of(run_copy_of_helper(real, via=link)) == "symlink in a trusted path"
    finally:
        link.unlink()
        shutil.rmtree(real, ignore_errors=True)


def test_a_world_writable_helper_directory_in_the_real_chain_is_caught_by_preflight(host):
    directory = Path(HELPER_PATH).parent
    before = directory.stat().st_mode & 0o7777
    os.chmod(directory, 0o777)
    try:
        report = host.core.call("preflight", workspace_root=WS, helper=HELPER_PATH, conf=host.paths.conf, python=INTERP,
                                strategy=host.strategy)
    finally:
        os.chmod(directory, before)
    assert report["ok"] is False and any(c["name"] == "helper install" and not c["ok"] for c in report["checks"])


def test_a_conf_readable_by_others_is_refused_by_the_helper(host):
    conf = Path(host.paths.conf)
    before = conf.stat().st_mode & 0o7777
    os.chmod(conf, 0o644)
    try:
        assert_refused(host, raw_spec(host), "conf")
    finally:
        os.chmod(conf, before)


# ── preflight, for real ────────────────────────────────────────────────────────────────────────────

def test_the_real_preflight_passes_on_a_correctly_installed_host(host):
    report = host.core.call("preflight", workspace_root=WS, helper=HELPER_PATH, conf=host.paths.conf, python=INTERP,
                            strategy=host.strategy)
    assert report["ok"], json.dumps(report, indent=1)
    names = {c["name"] for c in report["checks"]}
    assert {"Core freezes the session and sees frozen 1", "the killed session is empty (populated 0)",
            "session uid cannot write cgroup.kill", "pids.max set and readable on the session"} <= names


def test_preflight_failure_makes_the_supervisor_proposal_only_on_the_real_host(host, tmp_path):
    os.chmod(host.paths.conf, 0o666)
    try:
        report = host.core.call("preflight", workspace_root=WS, helper=HELPER_PATH, conf=host.paths.conf, python=INTERP,
                                strategy=host.strategy)
    finally:
        os.chmod(host.paths.conf, 0o600)
    assert report["ok"] is False
    from supervisor_fakes import Rig
    rig = Rig(tmp_path)
    from planet_express.missions.launcher.preflight import PreflightReport
    failing = PreflightReport(host.strategy)
    for c in report["checks"]:
        failing.add(c["name"], c["ok"], c["detail"])
    rig.sup.preflight_fn = lambda: failing
    from planet_express.missions.supervisor import ProposalOnly
    with pytest.raises(ProposalOnly):
        rig.sup.start(rig.mid)
    assert rig.sessions() == [] and rig.runtimes == []
