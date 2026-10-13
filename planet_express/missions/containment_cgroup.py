"""cgroup v2 containment for mission sessions (v4 slice 3b).

One session is one directory under a delegated cgroup root. The leader joins that cgroup BEFORE it
executes anything, so every descendant (including a double-fork/setsid escaper) is a member and
`cgroup.kill` reaches it. `is_empty()` reads `cgroup.events` and is the only "provably dead"
predicate: it fails closed (anything it cannot parse is "not empty").

How the leader is actually started is a `Launcher` strategy. The default, `DirectLauncher`, forks from
the Core process: joining a cgroup and dropping to the worker uid/gid need privilege (root, or
CAP_SETUID/CAP_SETGID plus write access to the cgroup), which Core does not normally have, so it fails
with a clear message instead of half-working. The real answer is a root-owned launcher invoked through
an exact sudoers rule (slice 3d), which plugs in here as another `Launcher`. Cgroup writes happen in the
child *before* the drop, so the worker never needs write access to any cgroup file.
"""

import ctypes
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from planet_express.missions.containment import ContainedSession, ContainmentError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_PR_SET_NO_NEW_PRIVS = 38
_CAP_CHOWN, _CAP_SETGID, _CAP_SETUID = 0, 6, 7


@dataclass(frozen=True)
class LaunchRequest:
    """Everything a launcher needs to start a session's leader inside its cgroup, as the worker."""
    argv: list[str]
    env: dict[str, str]
    cwd: str
    pass_fds: tuple[int, ...]
    procs_path: str           # the cgroup's cgroup.procs: the leader must be written into it before exec
    output_fd: int            # the leader's stdout and stderr (a pipe's write end); the caller closes its copy
    uid: int | None           # drop to this identity before exec; None only in unprivileged tests
    gid: int | None


class Launcher(Protocol):
    def launch(self, request: LaunchRequest) -> subprocess.Popen: ...


def cgroup2_mountpoint(path: str, mountinfo: str) -> str | None:
    """The cgroup2 mount point containing `path` (longest match in /proc/self/mountinfo text), else None."""
    best, best_type = "", None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        point = fields[4].replace("\\040", " ")
        if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, best_type = point, right.split()[0]
    return best if best_type == "cgroup2" else None


def cgroup2_mount_of(path: str, mountinfo: str) -> bool:
    return cgroup2_mountpoint(path, mountinfo) is not None


def _have_caps(*caps: int) -> bool:
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("CapEff:"):
                    mask = int(line.split()[1], 16)
                    return all(mask >> cap & 1 for cap in caps)
    except (OSError, ValueError, IndexError):
        pass
    return False


class DirectLauncher:
    """Fork from this process, join the cgroup, drop to the worker identity, exec. Needs privilege."""

    def __init__(self):
        self._libc = ctypes.CDLL(None, use_errno=True)

    def launch(self, request: LaunchRequest) -> subprocess.Popen:
        drop = request.uid is not None and request.gid is not None
        if drop and os.geteuid() != 0 and not _have_caps(_CAP_SETUID, _CAP_SETGID, _CAP_CHOWN):
            raise ContainmentError(
                "cannot start a mission session as the worker: the Core process lacks root or "
                "CAP_SETUID/CAP_SETGID/CAP_CHOWN (the worker must own its workspace). "
                "Sessions need the root-owned launcher (slice 3d); "
                "pass it as CgroupContainment(launcher=...)")
        try:
            procs_fd = os.open(request.procs_path, os.O_WRONLY | os.O_CLOEXEC)
        except OSError as exc:
            raise ContainmentError(
                f"cannot join the session cgroup ({exc.strerror}); the cgroup subtree must be writable by "
                "the launcher") from exc
        libc, uid, gid = self._libc, request.uid, request.gid

        def pre_exec():
            # Order matters: join the cgroup while still privileged, then shed identity, then
            # forbid regaining it. Any failure aborts the exec (fail closed).
            os.write(procs_fd, str(os.getpid()).encode())
            if drop:
                os.setgroups([])
                os.setresgid(gid, gid, gid)
                os.setresuid(uid, uid, uid)
            if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")

        try:
            return subprocess.Popen(
                list(request.argv), env=dict(request.env), cwd=request.cwd, stdin=subprocess.DEVNULL,
                stdout=request.output_fd, stderr=request.output_fd, close_fds=True,
                pass_fds=tuple(request.pass_fds), start_new_session=True,
                preexec_fn=pre_exec,
            )
        finally:
            os.close(procs_fd)


def allow_check(containment) -> bool:
    """Spawn-time re-check of the worker identity (the constructor check can be bypassed only in tests)."""
    if containment.unprivileged_for_tests:
        return True
    return containment.worker_uid not in (0, os.geteuid()) and containment.worker_gid != 0


class CgroupContainment:
    """`SessionContainment` on cgroup v2. `root` is a delegated subtree (e.g. under /sys/fs/cgroup)."""

    def __init__(self, root, *, worker_uid, worker_gid, kill_timeout_s=10.0,
                 sleep=time.sleep, clock=time.monotonic, allow_unprivileged_for_tests=False,
                 launcher: Launcher | None = None, freeze_timeout_s=5.0, require_cgroup2=True,
                 mountinfo_path="/proc/self/mountinfo"):
        root = os.fspath(root)
        if not os.path.isabs(root) or os.path.normpath(root) != root.rstrip("/"):
            raise ValueError("cgroup root must be a normalised absolute path")
        if not allow_unprivileged_for_tests:
            if not isinstance(worker_uid, int) or not isinstance(worker_gid, int):
                raise ValueError("worker uid and gid are required: sessions never run as the Core user")
            if worker_uid == 0 or worker_gid == 0:
                raise ValueError("the worker must not be root")
            if worker_uid == os.geteuid():
                raise ValueError("the worker must not be the Core user")
            if require_cgroup2:
                try:
                    with open(mountinfo_path, encoding="utf-8", errors="replace") as handle:
                        info = handle.read()
                except OSError as exc:
                    raise ContainmentError(f"cannot read the mount table: {exc.strerror}") from exc
                if not cgroup2_mount_of(os.path.normpath(root), info):
                    raise ContainmentError(
                        f"missions.cgroup_root {root} is not on a cgroup v2 mount; mission sessions need "
                        "a delegated cgroup2 subtree")
        self.root = root.rstrip("/") or "/"
        self.worker_uid = worker_uid
        self.worker_gid = worker_gid
        self.kill_timeout_s = kill_timeout_s
        self.freeze_timeout_s = freeze_timeout_s
        self._sleep = sleep
        self._clock = clock
        self.launcher = launcher or DirectLauncher()
        self.unprivileged_for_tests = bool(allow_unprivileged_for_tests)
        self._leaders: dict[str, subprocess.Popen] = {}
        self._out: dict[str, int] = {}

    # --- low-level file access (overridable seam for tests) ---------------------------------

    def _write(self, path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
        try:
            os.write(fd, value.encode())
        finally:
            os.close(fd)

    def _rmdir(self, path):
        os.rmdir(path)

    def _read(self, path):
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read()

    # --- helpers ---------------------------------------------------------------------------

    def _dir(self, cs: ContainedSession) -> str:
        path = cs.cgroup_path
        if cs.kind != "local_cgroup" or not isinstance(path, str):
            raise ValueError("not a cgroup session")
        if os.path.normpath(path) != path or os.path.dirname(path) != self.root:
            raise ValueError("cgroup path is outside the delegated root")
        if not _ID.fullmatch(os.path.basename(path)):
            raise ValueError("unsafe cgroup name")
        return path

    def _reap(self, session_id, path):
        leader = self._leaders.get(session_id)
        if leader is not None:
            if leader.poll() is not None:
                del self._leaders[session_id]
            return
        # A session this instance did not launch (Core restarted or re-exec'd; the children stayed): its dead
        # leader is our zombie child and keeps the cgroup populated until somebody waits for it.
        try:
            members = [int(x) for x in self._read(os.path.join(path, "cgroup.procs")).split() if x.isdigit()]
        except OSError:
            return
        for pid in members:
            try:
                os.waitpid(pid, os.WNOHANG)       # reaps only a child of ours that has already exited
            except (ChildProcessError, OSError):
                pass

    # --- SessionContainment ----------------------------------------------------------------

    def create(self, session_id: str) -> ContainedSession:
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise ValueError("unsafe session id")
        path = os.path.join(self.root, session_id)
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            raise ContainmentError("cgroup already exists for this session") from None
        except OSError as exc:
            raise ContainmentError(f"cannot create cgroup: {exc.strerror}") from exc
        return ContainedSession(session_id, "local_cgroup", path, self.worker_uid)

    def spawn(self, cs, argv, env, cwd, pass_fds=()):
        path = self._dir(cs)
        if not allow_check(self):
            raise ContainmentError("the worker must not be the Core user or root")
        if cs.session_id in self._out:
            raise ContainmentError("session already has a leader")
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        read_end, write_end = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(read_end, False)
        request = LaunchRequest(
            argv=list(argv), env=dict(env), cwd=cwd, pass_fds=tuple(pass_fds),
            procs_path=os.path.join(path, "cgroup.procs"), output_fd=write_end,
            uid=self.worker_uid, gid=self.worker_gid)
        try:
            leader = self.launcher.launch(request)
        except ContainmentError:
            os.close(read_end)
            raise
        except Exception as exc:
            os.close(read_end)
            raise ContainmentError(f"spawn failed: {type(exc).__name__}") from exc
        finally:
            os.close(write_end)
        self._leaders[cs.session_id] = leader
        self._out[cs.session_id] = read_end
        return leader.pid

    def pids(self, cs) -> list[int]:
        text = self._read(os.path.join(self._dir(cs), "cgroup.procs"))
        return [int(line) for line in text.split() if line.isdigit()]

    def _set_frozen(self, cs, value: str) -> None:
        path = self._dir(cs)
        self._write(os.path.join(path, "cgroup.freeze"), value)
        deadline = self._clock() + self.freeze_timeout_s
        while True:
            try:
                events = self._read(os.path.join(path, "cgroup.events"))
            except FileNotFoundError:
                return              # the cgroup is gone, so there is nothing left to freeze
            for line in events.splitlines():
                key, _, state = line.partition(" ")
                if key == "frozen" and state.strip() == value:
                    return
            if self._clock() >= deadline:
                raise ContainmentError(f"cgroup did not reach frozen {value} in time")
            self._sleep(0.02)

    def freeze(self, cs) -> None:
        self._set_frozen(cs, "1")

    def thaw(self, cs) -> None:
        self._set_frozen(cs, "0")

    def kill(self, cs) -> None:
        path = self._dir(cs)
        if not os.path.isdir(path):
            return          # gone (reboot, or removed behind our back): the kernel only removes an empty cgroup
        try:
            self.freeze(cs)  # stop members forking while the kill is delivered
        except (OSError, ContainmentError):
            pass  # no freezer, or it is slow: cgroup.kill alone still reaches every member
        try:
            self._write(os.path.join(path, "cgroup.kill"), "1")
        except FileNotFoundError:
            if self.is_empty(cs):
                return
            raise ContainmentError("cgroup.kill failed: no such file") from None
        except OSError as exc:
            raise ContainmentError(f"cgroup.kill failed: {exc.strerror}") from exc
        deadline = self._clock() + self.kill_timeout_s
        while True:
            if self.is_empty(cs):
                return
            if self._clock() >= deadline:
                raise ContainmentError("cgroup still populated after kill")
            self._sleep(0.05)

    def is_empty(self, cs) -> bool:
        path = self._dir(cs)
        self._reap(cs.session_id, path)  # a zombie leader still counts as a member until reaped
        try:
            events = self._read(os.path.join(path, "cgroup.events"))
        except FileNotFoundError:
            return not os.path.isdir(path)  # the kernel only removes an empty cgroup
        except OSError:
            return False
        for line in events.splitlines():
            key, _, value = line.partition(" ")
            if key == "populated":
                return value.strip() == "0"
        return False

    def cleanup(self, cs) -> None:
        path = self._dir(cs)
        if not self.is_empty(cs):
            raise ContainmentError("refusing to remove a populated cgroup")
        self.release_output(cs)
        try:
            self._rmdir(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ContainmentError(f"cannot remove cgroup: {exc.strerror}") from exc

    # --- output capture (extension used by the command adapter) -----------------------------

    def output_fd(self, cs) -> int:
        """Non-blocking read end of the leader's merged stdout/stderr pipe."""
        try:
            return self._out[cs.session_id]
        except KeyError:
            raise ContainmentError("no output pipe for this session") from None

    def release_output(self, cs) -> None:
        fd = self._out.pop(cs.session_id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


__all__ = ["CgroupContainment", "cgroup2_mountpoint", "ContainmentError", "DirectLauncher", "LaunchRequest", "Launcher"]
