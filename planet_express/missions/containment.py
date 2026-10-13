"""Session containment: the types S3a and S3b share, plus an in-memory fake for tests.

`SessionContainment` is the ONLY authority on whether a session's processes are gone. `is_empty()` is the
single "provably dead" predicate (contract §2.1): a leased resource is never freed on any other signal.
The real implementations (cgroup v2, per-uid) are S3b's; this module only declares the interface and the
fake, so there is no OS access here.
"""

import os
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class ContainedSession:
    session_id: str
    kind: Literal["local_cgroup", "local_uid", "fake"]
    cgroup_path: str | None          # absolute path under the cgroup v2 mount
    session_uid: int | None          # the uid the process runs as (worker identity)


class SessionContainment(Protocol):
    def create(self, session_id: str) -> ContainedSession: ...
    def spawn(self, cs: ContainedSession, argv: list[str], env: dict[str, str], cwd: str,
              pass_fds: tuple[int, ...] = ()) -> int: ...   # returns the leader pid, already inside cs
    def pids(self, cs: ContainedSession) -> list[int]: ...
    def freeze(self, cs: ContainedSession) -> None: ...
    def thaw(self, cs: ContainedSession) -> None: ...
    def kill(self, cs: ContainedSession) -> None: ...        # freeze, kill every member, wait until empty
    def is_empty(self, cs: ContainedSession) -> bool: ...    # the ONLY "provably dead" predicate
    def cleanup(self, cs: ContainedSession) -> None: ...     # remove cgroup dir etc; only when empty


class ContainmentError(RuntimeError):
    pass


@dataclass
class _Group:
    cs: ContainedSession
    members: set[int] = field(default_factory=set)
    leaked: set[int] = field(default_factory=set)       # survive kill until reap_leaks()
    frozen: bool = False
    kill_calls: int = 0
    ignore_polls: int = 0                               # is_empty() polls the members survive after a kill
    kill_requested: bool = False
    spawns: list[dict] = field(default_factory=list)
    out_read: int | None = None                         # merged stdout/stderr, created on first write_output
    out_write: int | None = None


class FakeContainment:
    """Deterministic in-memory containment. Simulates a process that ignores kill for N `is_empty()` polls
    (`ignore_kill`), a crash of the whole session (`crash`) and a child that escapes the kill (`leak_child`).
    Like a real `kill()` that hit its timeout, `kill()` here may return while members are still alive."""

    def __init__(self) -> None:
        self._groups: dict[str, _Group] = {}
        self._next_pid = 1000
        self._kills: dict[str, int] = {}               # survives cleanup, so tests can see a kill happened

    # ── the protocol ────────────────────────────────────────────────────────
    def create(self, session_id: str) -> ContainedSession:
        if session_id in self._groups:
            raise ContainmentError(f"{session_id} already exists")
        cs = ContainedSession(session_id, "fake", f"/fake-cgroup/{session_id}", None)
        self._groups[session_id] = _Group(cs)
        return cs

    def _group(self, cs: ContainedSession) -> _Group | None:
        return self._groups.get(cs.session_id)

    def spawn(self, cs: ContainedSession, argv: list[str], env: dict[str, str], cwd: str,
              pass_fds: tuple[int, ...] = ()) -> int:
        group = self._group(cs)
        if group is None:
            raise ContainmentError(f"{cs.session_id} does not exist")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ContainmentError("argv must be a list of strings (there is no shell)")
        self._next_pid += 1
        group.members.add(self._next_pid)
        group.spawns.append({"pid": self._next_pid, "argv": list(argv), "env": dict(env), "cwd": cwd,
                             "pass_fds": tuple(pass_fds)})
        return self._next_pid

    def pids(self, cs: ContainedSession) -> list[int]:
        group = self._group(cs)
        return sorted(group.members) if group else []

    def freeze(self, cs: ContainedSession) -> None:
        if (group := self._group(cs)):
            group.frozen = True

    def thaw(self, cs: ContainedSession) -> None:
        if (group := self._group(cs)):
            group.frozen = False

    def kill(self, cs: ContainedSession) -> None:
        group = self._group(cs)
        if group is None:
            return
        group.frozen = True
        group.kill_calls += 1
        self._kills[cs.session_id] = self._kills.get(cs.session_id, 0) + 1
        group.kill_requested = True
        if group.ignore_polls <= 0:
            group.members &= group.leaked

    def is_empty(self, cs: ContainedSession) -> bool:
        group = self._group(cs)
        if group is None:
            return True
        if group.kill_requested and group.ignore_polls > 0:
            group.ignore_polls -= 1
            if group.ignore_polls == 0:
                group.members &= group.leaked
            return False
        return not group.members

    def cleanup(self, cs: ContainedSession) -> None:
        group = self._group(cs)
        if group is None:
            return
        if group.members:
            raise ContainmentError(f"{cs.session_id} is not empty")
        self.release_output(cs)
        del self._groups[cs.session_id]

    # ── optional output capture (the same extension the cgroup containment offers the command adapter) ──
    def output_fd(self, cs: ContainedSession) -> int | None:
        """Non-blocking read end of the session's merged output, or None before any output / after release."""
        group = self._group(cs)
        return group.out_read if group else None

    def release_output(self, cs: ContainedSession) -> None:
        group = self._group(cs)
        if group is None:
            return
        for name in ("out_read", "out_write"):
            fd = getattr(group, name)
            if fd is not None:
                setattr(group, name, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

    # ── simulation controls (tests only) ────────────────────────────────────
    def write_output(self, session_id: str, data: bytes) -> None:
        """What a session's process printed. Creates the pipe on first use."""
        group = self._groups[session_id]
        if group.out_read is None:
            group.out_read, group.out_write = os.pipe()
            os.set_blocking(group.out_read, False)
            os.set_blocking(group.out_write, False)
        os.write(group.out_write, data)

    def ignore_kill(self, session_id: str, polls: int) -> None:
        """After kill(), the members survive `polls` is_empty() calls."""
        self._groups[session_id].ignore_polls = polls

    def crash(self, session_id: str) -> None:
        """The session's processes die on their own (leaked children are not affected)."""
        group = self._groups[session_id]
        group.members &= group.leaked

    def leak_child(self, session_id: str) -> int:
        group = self._groups[session_id]
        self._next_pid += 1
        group.members.add(self._next_pid)
        group.leaked.add(self._next_pid)
        return self._next_pid

    def reap_leaks(self, session_id: str) -> None:
        group = self._groups[session_id]
        group.members -= group.leaked
        group.leaked.clear()

    def exists(self, session_id: str) -> bool:
        return session_id in self._groups

    def kill_calls(self, session_id: str) -> int:
        return self._kills.get(session_id, 0)

    def spawns(self, session_id: str) -> list[dict]:
        group = self._groups.get(session_id)
        return list(group.spawns) if group else []


__all__ = ["ContainedSession", "ContainmentError", "FakeContainment", "SessionContainment"]
