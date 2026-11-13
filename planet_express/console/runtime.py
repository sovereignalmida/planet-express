"""One Console turn as an `AgentRuntime`: the supervisor starts it exactly as it starts any session (uid, cgroup, hooks, broker,
budgets, kill-before-release); this runtime only decides what the process is asked and what is kept of its answer.

A turn is ONE contained process per operator message, resumed from the conversation's saved agent state (`--session-id` the first
time, `--resume <the id the last stream reported>` after). The agent's state (home and config) lives in the conversation's state
directory beside the workspaces, owned by the conversation's one uid; the session's own workspace is new every turn. The agent has
full access (trust mode) but runs at the Observe tier, asked to investigate and propose, not change; it may write `plan.json` in its
workspace, and PE validates that file. Its
words are untrusted data: they are redacted and stored, shown to the operator, and never executed or treated as a decision.
"""

import contextlib
import logging
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from config_schema import MissionConsoleConfig
from planet_express.console import lessons as lessons_mod, plan as plan_mod
from planet_express.console.store import ConsoleStore, clean_text
from planet_express.console.stream import StreamParser
from planet_express.memory import store as memory_store
from planet_express.missions.state import CONSOLE_PLAN_RETRY
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
MAX_PLAN_RETRIES = 2                  # automatic "fix your plan" turns per operator message (rc14.2)
MAX_TAILS = 64                        # ended turns whose output tail waits for the supervisor to take it
SYSTEM = (
    "You are Planet Express's investigation agent for one operator's Docker Compose homelab. You are PE-aware: BRIEF.md in the current "
    "directory holds the host evidence and memory PE has for this conversation, refreshed every turn; read it first. You have full "
    "access to this host, as the operator has at a terminal (docker, passwordless sudo, the logs; files under the operator's home are "
    "readable with sudo), and everything you run is recorded. Investigate with whatever helps and answer plainly: say what you know, "
    "with the evidence behind it, and what you are unsure of. Do not change the host before the operator confirms a plan: when the "
    "right response is a change, propose it as a plan the operator can confirm, scrap or modify: read PLAN_FORMAT.md for the exact "
    "format and write plan.json in the current directory. You cannot approve a plan. Never wipe disks, delete Docker volumes or data, "
    "or touch PE's own config, secrets or database. If you learn something durable about this host, LESSONS_FORMAT.md says how to offer it to the operator to remember. Keep answers "
    "short and specific, and ask when the question is ambiguous.")


log = logging.getLogger(__name__)


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
        self.stopping = False          # PE is stopping it (an interrupt): a turn PE ended is never "ended early" (rc14.4)
        self.ended_early = False       # rc14.4: the process exited by itself with no result line, but left a plan or a reply


class ConsoleRuntime:
    counts_calls_from_events = True           # the supervisor also counts the calls this runtime reports (`call` events) against the tool budget

    def __init__(self, containment, console: ConsoleStore, cfg: MissionConsoleConfig, *, worker_socket: str, env_policy=None,
                 broker=None, inventory=None, checks=None, other_hosts=None):
        self.containment = containment            # only for is_empty/kill/output capture: spawning is `context.spawn`
        self.console, self.cfg = console, cfg
        self.worker_socket = str(worker_socket)
        self.env_policy = env_policy              # grant -> environment: the broker's endpoint and code, the hook inputs
        self.broker = broker                      # to lower the turn's token ceiling to the conversation's remainder
        self.inventory = inventory                # () -> the host's stacks and services (None where Repair is not available): a plan's scope is checked against it
        self.checks = checks                      # () -> `verify.checks.Availability`: the checks this host can run (offered in PLAN_FORMAT.md, enforced on the plan)
        self.other_hosts = other_hosts            # () -> text: the other Docker hosts as Core last read them, read-only (multi-host release C)
        self._runs: dict[str, _Run] = {}
        self._tails: dict[str, dict] = {}         # run id -> the ended turn's output tail, until the supervisor takes it (rc14.4)

    # ── AgentRuntime ──────────────────────────────────────────────────────────────────────
    def start(self, mission, context: RunContext, grant) -> RunHandle:
        conv = self.console.by_mission(mission.id)
        if conv is None or conv.state != "open":
            raise TurnRefused("this conversation is closed")
        message = self.console.pending(conv.id)
        if message is None:
            raise TurnRefused("there is no message waiting")
        cs, workspace = context.session, Path(context.cwd)
        self._prepare_workspace(workspace, context.brief, message.text, self._inventory_text(),
                                lessons_mod.LESSONS_INSTRUCTIONS if self.console.memory_ready() else lessons_mod.NO_MEMORY_INSTRUCTIONS,
                                plan_mod.instructions(self._checks()))
        state = str(workspace.parent / conv.state_dir)
        # The agent's own session id is stored only once the stream has CONFIRMED it (`_record`): a first turn that dies before the CLI
        # saved its transcript must not leave a `--resume` id that does not exist. Until then every turn starts a new one.
        resume = ["--resume", conv.agent_session_id] if conv.agent_session_id else ["--session-id", str(uuid.uuid4())]
        text = message.text
        if message.role == "operator" and plan_mod.goes_ahead(text)[0] and self.console.current_plan(conv.id) is None:
            # "Go ahead" with no plan to authorise: the agent is asked for the plan, not left to answer that it cannot change the host.
            # (With a valid plan, the Console answers the operator with the Authorise card itself and never sends the words here.)
            text = plan_mod.GO_AHEAD_PROMPT.format(text=" ".join(text.split())[:80])
        prompt = text if len(text) <= MAX_ARG_PROMPT else (
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
        # rc14.6: no per-turn cap from the conversation's budget (it is a usage mark, not a stop). The broker's own per-session ceilings apply.
        turn = self.console.begin_turn(conv.id)
        pid = context.spawn(argv, env, str(workspace), (), state_dir=conv.state_dir)
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
        if run.parser.result.subtype is None and not run.stopping and not run.ended_early and self._left_something(run):
            # rc14.4 (live M-26): the CLI exited by itself without its result line, after writing a plan or streaming its reply. That is
            # not a crash: the turn is collected like a finished one, its plan and reply kept, and the operator told it ended early.
            run.ended_early = True
        return AgentUpdate(state="finished" if run.parser.result.subtype is not None or run.ended_early else "failed", events=update)

    def stopping(self, handle) -> None:
        """PE is about to stop this turn itself (an operator's interrupt): what is collected after is a stopped turn, never one that
        ended early by itself."""
        run = self._runs.get(handle.run_id)
        if run is not None:
            run.stopping = True

    def take_tail(self, handle) -> dict | None:
        """The ended turn's last non-JSON output lines (redacted, bounded) and whether it ended without a result line, once."""
        tail = self._tails.pop(handle.run_id, None)
        run = self._runs.get(handle.run_id)
        if run is not None:                                 # still tracked (its cancel could not prove it dead): its latest tail
            with contextlib.suppress(Exception):
                run.parser.feed(self._drain(run))
                tail = {"lines": run.parser.tail(), "no_result": run.parser.result.subtype is None}
        return tail

    def _keep_tail(self, run: _Run, handle_id: str) -> None:
        try:
            self._tails[handle_id] = {"lines": run.parser.tail(), "no_result": run.parser.result.subtype is None}
            while len(self._tails) > MAX_TAILS:
                self._tails.pop(next(iter(self._tails)))
        except Exception:                                   # the tail is diagnostic only
            log.debug("console turn %s: no output tail kept", handle_id, exc_info=True)

    def _left_something(self, run: _Run) -> bool:
        """A turn that died without its result line left something the operator can use: a plan file, or words it said. Read only
        after the process is provably gone and its output drained."""
        getter = getattr(self.containment, "exit_status", None)
        status = None
        if getter is not None:
            try:
                status = getter(run.cs)
            except Exception:                               # noqa: BLE001 -- unknown is not a signal
                status = None
        if isinstance(status, int) and status < 0:
            return False                                    # killed by a signal (the OOM killer, say): a crash, whatever it left
        return os.path.lexists(run.workspace / plan_mod.PLAN_FILE) or run.parser.has_closing_text()

    def cancel(self, handle) -> None:
        run = self._runs.get(handle.run_id)
        if run is None:
            return
        run.ended_early = False                             # PE stopped it (a budget, a tamper, a crash): never "ended early"
        try:
            self.containment.kill(run.cs)                   # raises if it cannot prove the session empty
        except BaseException:
            self._keep_tail(run, handle.run_id)             # the hard-to-kill case is the one whose tail matters most
            raise
        try:
            # A turn that is killed (a budget, a crash, a tamper) still used the model and still said something: keep it and charge it.
            run.parser.feed(self._drain(run), final=True)
            self._record(run, run.parser.finish(), keep_plan=False)       # a stopped turn's plan is not offered to the operator
        except Exception:                                   # noqa: BLE001 -- recording must never block the kill that already happened
            pass
        self._keep_tail(run, handle.run_id)
        self._release(run)
        del self._runs[handle.run_id]

    def collect(self, handle) -> AgentResult:
        run = self._runs[handle.run_id]
        if not self.containment.is_empty(run.cs):
            raise RuntimeError("session is not provably empty; refusing to collect")
        try:
            run.parser.feed(self._drain(run), final=True)
            result = run.parser.finish()
            # A turn stopped part-way never publishes a plan; one that ended early by itself (rc14.4) does: the agent finished writing it.
            self._record(run, result, keep_plan=not result.interrupted or run.ended_early)
            reply = result.reply or "(the agent gave no reply)"
            return AgentResult(diagnosis=reply[:8000], confidence="low" if result.interrupted else "medium")
        finally:
            self._keep_tail(run, handle.run_id)
            self._release(run)
            self._runs.pop(handle.run_id, None)

    # ── what is kept of a turn ────────────────────────────────────────────────────────────
    def _record(self, run: _Run, result, *, keep_plan: bool = True) -> None:
        conv_id = run.conversation_id
        if result.reply:
            self.console.add_message(conv_id, "agent", result.reply, turn=run.turn)
        if run.ended_early:
            pass                                       # rc14.4: said below, after the plan is read, so the note names what is really shown
        elif result.interrupted:
            self.console.add_message(conv_id, "system", "This turn was stopped before it finished.", turn=run.turn)
        elif not result.reply:
            self.console.add_message(conv_id, "system", "The agent gave no reply.", turn=run.turn)
        if result.session_id:
            self.console.set_agent_session(conv_id, result.session_id)         # the id the stream confirmed: it can change, so every turn
        tokens = result.tokens
        if self.broker is not None:
            usage = self.broker.usage(run.cs.session_id)           # the broker counts what the vendor charged: that is the budget
            if usage is not None:
                # A request cut off mid-stream is finalised by the broker after this look: count what it was promised too, so
                # interrupting turns over and over cannot dodge the conversation's budget (conservative, never an undercount).
                tokens = usage["tokens"] + usage.get("reserved", 0)
        self.console.add_usage(conv_id, tokens)                    # before anything that can fail: what the vendor charged is always counted
        planned = False
        if keep_plan:
            planned = self._plan(run)
            try:
                self._lessons(run)
            except Exception:
                log.exception("console lessons for %s failed", conv_id)
        if run.ended_early:
            shown = " and ".join(w for w, there in (("plan", planned), ("reply", bool(result.reply))) if there)
            self.console.add_message(conv_id, "system", "The agent's session ended before it signed off"
                                     + (f"; its {shown} {'are' if ' and ' in shown else 'is'} shown." if shown else "."), turn=run.turn)

    def _plan(self, run: _Run) -> bool:
        """Read the turn's plan. True when one was stored for the operator."""
        if not os.path.lexists(run.workspace / plan_mod.PLAN_FILE):
            return False                                               # the agent proposed no plan this turn
        inventory = self._inventory()
        if inventory is None:
            # The agent was told it cannot name a scope: a plan written anyway is refused, never taken on faith.
            self.console.add_message(run.conversation_id, "system", "The agent's plan was refused: this host's inventory is not available, so its scope cannot be checked.", turn=run.turn)
            return False
        try:
            plan = plan_mod.load_file(str(run.workspace), inventory=inventory,                # a link, a directory, a FIFO or a bad file is a refused plan, never a crash
                                      available=self._checks())
        except plan_mod.PlanError as exc:
            self._refused(run, str(exc))
            return False
        self.console.add_plan(run.conversation_id, run.turn, plan_mod.canonical(plan), plan_mod.plan_hash(plan))
        return True

    def _refused(self, run: _Run, reason: str) -> None:
        """A refused plan goes back to the planner (rc14.2): PE queues the next turn of the same conversation itself, attributed to
        `CONSOLE_PLAN_RETRY` (never the operator), with the refusal (PlanError never echoes agent text). At most MAX_PLAN_RETRIES per
        operator message: past that, the operator sees the refusal as before.
        The operator sees a short note either way."""
        conv_id = run.conversation_id
        tries = self.console.core_messages_since_operator(conv_id, CONSOLE_PLAN_RETRY)
        conv = self.console.get(conv_id)
        if tries < MAX_PLAN_RETRIES and conv.state == "open":
            which = "the plan" if tries == 0 else "the corrected plan"
            self.console.add_message(conv_id, "system", f"PE refused {which} ({reason}) and asked the agent to fix it.", turn=run.turn)
            self.console.add_message(conv_id, "core", plan_mod.RETRY_PROMPT.format(reason=reason), actor=CONSOLE_PLAN_RETRY)
            return
        after = f" (after {tries} automatic attempt{'s' if tries != 1 else ''} to have it fixed)" if tries else ""
        self.console.add_message(conv_id, "system", f"The agent's plan was refused{after}: {reason}", turn=run.turn)

    def _lessons(self, run: _Run) -> None:
        """`lessons.json` becomes memory CANDIDATES (quarantined: nothing uses them until the operator keeps them). A refused or secret-shaped
        lesson is dropped and said so, without echoing it."""
        if not os.path.lexists(run.workspace / lessons_mod.LESSONS_FILE):
            return
        conv_id = run.conversation_id

        def say(text: str) -> None:
            self.console.add_message(conv_id, "system", text, turn=run.turn)
        if not self.console.memory_ready():
            say("The agent offered lessons, but Ship's Memory is not available on this host, so none was kept.")
            return
        try:
            found = lessons_mod.load_file(str(run.workspace), self._inventory())
        except lessons_mod.LessonError as exc:
            say(f"The agent's lessons were refused: {exc}")
            return
        room = lessons_mod.MAX_PER_CONVERSATION - len(self.console.lesson_ids(conv_id))
        stored = secret = other = 0
        now = datetime.fromtimestamp(float(self.console.store._clock()), UTC)
        for lesson in found.lessons:
            if stored >= room:
                other += 1
                continue
            try:
                card = lessons_mod.to_card(conv_id, lesson, now)
                with self.console.store._write() as conn:
                    if memory_store.get_card(conn, card.id) is not None:
                        continue                                   # already offered (waiting, kept or discarded): never rewritten under the operator's eyes
                    memory_store.upsert_candidate(conn, card, lessons_mod.actor(conv_id), now=now)
                    self.console.link_lesson(conn, conv_id, card.id)   # the card and its link are one write: no orphan candidate
                stored += 1
            except memory_store.SecretShapedContent:
                secret += 1
            except Exception:                                      # noqa: BLE001 -- an odd lesson is dropped, never fatal to the turn
                other += 1
        if stored:
            say(f"The agent noted {stored} thing{'s' if stored != 1 else ''} worth remembering. Open {'them' if stored != 1 else 'it'} in the Console to keep, edit or discard: nothing is remembered until you keep it.")
        if secret:
            say(f"{secret} lesson{'s were' if secret != 1 else ' was'} refused: {'they' if secret != 1 else 'it'} looked like {'they held' if secret != 1 else 'it held'} a secret.")
        if other:
            say(f"{other} lesson{'s were' if other != 1 else ' was'} not kept for review (too many, or {'they' if other != 1 else 'it'} could not be stored).")

    # ── the workspace and the pipe ────────────────────────────────────────────────────────
    def _inventory(self):
        if self.inventory is None:
            return None
        try:
            return self.inventory()
        except Exception:                                           # noqa: BLE001 -- an unreadable inventory means no plan can name a scope, never a wider one
            return None

    def _checks(self):
        """The checks this host can run, or None (every catalogue kind) when no source is wired. A source that fails offers NO kind
        that needs something optional: an unreadable answer must not widen what a plan may name."""
        if self.checks is None:
            return None
        try:
            return self.checks()
        except Exception:                                           # noqa: BLE001
            from planet_express.missions.verify.checks import Availability
            return Availability({"exec_probe": "unknown", "query": "unknown", "project_complete": "unknown"})

    def _inventory_text(self) -> str:
        inv = self._inventory()
        if inv is None:
            # The other hosts do not depend on this host's Repair controls (Codex, release C).
            return ("The host inventory is not available, so a plan cannot name a scope: describe what you would do and do not write "
                    "plan.json.\n" + self._other_hosts_text())
        return ("Stacks on this host:\n" + "".join(f"- {s}\n" for s in inv.stacks) + "\nServices (as <stack>/<service>):\n"
                + "".join(f"- {s}\n" for s in inv.services) + self._other_hosts_text())

    def _other_hosts_text(self) -> str:
        if self.other_hosts is None:
            return ""
        try:
            return self.other_hosts()
        except Exception:                                           # noqa: BLE001 -- another host must never stop a turn
            return "\nOther hosts: not available this turn.\n"

    @staticmethod
    def _prepare_workspace(workspace: Path, brief: str, message: str, inventory_text: str = "", lessons_text: str = "",
                           plan_text: str | None = None) -> None:
        if not stat.S_ISDIR(workspace.lstat().st_mode):             # made by the supervisor; never created or followed here
            raise ValueError("workspace path is not a directory")
        files = {"BRIEF.md": brief, "PLAN_FORMAT.md": plan_text if plan_text is not None else plan_mod.instructions(), "INVENTORY.md": inventory_text,
                 "LESSONS_FORMAT.md": lessons_text}
        if len(message) > MAX_ARG_PROMPT:
            files["MESSAGE.md"] = clean_text(message)
        for name in ("BRIEF.md", "PLAN_FORMAT.md", "INVENTORY.md", "LESSONS_FORMAT.md", "MESSAGE.md", plan_mod.PLAN_FILE, lessons_mod.LESSONS_FILE):
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
