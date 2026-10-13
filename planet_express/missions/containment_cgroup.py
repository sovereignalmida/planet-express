"""cgroup v2 containment for mission sessions (v4 slice 3b).

One session is one directory under a delegated cgroup root. The leader joins that cgroup BEFORE it
executes anything, so every descendant (including a double-fork/setsid escaper) is a member and
`cgroup.kill` reaches it. `is_empty()` reads `cgroup.events` and is the only "provably dead"
predicate: it fails closed (anything it cannot parse is "not empty").

Spawning drops to the worker uid/gid, so the Core process must be able to do that (root, or
CAP_SETUID/CAP_SETGID plus the ability to enter the worker-owned workspace). Cgroup writes happen
in the child *before* the drop, so the worker never needs write access to any cgroup file.
"""

import ctypes
import os
import re
import subprocess
import time

from planet_express.missions.containment import ContainedSession

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_PR_SET_NO_NEW_PRIVS = 38


class ContainmentError(RuntimeError):
    """A containment operation could not be completed; the session is NOT known to be dead."""


class CgroupContainment:
    """`SessionContainment` on cgroup v2. `root` is a delegated subtree (e.g. under /sys/fs/cgroup)."""

    def __init__(self, root, *, worker_uid, worker_gid, kill_timeout_s=10.0,
                 sleep=time.sleep, clock=time.monotonic, allow_unprivileged_for_tests=False):
        root = os.fspath(root)
        if not os.path.isabs(root) or os.path.normpath(root) != root.rstrip("/"):
            raise ValueError("cgroup root must be a normalised absolute path")
        if not allow_unprivileged_for_tests:
            if not isinstance(worker_uid, int) or not isinstance(worker_gid, int):
                raise ValueError("worker uid and gid are required: sessions never run as the Core user")
            if worker_uid == 0 or worker_gid == 0:
                raise ValueError("the worker must not be root")
        self.root = root.rstrip("/") or "/"
        self.worker_uid = worker_uid
        self.worker_gid = worker_gid
        self.kill_timeout_s = kill_timeout_s
        self._sleep = sleep
        self._clock = clock
        self._drop = worker_uid is not None and worker_gid is not None
        self._libc = ctypes.CDLL(None, use_errno=True)
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

    def _reap(self, session_id):
        leader = self._leaders.get(session_id)
        if leader is not None and leader.poll() is not None:
            del self._leaders[session_id]

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
        if cs.session_id in self._out:
            raise ContainmentError("session already has a leader")
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        procs_fd = os.open(os.path.join(path, "cgroup.procs"), os.O_WRONLY | os.O_CLOEXEC)
        read_end, write_end = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(read_end, False)
        drop, uid, gid, libc = self._drop, self.worker_uid, self.worker_gid, self._libc

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
            leader = subprocess.Popen(
                list(argv), env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=write_end, stderr=write_end, close_fds=True, pass_fds=tuple(pass_fds),
                start_new_session=True, preexec_fn=pre_exec,  # noqa: PLW1509 -- cgroup join must precede exec
            )
        except Exception as exc:
            os.close(read_end)
            raise ContainmentError(f"spawn failed: {type(exc).__name__}") from exc
        finally:
            os.close(write_end)
            os.close(procs_fd)
        self._leaders[cs.session_id] = leader
        self._out[cs.session_id] = read_end
        return leader.pid

    def pids(self, cs) -> list[int]:
        text = self._read(os.path.join(self._dir(cs), "cgroup.procs"))
        return [int(line) for line in text.split() if line.isdigit()]

    def freeze(self, cs) -> None:
        self._write(os.path.join(self._dir(cs), "cgroup.freeze"), "1")

    def thaw(self, cs) -> None:
        self._write(os.path.join(self._dir(cs), "cgroup.freeze"), "0")

    def kill(self, cs) -> None:
        path = self._dir(cs)
        try:
            self.freeze(cs)  # stop members forking while the kill is delivered
        except OSError:
            pass  # no freezer: cgroup.kill alone still reaches every member
        try:
            self._write(os.path.join(path, "cgroup.kill"), "1")
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
        self._reap(cs.session_id)  # a zombie leader still counts as a member until reaped
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


__all__ = ["CgroupContainment", "ContainmentError"]
