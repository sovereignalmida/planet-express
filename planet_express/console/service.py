"""The Console's conversation lifecycle: create, post, interrupt, archive, and the once-per-tick work that runs them.

Everything that is safety stays in the mission machinery this sits on. A conversation is a mission of runtime `console`, tier
observe, whose sessions the supervisor starts, supervises and kills; the conversation's one uid is reserved for its whole life
(`MissionStore.reserve_uid`) and returned to the pool only after its state directory has been emptied and removed. This module
decides WHEN: an operator's message starts a turn at the next tick, an idle conversation is archived after the configured days, an
archive empties the state directory with a cleanup session as the same uid and only then frees the uid.
"""

import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from config_schema import MissionConsoleConfig
from planet_express.console.store import ConsoleStore, Conversation, clean_text
from planet_express.missions.models import Grant
from planet_express.missions.events import insert_event
from planet_express.missions.state import CONSOLE_PLAN_RETRY, TERMINAL, IllegalTransition, transition, transition_in_txn
from planet_express.missions.store import MissionStore, NoFreeUid
from planet_express.missions.supervisor import (
    ProposalOnly,
    RepairNotReady,
    StartDeferred,
    Supervisor,
    UnsupportedTier,
)

log = logging.getLogger(__name__)
FOLLOWUP_FAILURES = 3
SECONDS_PER_DAY = 86_400
ADOPT_GRACE_S = 30            # a confirmation approved elsewhere is adopted once its run has been going this long
_WHY = {
    "crash": "The agent stopped unexpectedly.", "recover": "PE restarted while this turn was running, so it was stopped.",
    "start_failed": "The turn could not start.", "hook_tamper": "PE stopped the turn: the agent's own settings were changed.",
    "budget:wall": "The turn reached its time limit.", "budget:tool_calls": "The turn reached its limit on tool use.",
    "budget:output_bytes": "The turn reached its output limit.", "lease_lost": "The turn lost its place and was stopped.",
    "interrupt_failed": "The turn could not be stopped cleanly.", "state_changed": "The turn was stopped because its record changed.",
    "result_invalid": "The agent's answer could not be read.", "runtime_error": "The turn failed.",
}


class ConsoleError(RuntimeError):
    """A request the Console refuses. The message is for the operator."""


class ConsoleConflict(ConsoleError):
    """The request is fine but the conversation's state does not allow it now (full, spent, closed)."""


class ConsoleFull(ConsoleConflict):
    pass


def _actor(operator: str) -> str:
    name = operator.removeprefix("operator:").strip()
    if not name or len(name) > 100 or any(c in name for c in "\0\r\n"):
        raise ConsoleError("an operator name is required")
    return f"operator:{name}"


class ConsoleService:
    def __init__(self, ms: MissionStore, console: ConsoleStore, supervisor: Supervisor, cfg: MissionConsoleConfig, *,
                 uid_range: Callable[[], tuple[int, int] | None], clock: Callable[[], float]):
        self.ms, self.console, self.supervisor, self.cfg = ms, console, supervisor, cfg
        self._uid_range, self._clock = uid_range, clock
        self._create_lock = threading.Lock()          # the open-conversation cap is checked and the conversation made as one step
        self._failures: dict[str, int] = {}
        self.after_tick: Callable[[], None] | None = None          # the Telegram push, attached by the daemon

    # ── what the operator does ────────────────────────────────────────────────────────────
    def create(self, operator: str, text: str, *, incident_id: str | None = None, title: str | None = None) -> Conversation:
        """Open a conversation with the operator's first message. The first turn starts at the next tick."""
        actor = _actor(operator)
        if not text.strip():
            raise ConsoleError("a conversation starts with a message")
        with self._create_lock:
            return self._create(actor, text, incident_id, title)

    def _create(self, actor: str, text: str, incident_id: str | None, title: str | None) -> Conversation:
        if self.console.count_open() >= self.cfg.max_open:
            raise ConsoleFull(f"{self.cfg.max_open} conversations are open: archive one first")
        uid_range = self._uid_range()
        if uid_range is None:
            raise ConsoleError("this host has no session identities: the Console cannot run")
        grant = Grant.model_validate({"tier": "observe", "budgets": {"wall_s": self.cfg.turn_wall_s, "tool_calls": self.cfg.turn_tool_calls,
                                                                      "retries": 0}})
        try:
            mission = self.ms.create_mission(title=clean_text(title or text, 120).splitlines()[0], origin="incident" if incident_id else "operator",
                                             incident_id=incident_id, grant=grant, goal=clean_text(text, 4000),
                                             success_criteria="Answer the operator and help them decide.", runtime="console", actor=actor)
        except sqlite3.IntegrityError:
            raise ConsoleError("that incident no longer exists") from None
        try:
            try:
                uid = self.ms.reserve_uid(mission.id, uid_range)
            except NoFreeUid:
                raise ConsoleFull("no session identity is free for a new conversation") from None
            conv = self.console.create(mission.id, title=title or text, origin="incident" if incident_id else "operator",
                                       token_budget=self.cfg.token_budget, uid=uid)
            self.console.add_message(conv.id, "operator", text, actor=actor)
            transition(self.ms.store, mission.id, "queued", actor)          # last: nothing starts until the record is whole
        except BaseException:
            self.ms.release_uid_reservation(mission.id)
            try:
                transition(self.ms.store, mission.id, "cancelled", "core")
            except IllegalTransition:
                pass
            raise
        return self.console.get(conv.id)

    def post(self, conversation_id: str, operator: str, text: str) -> int:
        """The operator's next message. It is the agent's next turn at the next tick, or the one after the running turn."""
        actor = _actor(operator)
        if not text.strip():
            raise ConsoleError("a message cannot be empty")
        conv = self.console.get(conversation_id)
        if self.ms.get_mission(conv.mission_id).status in TERMINAL:
            raise ConsoleConflict("this conversation was closed from the Work log: start a new conversation")
        return self.console.add_message(conversation_id, "operator", text, actor=actor, require_open=True)

    def interrupt(self, conversation_id: str, operator: str) -> bool:
        """Stop the running turn and keep what it said. The conversation stays open. False if no turn was running."""
        actor = _actor(operator)
        conv = self.console.get(conversation_id)
        return self.supervisor.interrupt_turn(conv.mission_id, actor)

    def archive(self, conversation_id: str, operator: str) -> bool:
        """Close the conversation: no more messages. Its state directory is emptied and its uid freed over the next ticks."""
        actor = _actor(operator) if operator != "system" else "system"
        if not self.console.set_state(conversation_id, "archiving"):
            return False
        self.console.add_message(conversation_id, "system", "This conversation was archived." if actor != "system"
                                 else "This conversation was archived after being idle.", actor=actor)
        return True

    # ── the supervisor's callbacks ────────────────────────────────────────────────────────
    def turn_ended(self, mission_id: str, reason: str, had_result: bool) -> None:
        tail = getattr(self.supervisor, "last_session_tail", {}).pop(mission_id, None)     # rc14.4: why it died, kept as an artifact
        conv = self.console.by_mission(mission_id)
        if conv is None or had_result or reason in ("cancel", "result"):
            return
        if reason == "start_failed":
            # The start took the operator's message and failed: it is not retried by itself (that would be a loop that adds a message
            # every tick). The operator sees why and sends it again, or something else.
            message = self.console.pending(conv.id)
            if message is not None:
                self.console.consume(message.seq, conv.turns)
        why = _WHY.get(reason, "The turn did not finish.")
        if tail:
            why += " What the session printed last is kept with this conversation's run record (session-tail.txt)."
        self.console.add_message(conv.id, "system", why, turn=conv.turns or None)

    def tick(self) -> None:
        """Once per supervisor tick: start follow-up turns, archive the idle, advance the archives. One conversation's trouble never
        stops the others."""
        for conversation_id in self.console.has_pending():
            try:
                self._followup(self.console.get(conversation_id))
            except Exception:
                log.exception("console follow-up for %s failed", conversation_id)
        cutoff = int(self._clock()) - self.cfg.archive_after_days * SECONDS_PER_DAY
        for conv in self.console.idle_since(cutoff):
            # Not while a confirmed run is unfinished: archiving drops it from the list and from Telegram, and its outcome would
            # never be pushed (rc14.7.5).
            if not self.supervisor.has_run(conv.mission_id) and not self._run_unsettled(conv.id):
                self.archive(conv.id, "system")
        for conv in self.console.conversations(states=("archiving",)):
            try:
                self._archive_step(conv)
            except Exception:
                log.exception("console archive of %s failed", conv.id)
        try:
            self._report_executions()
        except Exception:
            log.exception("console execution reporting failed")
        if self.after_tick is not None:
            try:
                self.after_tick()
            except Exception:
                log.exception("console after-tick hook failed")

    def _report_executions(self) -> None:
        """Tell each run's thread, once per change, how the run is going, in the words of its phase (`execution.PHASES`). Driven by the
        mission's state events, not by whether the run is "ended": a completed run can still be undone, and a held one kept or closed,
        long after it ended (rc14.7.5; until then an undo's outcome never reached the conversation or Telegram)."""
        from planet_express.console import execution as ex_mod
        self._reconcile_proposed()
        for ex in self.console.reportable_executions():
            mission = self.ms.get_mission(ex["mission_id"])
            keys = self.console.reported_keys(ex["id"])
            mark = self._watermark(mission.id)
            if f"seen@{mark}" in keys:
                continue                                  # nothing happened to it since the thread last heard
            seq, actor = ex_mod.latest_state_event(self.ms, mission.id)
            phase = ex_mod.run_phase(mission, ex_mod.rollback_running(self.supervisor, mission.id))
            key = f"{phase.name}@{seq}"
            # A run reported before rc14.7.5 carries the old keys (`completed`, `ended:<status>`) and no `seen@`: what it already
            # posted is recorded under the new key and not posted again.
            legacy = bool(keys) and not any("@" in k for k in keys)
            old_key = mission.status if mission.status in ("executing", "verifying", "completed") else f"ended:{mission.status}"
            if legacy and old_key in keys:
                self.console.mark_reported(ex["id"], key)
            elif key not in keys:
                line = ex_mod.thread_line(phase, mission, ex_mod.verification_view(mission), actor)
                if line:
                    self.console.add_message(ex["conversation_id"], "system", line)     # post first, then record: a failed insert is retried
                    self.console.mark_reported(ex["id"], key)
            self.console.mark_seen(ex["id"], mark)
            if ex["status"] == "authorised" and mission.status in ex_mod.TERMINALISH:
                self.console.set_execution_status(ex["id"], "ended")

    def _watermark(self, mission_id: str) -> int:
        """The newest state or decision event of the mission: what the thread has heard of once it is recorded as `seen@`."""
        with self.ms.store._connect() as conn:
            row = conn.execute("SELECT MAX(seq) FROM mission_events WHERE mission_id=? AND kind IN ('state', 'decision')",
                               (mission_id,)).fetchone()
        return int(row[0] or 0)

    def _reconcile_proposed(self) -> None:
        """A confirmation the operator has not authorised here follows what happened to it elsewhere (rc14.7.5):
        - its Repair mission was cancelled (denied in the Work log): withdrawn;
        - it was approved in the Work log and has started: authorised, so the conversation follows the run (it never showed before);
        - its plan was scrapped, superseded or expired, or the conversation closed, while nobody approved it: the waiting Repair
          mission is cancelled and the confirmation withdrawn. Before, it stayed waiting for a decision in the Work log."""
        from planet_express.console import plan as plan_mod
        from planet_express.missions.approvals import evaluate
        now = int(self._clock())
        for ex in self.console.proposed_executions():
            try:
                mission = self.ms.get_mission(ex["mission_id"])
                if mission.status == "cancelled":
                    self.console.set_execution_status(ex["id"], "withdrawn")
                    continue
                if mission.status != "awaiting_decision":
                    # Approved elsewhere and started. A moment's grace: the Console's own Authorise approves, then marks the
                    # confirmation authorised itself, and must not be beaten to it and misattributed.
                    if now - int(mission.updated_at) >= ADOPT_GRACE_S:
                        who = self._approver(mission.id, mission.grant_hash) or "operator:work-log"
                        if self.console.set_execution_status(ex["id"], "authorised", by=who):
                            self.console.add_message(ex["conversation_id"], "system",
                                                     f"Authorised from the Work log by {who.removeprefix('operator:')}. PE runs it and "
                                                     "reports back here.")
                    continue
                plan = self.console.plan(ex["plan_id"])
                conv = self.console.get(ex["conversation_id"])
                if conv.state != "open":
                    gone = "the conversation was closed"
                elif plan is None or plan["status"] in ("superseded", "scrapped", "expired"):
                    gone = f"its plan was {plan['status'] if plan else 'removed'}"
                elif plan["status"] == "pending" and now - int(plan["created_at"]) > plan_mod.PLAN_TTL_S:
                    gone = "its plan expired"
                else:
                    continue
                with self.ms.store._write() as conn:      # checked and cancelled in one transaction: an approval cannot land between
                    if evaluate(conn, mission.id, now):    # fully approved in the Work log (the supervisor's own predicate): it
                        continue                          # starts, and is adopted above
                    transition_in_txn(conn, mission.id, "cancelled", "system", now)
                    insert_event(conn, mission.id, "decision", "system",
                                 {"event": "console.confirmation_withdrawn", "why": gone}, now)
                self.console.set_execution_status(ex["id"], "withdrawn")
                self.console.add_message(ex["conversation_id"], "system", f"The confirmation was withdrawn because {gone}. Nothing was changed.")
            except IllegalTransition:
                continue                                  # it moved on meanwhile; the next tick sees where to
            except Exception:
                log.exception("console confirmation %s could not be reconciled", ex["id"])

    def _approver(self, mission_id: str, grant_hash: str) -> str | None:
        with self.ms.store._connect() as conn:
            row = conn.execute("SELECT operator FROM mission_approvals WHERE mission_id=? AND phase='grant' AND bound_hash=? "
                               "AND decision='approve' ORDER BY decided_at DESC LIMIT 1", (mission_id, grant_hash)).fetchone()
        return row["operator"] if row else None

    def _run_unsettled(self, conversation_id: str) -> bool:
        """A confirmed run of this conversation is not finished (still going, held for the operator, or being rolled back)."""
        return any(ex["status"] in ("authorised", "ended") and self.ms.get_mission(ex["mission_id"]).status not in TERMINAL
                   for ex in self.console.executions(conversation_id))

    # ── internals ─────────────────────────────────────────────────────────────────────────
    def _followup(self, conv: Conversation) -> None:
        mission = self.ms.get_mission(conv.mission_id)
        if mission.status not in ("awaiting_decision", "needs_operator") or self.supervisor.has_run(mission.id):
            return                                       # the first turn starts from `queued` by the supervisor itself; a running turn finishes first
        message = self.console.pending(conv.id)
        if message is None or not message.actor:
            return
        auto = message.role == "core" and message.actor == CONSOLE_PLAN_RETRY
        if not auto and not (message.role == "operator" and message.actor.startswith("operator:")):
            return
        if auto and mission.status != "awaiting_decision":
            # PE's own retry runs only on a conversation that is simply waiting. Otherwise it is dropped, once, and the operator sees
            # why: it never waits to retry later, so it can never loop.
            if self.console.consume(message.seq, conv.turns):
                self.console.add_message(conv.id, "system", "PE did not ask the agent to fix its plan: the last turn did not finish.")
            return
        try:
            self.supervisor.start_followup(mission.id, message.actor)
            self._failures.pop(conv.id, None)
        except (StartDeferred, ProposalOnly, RepairNotReady):
            return                                       # not yet: the message waits and is tried again next tick
        except (UnsupportedTier, IllegalTransition, ValueError) as exc:
            count = self._failures.get(conv.id, 0) + 1
            self._failures[conv.id] = count
            if count >= FOLLOWUP_FAILURES:
                self.console.consume(message.seq, conv.turns)
                from planet_express.missions.approval_flow import plain_error
                self.console.add_message(conv.id, "system", f"PE could not start this turn: {plain_error(exc, 160)}")
                self._failures.pop(conv.id, None)

    def _archive_step(self, conv: Conversation) -> None:
        mission = self.ms.get_mission(conv.mission_id)
        if self.supervisor.has_run(mission.id):
            self.supervisor.interrupt_turn(mission.id, "system")           # a turn still running is stopped, and collected, first
            return
        if mission.status not in TERMINAL:
            try:
                transition(self.ms.store, mission.id, "completed" if mission.status in ("investigating", "awaiting_decision") else "cancelled",
                           "system")
            except IllegalTransition:
                return
        path = self.supervisor.workspace_dir / conv.state_dir
        uid = conv.uid if conv.uid is not None else self.ms.reserved_uid(mission.id)
        if uid is None:
            shutil.rmtree(path, ignore_errors=True)
            cleared = not path.exists()
        else:
            cleared = self.supervisor.clear_tree(conv.state_dir, Path(path), uid)
        if cleared and (self.ms.release_uid_reservation(mission.id) or self.ms.reserved_uid(mission.id) is None):
            self.console.set_state(conv.id, "archived")


__all__ = ["ConsoleConflict", "ConsoleError", "ConsoleFull", "ConsoleService"]
