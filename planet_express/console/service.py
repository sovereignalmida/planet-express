"""The Console's conversation lifecycle: create, post, interrupt, archive, and the once-per-tick work that runs them.

Everything that is safety stays in the mission machinery this sits on. A conversation is a mission of runtime `console`, tier
observe, whose sessions the supervisor starts, supervises and kills; the conversation's one uid is reserved for its whole life
(`MissionStore.reserve_uid`) and returned to the pool only after its state directory has been emptied and removed. This module
decides WHEN: an operator's message starts a turn at the next tick, an idle conversation is archived after the configured days, an
archive empties the state directory with a cleanup session as the same uid and only then frees the uid.
"""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from config_schema import MissionConsoleConfig
from planet_express.console.store import ConsoleStore, Conversation, clean_text
from planet_express.missions.models import Grant
from planet_express.missions.state import TERMINAL, IllegalTransition, transition
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


class BudgetSpent(ConsoleConflict):
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
        self._failures: dict[str, int] = {}

    # ── what the operator does ────────────────────────────────────────────────────────────
    def create(self, operator: str, text: str, *, incident_id: str | None = None, title: str | None = None) -> Conversation:
        """Open a conversation with the operator's first message. The first turn starts at the next tick."""
        actor = _actor(operator)
        if not text.strip():
            raise ConsoleError("a conversation starts with a message")
        if self.console.count_open() >= self.cfg.max_open:
            raise ConsoleFull(f"{self.cfg.max_open} conversations are open: archive one first")
        uid_range = self._uid_range()
        if uid_range is None:
            raise ConsoleError("this host has no session identities: the Console cannot run")
        grant = Grant.model_validate({"tier": "observe", "budgets": {"wall_s": self.cfg.turn_wall_s, "tool_calls": self.cfg.turn_tool_calls,
                                                                      "retries": 0}})
        mission = self.ms.create_mission(title=clean_text(title or text, 120).splitlines()[0], origin="incident" if incident_id else "operator",
                                         incident_id=incident_id, grant=grant, goal=clean_text(text, 4000),
                                         success_criteria="Answer the operator and help them decide.", runtime="console", actor=actor)
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
        if conv.tokens_left < self.cfg.min_turn_tokens:
            raise BudgetSpent("this conversation's model budget is used up: start a new conversation")
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
        if reason == "start_failed" and conv.tokens_left < self.cfg.min_turn_tokens:
            why = "This conversation's model budget is used up. Start a new conversation to go on."
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
            if not self.supervisor.has_run(conv.mission_id):
                self.archive(conv.id, "system")
        for conv in self.console.conversations(states=("archiving",)):
            try:
                self._archive_step(conv)
            except Exception:
                log.exception("console archive of %s failed", conv.id)

    # ── internals ─────────────────────────────────────────────────────────────────────────
    def _followup(self, conv: Conversation) -> None:
        mission = self.ms.get_mission(conv.mission_id)
        if mission.status not in ("awaiting_decision", "needs_operator") or self.supervisor.has_run(mission.id):
            return                                       # the first turn starts from `queued` by the supervisor itself; a running turn finishes first
        message = self.console.pending(conv.id)
        if message is None or not message.actor or not message.actor.startswith("operator:"):
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
                self.console.add_message(conv.id, "system", f"PE could not start this turn: {str(exc)[:160]}")
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


__all__ = ["BudgetSpent", "ConsoleConflict", "ConsoleError", "ConsoleFull", "ConsoleService"]
