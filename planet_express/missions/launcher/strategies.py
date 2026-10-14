"""The two launcher strategies. Both spawn the SAME helper as a plain subprocess (contract 22.2, 22.8.2).

`SudoLauncher` is for an unprivileged Core: `sudo -n /usr/libexec/planetexpress/pe-launch`, which the sudoers
fragment allows with no arguments at all. `DirectLauncher` is for a Core that is already root (MOS): the same
helper, no sudo. Neither forks Python between fork and exec: `subprocess.Popen` with no `preexec_fn`, so a
multithreaded Core cannot deadlock here, and all privilege handling happens inside the single-threaded helper.

The helper's stdout IS the session's merged output pipe. Its first line is the framed `PE-LAUNCH-STATUS` line:
`read_status` consumes exactly that line (byte by byte, so nothing after it is lost) and the rest stays in the
pipe for the output drain.
"""

import json
import os
import re
import select
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from planet_express.missions.containment import ContainmentError

from .config import HELPER_PATH
from .spec import STATUS_PREFIX, LaunchSpec, SpecError

LAUNCH_TIMEOUT_S = 15.0
MAX_STATUS_BYTES = 4096
MINIMAL_ENV = {"PATH": "/usr/bin:/bin"}          # Core's own environment never reaches the helper
_CODE = re.compile(r"[A-Za-z0-9 _.\[\]()-]{1,64}")


class LaunchError(ContainmentError):
    """The helper could not start the session (it refused, timed out, or answered nonsense)."""


@dataclass
class Launched:
    proc: subprocess.Popen
    status: dict
    # The merged output pipe is `proc.stdout`; the caller owns it and closes it at release.

    @property
    def output_fd(self) -> int:
        return self.proc.stdout.fileno()


def read_status(fd: int, timeout_s: float, *, clock: Callable[[], float] = time.monotonic) -> str:
    """Exactly the first line of the pipe (without its newline). Raises LaunchError on timeout, EOF or overlength."""
    deadline = clock() + timeout_s
    data = bytearray()
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise LaunchError("the launcher did not answer in time")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        chunk = os.read(fd, 1)
        if not chunk:
            raise LaunchError("the launcher exited without a status line")
        if chunk == b"\n":
            return data.decode("utf-8", errors="replace")
        data += chunk
        if len(data) > MAX_STATUS_BYTES:
            raise LaunchError("the launcher's status line is too long")


def parse_status(line: str, session_id: str, spec: LaunchSpec) -> dict:
    """The status must be the framed line, say ok, and describe exactly the session that was asked for."""
    if not line.startswith(STATUS_PREFIX):
        raise LaunchError("the launcher's first line was not a status line")
    try:
        status = json.loads(line[len(STATUS_PREFIX):])
    except ValueError:
        raise LaunchError("the launcher's status line is not JSON") from None
    if not isinstance(status, dict) or not isinstance(status.get("ok"), bool):
        raise LaunchError("the launcher's status line is malformed")
    if not status["ok"]:
        code = status.get("error")
        raise LaunchError("the launcher refused: " + (code if isinstance(code, str) and _CODE.fullmatch(code)
                                                      else "unspecified"))
    if (status.get("session_id") != session_id or status.get("uid") != spec.uid or status.get("gid") != spec.uid
            or not isinstance(status.get("cgroup"), str) or not status["cgroup"].startswith("/")):
        raise LaunchError("the launcher's status does not match the request")
    return status


class HelperLauncher:
    """Run `argv` (the helper, possibly behind sudo) with the spec on stdin and wait for its status line."""

    def __init__(self, argv: list[str], *, timeout_s: float = LAUNCH_TIMEOUT_S, popen=subprocess.Popen):
        self.argv = list(argv)
        self.timeout_s = timeout_s
        self._popen = popen

    def start(self, session_id: str, spec: LaunchSpec) -> Launched:
        try:
            wire = spec.to_wire(session_id)
        except SpecError as exc:
            raise LaunchError(f"the launch spec is not acceptable: {exc}") from None
        proc = self._popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=dict(MINIMAL_ENV), cwd="/", close_fds=True, bufsize=0, start_new_session=True)
        try:
            try:
                proc.stdin.write(wire)          # under the pipe buffer by construction, so it cannot block
            except BrokenPipeError:
                pass                            # the helper already died: its status line says why
            finally:
                proc.stdin.close()
            status = parse_status(read_status(proc.stdout.fileno(), self.timeout_s), session_id, spec)
            os.set_blocking(proc.stdout.fileno(), False)
            return Launched(proc, status)
        except BaseException:
            self._abandon(proc)
            raise

    @staticmethod
    def _abandon(proc: subprocess.Popen) -> None:
        try:
            proc.kill()                          # a root helper behind sudo cannot be signalled: its own alarm ends it
        except (PermissionError, ProcessLookupError, OSError):
            pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            pass


class SudoLauncher(HelperLauncher):
    """Unprivileged Core: the sudoers rule allows exactly `pe-launch` with no arguments."""

    def __init__(self, helper: str = HELPER_PATH, *, sudo: str = "/usr/bin/sudo", **kw):
        super().__init__([sudo, "-n", helper], **kw)
        self.helper = helper


class DirectLauncher(HelperLauncher):
    """Core is root (MOS): the same helper without sudo. Refuses to exist for a non-root Core."""

    def __init__(self, helper: str = HELPER_PATH, *, euid: int | None = None, **kw):
        if (os.geteuid() if euid is None else euid) != 0:
            raise LaunchError("DirectLauncher needs a Core running as root; use SudoLauncher")
        super().__init__([helper], **kw)
        self.helper = helper


__all__ = [
    "MINIMAL_ENV",
    "DirectLauncher",
    "HelperLauncher",
    "LaunchError",
    "Launched",
    "SudoLauncher",
    "parse_status",
    "read_status",
]
