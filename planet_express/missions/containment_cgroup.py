"""cgroup v2 containment for mission sessions (v4 slice 3b, reworked in slice 3d).

One session is one directory under a delegated cgroup root, created BY THE LAUNCHER (contract 22.3): the root
helper makes `<root>/<session_id>`, hands Core the control files, joins the cgroup BEFORE it drops privilege and
executes anything, so every descendant (including a double-fork/setsid escaper) is a member and `cgroup.kill`
reaches it. `launch()` replaces the old create()+spawn(): it returns only after the helper reported success.
`is_empty()` reads `cgroup.events` and is the only "provably dead" predicate: it fails closed.

Which helper strategy starts the leader (`SudoLauncher` or `DirectLauncher`, both the same `pe-launch`) is passed
in; this class never forks Python itself. Core owns freeze/kill/rmdir of the cgroup it was handed, and a session
uid has no write access to any cgroup file.
"""

import os
import time

from planet_express.missions.containment import ContainedSession, ContainmentError
from planet_express.missions.launcher.spec import SESSION_ID, LaunchSpec
from planet_express.missions.launcher.strategies import LaunchError

_ID = SESSION_ID


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


def uid_has_processes(uid: int, proc: str = "/proc") -> bool:
    """True if any process has `uid` as its real, effective, saved or filesystem uid. Fails closed: an unreadable
    /proc, or ANY process whose status cannot be read for a reason other than having exited (hidepid, EACCES), counts
    as 'yes'. Only a process that vanished between the listing and the read is known not to be running."""
    try:
        entries = os.listdir(proc)
    except OSError:
        return True
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/status", encoding="ascii", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Uid:"):
                        if str(uid) in line.split()[1:5]:
                            return True
                        break
        except (FileNotFoundError, ProcessLookupError):
            continue                  # gone between listdir and open: it cannot be running
        except OSError:
            return True               # present but unreadable: it may well be running as this uid
    return False


class CgroupContainment:
    """`SessionContainment` on cgroup v2. `root` is the delegated sessions parent (e.g. under /sys/fs/cgroup)."""

    def __init__(self, root, *, launcher, uid_range: tuple[int, int] | None = None, sock_root: str | None = None,
                 kill_timeout_s=10.0, sleep=time.sleep, clock=time.monotonic, require_cgroup2=True,
                 mountinfo_path="/proc/self/mountinfo", proc="/proc"):
        root = os.fspath(root)
        if not os.path.isabs(root) or os.path.normpath(root) != root.rstrip("/"):
            raise ValueError("cgroup root must be a normalised absolute path")
        if uid_range is not None:
            low, high = uid_range
            if not (isinstance(low, int) and isinstance(high, int) and 0 < low < high):
                raise ValueError("the session uid range must be positive and have room for a probe uid")
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
        # `start_session` allocates from [low, high - 1]: the top uid is reserved for preflight's probe session.
        self.uid_range = None if uid_range is None else (uid_range[0], uid_range[1] - 1)
        self.sock_root = sock_root
        self.kill_timeout_s = kill_timeout_s
        self.freeze_timeout_s = 5.0
        self._sleep = sleep
        self._clock = clock
        self._proc = proc
        self.launcher = launcher
        self._leaders: dict[str, object] = {}
        self._out: dict[str, object] = {}

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

    def cgroup_path(self, session_id: str) -> str:
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise ValueError("unsafe session id")
        return os.path.join(self.root, session_id)

    def launch(self, session_id: str, spec: LaunchSpec) -> ContainedSession:
        """Create the cgroup, drop to the session uid and exec, all inside the helper. Returns only after the helper
        reported success; on failure the helper has removed what it made (or Core's normal kill/cleanup will)."""
        path = self.cgroup_path(session_id)
        if session_id in self._leaders:
            raise ContainmentError("session already has a leader")
        if not isinstance(spec, LaunchSpec):
            raise TypeError("a LaunchSpec is required")
        # Sessions never run as root or as Core, and only from the reserved range (the probe uid is its top).
        if spec.uid in (0, os.geteuid()):
            raise ContainmentError("a session must not run as root or as the Core user")
        if self.uid_range is not None and not self.uid_range[0] <= spec.uid <= self.uid_range[1] + 1:
            raise ContainmentError("the session uid is outside the reserved range")
        try:
            launched = self.launcher.start(session_id, spec)
        except ContainmentError:
            raise
        except Exception as exc:
            raise ContainmentError(f"launch failed: {type(exc).__name__}") from exc
        reported = launched.status.get("cgroup")
        if reported != path:
            # The helper's config and Core's disagree about where sessions live: the process runs somewhere Core does
            # not look. Kill it where the helper says it is, reap it, remove that cgroup and its output pipe, and
            # refuse the session, so nothing is left tracked under a name nobody will clean up.
            self._discard_misplaced(launched, str(reported))
            raise ContainmentError("the launcher put the session in a different cgroup than Core expects")
        self._leaders[session_id] = launched.proc
        self._out[session_id] = launched.proc.stdout
        return ContainedSession(session_id, "local_cgroup", path, spec.uid)

    def _discard_misplaced(self, launched, reported: str) -> None:
        """Best effort, bounded: kill the cgroup the helper named, reap the leader, remove the (then empty) cgroup."""
        usable = os.path.isabs(reported) and os.path.normpath(reported) == reported
        if usable:
            try:
                self._write(os.path.join(reported, "cgroup.kill"), "1")
            except OSError:
                pass
        deadline = self._clock() + 2.0
        while True:
            reaped = launched.proc.poll() is not None
            removed = False
            if usable:
                try:
                    self._rmdir(reported)
                    removed = True
                except FileNotFoundError:
                    removed = True
                except OSError:
                    pass
            if (reaped and removed) or self._clock() >= deadline:
                break
            self._sleep(0.05)
        try:
            launched.proc.stdout.close()
        except OSError:
            pass

    def uid_in_use(self, uid: int) -> bool:
        return uid_has_processes(uid, self._proc)

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
        self._remove_sock_dir(cs.session_id)
        self._leaders.pop(cs.session_id, None)

    def _remove_sock_dir(self, session_id: str) -> None:
        """The per-session socket directory (made by Core before the launch, handed to the session's group by the helper): unlink what Core bound in it and
        remove it. A leftover is harmless (nothing listens) and is retried on the next cleanup."""
        if not self.sock_root:
            return
        path = os.path.join(self.sock_root, session_id)
        try:
            for name in os.listdir(path):
                os.unlink(os.path.join(path, name))
            os.rmdir(path)
        except OSError:
            pass

    # --- output capture (extension used by the command adapter) -----------------------------

    def output_fd(self, cs) -> int:
        """Non-blocking read end of the session's merged stdout/stderr pipe (the helper's own stdout)."""
        try:
            return self._out[cs.session_id].fileno()
        except (KeyError, ValueError):
            raise ContainmentError("no output pipe for this session") from None

    def release_output(self, cs) -> None:
        handle = self._out.pop(cs.session_id, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


__all__ = ["CgroupContainment", "ContainmentError", "LaunchError", "cgroup2_mountpoint", "uid_has_processes"]
