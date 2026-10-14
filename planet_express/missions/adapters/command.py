"""Generic command adapter: run a configured argv inside a containment and collect its JSON result.

Nothing here knows which agent CLI is behind the argv. The contract with the child is:

  * cwd is the per-session workspace the Supervisor made (`RunContext.cwd`). The launcher hands it to the
    session's uid when it starts the session (this adapter never chowns anything); the brief is `BRIEF.md` in it
    (read-only);
  * env is minimal: `PE_WORKER_SOCKET`, `PE_SESSION_TOKEN_FD`, a safe PATH/LANG/HOME and the few
    names the operator allowed. The session token is on fd 3, put there by the launcher (never argv/env); the
    Supervisor sets `PE_SESSION_TOKEN_FD=3` itself;
  * stdout/stderr come back as bounded, redacted `note` events in `AgentUpdate`s (the child is always
    drained, so a chatty child can never block on a full pipe). A containment without `output_fd`
    simply streams nothing;
  * the final answer is `result.json` in the workspace, validated as an `AgentResult`. The worker
    controls that file, so it is opened without following links, size-capped and parsed strictly.

The supervisor (S3a) owns budgets, persistence, kill-before-release and what happens next; this
adapter only starts, reports, cancels and collects. Spawning always goes through `RunContext.spawn`,
which puts the process inside the session's containment.
"""

import json
import os
import re
import stat
from pathlib import Path

from config_schema import MissionRuntimeConfig
from planet_express.core.redact import redact
from planet_express.missions.runtime import (
    AgentEvent,
    AgentResult,
    AgentUpdate,
    RunContext,
    RunHandle,
)

_MISSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MAX_RESULT_BYTES = 256 * 1024
MAX_LINE_CHARS = 4096
_DRAIN_PER_CALL = 256 * 1024
TRUNCATED = "[output truncated]"


class AgentResultError(ValueError):
    """The child produced no usable result. The message never echoes the child's content."""


def _chunks(text: str, size: int = 8000):
    return [text[i:i + size] for i in range(0, len(text), size)][:256]


class _Run:
    def __init__(self, cs, workspace):
        self.cs = cs
        self.workspace = workspace
        self.partial = b""
        self.accepted = 0
        self.truncated = False
        self.skipping = False  # inside an over-long line: drop until its newline
        self.output_open = True
        self.notified = False
        self.emitted = 0


class CommandRuntime:
    def __init__(self, containment, *, argv, worker_socket, env_allow=(),
                 max_output_bytes=4 * 1024 * 1024, max_result_bytes=MAX_RESULT_BYTES,
                 environ=None):
        # Same validation as the config model, so a hand-built runtime cannot be looser than a configured one.
        checked = MissionRuntimeConfig(argv=list(argv), env_allow=list(env_allow))
        if not checked.argv:
            raise ValueError("runtime argv is empty")
        self.containment = containment      # only for is_empty/kill/output capture; spawning is context.spawn
        self.argv = checked.argv
        self.env_allow = checked.env_allow
        self.worker_socket = str(worker_socket)
        self.max_output_bytes = max_output_bytes
        self.max_result_bytes = max_result_bytes
        self.environ = os.environ if environ is None else environ
        self._runs: dict[str, _Run] = {}

    # --- AgentRuntime ----------------------------------------------------------------------

    def start(self, mission, context: RunContext, grant) -> RunHandle:
        mission_id = str(mission.id)
        if not _MISSION_ID.fullmatch(mission_id):
            raise ValueError("unsafe mission id")
        cs = context.session
        workspace = self._prepare_workspace(Path(context.cwd), context.brief)
        env = self._env(workspace, context.token_fd)
        pid = context.spawn(list(self.argv), env, str(workspace), ())
        run_id = f"{mission_id}:{cs.session_id}"
        self._runs[run_id] = _Run(cs, workspace)
        return RunHandle(run_id=run_id, session_id=cs.session_id, pid=pid)

    def status(self, handle) -> AgentUpdate:
        run = self._runs[handle.run_id]
        # Ask "is it dead" BEFORE draining: whatever it wrote before dying is then still in the pipe.
        empty = self.containment.is_empty(run.cs)
        text = self._drain(run, final=empty)
        events = [AgentEvent(kind="note", body={"output": chunk}) for chunk in _chunks(text)]
        if not empty:
            state = "running"
        else:
            state = "finished" if self._has_result(run.workspace) else "failed"
        # Event sizes are what the Supervisor counts against the output budget; reporting bytes too would count twice.
        return AgentUpdate(state=state, events=events)

    def cancel(self, handle) -> None:
        run = self._runs.get(handle.run_id)
        if run is None:
            return
        self.containment.kill(run.cs)  # raises if it cannot prove the session empty
        self._release(run)
        del self._runs[handle.run_id]

    def collect(self, handle) -> AgentResult:
        run = self._runs[handle.run_id]
        if not self.containment.is_empty(run.cs):
            raise RuntimeError("session is not provably empty; refusing to collect")
        try:
            return self._read_result(run.workspace)
        finally:
            self._release(run)
            self._runs.pop(handle.run_id, None)

    # --- internals -------------------------------------------------------------------------

    @staticmethod
    def _has_result(workspace) -> bool:
        try:
            return stat.S_ISREG(os.lstat(Path(workspace) / "result.json").st_mode)
        except OSError:
            return False

    def _prepare_workspace(self, workspace: Path, brief):
        info = workspace.lstat()   # made by the Supervisor; the adapter never creates or follows it
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("workspace path is not a directory")
        for name in ("BRIEF.md", "result.json"):
            try:
                (workspace / name).unlink()  # unlink never follows a link; clears a previous attempt
            except FileNotFoundError:
                pass
        fd = os.open(workspace / "BRIEF.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            os.write(fd, brief.encode())
        finally:
            os.close(fd)
        return workspace

    def _env(self, workspace, token_fd):
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(workspace)}
        for name in self.env_allow:
            value = self.environ.get(name)
            if isinstance(value, str) and "\0" not in value:
                env[name] = value
        env["PE_WORKER_SOCKET"] = self.worker_socket
        env["PE_SESSION_TOKEN_FD"] = str(token_fd)
        return env

    def _fd(self, run):
        getter = getattr(self.containment, "output_fd", None)
        return getter(run.cs) if getter is not None and run.output_open else None

    def _release(self, run):
        if run.output_open:
            run.output_open = False
            releaser = getattr(self.containment, "release_output", None)
            if releaser is not None:
                releaser(run.cs)

    def _drain(self, run, final):
        fd = self._fd(run)
        raw = b""
        if fd is not None:
            budget = _DRAIN_PER_CALL
            while budget > 0:
                try:
                    chunk = os.read(fd, min(65536, budget))
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if not chunk:
                    break
                budget -= len(chunk)
                room = self.max_output_bytes - run.accepted
                if room < len(chunk):
                    run.truncated = run.truncated or len(chunk) > max(room, 0)
                    chunk = chunk[:max(room, 0)]
                run.accepted += len(chunk)
                raw += chunk  # past the cap nothing is kept, but the child keeps being drained
        out = []
        data = run.partial + raw
        run.partial = b""
        while True:
            line, newline, rest = data.partition(b"\n")
            if not newline:
                break
            if not run.skipping:
                out.append(self._clean(line) + "\n")
            run.skipping = False
            data = rest
        if run.skipping:
            data = b""
        elif len(data) > MAX_LINE_CHARS:
            out.append(self._clean(data[:MAX_LINE_CHARS]) + " [line truncated]\n")
            run.skipping, data = True, b""
        if final or (run.truncated and run.accepted >= self.max_output_bytes):
            if data and not run.skipping:
                out.append(self._clean(data) + "\n")
            data = b""
        run.partial = data
        text = "".join(out)
        # Redaction can expand a line (`TOKEN=x` -> `TOKEN=[REDACTED]`), so the ceiling is enforced
        # on what is emitted as well as on what was read.
        room = max(self.max_output_bytes - run.emitted, 0)
        encoded = text.encode()
        if len(encoded) > room:
            text = encoded[:room].decode("utf-8", errors="ignore")
            run.truncated = True
            run.accepted = self.max_output_bytes
        run.emitted += len(text.encode())
        if run.truncated and not run.notified:
            run.notified = True
            text += TRUNCATED + "\n"
        return text

    @staticmethod
    def _clean(line: bytes) -> str:
        return redact(line.decode("utf-8", errors="replace"))

    def _read_result(self, workspace):
        try:
            dir_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise AgentResultError("workspace is unavailable") from None
        try:
            try:
                fd = os.open("result.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            except OSError:
                raise AgentResultError("no result file") from None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_result_bytes:
                    raise AgentResultError("result file is not a regular file within the size limit")
                data = os.read(fd, self.max_result_bytes + 1)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
        if len(data) > self.max_result_bytes:
            raise AgentResultError("result too large")
        try:
            obj = json.loads(data.decode("utf-8"))
            if not isinstance(obj, dict):
                raise TypeError
            return AgentResult.model_validate(obj)
        except (ValueError, TypeError, RecursionError):
            raise AgentResultError("result is not a valid AgentResult") from None


class EchoRuntime:
    """In-memory `AgentRuntime` for tests: no process, the 'result' just echoes the brief."""

    def __init__(self):
        self._runs: dict[str, str] = {}

    def start(self, mission, context: RunContext, grant) -> RunHandle:
        run_id = f"echo:{mission.id}"
        self._runs[run_id] = context.brief
        sid = getattr(context.session, "session_id", "echo")
        return RunHandle(run_id=run_id, session_id=sid)

    def status(self, handle) -> AgentUpdate:
        text = f"echo: {self._runs[handle.run_id][:200]}\n"
        return AgentUpdate(state="finished", events=[AgentEvent(kind="note", body={"output": text})])

    def cancel(self, handle) -> None:
        self._runs.pop(handle.run_id, None)

    def collect(self, handle) -> AgentResult:
        return AgentResult(diagnosis=f"echo: {self._runs[handle.run_id][:200]}", confidence="low")
