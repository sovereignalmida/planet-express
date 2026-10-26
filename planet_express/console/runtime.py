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
MAX_EVENTS_PER_POLL = 256             # AgentUpdate's own limit
MAX_BACKLOG = 10_000                  # events carried between polls while the turn runs
_DRAIN_PER_CALL = 256 * 1024
SYSTEM = (
    "You are Planet Express's investigation agent for one operator's Docker Compose homelab. You are PE-aware: BRIEF.md in the current "
    "directory holds the host evidence and memory PE has for this conversation, refreshed every turn; read it first. You investigate "
    "with your read tools and answer plainly: say what you know, with the evidence behind it, and what you are unsure of. You never "
    "change the host. When the right response is a change, propose it as a plan the operator can confirm, scrap or modify: read "
    "PLAN_FORMAT.md for the exact format and write plan.json in the current directory. You cannot approve a plan or widen what it "
    "says. Keep answers short and specific, and ask when the question is ambiguous.")


class TurnRefused(RuntimeError):
    """The turn cannot start (the conversation is closed, its budget is spent, or there is no message). The message is operator-safe."""


def _capped(events: list[AgentEvent]) -> list[AgentEvent]:
    """At most AgentUpdate's limit of events per poll. Calls are kept in preference to text (a call is what the budgets and the trail are
    about) and one summary note says how many were left out."""
    if len(events) <= MAX_EVENTS_PER_POLL:
        return events
    keep = MAX_EVENTS_PER_POLL - 1
    calls = [e for e in events if "call" in e.body]
    chosen = set(map(id, calls[:keep]))
    for event in events:
        if len(chosen) >= keep:
            break
        chosen.add(id(event))
    kept = [e for e in events if id(e) in chosen]
    return [*kept, AgentEvent(kind="note", body={"omitted": len(events) - len(kept)})]


class _Run:
    def __init__(self, cs, workspace: Path, conversation_id: str, turn: int, mission_id: str = ""):
        self.cs, self.workspace, self.conversation_id, self.turn, self.mission_id = cs, workspace, conversation_id, turn, mission_id
        self.parser = StreamParser()
        self.backlog: list[AgentEvent] = []
        self.output_open = True


class ConsoleRuntime:
    counts_calls_from_events = True           # the supervisor also counts the calls this runtime reports (`call` events) against the tool budget

    def __init__(self, containment, console: ConsoleStore, cfg: MissionConsoleConfig, *, worker_socket: str, env_policy=None,
                 broker=None):
        self.containment = containment            # only for is_empty/kill/output capture: spawning is `context.spawn`
        self.console, self.cfg = console, cfg
        self.worker_socket = str(worker_socket)
        self.env_policy = env_policy              # grant -> environment: the broker's endpoint and code, the hook inputs
        self.broker = broker                      # to lower the turn's token ceiling to the conversation's remainder
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
        # The agent's own session id is stored only once the stream has CONFIRMED it (`_record`): a first turn that dies before the CLI
        # saved its transcript must not leave a `--resume` id that does not exist. Until then every turn starts a new one.
        resume = ["--resume", conv.agent_session_id] if conv.agent_session_id else ["--session-id", str(uuid.uuid4())]
        prompt = message.text if len(message.text) <= MAX_ARG_PROMPT else (
            "The operator's message is long: read it in full from MESSAGE.md in the current directory, then answer it.")
        tools = ",".join(self.cfg.tools)
        # The message is the LAST argument, after `--`: whatever it says (a bullet, `--verbose`, `--settings=...`) is text, never an option.
        argv = [self.cfg.claude, "-p", *resume, "--model", self.cfg.model, "--max-turns", str(self.cfg.max_turns),
                "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--system-prompt", SYSTEM,
                "--tools", tools, "--allowedTools", tools, "--disallowedTools", ",".join(self.cfg.disallowed_tools), "--", prompt]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "PE_WORKER_SOCKET": self.worker_socket,
               "PE_SESSION_TOKEN_FD": str(context.token_fd)}
        if self.env_policy is not None:
            env = dict(self.env_policy(grant, str(mission.id), str(workspace), env))
            env["PE_SESSION_TOKEN_FD"] = str(context.token_fd)
        # What a Console turn needs and the grant's environment policy would drop or reset (it forces HOME to the workspace and removes
        # anything named like a token). PE computes these values itself; none is a secret and none comes from the agent.
        env.update({"HOME": state, "CLAUDE_CONFIG_DIR": f"{state}/cfg", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.cfg.max_output_tokens)})
        if self.broker is not None:
            self.broker.set_mission_cap(mission.id, conv.tokens_left)      # the broker opens this turn's endpoint during spawn: ask first
        try:
            turn = self.console.begin_turn(conv.id)
            pid = context.spawn(argv, env, str(workspace), (), state_dir=conv.state_dir)
        except BaseException:
            if self.broker is not None:
                self.broker.clear_mission_cap(mission.id)              # no session was opened: the ceiling must not wait for a later one
            raise
        self.console.consume(message.seq, turn)                       # taken only once the process is running
        run_id = f"{mission.id}:{cs.session_id}"
        self._runs[run_id] = _Run(cs, workspace, conv.id, turn, mission.id)
        return RunHandle(run_id=run_id, session_id=cs.session_id, pid=pid)

    def status(self, handle) -> AgentUpdate:
        run = self._runs[handle.run_id]
        empty = self.containment.is_empty(run.cs)           # ask "is it dead" BEFORE draining: what it wrote dying is still in the pipe
        events = run.parser.feed(self._drain(run), final=empty)
        pending = run.backlog + [AgentEvent(kind=e.kind, body=e.body) for e in events]
        if empty or len(pending) <= MAX_EVENTS_PER_POLL:
            update, run.backlog = _capped(pending), []                 # the last poll must hand everything over: calls first, the rest summarised
        else:
            update, run.backlog = pending[:MAX_EVENTS_PER_POLL], pending[MAX_EVENTS_PER_POLL:][-MAX_BACKLOG:]    # carried to the next poll, in order
        if not empty:
            return AgentUpdate(state="running", events=update)
        return AgentUpdate(state="finished" if run.parser.result.subtype is not None else "failed", events=update)

    def cancel(self, handle) -> None:
        run = self._runs.get(handle.run_id)
        if run is None:
            return
        self.containment.kill(run.cs)                       # raises if it cannot prove the session empty
        try:
            # A turn that is killed (a budget, a crash, a tamper) still used the model and still said something: keep it and charge it.
            run.parser.feed(self._drain(run), final=True)
            self._record(run, run.parser.finish(), keep_plan=False)       # a stopped turn's plan is not offered to the operator
        except Exception:                                   # noqa: BLE001 -- recording must never block the kill that already happened
            pass
        if self.broker is not None:
            self.broker.clear_mission_cap(run.mission_id)
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
    def _record(self, run: _Run, result, *, keep_plan: bool = True) -> None:
        conv_id = run.conversation_id
        if result.reply:
            self.console.add_message(conv_id, "agent", result.reply, turn=run.turn)
        if result.interrupted:
            self.console.add_message(conv_id, "system", "This turn was stopped before it finished.", turn=run.turn)
        elif not result.reply:
            self.console.add_message(conv_id, "system", "The agent gave no reply.", turn=run.turn)
        if result.session_id:
            self.console.set_agent_session(conv_id, result.session_id)         # the id the stream confirmed: it can change, so every turn
        if keep_plan:
            self._plan(run)
        tokens = result.tokens
        if self.broker is not None:
            usage = self.broker.usage(run.cs.session_id)           # the broker counts what the vendor charged: that is the budget
            if usage is not None:
                tokens = usage["tokens"]
        self.console.add_usage(conv_id, tokens)

    def _plan(self, run: _Run) -> None:
        if not os.path.lexists(run.workspace / plan_mod.PLAN_FILE):
            return                                                     # the agent proposed no plan this turn
        try:
            plan = plan_mod.load_file(str(run.workspace))              # a link, a directory, a FIFO or a bad file is a refused plan, never a crash
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
            raw += chunk
        return raw                       # nothing is dropped here: the parser bounds what it keeps, and the budgets bound the turn

    def _release(self, run: _Run) -> None:
        if run.output_open:
            run.output_open = False
            releaser = getattr(self.containment, "release_output", None)
            if releaser is not None:
                releaser(run.cs)


__all__ = ["SYSTEM", "ConsoleRuntime", "TurnRefused"]
