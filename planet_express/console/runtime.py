"""One Console turn as an `AgentRuntime`: the supervisor starts it exactly as it starts any session (uid, cgroup, hooks, broker,
budgets, kill-before-release); this runtime only decides what the process is asked and what is kept of its answer.

A turn is ONE contained process per operator message, resumed from the conversation's saved agent state (`--session-id` the first
time, `--resume <the id the last stream reported>` after). The agent's state (home and config) lives in the conversation's state
directory beside the workspaces, owned by the conversation's one uid; the session's own workspace is new every turn. The agent is
read-only (Observe/Propose tier, hooks, no Edit); it may write `plan.json` in its workspace, and PE validates that file. Its
words are untrusted data: they are redacted and stored, shown to the operator, and never executed or treated as a decision.
"""

import os
import stat
import uuid
from pathlib import Path

from config_schema import MissionConsoleConfig
from planet_express.console import plan as plan_mod
from planet_express.console.store import ConsoleStore, clean_text
from planet_express.console.stream import StreamParser
from planet_express.missions.runtime import (
    AgentEvent,
    AgentResult,
    AgentUpdate,
    RunContext,
    RunHandle,
)

MAX_ARG_PROMPT = 3000                 # the launcher refuses an argument over 4096 characters: a longer message travels as a file
_DRAIN_PER_CALL = 256 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
SYSTEM = (
    "You are Planet Express's investigation agent for one operator's Docker Compose homelab. You are PE-aware: BRIEF.md in the current "
    "directory holds the host evidence and memory PE has for this conversation, refreshed every turn; read it first. You investigate "
    "with your read tools and answer plainly: say what you know, with the evidence behind it, and what you are unsure of. You never "
    "change the host. When the right response is a change, propose it as a plan the operator can confirm, scrap or modify: read "
    "PLAN_FORMAT.md for the exact format and write plan.json in the current directory. You cannot approve a plan or widen what it "
    "says. Keep answers short and specific, and ask when the question is ambiguous.")


class TurnRefused(RuntimeError):
    """The turn cannot start (the conversation is closed, its budget is spent, or there is no message). The message is operator-safe."""


class _Run:
    def __init__(self, cs, workspace: Path, conversation_id: str, turn: int):
        self.cs, self.workspace, self.conversation_id, self.turn = cs, workspace, conversation_id, turn
        self.parser = StreamParser()
        self.output_open = True
        self.accepted = 0


class ConsoleRuntime:
    def __init__(self, containment, console: ConsoleStore, cfg: MissionConsoleConfig, *, worker_socket: str, env_policy=None,
                 broker=None, max_output_bytes: int = MAX_OUTPUT_BYTES):
        self.containment = containment            # only for is_empty/kill/output capture: spawning is `context.spawn`
        self.console, self.cfg = console, cfg
        self.worker_socket = str(worker_socket)
        self.env_policy = env_policy              # grant -> environment: the broker's endpoint and code, the hook inputs
        self.broker = broker                      # to lower the turn's token ceiling to the conversation's remainder
        self.max_output_bytes = max_output_bytes
        self._runs: dict[str, _Run] = {}

    # ── AgentRuntime ──────────────────────────────────────────────────────────────────────
    def start(self, mission, context: RunContext, grant) -> RunHandle:
        conv = self.console.by_mission(mission.id)
        if conv is None or conv.state != "open":
            raise TurnRefused("this conversation is closed")
        if conv.tokens_left < self.cfg.min_turn_tokens:
            raise TurnRefused("this conversation's model budget is used up")
        message = self.console.pending(conv.id)
        if message is None:
            raise TurnRefused("there is no message waiting")
        cs, workspace = context.session, Path(context.cwd)
        self._prepare_workspace(workspace, context.brief, message.text)
        state = str(workspace.parent / conv.state_dir)
        if conv.agent_session_id:
            resume = ["--resume", conv.agent_session_id]
        else:
            first = str(uuid.uuid4())
            self.console.set_agent_session(conv.id, first)           # recorded before the turn runs: a crash can still be resumed
            resume = ["--session-id", first]
        prompt = message.text if len(message.text) <= MAX_ARG_PROMPT else (
            "The operator's message is long: read it in full from MESSAGE.md in the current directory, then answer it.")
        tools = ",".join(self.cfg.tools)
        argv = [self.cfg.claude, "-p", prompt, *resume, "--model", self.cfg.model, "--max-turns", str(self.cfg.max_turns),
                "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--system-prompt", SYSTEM,
                "--tools", tools, "--allowedTools", tools, "--disallowedTools", ",".join(self.cfg.disallowed_tools)]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": state, "CLAUDE_CONFIG_DIR": f"{state}/cfg",
               "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.cfg.max_output_tokens), "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "PE_WORKER_SOCKET": self.worker_socket,
               "PE_SESSION_TOKEN_FD": str(context.token_fd)}
        if self.env_policy is not None:
            env = dict(self.env_policy(grant, str(mission.id), str(workspace), env))
            env["PE_SESSION_TOKEN_FD"] = str(context.token_fd)
        if self.broker is not None and not self.broker.set_session_cap(cs.session_id, conv.tokens_left):
            raise TurnRefused("the model broker has no session for this turn")
        turn = self.console.begin_turn(conv.id)
        pid = context.spawn(argv, env, str(workspace), (), state_dir=conv.state_dir)
        self.console.consume(message.seq, turn)                       # taken only once the process is running
        run_id = f"{mission.id}:{cs.session_id}"
        self._runs[run_id] = _Run(cs, workspace, conv.id, turn)
        return RunHandle(run_id=run_id, session_id=cs.session_id, pid=pid)

    def status(self, handle) -> AgentUpdate:
        run = self._runs[handle.run_id]
        empty = self.containment.is_empty(run.cs)           # ask "is it dead" BEFORE draining: what it wrote dying is still in the pipe
        events = run.parser.feed(self._drain(run), final=empty)
        update = [AgentEvent(kind=e.kind, body=e.body) for e in events]
        if not empty:
            return AgentUpdate(state="running", events=update)
        return AgentUpdate(state="finished" if run.parser.result.subtype is not None else "failed", events=update)

    def cancel(self, handle) -> None:
        run = self._runs.get(handle.run_id)
        if run is None:
            return
        self.containment.kill(run.cs)                       # raises if it cannot prove the session empty
        self._release(run)
        del self._runs[handle.run_id]

    def collect(self, handle) -> AgentResult:
        run = self._runs[handle.run_id]
        if not self.containment.is_empty(run.cs):
            raise RuntimeError("session is not provably empty; refusing to collect")
        try:
            run.parser.feed(self._drain(run), final=True)
            result = run.parser.finish()
            self._record(run, result)
            reply = result.reply or "(the agent gave no reply)"
            return AgentResult(diagnosis=reply[:8000], confidence="low" if result.interrupted else "medium")
        finally:
            self._release(run)
            self._runs.pop(handle.run_id, None)

    # ── what is kept of a turn ────────────────────────────────────────────────────────────
    def _record(self, run: _Run, result) -> None:
        conv_id = run.conversation_id
        if result.reply:
            self.console.add_message(conv_id, "agent", result.reply, turn=run.turn)
        if result.interrupted:
            self.console.add_message(conv_id, "system", "This turn was stopped before it finished.", turn=run.turn)
        elif not result.reply:
            self.console.add_message(conv_id, "system", "The agent gave no reply.", turn=run.turn)
        if result.session_id:
            self.console.set_agent_session(conv_id, result.session_id)         # the id the stream last reported: it can change
        self._plan(run)
        tokens = result.tokens
        if self.broker is not None:
            usage = self.broker.usage(run.cs.session_id)           # the broker counts what the vendor charged: that is the budget
            if usage is not None:
                tokens = usage["tokens"]
        self.console.add_usage(conv_id, tokens)

    def _plan(self, run: _Run) -> None:
        try:
            if not stat.S_ISREG(os.lstat(run.workspace / plan_mod.PLAN_FILE).st_mode):
                raise plan_mod.PlanError("the plan file is not a regular file")
        except FileNotFoundError:
            return
        except OSError:
            return
        try:
            plan = plan_mod.load_file(str(run.workspace))
        except plan_mod.PlanError as exc:
            self.console.add_message(run.conversation_id, "system", f"The agent's plan was refused: {exc}", turn=run.turn)
            return
        self.console.add_plan(run.conversation_id, run.turn, plan_mod.canonical(plan), plan_mod.plan_hash(plan))

    # ── the workspace and the pipe ────────────────────────────────────────────────────────
    @staticmethod
    def _prepare_workspace(workspace: Path, brief: str, message: str) -> None:
        if not stat.S_ISDIR(workspace.lstat().st_mode):             # made by the supervisor; never created or followed here
            raise ValueError("workspace path is not a directory")
        files = {"BRIEF.md": brief, "PLAN_FORMAT.md": plan_mod.instructions()}
        if len(message) > MAX_ARG_PROMPT:
            files["MESSAGE.md"] = clean_text(message)
        for name in ("BRIEF.md", "PLAN_FORMAT.md", "MESSAGE.md", plan_mod.PLAN_FILE):
            try:
                (workspace / name).unlink()                          # unlink never follows a link; clears a previous attempt
            except FileNotFoundError:
                pass
        for name, text in files.items():
            fd = os.open(workspace / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            try:
                os.write(fd, text.encode())
            finally:
                os.close(fd)

    def _drain(self, run: _Run) -> bytes:
        getter = getattr(self.containment, "output_fd", None)
        fd = getter(run.cs) if getter is not None and run.output_open else None
        raw = b""
        if fd is None:
            return raw
        budget = _DRAIN_PER_CALL
        while budget > 0:
            try:
                chunk = os.read(fd, min(65536, budget))
            except (BlockingIOError, InterruptedError, OSError):
                break
            if not chunk:
                break
            budget -= len(chunk)
            room = self.max_output_bytes - run.accepted
            run.accepted += min(len(chunk), max(room, 0))
            raw += chunk[:max(room, 0)]                  # past the cap nothing is kept, but the child keeps being drained
        return raw

    def _release(self, run: _Run) -> None:
        if run.output_open:
            run.output_open = False
            releaser = getattr(self.containment, "release_output", None)
            if releaser is not None:
                releaser(run.cs)


__all__ = ["SYSTEM", "ConsoleRuntime", "TurnRefused"]
