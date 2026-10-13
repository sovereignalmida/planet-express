"""CgroupContainment against a fake cgroup tree on tmpfs (plain files), plus root-only real tests."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from planet_express.missions.containment import ContainedSession
from planet_express.missions.containment_cgroup import (
    CgroupContainment,
    ContainmentError,
)


class FakeFsContainment(CgroupContainment):
    def _rmdir(self, path):  # a real cgroupfs drops its own control files on rmdir
        for f in Path(path).iterdir():
            f.unlink()
        super()._rmdir(path)


class Recording(CgroupContainment):
    """Records every control-file write in order; optionally empties the cgroup after N polls."""

    def __init__(self, *a, empty_after_polls=None, **kw):
        self.writes = []
        self.polls = 0
        self.empty_after_polls = empty_after_polls
        super().__init__(*a, **kw)

    def _write(self, path, value):
        self.writes.append((Path(path).name, value))
        super()._write(path, value)
        if Path(path).name == "cgroup.freeze":      # the kernel reflects the request in cgroup.events
            events = Path(path).with_name("cgroup.events")
            if events.exists():
                text = events.read_text().splitlines()
                events.write_text("\n".join([l for l in text if not l.startswith("frozen")] + [f"frozen {value}"]) + "\n")

    def _read(self, path):
        if Path(path).name == "cgroup.events":
            self.polls += 1
            if self.empty_after_polls is not None and self.polls > self.empty_after_polls:
                Path(path).write_text("populated 0\nfrozen 1\n")
        return super()._read(path)


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "cg"
    d.mkdir()
    return d


def make(root, cls=CgroupContainment, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("allow_unprivileged_for_tests", True)
    return cls(root, worker_uid=None, worker_gid=None, **kw)


def seed_cgroup(cs, populated=1):
    d = Path(cs.cgroup_path)
    (d / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    (d / "cgroup.procs").write_text("")
    (d / "cgroup.freeze").write_text("0")
    (d / "cgroup.kill").write_text("")


def test_create_makes_dir_under_root_and_describes_session(root):
    c = make(root)
    cs = c.create("s1")
    assert cs.kind == "local_cgroup" and cs.session_id == "s1"
    assert cs.cgroup_path == str(root / "s1") and Path(cs.cgroup_path).is_dir()


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "x" * 65, "a b", "a\0b"])
def test_create_rejects_unsafe_ids(root, bad):
    with pytest.raises(ValueError):
        make(root).create(bad)


def test_create_twice_refused(root):
    c = make(root)
    c.create("s1")
    with pytest.raises(ContainmentError):
        c.create("s1")


def test_operations_refuse_a_path_outside_the_root(root, tmp_path):
    c = make(root)
    outside = tmp_path / "other"
    outside.mkdir()
    evil = ContainedSession("s1", "local_cgroup", str(outside), None)
    for op in (c.pids, c.freeze, c.thaw, c.kill, c.is_empty, c.cleanup):
        with pytest.raises(ValueError):
            op(evil)
    for bad in (ContainedSession("s", "fake", str(root / "s"), None),
                ContainedSession("s", "local_cgroup", None, None),
                ContainedSession("s", "local_cgroup", str(root / "s" / ".." / ".."), None)):
        with pytest.raises(ValueError):
            c.is_empty(bad)


def test_freeze_thaw_write_the_protocol_values(root):
    c = make(root, Recording)
    cs = c.create("s1")
    seed_cgroup(cs)
    c.freeze(cs)
    c.thaw(cs)
    assert c.writes == [("cgroup.freeze", "1"), ("cgroup.freeze", "0")]


def test_kill_is_freeze_then_kill_then_poll_until_empty(root):
    c = make(root, Recording, empty_after_polls=2)
    cs = c.create("s1")
    seed_cgroup(cs)
    c.kill(cs)
    assert c.writes == [("cgroup.freeze", "1"), ("cgroup.kill", "1")]
    assert c.polls >= 3  # polled at least until populated 0 was seen
    assert c.is_empty(cs)


def test_kill_does_not_return_while_populated_and_times_out_loudly(root):
    now = [0.0]

    def sleep(s):
        now[0] += s

    c = make(root, Recording, sleep=sleep, clock=lambda: now[0], kill_timeout_s=5.0)
    cs = c.create("s1")
    seed_cgroup(cs)
    with pytest.raises(ContainmentError):
        c.kill(cs)
    assert not c.is_empty(cs)  # never claims dead when it is not


@pytest.mark.parametrize("events,expected", [
    ("populated 0\nfrozen 0\n", True),
    ("populated 1\nfrozen 0\n", False),
    ("frozen 0\n", False),            # no populated line: unknown is not dead
    ("populated maybe\n", False),
    ("", False),
])
def test_is_empty_reads_cgroup_events_and_fails_closed(root, events, expected):
    c = make(root)
    cs = c.create("s1")
    (Path(cs.cgroup_path) / "cgroup.events").write_text(events)
    assert c.is_empty(cs) is expected


def test_is_empty_missing_cgroup_inside_root_means_removed(root):
    c = make(root)
    cs = ContainedSession("gone", "local_cgroup", str(root / "gone"), None)
    assert c.is_empty(cs) is True  # the kernel only removes an empty cgroup


def test_pids_lists_cgroup_procs(root):
    c = make(root)
    cs = c.create("s1")
    (Path(cs.cgroup_path) / "cgroup.procs").write_text("12\n40\n")
    assert c.pids(cs) == [12, 40]


def test_cleanup_refuses_until_empty_then_removes(root):
    c = make(root, FakeFsContainment)
    cs = c.create("s1")
    d = Path(cs.cgroup_path)
    seed_cgroup(cs, populated=1)
    with pytest.raises(ContainmentError):
        c.cleanup(cs)
    assert d.exists()
    seed_cgroup(cs, populated=0)
    c.cleanup(cs)
    assert not d.exists()


PROBE = r"""
import json, os, sys
out = {
  "in_procs": str(os.getpid()) in open(sys.argv[1]).read(),
  "env": dict(os.environ),
  "fds": sorted(int(f) for f in os.listdir("/proc/self/fd")),
  "sid_is_self": os.getsid(0) == os.getpid(),
  "cwd": os.getcwd(),
  "nnp": [l for l in open("/proc/self/status") if l.startswith("NoNewPrivs")][0].split()[1],
  "pass_fd_data": os.read(int(sys.argv[2]), 100).decode(),
}
open("probe.json", "w").write(json.dumps(out))
"""


def test_spawn_joins_cgroup_before_exec_and_isolates_the_child(root, tmp_path):
    c = make(root)
    cs = c.create("s1")
    seed_cgroup(cs)
    work = tmp_path / "work"
    work.mkdir()
    r, w = os.pipe()
    os.write(w, b"tok")
    os.close(w)
    leak = os.open(__file__, os.O_RDONLY)
    os.set_inheritable(leak, True)  # an fd the child must NOT inherit
    try:
        pid = c.spawn(cs, [sys.executable, "-c", PROBE, str(Path(cs.cgroup_path) / "cgroup.procs"), str(r)],
                      {"LC_ALL": "C.UTF-8", "FOO": "bar"}, str(work), (r,))
        deadline = time.time() + 10
        while not (work / "probe.json").exists() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
    finally:
        os.close(leak)
        os.close(r)
    out = json.loads((work / "probe.json").read_text())
    assert out["in_procs"] is True                       # already a member when the program started
    assert str(pid) in (Path(cs.cgroup_path) / "cgroup.procs").read_text().split()
    assert {k: v for k, v in out["env"].items() if k != "LC_CTYPE"} == {"LC_ALL": "C.UTF-8", "FOO": "bar"}
    assert out["sid_is_self"] is True                    # new session
    assert out["nnp"] == "1"                             # no_new_privs
    assert out["pass_fd_data"] == "tok"
    assert leak not in out["fds"] and r in out["fds"]
    assert len(set(out["fds"]) - {0, 1, 2, r}) <= 1      # only listdir's own fd may remain
    assert out["cwd"] == str(work)


def test_privilege_drop_is_required_unless_explicitly_waived(root):
    with pytest.raises(ValueError):
        CgroupContainment(root, worker_uid=None, worker_gid=None)
    with pytest.raises(ValueError):
        CgroupContainment(root, worker_uid=0, worker_gid=0)  # the worker is never root


def test_output_fd_is_a_nonblocking_pipe_of_child_stdout_and_stderr(root, tmp_path):
    c = make(root)
    cs = c.create("s1")
    seed_cgroup(cs)
    c.spawn(cs, [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            {}, str(tmp_path))
    fd = c.output_fd(cs)
    data = b""
    deadline = time.time() + 10
    while b"err" not in data and time.time() < deadline:
        try:
            data += os.read(fd, 100)
        except BlockingIOError:
            time.sleep(0.02)
    assert b"out" in data and b"err" in data
    c.release_output(cs)
    with pytest.raises(OSError):
        os.fstat(fd)


# --- real cgroup v2, root only --------------------------------------------------------------

root_only = pytest.mark.skipif(
    os.geteuid() != 0 or not Path("/sys/fs/cgroup/cgroup.controllers").exists(),
    reason="needs root and cgroup v2; see scripts/mission_containment_rehearsal.py",
)


@root_only
def test_real_cgroup_kill_catches_a_double_forking_escaper(tmp_path):
    base = Path("/sys/fs/cgroup") / f"pe-test-{os.getpid()}"
    base.mkdir()
    try:
        c = CgroupContainment(base, worker_uid=65534, worker_gid=65534)
        cs = c.create("s1")
        script = ("import os,time\n"
                  "if os.fork()==0:\n os.setsid()\n if os.fork()==0:\n  time.sleep(300)\n os._exit(0)\n"
                  "time.sleep(300)\n")
        os.chmod(tmp_path, 0o755)
        c.spawn(cs, [sys.executable, "-c", script], {}, str(tmp_path))
        time.sleep(0.5)
        assert len(c.pids(cs)) >= 2 and not c.is_empty(cs)
        c.kill(cs)
        assert c.is_empty(cs)
        c.cleanup(cs)
    finally:
        subprocess.run(["rmdir", str(base)], check=False)


# ── S3c: the launcher strategy ──────────────────────────────────────────────

@pytest.mark.skipif(os.geteuid() == 0, reason="the unprivileged failure path needs a non-root caller")
def test_default_launcher_without_privilege_fails_with_a_clear_message(root, tmp_path):
    c = CgroupContainment(root, worker_uid=4242, worker_gid=4242, sleep=lambda s: None, require_cgroup2=False)
    cs = c.create("s1")
    seed_cgroup(cs)
    with pytest.raises(ContainmentError) as exc:
        c.spawn(cs, [sys.executable, "-c", "pass"], {}, str(tmp_path))
    assert "launcher" in str(exc.value) and "CAP_SETUID" in str(exc.value)
    assert cs.session_id not in c._out and cs.session_id not in c._leaders       # nothing half-started
    assert c.pids(cs) == []


def test_a_custom_launcher_receives_a_complete_request_and_its_leader_is_tracked(root, tmp_path):
    seen = []

    class Fake:
        def launch(self, request):
            seen.append(request)
            return subprocess.Popen([sys.executable, "-c", "print('hi')"], stdout=request.output_fd,
                                    stderr=request.output_fd, cwd=request.cwd)

    c = CgroupContainment(root, worker_uid=4242, worker_gid=4242, sleep=lambda s: None, launcher=Fake(), require_cgroup2=False)
    cs = c.create("s1")
    seed_cgroup(cs)
    r, w = os.pipe()
    try:
        pid = c.spawn(cs, ["/bin/agent", "--x"], {"A": "b"}, str(tmp_path), (r,))
    finally:
        os.close(r)
        os.close(w)
    req = seen[0]
    assert (req.argv, req.env, req.cwd, req.pass_fds) == (["/bin/agent", "--x"], {"A": "b"}, str(tmp_path), (r,))
    assert req.procs_path == str(Path(cs.cgroup_path) / "cgroup.procs")
    assert (req.uid, req.gid) == (4242, 4242)
    assert pid == c._leaders["s1"].pid
    deadline = time.time() + 10
    data = b""
    while b"hi" not in data and time.time() < deadline:
        try:
            data += os.read(c.output_fd(cs), 100)
        except BlockingIOError:
            time.sleep(0.02)
    assert b"hi" in data
    c.release_output(cs)


def test_a_failing_launcher_leaks_no_pipe(root, tmp_path):
    class Boom:
        def launch(self, request):
            raise RuntimeError("no")

    c = CgroupContainment(root, worker_uid=4242, worker_gid=4242, launcher=Boom(), require_cgroup2=False)
    cs = c.create("s1")
    seed_cgroup(cs)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ContainmentError):
        c.spawn(cs, ["/bin/x"], {}, str(tmp_path))
    assert len(os.listdir("/proc/self/fd")) == before


# ── addendum: identity, cgroup2, frozen polling ─────────────────────────────

def test_worker_uid_equal_to_the_core_user_is_refused(root):
    with pytest.raises(ValueError):
        CgroupContainment(root, worker_uid=os.geteuid(), worker_gid=4242, require_cgroup2=False)


def test_spawn_rechecks_the_worker_identity(root, tmp_path):
    c = CgroupContainment(root, worker_uid=4242, worker_gid=4242, require_cgroup2=False,
                          launcher=type("L", (), {"launch": lambda self, r: pytest.fail("launched")})())
    cs = c.create("s1")
    seed_cgroup(cs)
    c.worker_uid = os.geteuid()
    with pytest.raises(ContainmentError):
        c.spawn(cs, ["/bin/x"], {}, str(tmp_path))


def test_cgroup_root_must_be_on_a_cgroup2_mount(root, tmp_path):
    from planet_express.missions.containment_cgroup import cgroup2_mount_of
    info = ("25 1 0:22 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
            "30 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "31 25 0:40 / /sys/fs/cgroup/odd rw - tmpfs tmpfs rw\n")
    assert cgroup2_mount_of("/sys/fs/cgroup/pe-missions", info)
    assert not cgroup2_mount_of("/sys/fs/cgroup/odd/x", info)
    assert not cgroup2_mount_of("/var/lib/pe", info)
    assert not cgroup2_mount_of("/sys/fs/cgroupx/y", info)
    mounts = tmp_path / "mountinfo"
    mounts.write_text("30 1 8:1 / / rw - ext4 /dev/sda1 rw\n")
    with pytest.raises(ContainmentError) as exc:
        CgroupContainment(root, worker_uid=4242, worker_gid=4242, mountinfo_path=str(mounts))
    assert "cgroup v2" in str(exc.value)
    mounts.write_text(f"30 1 0:22 / {root.parent} rw - cgroup2 cgroup2 rw\n")
    CgroupContainment(root, worker_uid=4242, worker_gid=4242, mountinfo_path=str(mounts))


def test_freeze_waits_for_frozen_1_and_thaw_for_frozen_0(root):
    now = [0.0]
    c = make(root, Recording, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    cs = c.create("s1")
    seed_cgroup(cs)
    events = Path(cs.cgroup_path) / "cgroup.events"
    reads = []
    orig = c._read

    def slow(path):
        text = orig(path)
        if Path(path).name == "cgroup.events":
            reads.append(1)
            if len(reads) < 3:                      # the kernel has not finished freezing yet
                return "populated 1\nfrozen 0\n"
        return text
    c._read = slow
    c.freeze(cs)
    assert len(reads) >= 3 and "frozen 1" in events.read_text()
    c._read = orig
    c.thaw(cs)
    assert "frozen 0" in events.read_text()


def test_freeze_times_out_loudly_and_kill_still_proceeds(root):
    now = [0.0]

    class Stuck(Recording):
        def _write(self, path, value):
            self.writes.append((Path(path).name, value))
            CgroupContainment._write(self, path, value)     # never reflected in cgroup.events

    def sleep(s):
        now[0] += s

    c = make(root, Stuck, sleep=sleep, clock=lambda: now[0], freeze_timeout_s=1.0)
    cs = c.create("s1")
    seed_cgroup(cs)
    with pytest.raises(ContainmentError):
        c.freeze(cs)
    c.writes.clear()
    c.empty_after_polls = 0
    c.kill(cs)
    assert ("cgroup.kill", "1") in c.writes


# ── round 2: missing cgroup, adopted zombies ────────────────────────────────

def test_kill_of_a_cgroup_that_no_longer_exists_is_already_dead(root):
    import shutil
    c = make(root)
    cs = c.create("s1")
    shutil.rmtree(cs.cgroup_path)               # e.g. after a reboot
    assert c.is_empty(cs)
    c.kill(cs)                                  # must not raise
    c.cleanup(cs)


def test_kill_when_cgroup_kill_file_vanishes_mid_way_is_dead_if_empty(root):
    import shutil

    class Vanish(Recording):
        def _write(self, path, value):
            if Path(path).name == "cgroup.kill":
                shutil.rmtree(Path(path).parent)
                raise FileNotFoundError(path)
            super()._write(path, value)

    c = make(root, Vanish)
    cs = c.create("s1")
    seed_cgroup(cs)
    c.kill(cs)


def test_is_empty_reaps_a_dead_child_this_instance_did_not_launch(root):
    c = make(root)
    cs = c.create("s1")
    seed_cgroup(cs)
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    deadline = time.time() + 5
    while time.time() < deadline:                 # wait until it is a zombie
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            break
        if state == "Z":
            break
        time.sleep(0.01)
    (Path(cs.cgroup_path) / "cgroup.procs").write_text(f"{pid}\n")
    assert c._leaders == {}                       # a restarted Core has no Popen for it
    c.is_empty(cs)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)               # already reaped by is_empty


def test_reaping_tolerates_pids_that_are_not_our_children(root):
    c = make(root)
    cs = c.create("s1")
    seed_cgroup(cs)
    (Path(cs.cgroup_path) / "cgroup.procs").write_text("1\n999999\n")
    assert c.is_empty(cs) is False
