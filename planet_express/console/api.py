"""What the dashboard and Telegram may ask of the Console: read models (the list, one conversation's live feed) and the operator's
actions (open, say something, stop a turn, archive, scrap a plan).

Everything returned is redacted and bounded again here, whatever the store holds; every word of status text is written by PE from
state and counts, never taken from the agent. The acting operator is always the authenticated identity the caller passes, built
into `operator:<name>` by the service. Nothing here approves, widens or executes anything: a plan card is read-only until the
confirm step exists (P2), and "scrap" only marks a plan as not wanted.
"""

import bisect
import json
import time
from collections.abc import Callable
from typing import Any

from config_schema import MissionConsoleConfig
from planet_express.console.service import ConsoleConflict, ConsoleError, ConsoleService
from planet_express.console.store import (
    ConsoleStore,
    Conversation,
    ConversationClosed,
    ConversationNotFound,
    clean_text,
)
from planet_express.console import execution as execution_mod
from planet_express.console import plan as plan_mod
from planet_express.missions.approval_flow import FlowError, phase_demands_elevation
from planet_express.missions.events import redact_value
from planet_express.missions.state import TERMINAL
from planet_express.missions.store import MissionStore
from planet_express.missions.supervisor import Supervisor

MAX_MESSAGES = 200
MAX_EVENTS = 300
MAX_TEXT = 8_000
PLAN_TTL_S = 24 * 3600           # plan section 9: a plan is valid for 24 hours or until its evidence changes
IDLE_MODES = ("idle", "busy", "off")


class ConsoleApiError(Exception):
    """A typed refusal. `code` is one of bad_request, not_found, conflict, unavailable, forbidden."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _level(text: str, level: str, group: str, h: str, p: str) -> dict:
    return {"text": text, "level": level, "group": group, "verdict": {"h": h, "p": p}}


def describe(conv: Conversation, mission_status: str, plan: dict | None, running: bool, last_system: str | None,
             min_turn_tokens: int, starting: bool = False) -> dict:
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
    if conv.tokens_left < min_turn_tokens:
        return _level("Budget used up", "warn", "Recent", "This conversation's model budget is used up",
                      "Start a new conversation to go on. Everything said so far stays here.")
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

    # ── reads ─────────────────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        report = getattr(self.supervisor, "preflight_report", None)
        configured = self.supervisor.preflight_fn is not None
        proven = not configured or bool(report is not None and report.ok)
        return {"enabled": True, "open": self.console.count_open(), "max_open": self.cfg.max_open, "token_budget": self.cfg.token_budget,
                "min_turn_tokens": self.cfg.min_turn_tokens, "proposal_only": not proven,
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
        used_pct = min(100, round(100 * conv.tokens_used / conv.token_budget)) if conv.token_budget else 100
        spent = conv.tokens_left < self.cfg.min_turn_tokens
        return {
            "conversation": {"id": conv.id, "title": conv.title, "origin": conv.origin, "state": conv.state, "turns": conv.turns,
                             "status": {k: view[k] for k in ("text", "level", "group")}, "verdict": view["verdict"]},
            "budget": {"used_pct": used_pct, "tokens_used": conv.tokens_used, "token_budget": conv.token_budget, "tokens_left": conv.tokens_left,
                       "state": "stop" if spent else "warn" if used_pct >= 80 else "ok"},
            "running": running, "stoppable": stoppable, "composer": self._composer(conv, running, spent, status, status_view),
            "messages": [{"seq": m.seq, "role": m.role, "turn": m.turn, "at": m.at, "text": clean_text(m.text, MAX_TEXT),
                          "by": (m.actor or "").removeprefix("operator:") or None} for m in messages],
            "last_message": messages[-1].seq if messages else after_message,
            "events": events, "last_event": last_event, "turn_starts": turn_starts,
            "plan": self._plan(plan), "execution": self._execution_view(conv), "status": status,
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
        self._conversation(conversation_id)
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

    def scrap_plan(self, conversation_id: str, operator: str) -> dict:
        """The operator does not want the pending plan. Nothing was changed by it, so nothing is undone; the conversation goes on."""
        conv = self._conversation(conversation_id)
        plan = self.console.current_plan(conv.id)
        if plan is None or not self.console.set_plan_status(plan["id"], "scrapped"):
            raise ConsoleApiError("there is no pending plan to scrap", "conflict")
        self.console.add_message(conv.id, "system", f"Plan scrapped by {operator}. Nothing was changed.", actor=f"operator:{operator}")
        return {"scrapped": True}

    def execution_action(self, conversation_id: str, execution_id: int, action: str, operator: str, *, ack_drift: bool = False) -> dict:
        """The operator's controls on a running plan: pause, resume, roll back, take over. Each goes through the same approval-flow action the Work log
        uses (as the signed-in operator); the supervisor decides whether it can be done now. Nothing here is available to the agent."""
        conv = self._conversation(conversation_id)
        ex = self.console.execution(execution_id)
        if ex is None or ex["conversation_id"] != conv.id or ex["status"] not in ("authorised", "ended"):
            raise ConsoleApiError("That run no longer exists.", "not_found")
        if action not in execution_mod.ACTIONS:
            raise ConsoleApiError("that is not an action", "bad_request")
        flow = self._require_flow()
        actor = f"operator:{operator}"
        restore: dict = {}
        try:
            if action == "pause":
                flow.pause(actor, ex["mission_id"])
            elif action == "resume":
                flow.resume(actor, ex["mission_id"])
            elif action == "rollback":
                restore = flow.roll_back(actor, ex["mission_id"], ack_drift=ack_drift)
            else:
                flow.take_over(actor, ex["mission_id"])
        except FlowError as exc:
            raise self._flow_error(exc) from None
        said = {"pause": "paused the run", "resume": "resumed the run", "rollback": "asked PE to roll back to the checkpoint",
                "takeover": "took over: the agent was stopped and the host is theirs. PE checks nothing until you roll back or start a new plan."}[action]
        self.console.add_message(conv.id, "system", f"{operator} {said}.", actor=actor)
        out = {"done": action, "status": self._execution_view(conv)["status"]["text"]}
        if action == "rollback":
            # A restore PE refused (typically because files changed after the checkpoint) is reported, so the page can offer the explicit
            # "roll back anyway" the operator must choose; PE never assumes it.
            out.update(restore=str(restore.get("restore", "unknown"))[:20], reason=str(restore.get("reason", ""))[:200])
        return out

    # ── confirming a plan option: propose the repair, show the approval card, authorise bound to its hash ──────────
    def confirm_prepare(self, conversation_id: str, plan_id: int, option: int, operator: str, no_backup: bool = False) -> dict:
        """Step one of Confirm. Makes the Repair mission the option describes (the same grant checks, backup floor and protected set as any
        Repair mission; nothing here widens them) and returns what the operator is about to authorise. Nothing runs until `confirm_authorise`."""
        conv = self._conversation(conversation_id)
        if conv.state != "open":
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
            elif ex["status"] == "authorised" and self._mission_open(ex["mission_id"]):
                raise ConsoleApiError("A plan is already running in this conversation. Wait for it to finish, or stop it first.", "conflict")
        chosen = plan.options[option]
        actor = f"operator:{operator}"
        try:
            made = flow.propose_repair(
                actor, title=f"{conv.title[:120]} · {chosen.name}"[:256], goal=plan_mod.goal_text(chosen),
                scope={"stacks": list(chosen.scope.stacks), "services": list(chosen.scope.services), "hosts": ["local"]},
                checks=[c.model_dump() for c in chosen.checks], exclusions=[*chosen.exclude, *chosen.will_not_touch][:32],
                incident_id=self.ms.get_mission(conv.mission_id).incident_id, acknowledged_no_backup=no_backup)
        except FlowError as exc:
            if exc.code == "backup_floor" and not no_backup:
                raise ConsoleApiError("This scope has no recent verified backup, so PE will not run it by default. You can prepare it anyway: "
                                      "you would be accepting that risk, and it needs an elevated approval.", "no_backup") from None
            raise self._flow_error(exc) from None
        execution_id = self.console.add_execution(conv.id, plan_id, option, made["mission_id"], made["grant_hash"])
        self.console.add_message(conv.id, "system", f"{operator} is reviewing option {chr(65 + option)} ({chosen.name}) for confirmation. Nothing has been changed.",
                                 actor=actor)
        return self._confirm_view(flow, execution_id, made["mission_id"], chosen, operator)

    def confirm_authorise(self, conversation_id: str, execution_id: int, grant_hash: str, operator: str, elevated: bool = False) -> dict:
        """Step two: the operator authorises exactly the grant whose fingerprint they saw. If the grant changed in between, it is refused as
        stale and they must look again. Approval is recorded through the same flow as any other, as the signed-in operator."""
        conv = self._conversation(conversation_id)
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
                # Checked before any phase is approved: approving "grant" and then failing on "no_backup" would leave the run half-approved and stuck.
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
                "elevation_required": "elevation_required", "invalid_grant": "bad_request", "backup_floor": "bad_request"}.get(exc.code, "bad_request")
        return ConsoleApiError(str(exc), code)

    def _mission_open(self, mission_id: str) -> bool:
        return self.ms.get_mission(mission_id).status not in TERMINAL

    def _pending_plan(self, row: dict | None) -> plan_mod.Plan:
        """The plan a confirmation may act on: pending, within its 24 hours, and exactly what was hashed when it was stored."""
        if row is None or row["status"] != "pending":
            raise ConsoleApiError("That plan is no longer waiting for a decision.", "conflict")
        if int(self._clock()) - int(row["created_at"]) > PLAN_TTL_S:
            self.console.set_plan_status(row["id"], "expired")
            raise ConsoleApiError("That plan has expired. Ask the agent for a fresh one.", "conflict")
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
        """The latest confirmed plan's run, live from its Repair mission: plain status, a short timeline, what a pause was about, PE's verification
        and which controls apply now. Computed from state at every feed, so it is never stale and never the agent's account."""
        shown = [e for e in self.console.executions(conv.id) if e["status"] in ("authorised", "ended")]
        if not shown:
            return None
        ex = shown[-1]
        mission = self.ms.get_mission(ex["mission_id"])
        pause = None
        if mission.status == "paused_tripwire":
            from planet_express.missions.tripwire.monitor import pause_card
            pause = execution_mod.pause_view(pause_card(self.ms, mission.id))
        text, level = execution_mod.repair_status(mission, pause["who"] if pause and pause["by_operator"] else None)
        row = self.console.plan(ex["plan_id"])
        name = ""
        if row is not None:
            try:
                name = json.loads(row["plan_json"])["options"][ex["option_index"]]["name"]
            except (ValueError, KeyError, IndexError):
                name = ""
        return {"id": ex["id"], "option": ex["option_index"], "name": name, "fingerprint": ex["grant_hash"][:8], "status": {"text": text, "level": level},
                "timeline": execution_mod.timeline(mission, ex["authorised_by"]), "pause": pause,
                "verification": execution_mod.verification_view(mission), "controls": execution_mod.controls(mission),
                "finished": mission.status in execution_mod.TERMINALISH}

    def _confirm_view(self, flow, execution_id: int, mission_id: str, option: plan_mod.Option, operator: str) -> dict:
        """What the operator is about to authorise, in plain words, from the approval card PE built (never from the agent's text)."""
        card = flow.card(mission_id)
        budgets = card["budgets"]
        pending = [r for r in card["required"] if r["state"] == "pending"]
        return {
            "execution_id": execution_id, "mission_id": mission_id, "grant_hash": card["grant_hash"], "fingerprint": card["grant_hash"][:8],
            "operator": operator, "option": option.name, "summary": option.summary,
            "scope": {"stacks": card["scope"]["stacks"], "services": card["scope"]["services"]},
            "warnings": [w["text"] for w in card["warnings"]],
            "backup": {"covered": bool(card["backup"].get("covered")),
                       "text": "A recent verified backup covers this scope." if card["backup"].get("covered") else
                               "No recent verified backup covers this scope, and you are accepting that risk. This needs an elevated approval."},
            "protected": {"count": card["protected"]["count"], "enforced": card["protected"]["exclusions_enforced"],
                          "advisory": card["protected"]["exclusions_advisory"]},
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
        out = describe(conv, mission.status, plan, in_flight, last_system, self.cfg.min_turn_tokens, starting)
        shown = pending or self.console.latest_plan(conv.id)                  # the card still shows an expired plan, as expired
        out.update(running=in_flight or starting, plan_row=shown if shown and shown["status"] in ("pending", "expired") else None, mission_closed=closed)
        if not in_flight and not starting and not closed:
            runs = [e for e in self.console.executions(conv.id) if e["status"] in ("authorised", "ended")]
            if runs:                                                      # an active or troubled run is never hidden behind "Result ready"
                override = execution_mod.conversation_status(self.ms.get_mission(runs[-1]["mission_id"]), runs[-1]["status"] == "authorised")
                # A finished run never hides a newer plan waiting for the operator, or a closing conversation.
                if override and not (override["group"] == "Recent" and (pending is not None or conv.state != "open")):
                    out.update(override)
        return out

    def _composer(self, conv: Conversation, running: bool, spent: bool, status: dict, mission_closed: bool = False) -> dict:
        if conv.state != "open" or mission_closed:
            return {"mode": "off", "reason": "This conversation is closed."}
        if status["proposal_only"]:
            return {"mode": "off", "reason": status["proposal_only_reason"]}
        if spent:
            return {"mode": "off", "reason": "This conversation's model budget is used up. Start a new conversation."}
        if running:
            return {"mode": "busy", "reason": "Working. Your message waits for this turn to finish, or press Stop to interrupt it."}
        return {"mode": "idle", "reason": None}

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
