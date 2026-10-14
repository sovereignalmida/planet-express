"""CgroupContainment against a fake cgroup tree on tmpfs (plain files) and a stand-in launcher.

The real helper, real cgroups and real uids are exercised by tests/test_launcher_privileged.py (root only)."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from planet_express.missions.containment import ContainedSession
from planet_express.missions.containment_cgroup import (
    CgroupContainment,
    ContainmentError,
    uid_has_processes,
)
from planet_express.missions.launcher.spec import LaunchSpec
from planet_express.missions.launcher.strategies import LaunchError, Launched

SPEC = LaunchSpec(uid=61005, cwd="/w/s1", argv=("/bin/agent", "--x"), env={"A": "b"}, token="T" * 40)


class StubLauncher:
    """What the helper does to the cgroup tree (make the directory and its control files) without privilege."""

    def __init__(self, root, command=("print('hi')",), cgroup=None, error=None, populated=1):
        self.root, self.command, self.cgroup, self.error, self.populated = root, command, cgroup, error, populated
        self.seen = []

    def start(self, session_id, spec):
        self.seen.append((session_id, spec))
        if self.error:
            raise self.error
        path = Path(self.root) / session_id
        try:
            path.mkdir()
        except FileExistsError:
            raise LaunchError("the launcher refused: cgroup create") from None
        (path / "cgroup.events").write_text(f"populated {self.populated}\nfrozen 0\n")
        (path / "cgroup.procs").write_text("")
        (path / "cgroup.freeze").write_text("0")
        (path / "cgroup.kill").write_text("")
        proc = subprocess.Popen([sys.executable, "-c", *self.command], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0)
        os.set_blocking(proc.stdout.fileno(), False)
        return Launched(proc, {"ok": True, "session_id": session_id, "uid": spec.uid, "gid": spec.uid,
                               "cgroup": self.cgroup or str(path)})


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


def make(root, cls=CgroupContainment, launcher=None, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("require_cgroup2", False)
    kw.setdefault("uid_range", (61000, 61999))
    return cls(root, launcher=launcher or StubLauncher(root), **kw)


def launched(c, sid="s1", spec=SPEC):
    cs = c.launch(sid, spec)
    c.release_output(cs)
    return cs


def test_launch_returns_the_identity_the_launcher_made(root):
    c = make(root)
    cs = c.launch("s1", SPEC)
    assert cs == ContainedSession("s1", "local_cgroup", str(root / "s1"), 61005)
    assert Path(cs.cgroup_path).is_dir() and c.cgroup_path("s1") == cs.cgroup_path
    c.release_output(cs)


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "x" * 65, "a b", "a\0b"])
def test_unsafe_session_ids_are_refused(root, bad):
    with pytest.raises(ValueError):
        make(root).cgroup_path(bad)
    with pytest.raises(ValueError):
        make(root).launch(bad, SPEC)


def test_launching_the_same_session_twice_is_refused(root):
    c = make(root)
    c.launch("s1", SPEC)
    with pytest.raises(ContainmentError):
        c.launch("s1", SPEC)
    c.release_output(ContainedSession("s1", "local_cgroup", str(root / "s1"), 61005))


def test_the_spec_reaches_the_launcher_unchanged(root):
    stub = StubLauncher(root)
    c = make(root, launcher=stub)
    c.release_output(c.launch("s1", SPEC))
    assert stub.seen == [("s1", SPEC)]


@pytest.mark.parametrize("uid", [0, os.geteuid(), 999, 62000])
def test_a_uid_that_is_root_core_or_outside_the_range_is_refused_before_the_launcher_runs(root, uid):
    stub = StubLauncher(root)
    spec = LaunchSpec(uid=uid, cwd="/w/x", argv=("/bin/x",), token="T" * 40)
    with pytest.raises(ContainmentError):
        make(root, launcher=stub).launch("s1", spec)
    assert stub.seen == []


def test_the_probe_uid_at_the_top_of_the_range_is_accepted(root):
    c = make(root)                       # the allocatable range stops one short; the probe uid is the top one
    assert c.uid_range == (61000, 61998)
    c.release_output(c.launch("s1", LaunchSpec(uid=61999, cwd="/w/x", argv=("/bin/x",), token="T" * 40)))


def test_a_launcher_that_refuses_leaves_nothing_tracked(root):
    c = make(root, launcher=StubLauncher(root, error=LaunchError("the launcher refused: uid")))
    with pytest.raises(LaunchError):
        c.launch("s1", SPEC)
    assert c._leaders == {} and c._out == {}


def test_an_unexpected_launcher_exception_becomes_a_containment_error(root):
    c = make(root, launcher=StubLauncher(root, error=RuntimeError("secret detail")))
    with pytest.raises(ContainmentError) as exc:
        c.launch("s1", SPEC)
    assert "secret detail" not in str(exc.value) and c._leaders == {}


def test_a_launcher_that_used_another_cgroup_is_killed_there_removed_and_leaves_nothing_tracked(root, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "cgroup.kill").write_text("")
    removed = []

    class Cleaning(FakeFsContainment):
        def _write(self, path, value):
            super()._write(path, value)
            if Path(path).name == "cgroup.kill" and Path(path).parent == other:
                (other / "cgroup.kill").unlink()                  # the kernel empties it and drops its control files

        def _rmdir(self, path):
            removed.append(path)
            super()._rmdir(path)

    c = make(root, Cleaning, launcher=StubLauncher(root, cgroup=str(other), command=("pass",)))
    with pytest.raises(ContainmentError, match="different cgroup"):
        c.launch("s1", SPEC)
    assert set(removed) == {str(other)} and not other.exists()      # the cgroup the helper really used is gone
    assert c._leaders == {} and c._out == {}                       # and no pipe or leader is left tracked


def test_a_misplaced_cgroup_that_cannot_be_removed_is_still_not_left_tracked(root, tmp_path):
    c = make(root, launcher=StubLauncher(root, cgroup="/nonexistent/elsewhere", command=("pass",)))
    with pytest.raises(ContainmentError, match="different cgroup"):
        c.launch("s1", SPEC)
    assert c._leaders == {} and c._out == {}


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
    cs = launched(c)
    c.freeze(cs)
    c.thaw(cs)
    assert c.writes == [("cgroup.freeze", "1"), ("cgroup.freeze", "0")]


def test_kill_is_freeze_then_kill_then_poll_until_empty(root):
    c = make(root, Recording, empty_after_polls=2)
    cs = launched(c)
    c.kill(cs)
    assert c.writes == [("cgroup.freeze", "1"), ("cgroup.kill", "1")]
    assert c.polls >= 3  # polled at least until populated 0 was seen
    assert c.is_empty(cs)


def test_kill_does_not_return_while_populated_and_times_out_loudly(root):
    now = [0.0]

    def sleep(s):
        now[0] += s

    c = make(root, Recording, sleep=sleep, clock=lambda: now[0], kill_timeout_s=5.0)
    cs = launched(c)
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
    cs = launched(c)
    (Path(cs.cgroup_path) / "cgroup.events").write_text(events)
    assert c.is_empty(cs) is expected


def test_is_empty_missing_cgroup_inside_root_means_removed(root):
    c = make(root)
    cs = ContainedSession("gone", "local_cgroup", str(root / "gone"), None)
    assert c.is_empty(cs) is True  # the kernel only removes an empty cgroup


def test_pids_lists_cgroup_procs(root):
    c = make(root)
    cs = launched(c)
    (Path(cs.cgroup_path) / "cgroup.procs").write_text("12\n40\n")
    assert c.pids(cs) == [12, 40]


def test_cleanup_refuses_until_empty_then_removes(root):
    c = make(root, FakeFsContainment)
    cs = launched(c)
    d = Path(cs.cgroup_path)
    with pytest.raises(ContainmentError):
        c.cleanup(cs)
    assert d.exists()
    (d / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    c.cleanup(cs)
    assert not d.exists()


def test_cleanup_removes_the_per_session_socket_directory(root, tmp_path):
    socks = tmp_path / "socks"
    (socks / "s1").mkdir(parents=True)
    (socks / "s1" / "worker.sock").write_text("")
    c = make(root, FakeFsContainment, sock_root=str(socks))
    cs = launched(c)
    (Path(cs.cgroup_path) / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    c.cleanup(cs)
    assert not (socks / "s1").exists() and socks.exists()


def test_cleanup_never_removes_a_socket_dir_while_the_session_is_populated(root, tmp_path):
    socks = tmp_path / "socks"
    (socks / "s1").mkdir(parents=True)
    c = make(root, FakeFsContainment, sock_root=str(socks))
    cs = launched(c)
    with pytest.raises(ContainmentError):
        c.cleanup(cs)
    assert (socks / "s1").exists()


def test_output_fd_is_a_nonblocking_pipe_of_the_helpers_stdout_and_stderr(root):
    c = make(root, launcher=StubLauncher(root, command=("import sys; print('out'); print('err', file=sys.stderr)",)))
    cs = c.launch("s1", SPEC)
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
    with pytest.raises(ContainmentError):
        c.output_fd(cs)


def test_a_session_with_no_output_pipe_says_so(root):
    with pytest.raises(ContainmentError):
        make(root).output_fd(ContainedSession("s1", "local_cgroup", str(root / "s1"), 1))


# ── configuration of the containment itself ─────────────────────────────────

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
        CgroupContainment(root, launcher=StubLauncher(root), mountinfo_path=str(mounts))
    assert "cgroup v2" in str(exc.value)
    mounts.write_text(f"30 1 0:22 / {root.parent} rw - cgroup2 cgroup2 rw\n")
    CgroupContainment(root, launcher=StubLauncher(root), mountinfo_path=str(mounts))


@pytest.mark.parametrize("bad", [(0, 10), (-5, 10), (100, 100), (200, 100)])
def test_the_uid_range_needs_room_for_a_probe_uid(root, bad):
    with pytest.raises(ValueError):
        make(root, uid_range=bad)


def test_a_relative_or_unnormalised_root_is_refused(tmp_path):
    for bad in ("relative", str(tmp_path) + "/a/../b"):
        with pytest.raises(ValueError):
            CgroupContainment(bad, launcher=None, require_cgroup2=False)


def test_the_containment_has_no_python_fork_path_of_its_own():
    text = (Path(__file__).resolve().parents[1] / "planet_express/missions/containment_cgroup.py").read_text()
    for forbidden in ("preexec_fn", "subprocess", "os.fork", "setresuid", "setuid", "shell=True"):
        assert forbidden not in text, forbidden


# ── freezing ────────────────────────────────────────────────────────────────

def test_freeze_waits_for_frozen_1_and_thaw_for_frozen_0(root):
    now = [0.0]
    c = make(root, Recording, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    cs = launched(c)
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

    c = make(root, Stuck, sleep=sleep, clock=lambda: now[0])
    c.freeze_timeout_s = 1.0
    cs = launched(c)
    with pytest.raises(ContainmentError):
        c.freeze(cs)
    c.writes.clear()
    c.empty_after_polls = 0
    c.kill(cs)
    assert ("cgroup.kill", "1") in c.writes


# ── missing cgroup, adopted zombies ─────────────────────────────────────────

def test_kill_of_a_cgroup_that_no_longer_exists_is_already_dead(root):
    c = make(root)
    cs = launched(c)
    shutil.rmtree(cs.cgroup_path)               # e.g. after a reboot
    assert c.is_empty(cs)
    c.kill(cs)                                  # must not raise
    c.cleanup(cs)


def test_kill_when_cgroup_kill_file_vanishes_mid_way_is_dead_if_empty(root):
    class Vanish(Recording):
        def _write(self, path, value):
            if Path(path).name == "cgroup.kill":
                shutil.rmtree(Path(path).parent)
                raise FileNotFoundError(path)
            super()._write(path, value)

    c = make(root, Vanish)
    cs = launched(c)
    c.kill(cs)


def test_is_empty_reaps_a_dead_child_this_instance_did_not_launch(root):
    c = make(root)
    cs = ContainedSession("s9", "local_cgroup", str(root / "s9"), 61005)
    (root / "s9").mkdir()
    (root / "s9" / "cgroup.events").write_text("populated 1\nfrozen 0\n")
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
    (root / "s9" / "cgroup.procs").write_text(f"{pid}\n")
    assert c._leaders == {}                       # a restarted Core has no Popen for it
    c.is_empty(cs)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)               # already reaped by is_empty


def test_reaping_tolerates_pids_that_are_not_our_children(root):
    c = make(root)
    cs = launched(c)
    (Path(cs.cgroup_path) / "cgroup.procs").write_text("1\n999999\n")
    assert c.is_empty(cs) is False


# ── is any process still running as a uid ───────────────────────────────────

def test_uid_has_processes_reads_proc_and_fails_closed(tmp_path):
    proc = tmp_path / "proc"
    for pid, uids in (("10", "0\t0\t0\t0"), ("11", "61005\t61005\t61005\t61005"), ("12", "5\t61007\t5\t5")):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "status").write_text(f"Name:\tx\nUid:\t{uids}\nGid:\t0 0 0 0\n")
    (proc / "self").mkdir()
    (proc / "11x").mkdir()
    assert uid_has_processes(61005, str(proc))
    assert uid_has_processes(61007, str(proc))              # an effective uid counts too
    assert not uid_has_processes(61006, str(proc))
    assert uid_has_processes(61005, str(tmp_path / "missing"))   # unreadable /proc: assume the worst


def test_a_process_whose_status_cannot_be_read_counts_as_running_hidepid_and_eacces(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    proc = tmp_path / "proc"
    (proc / "10").mkdir(parents=True)
    (proc / "10" / "status").write_text("Uid:\t5\t5\t5\t5\n")
    (proc / "10" / "status").chmod(0)
    assert uid_has_processes(61005, str(proc)) is True       # unreadable is not the same as gone
    (proc / "10" / "status").chmod(0o644)
    assert uid_has_processes(61005, str(proc)) is False


def test_a_process_that_vanishes_between_listing_and_reading_is_not_running(tmp_path):
    proc = tmp_path / "proc"
    (proc / "99").mkdir(parents=True)               # a directory with no status file
    assert not uid_has_processes(61005, str(proc))


def test_uid_in_use_sees_this_process_as_its_own_uid(root):
    c = make(root)
    assert c.uid_in_use(os.getuid())
    assert not c.uid_in_use(61999)
