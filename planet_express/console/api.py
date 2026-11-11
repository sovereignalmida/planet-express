"""What the dashboard and Telegram may ask of the Console: read models (the list, one conversation's live feed) and the operator's
actions (open, say something, stop a turn, archive, scrap a plan).

Everything returned is redacted and bounded again here, whatever the store holds; every word of status text is written by PE from
state and counts, never taken from the agent. The acting operator is always the authenticated identity the caller passes, built
into `operator:<name>` by the service. Nothing here approves, widens or executes anything: a plan card is read-only until the
confirm step exists (P2), and "scrap" only marks a plan as not wanted.
"""

from __future__ import annotations

import bisect
import json
import time
from collections.abc import Callable
from typing import Any

from config_schema import MissionConsoleConfig
from planet_express.console import (
    execution as execution_mod,
    lessons as lessons_mod,
    plan as plan_mod,
)
from planet_express.console.service import ConsoleConflict, ConsoleError, ConsoleService
from planet_express.console.store import (
    is_closed,
    ConsoleStore,
    Conversation,
    ConversationClosed,
    ConversationNotFound,
    clean_text,
)
from planet_express.memory import store as memory_store
from planet_express.memory.models import parse_actor
from planet_express.missions import holds
from planet_express.missions.approval_flow import FlowError, phase_demands_elevation
from planet_express.missions.events import redact_value
from planet_express.missions.state import TERMINAL
from planet_express.missions.store import MissionStore
from planet_express.missions.supervisor import Supervisor

MAX_MESSAGES = 200
MAX_EVENTS = 300
MAX_TEXT = 8_000
PLAN_TTL_S = plan_mod.PLAN_TTL_S
IDLE_MODES = ("idle", "busy", "off")


class ConsoleApiError(Exception):
    """A typed refusal. `code` is one of bad_request, not_found, conflict, unavailable, forbidden."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _level(text: str, level: str, group: str, h: str, p: str) -> dict:
    return {"text": text, "level": level, "group": group, "verdict": {"h": h, "p": p}}


def describe(conv: Conversation, mission_status: str, plan: dict | None, running: bool, last_system: str | None,
             starting: bool = False) -> dict:
    """The plain-language status of a conversation (the design brief's vocabulary): never an internal state name. Written by PE.
    `running` is a turn in progress; `starting` is one that is about to begin (queued, or the operator's message is waiting)."""
    if conv.state == "archived":
        return _level("Archived", "none", "Recent", "Archived", "This conversation is closed and its working files are gone.")
    if conv.state == "archiving":
        return _level("Archiving", "none", "Recent", "Archiving", "Stopping and cleaning up; it takes a moment.")
    if mission_status in TERMINAL:
        return _level("Closed", "none", "Recent", "This conversation was closed", "It was closed from the Work log. Start a new conversation to go on.")
    if running:
        return _level("Looking into it", "ok", "Working", "Working on it", "The agent is investigating. You can stop it at any time.")
    if starting:
        return _level("Getting ready", "none", "Working", "Getting ready", "The agent is about to start on your message.")
    if mission_status == "needs_operator":
        return _level("Didn't finish", "high", "Needs you", "The last turn didn't finish", last_system or "Send your message again, or ask something else.")
    if plan is not None:
        n = len(plan["options"])
        rec = next((o["name"] for o in plan["options"] if o.get("recommended")), None)
        return _level("Needs your call", "warn", "Needs you", f"Waiting for your choice between {n} option{'s' if n != 1 else ''}",
                      f"Recommended: {rec}. Nothing has been changed." if rec else "Nothing has been changed.")
    return _level("Result ready", "ok", "Recent", "Answer ready", "Ask a follow-up, or close the conversation when you are done.")


def _age(now: float, then: int) -> str:
    seconds = max(int(now) - int(then), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class ConsoleApi:
    def __init__(self, service: ConsoleService, console: ConsoleStore, ms: MissionStore, supervisor: Supervisor,
                 cfg: MissionConsoleConfig, *, clock: Callable[[], float] = time.time, flow: Callable[[], Any] | None = None):
        self.service, self.console, self.ms, self.supervisor, self.cfg, self._clock = service, console, ms, supervisor, cfg, clock
        self._flow = flow                      # () -> the approval flow, or None where Repair missions are not available (late-bound by the daemon)
        self.provider_usage = None             # `missions.provider_usage.ProviderUsage`, attached by the daemon: credit and spend in the header

    # ── reads ─────────────────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        report = getattr(self.supervisor, "preflight_report", None)
        configured = self.supervisor.preflight_fn is not None
        proven = not configured or bool(report is not None and report.ok)
        return {"enabled": True, "open": self.console.count_open(), "max_open": self.cfg.max_open, "token_budget": self.cfg.token_budget,
                "proposal_only": not proven,
                "provider": self.provider_usage.snapshot() if self.provider_usage is not None else None,
                "proposal_only_reason": None if proven else "The session launcher has not been proven on this host, so the agent cannot start."}

    def list(self) -> dict:
        now = self._clock()
        status = self.status()
        rows = []
        for conv in self.console.conversations(states=("open", "archiving"), limit=200):
            view = self._view(conv)
            rows.append({"id": conv.id, "title": conv.title, "origin": conv.origin, "state": conv.state, "age": _age(now, conv.last_activity_at),
                         "turns": conv.turns, "status": {k: view[k] for k in ("text", "level", "group")}})
        return {"conversations": rows, "status": status}

    def feed(self, conversation_id: str, after_message: int = 0, after_event: int = 0) -> dict:
        conv = self._conversation(conversation_id)
        view = self._view(conv)
        status = self.status()
        # A first load of a long conversation shows the NEWEST messages; later polls ask for what is after the last one seen.
        messages = (self.console.recent_messages(conv.id, MAX_MESSAGES) if after_message == 0
                    else self.console.messages(conv.id, after=after_message, limit=MAX_MESSAGES))
        events, last_event, turn_starts = self._events(conv, after_event)
        plan = view.pop("plan_row")
        running = view.pop("running")
        status_view = view["mission_closed"]
        stoppable = self.supervisor.has_run(conv.mission_id) and not status_view        # a turn that is actually running, not one still queued
        # rc14.6: the conversation's token budget is a usage mark, never a stop (live M-31/M-32: a fleet-wide investigation ran past
        # 150k in one turn). Past it the page says so; the broker's own per-turn ceilings stay the runaway-cost backstop.
        # The mark is the CURRENT config's (it only affects what is shown), not the one stored when the conversation was made.
        mark = self.cfg.token_budget
        used_pct = round(100 * conv.tokens_used / mark) if mark else 0          # may pass 100: the page draws the bar capped
        over = bool(mark) and conv.tokens_used >= mark                           # raw counts: 99.5% rounds to 100 but is not over
        return {
            "conversation": {"id": conv.id, "title": conv.title, "origin": conv.origin, "state": conv.state, "turns": conv.turns,
                             "status": {k: view[k] for k in ("text", "level", "group")}, "verdict": view["verdict"]},
            "budget": {"used_pct": used_pct, "tokens_used": conv.tokens_used, "token_budget": mark, "tokens_left": max(mark - conv.tokens_used, 0),
                       "over": over, "state": "warn" if over or used_pct >= 80 else "ok"},
            "running": running, "stoppable": stoppable, "composer": self._composer(conv, running, status, status_view),
            # PE's own `core` turns (a refused plan handed back to the planner) are not drawn: the thread has PE's short note instead.
            "messages": [{"seq": m.seq, "role": m.role, "turn": m.turn, "at": m.at, "text": clean_text(m.text, MAX_TEXT),
                          "by": (m.actor or "").removeprefix("operator:") or None} for m in messages if m.role != "core"],
            "last_message": messages[-1].seq if messages else after_message,
            "events": events, "last_event": last_event, "turn_starts": turn_starts,
            "plan": self._plan(plan), "execution": self._execution_view(conv), "lessons": self._lessons(conv), "status": status,
            "context": self._context(conv, used_pct),
        }

    # ── actions (the operator is the authenticated dashboard or Telegram identity) ─────────
    def create(self, operator: str, text: str, incident_id: str | None = None) -> dict:
        try:
            conv = self.service.create(operator, text, incident_id=incident_id)
        except ConsoleConflict as exc:
            raise ConsoleApiError(str(exc), "conflict") from None
        except ConsoleError as exc:
            raise ConsoleApiError(str(exc)) from None
        return {"id": conv.id}

    def post(self, conversation_id: str, operator: str, text: str) -> dict:
        conv = self._conversation(conversation_id)
        card = self._go_ahead(conv, operator, text)
        if card is not None:
            return card
        try:
            seq = self.service.post(conversation_id, operator, text)
        except ConversationNotFound:
            raise ConsoleApiError("That conversation no longer exists.", "not_found") from None
        except ConsoleConflict as exc:
            raise ConsoleApiError(str(exc), "conflict") from None
        except ConversationClosed as exc:
            raise ConsoleApiError(str(exc), "conflict") from None
        except ConsoleError as exc:
            raise ConsoleApiError(str(exc)) from None
        return {"seq": seq}

    def _go_ahead(self, conv: Conversation, operator: str, text: str) -> dict | None:
        """rc14.2: "do it" / "go ahead" / "authorise" (a fixed list, the whole message) while a valid plan waits is answered by PE, not
        the agent: the operator's words are recorded as already handled and the SAME Authorise card as pressing Confirm is returned for
        the recommended option (or the one named). Chat text is never the approval: nothing runs until the Authorise button. None when
        this is not a go-ahead or there is no plan it can act on (then the words go to the agent, which is asked for a plan)."""
        go, named = plan_mod.goes_ahead(text)
        if not go or is_closed(conv, self.ms.get_mission(conv.mission_id).status):
            return None
        row = self.console.current_plan(conv.id)
        if row is None:
            return None
        try:
            plan = self._pending_plan(row)
        except ConsoleApiError:
            return None                                                    # expired or unreadable: the agent is asked for a fresh one
        index = named if named is not None else next(i for i, o in enumerate(plan.options) if o.recommended)
        if not 0 <= index < len(plan.options):
            return None
        actor = f"operator:{operator}"
        seq = self.console.add_message(conv.id, "operator", text, actor=actor, require_open=True, taken=True)
        try:
            view = self.confirm_prepare(conv.id, row["id"], index, operator)
        except ConsoleApiError as exc:
            self.console.add_message(conv.id, "system", f"PE did not open the Authorise card: {exc}", actor=actor)
            raise
        self.console.add_message(conv.id, "system", f"PE opened the Authorise card for option {chr(65 + index)}. Nothing runs until you press "
                                 "Authorise; Back leaves the plan as it was.", actor=actor)
        return {"seq": seq, "confirm": view, "option": index}

    def interrupt(self, conversation_id: str, operator: str) -> dict:
        self._conversation(conversation_id)
        try:
            return {"stopped": self.service.interrupt(conversation_id, operator)}
        except ConversationNotFound:
            raise ConsoleApiError("That conversation no longer exists.", "not_found") from None
        except ConsoleError as exc:
            raise ConsoleApiError(str(exc)) from None

    def archive(self, conversation_id: str, operator: str) -> dict:
        self._conversation(conversation_id)
        try:
            return {"archiving": self.service.archive(conversation_id, operator)}
        except ConversationNotFound:
            raise ConsoleApiError("That conversation no longer exists.", "not_found") from None
        except ConsoleError as exc:
            raise ConsoleApiError(str(exc)) from None

    def _lessons(self, conv: Conversation) -> list[dict]:
        """What this conversation offered to remember, with where each stands: candidate (waiting for the operator), kept or discarded."""
        ids = self.console.lesson_ids(conv.id)
        if not ids:
            return []
        out = []
        with self.console.store._connect() as conn:
            for card_id in ids[:lessons_mod.MAX_PER_CONVERSATION]:
                card = memory_store.get_card(conn, card_id)
                if card is None:
                    continue
                out.append({"id": card.id, "title": card.title, "statement": card.statement, "scope": list(card.scope)[:8],
                            "state": {"candidate": "waiting", "confirmed": "kept", "deprecated": "discarded"}[card.status]})
        return out

    def lesson_decide(self, conversation_id: str, card_id: str, decision: str, operator: str, statement: str | None = None) -> dict:
        """Keep (optionally with the operator's own wording) or discard one lesson. Keeping is the memory store's operator-only confirm: until
        then the card is quarantined and no session sees it. The operator may decide on a conversation that has since closed."""
        conv = self._conversation(conversation_id)
        if card_id not in self.console.lesson_ids(conv.id):
            raise ConsoleApiError("That lesson is not from this conversation.", "not_found")
        if decision not in ("keep", "discard"):
            raise ConsoleApiError("that is not a decision", "bad_request")
        actor = f"operator:{operator}"
        try:
            parse_actor(actor)
        except ValueError:
            raise ConsoleApiError("Cannot identify the operator.", "forbidden") from None
        try:
            with self.console.store._write() as conn:
                current = memory_store.get_card(conn, card_id)
                if current is None:
                    raise ConsoleApiError("That lesson no longer exists.", "not_found")
                if current.status != "candidate":
                    raise ConsoleApiError("That lesson was already decided.", "conflict")
                if decision == "discard":
                    memory_store.deprecate(conn, card_id, actor, "discarded from the Console")
                else:
                    words = (statement or "").strip()
                    if words and words != current.statement:
                        if len(words) > lessons_mod.MAX_STATEMENT:
                            raise ConsoleApiError(f"Keep it under {lessons_mod.MAX_STATEMENT} characters.", "bad_request")
                        # The operator's own words: the card is now operator-stated, and still goes through the memory store's checks (secrets).
                        edited = current.model_copy(update={"statement": words, "source": "operator-stated"})
                        memory_store.upsert_candidate(conn, edited, actor, detail="edited from the Console")
                    memory_store.confirm(conn, card_id, actor)
        except ConsoleApiError:
            raise
        except memory_store.UnknownCard:
            raise ConsoleApiError("That lesson no longer exists.", "not_found") from None
        except (memory_store.InvalidTransition, memory_store.CardExistsError):
            raise ConsoleApiError("That lesson was already decided.", "conflict") from None
        except memory_store.MemoryStoreError as exc:
            raise ConsoleApiError(f"That could not be saved: {str(exc)[:160]}", "bad_request") from None
        self.console.add_message(conv.id, "system", f"{operator} {'kept' if decision == 'keep' else 'discarded'} a lesson: {current.title[:100]}",
                                 actor=actor)
        return {"decided": decision}

    def scrap_plan(self, conversation_id: str, operator: str) -> dict:
        """The operator does not want the pending plan. Nothing was changed by it, so nothing is undone; the conversation goes on."""
        conv = self._conversation(conversation_id)
        plan = self.console.current_plan(conv.id)
        if plan is None or not self.console.set_plan_status(plan["id"], "scrapped"):
            raise ConsoleApiError("there is no pending plan to scrap", "conflict")
        self.console.add_message(conv.id, "system", f"Plan scrapped by {operator}. Nothing was changed.", actor=f"operator:{operator}")
        return {"scrapped": True}

    def execution_action(self, conversation_id: str, execution_id: int, action: str, operator: str, *, ack_drift: bool = False) -> dict:
        """The operator's control on a run that needs them: roll back (trust mode removed pause, resume and take-over). It goes through the same approval-flow action the Work log
        uses (as the signed-in operator); the supervisor decides whether it can be done now. Nothing here is available to the agent."""
        conv = self._conversation(conversation_id)
        ex = self.console.execution(execution_id)
        if ex is None or ex["conversation_id"] != conv.id or ex["status"] not in ("authorised", "ended"):
            raise ConsoleApiError("That run no longer exists.", "not_found")
        if action not in execution_mod.ACTIONS:
            raise ConsoleApiError("that is not an action", "bad_request")
        flow = self._require_flow()
        actor = f"operator:{operator}"
        if action in ("recheck", "keep"):                # rc14.7: a held run checked again, or kept and closed
            try:
                (flow.recheck if action == "recheck" else flow.keep_and_close)(actor, ex["mission_id"])
            except FlowError as exc:
                raise self._flow_error(exc) from None
            if action == "recheck":                     # a kept run's one line is the reporter's, which names who kept it
                self.console.add_message(conv.id, "system", f"{operator} asked PE to check the run again.", actor=actor)
            return {"done": action, "status": self._execution_view(conv)["status"]["text"], "restore": "", "reason": ""}
        undo = self.ms.get_mission(ex["mission_id"]).status in execution_mod.UNDO_STATES
        try:
            # The one action. A restore can take minutes, so where the flow can, it is started on the supervisor's rollback worker and
            # this request returns at once ("started"); the page shows "Rolling back" and the outcome lands with the next poll.
            start = getattr(flow, "roll_back_start", None)
            restore = (start or flow.roll_back)(actor, ex["mission_id"], ack_drift=ack_drift)
        except FlowError as exc:
            raise self._flow_error(exc) from None
        self.console.add_message(conv.id, "system", f"{operator} asked PE to {'undo this change by rolling back' if undo else 'roll back'} to the checkpoint.",
                                 actor=actor)
        # A restore PE refused (typically because files changed after the checkpoint) is reported, so the page can offer the explicit
        # "roll back anyway" the operator must choose; PE never assumes it. Started in the background, the refusal is read from the
        # record instead (`rollback` in the run's view).
        return {"done": action, "status": self._execution_view(conv)["status"]["text"],
                "restore": str(restore.get("restore", "unknown"))[:20], "reason": str(restore.get("reason", ""))[:200]}

    # ── confirming a plan option: propose the repair, show the approval card, authorise bound to its hash ──────────
    def confirm_prepare(self, conversation_id: str, plan_id: int, option: int, operator: str, no_backup: bool = False) -> dict:
        """Step one of Confirm. Makes the Repair mission the option describes (the same grant checks as any Repair mission) and returns what
        the operator is about to authorise. Nothing runs until `confirm_authorise`. `no_backup` is accepted from an older page and ignored:
        trust mode has no backup floor."""
        conv = self._conversation(conversation_id)
        if is_closed(conv, self.ms.get_mission(conv.mission_id).status):
            raise ConsoleApiError("This conversation is closed.", "conflict")
        row = self.console.plan(plan_id)
        if row is None or row["conversation_id"] != conv.id:
            raise ConsoleApiError("That plan no longer exists.", "not_found")
        plan = self._pending_plan(row)
        if not 0 <= option < len(plan.options):
            raise ConsoleApiError("that option does not exist", "bad_request")
        flow = self._require_flow()
        for ex in self.console.executions(conv.id):                                  # one execution at a time per conversation
            if ex["status"] == "proposed":
                if not self._withdraw(flow, ex, operator, quiet=True):
                    raise ConsoleApiError("An earlier confirmation in this conversation is still open or already running. Finish or withdraw it first.", "conflict")
            elif ex["status"] in ("authorised", "ended") and (earlier := self.ms.get_mission(ex["mission_id"])).status not in TERMINAL:
                phase = execution_mod.run_phase(earlier, self._rolling_back(earlier), hold=holds.hold_reason(self.ms.store, earlier))
                if phase.group == "Needs you":                     # nothing is running: it waits for the operator (live M-33)
                    raise ConsoleApiError(f"The earlier run, {ex['mission_id']}, is waiting for you ({phase.label.removeprefix('Needs you: ')}). "
                                          "Open it from Missions and close or restore it, then confirm again.", "conflict")
                raise ConsoleApiError(f"A plan is already running in this conversation ({ex['mission_id']}). Wait for it to finish, or "
                                      "stop it first.", "conflict")
        chosen = plan.options[option]
        actor = f"operator:{operator}"
        try:
            made = flow.propose_repair(
                actor, title=f"{conv.title[:120]} · {chosen.name}"[:256], goal=plan_mod.goal_text(chosen),
                scope={"stacks": list(chosen.scope.stacks), "services": list(chosen.scope.services), "hosts": ["local"],
                       **({"files": list(chosen.files)} if chosen.files else {})},
                checks=[c.model_dump() for c in chosen.checks],
                incident_id=self.ms.get_mission(conv.mission_id).incident_id)
        except FlowError as exc:
            raise self._flow_error(exc) from None
        execution_id = self.console.add_execution(conv.id, plan_id, option, made["mission_id"], made["grant_hash"])
        self.console.add_message(conv.id, "system", f"{operator} is reviewing option {chr(65 + option)} ({chosen.name}) for confirmation. Nothing has been changed.",
                                 actor=actor)
        return self._confirm_view(flow, execution_id, made["mission_id"], chosen, operator)

    def confirm_authorise(self, conversation_id: str, execution_id: int, grant_hash: str, operator: str, elevated: bool = False) -> dict:
        """Step two: the operator authorises exactly the grant whose fingerprint they saw. If the grant changed in between, it is refused as
        stale and they must look again. Approval is recorded through the same flow as any other, as the signed-in operator."""
        conv = self._conversation(conversation_id)
        if is_closed(conv, self.ms.get_mission(conv.mission_id).status):
            raise ConsoleApiError("This conversation is closed.", "conflict")
        ex = self._execution(conv, execution_id, "proposed")
        flow = self._require_flow()
        mission = self.ms.get_mission(ex["mission_id"])
        if mission.status != "awaiting_decision":
            raise ConsoleApiError("That confirmation is no longer waiting for a decision (it may have been decided in the Work log).", "conflict")
        if grant_hash != mission.grant_hash or grant_hash != ex["grant_hash"]:
            raise ConsoleApiError("That grant changed since you read it. Look at it again before authorising.", "stale")
        row = self.console.plan(ex["plan_id"])
        self._pending_plan(row)                                                    # expired or scrapped since: refuse, never run it
        actor = f"operator:{operator}"
        try:
            pending = [r for r in flow.card(mission.id)["required"] if r["state"] == "pending"]
            needs_elevation = any(r["elevated"] or phase_demands_elevation(r["phase"]) for r in pending)
            if needs_elevation and not elevated:
                # Checked before any phase is approved: approving one phase and then failing on an elevated one would leave the run half-approved and stuck.
                raise ConsoleApiError("This needs an elevated session. Elevate, then authorise again; nothing was approved.", "elevation_required")
            for needed in pending:
                # The grant is bound to the fingerprint the operator saw (checked above), not to whatever the card says now.
                flow.approve(actor, mission.id, needed["phase"], grant_hash if needed["phase"] != "plan" else needed["bound"], elevated=elevated)
            after = flow.card(mission.id)
            if any(r["state"] != "approve" for r in after["required"]):
                raise ConsoleApiError("That grant is not fully approved (it was denied or still needs an approval elsewhere), so nothing was started.", "conflict")
        except FlowError as exc:
            raise self._flow_error(exc) from None
        self.console.set_execution_status(execution_id, "authorised", by=actor)
        self.console.set_plan_status(row["id"], "confirmed")
        plan = plan_mod.Plan.model_validate(json.loads(row["plan_json"]))
        self.console.add_message(conv.id, "system", f"Authorised by {operator}: option {chr(65 + ex['option_index'])} ({plan.options[ex['option_index']].name}). "
                                 f"PE will take a checkpoint, carry it out under supervision, then check it worked. Grant {grant_hash[:8]}.", actor=actor)
        return {"authorised": True, "execution_id": execution_id, "mission_id": mission.id, "used_elevation": needs_elevation}

    def confirm_withdraw(self, conversation_id: str, execution_id: int, operator: str) -> dict:
        conv = self._conversation(conversation_id)
        ex = self._execution(conv, execution_id, "proposed")
        self._withdraw(self._require_flow(), ex, operator, quiet=False)
        return {"withdrawn": True}

    # ── internals ─────────────────────────────────────────────────────────────────────────
    def _require_flow(self):
        flow = self._flow() if self._flow is not None else None
        if flow is None:
            raise ConsoleApiError("Running a plan needs Repair missions, which are not available on this host.", "unavailable")
        return flow

    @staticmethod
    def _flow_error(exc: FlowError) -> ConsoleApiError:
        code = {"conflict": "conflict", "stale": "stale", "forbidden": "forbidden", "not_found": "not_found", "unavailable": "unavailable",
                "elevation_required": "elevation_required", "invalid_grant": "bad_request"}.get(exc.code, "bad_request")
        return ConsoleApiError(str(exc), code)

    def _pending_plan(self, row: dict | None) -> plan_mod.Plan:
        """The plan a confirmation may act on: pending, within its 24 hours, and exactly what was hashed when it was stored."""
        if row is None or row["status"] != "pending":
            raise ConsoleApiError("That plan is no longer waiting for a decision.", "conflict")
        if int(self._clock()) - int(row["created_at"]) > PLAN_TTL_S:
            self.console.set_plan_status(row["id"], "expired")
            raise ConsoleApiError("That plan has expired. Ask the agent for a fresh one.", "conflict")
        if plan_mod.stored_before_trust_mode(row["plan_json"]):
            # Stored before trust mode: a restart-only option meant a narrower run than the one that would start now. Not silently upgraded.
            raise ConsoleApiError("That plan was made before an update to what a confirmed plan may do. Ask the agent for a fresh one.",
                                  "conflict")
        try:
            plan = plan_mod.Plan.model_validate(json.loads(row["plan_json"]))
        except ValueError:
            raise ConsoleApiError("That plan could not be read.", "conflict") from None
        if plan_mod.plan_hash(plan) != row["plan_hash"]:
            raise ConsoleApiError("That plan does not match what was recorded. Ask the agent for a fresh one.", "conflict")
        return plan

    def _execution(self, conv: Conversation, execution_id: int, status: str) -> dict:
        ex = self.console.execution(execution_id)
        if ex is None or ex["conversation_id"] != conv.id:
            raise ConsoleApiError("That confirmation no longer exists.", "not_found")
        if ex["status"] != status:
            raise ConsoleApiError("That confirmation is no longer open.", "conflict")
        return ex

    def _withdraw(self, flow, ex: dict, operator: str, *, quiet: bool) -> bool:
        """Deny the proposed repair. True only if it really is withdrawn. If it was approved from the Work log in the meantime it is a live run and
        stays one (the confirmation becomes that run); if it could not be denied for another reason it is left as it was."""
        try:
            flow.deny(f"operator:{operator}", ex["mission_id"], ex["grant_hash"])
        except FlowError as exc:
            status = self.ms.get_mission(ex["mission_id"]).status
            approved = status == "awaiting_decision" and all(r["state"] == "approve" for r in flow.card(ex["mission_id"])["required"])
            if status == "cancelled":
                pass                                                          # denied or withdrawn elsewhere: nothing runs
            elif status != "awaiting_decision" or approved:                   # approved from the Work log: the supervisor starts it on its next tick
                self.console.set_execution_status(ex["id"], "authorised", by="operator:work-log")
                if not quiet:
                    raise ConsoleApiError("That was already approved from the Work log and is running. Follow it here.", "conflict") from None
                return False
            else:
                if not quiet:
                    raise self._flow_error(exc) from None
                return False
        self.console.set_execution_status(ex["id"], "withdrawn")
        if not quiet:
            self.console.add_message(ex["conversation_id"], "system", f"{operator} withdrew the confirmation. Nothing was changed.", actor=f"operator:{operator}")
        return True

    def _execution_view(self, conv: Conversation) -> dict | None:
        """The latest confirmed plan's run, live from its Repair mission: plain status, a short timeline, PE's verification
        and which controls apply now. Computed from state at every feed, so it is never stale and never the agent's account."""
        shown = [e for e in self.console.executions(conv.id) if e["status"] in ("authorised", "ended")]
        if not shown:
            return None
        ex = shown[-1]
        mission = self.ms.get_mission(ex["mission_id"])
        rolling = self._rolling_back(mission)
        waiting = execution_mod.start_waiting(self.ms, mission)
        hold = holds.hold_reason(self.ms.store, mission)
        phase = execution_mod.run_phase(mission, rolling, waiting, hold)  # the one place the run's words and actions come from
        text, level = phase.label, phase.level
        rollback = execution_mod.rollback_view(self.ms, mission, rolling)
        row = self.console.plan(ex["plan_id"])
        name = ""
        if row is not None:
            try:
                name = json.loads(row["plan_json"])["options"][ex["option_index"]]["name"]
            except (ValueError, KeyError, IndexError):
                name = ""
        return {"id": ex["id"], "option": ex["option_index"], "name": name, "fingerprint": ex["grant_hash"][:8], "status": {"text": text, "level": level},
                "timeline": execution_mod.timeline(mission, ex["authorised_by"], waiting),
                "verification": execution_mod.verification_view(mission), "controls": execution_mod.controls(mission, rolling, hold),
                "finished": mission.status in execution_mod.TERMINALISH and not rolling, "rollback": rollback,
                # A completed run's rollback is offered as "Undo this change" (rc14.2).
                "undo": execution_mod.can_undo(mission),
                "not_covered": execution_mod.not_covered_view(self.ms, mission),
                # "Keep working on it" is not offered while the run is waiting to be, or being, rolled back.
                "rolling_back": rolling or mission.status == "rollback_pending"}

    def _rolling_back(self, mission) -> bool:
        """A rollback of this run is running, from wherever it was started (the supervisor's own record)."""
        try:
            flow = self._flow() if self._flow is not None else None
        except Exception:                                  # noqa: BLE001 - a status read never breaks the feed
            return False
        return execution_mod.rollback_running(flow, mission.id)

    def _context(self, conv: Conversation, used_pct: int) -> dict:
        """The side panel, read from PE's own records (the mission's grant, its event log, the run's checkpoint), never from the agent's account.
        Scope and limits are those of the latest confirmed run when there is one, otherwise of the conversation's own read-only mission."""
        runs = [e for e in self.console.executions(conv.id) if e["status"] in ("authorised", "ended")]
        own = self.ms.get_mission(conv.mission_id)
        mission = self.ms.get_mission(runs[-1]["mission_id"]) if runs else own
        grant = mission.grant()
        ids = [conv.mission_id] + [r["mission_id"] for r in runs]       # the whole record: the conversation's own mission and every run's
        events = self.ms.activity_count(ids)
        active = bool(runs) and mission.status not in execution_mod.TERMINALISH
        return {
            "evidence": {"incident": own.incident_id, "turns": conv.turns, "budget_used_pct": used_pct},
            "scope": {"kind": "run" if active else "last_run" if runs else "read_only", "stacks": list(grant.scope.stacks),
                      "services": list(grant.scope.services)},
            "limits": {"kind": "run" if runs else "conversation", "token_budget": self.cfg.token_budget, "minutes": max(1, grant.budgets.wall_s // 60), "steps": grant.budgets.tool_calls},
            "record": {"events": events, "checkpoint": mission.checkpoint_id is not None, "runs": len(runs)},
        }

    def _confirm_view(self, flow, execution_id: int, mission_id: str, option: plan_mod.Option, operator: str) -> dict:
        """What the operator is about to authorise, in plain words, from the approval card PE built (never from the agent's text)."""
        card = flow.card(mission_id)
        budgets = card["budgets"]
        pending = [r for r in card["required"] if r["state"] == "pending"]
        return {
            "execution_id": execution_id, "mission_id": mission_id, "grant_hash": card["grant_hash"], "fingerprint": card["grant_hash"][:8],
            "operator": operator, "option": option.name, "summary": option.summary,
            "scope": {"stacks": card["scope"]["stacks"], "services": card["scope"]["services"],
                      **({"files": card["scope"]["files"]} if card["scope"].get("files") else {})},
            "warnings": [w["text"] for w in card["warnings"] if w["kind"] != "full_access"],
            "access": "Full access to this host (docker, sudo), as you have at a terminal. It will not wipe disks, delete Docker volumes "
                      "or data, or touch PE's own config, secrets or database.",
            "will_not_touch": [*option.will_not_touch, *option.exclude][:32],
            "limits": {"minutes": max(1, budgets["wall_s"] // 60), "steps": budgets["tool_calls"]},
            "checks": [plan_mod.check_phrase(c) for c in option.checks], "on_verify_fail": card["verification"]["on_verify_fail"],
            "needs_elevation": any(r["elevated"] for r in pending), "phases": [r["phase"] for r in pending],
        }

    def _conversation(self, conversation_id: str) -> Conversation:
        try:
            return self.console.get(conversation_id)
        except ConversationNotFound:
            raise ConsoleApiError("That conversation no longer exists.", "not_found") from None

    def _view(self, conv: Conversation) -> dict:
        mission = self.ms.get_mission(conv.mission_id)
        closed = mission.status in TERMINAL
        in_flight = not closed and (self.supervisor.has_run(conv.mission_id) or mission.status == "investigating")
        waiting = self.console.pending(conv.id) is not None and mission.status in ("queued", "draft", "awaiting_decision", "needs_operator")
        starting = not closed and not in_flight and (mission.status in ("queued", "draft") or waiting)
        pending = self.console.current_plan(conv.id)
        if pending is not None and int(self._clock()) - int(pending["created_at"]) > PLAN_TTL_S:
            self.console.set_plan_status(pending["id"], "expired")            # an expired plan is never pending again: nothing may act on it
            pending = None
        plan = json.loads(pending["plan_json"]) if pending else None
        last_system = self.console.last_system_text(conv.id) if mission.status == "needs_operator" else None
        out = describe(conv, mission.status, plan, in_flight, last_system, starting)
        shown = pending or self.console.latest_plan(conv.id)                  # the card still shows an expired plan, as expired
        out.update(running=in_flight or starting, plan_row=shown if shown and shown["status"] in ("pending", "expired") else None, mission_closed=closed)
        if not in_flight and not starting and not closed:
            runs = [e for e in self.console.executions(conv.id) if e["status"] in ("authorised", "ended")]
            if runs:                                                      # an active or troubled run is never hidden behind "Result ready"
                run = self.ms.get_mission(runs[-1]["mission_id"])
                override = execution_mod.conversation_status(run, rolling_back=self._rolling_back(run),
                                                             waiting=execution_mod.start_waiting(self.ms, run),
                                                             hold=holds.hold_reason(self.ms.store, run))
                # A finished run never hides a newer plan waiting for the operator, or a closing conversation.
                if override and not (override["group"] == "Recent" and (pending is not None or conv.state != "open")):
                    out.update(override)
        return out

    def _composer(self, conv: Conversation, running: bool, status: dict, mission_closed: bool = False) -> dict:
        if mission_closed or conv.state != "open":                   # `is_closed`, with the mission's status already read by _view
            return {"mode": "off", "reason": "This conversation is closed."}
        if status["proposal_only"]:
            return {"mode": "off", "reason": status["proposal_only_reason"]}
        if running:
            return {"mode": "busy", "reason": "Working. Your message waits for this turn to finish, or press Stop to interrupt it."}
        return {"mode": "idle", "reason": None}

    def plan_card(self, row: dict | None) -> dict | None:
        """A stored plan row as the card the operator reads (the same shape the page gets)."""
        return self._plan(row)

    def _plan(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        plan = redact_value(json.loads(row["plan_json"]))
        age = int(self._clock()) - int(row["created_at"])
        return {"id": row["id"], "turn": row["turn"], "status": row["status"], "created_at": row["created_at"], "hash": row["plan_hash"][:8],
                "expired": row["status"] == "expired" or age > PLAN_TTL_S, "plan": plan}

    def _events(self, conv: Conversation, after: int) -> tuple[list[dict], int, list[int]]:
        """The agent's activity for the trail, redacted and bounded, each tagged with the turn it belongs to (turns are the
        investigating transitions of the conversation's mission)."""
        with self.ms.store._connect() as conn:
            starts = [r[0] for r in conn.execute(
                "SELECT seq FROM mission_events WHERE mission_id=? AND kind='state' AND json_extract(body_json, '$.to')='investigating' ORDER BY seq",
                (conv.mission_id,))]
            if after == 0:      # a first load of a busy conversation shows the NEWEST activity, not the first
                rows = list(reversed(conn.execute("SELECT seq, at, kind, actor, body_json FROM mission_events WHERE mission_id=? "
                                                  "AND kind IN ('note','tool') AND actor='worker' ORDER BY seq DESC LIMIT ?",
                                                  (conv.mission_id, MAX_EVENTS)).fetchall()))
            else:
                rows = conn.execute("SELECT seq, at, kind, actor, body_json FROM mission_events WHERE mission_id=? AND seq>? "
                                    "AND kind IN ('note','tool') AND actor='worker' ORDER BY seq LIMIT ?",
                                    (conv.mission_id, after, MAX_EVENTS)).fetchall()
        out = []
        for seq, at, kind, _actor, body_json in rows:
            try:
                body = json.loads(body_json)
            except ValueError:
                continue
            step = self._step(kind, body)
            if step is not None:
                out.append({"seq": seq, "at": at, "turn": bisect.bisect_right(starts, seq), **step})
        return out, (rows[-1][0] if rows else after), starts

    @staticmethod
    def _step(kind: str, body: dict) -> dict | None:
        """One trail entry from a stored event body: a call, what it returned, text as it streams, or a hook's refusal."""
        def short(value: Any, limit: int = 240) -> str:
            return clean_text(str(value), limit)
        if "delta" in body:
            return {"type": "delta", "text": short(body["delta"], 2000)}
        if "call" in body:
            args = body.get("args") if isinstance(body.get("args"), dict) else {}
            detail = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("path") or ""
            return {"type": "call", "name": short(body["call"], 64), "detail": short(detail),
                    "kind": "changed" if body.get("changes_files") else "read" if body.get("read_only") else "ran"}
        if "result_of" in body:
            return {"type": "result", "name": short(body["result_of"], 64), "excerpt": short(body.get("excerpt", ""), 600),
                    "failed": bool(body.get("is_error"))}
        if body.get("via") == "hook" and body.get("verdict") in ("deny", "block", "blocked", "denied"):
            return {"type": "denied", "name": short(body.get("tool", "a tool"), 64), "detail": short(body.get("input", "")),
                    "why": short(body.get("reason", "PE does not allow this in a conversation."), 240)}
        if "omitted" in body:
            return {"type": "omitted", "count": int(body["omitted"]) if isinstance(body["omitted"], int) else 0}
        return None


__all__ = ["ConsoleApi", "ConsoleApiError", "describe"]
