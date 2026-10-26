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
                 cfg: MissionConsoleConfig, *, clock: Callable[[], float] = time.time):
        self.service, self.console, self.ms, self.supervisor, self.cfg, self._clock = service, console, ms, supervisor, cfg, clock

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
        used_pct = min(100, round(100 * conv.tokens_used / conv.token_budget)) if conv.token_budget else 100
        spent = conv.tokens_left < self.cfg.min_turn_tokens
        return {
            "conversation": {"id": conv.id, "title": conv.title, "origin": conv.origin, "state": conv.state, "turns": conv.turns,
                             "status": {k: view[k] for k in ("text", "level", "group")}, "verdict": view["verdict"]},
            "budget": {"used_pct": used_pct, "tokens_used": conv.tokens_used, "token_budget": conv.token_budget, "tokens_left": conv.tokens_left,
                       "state": "stop" if spent else "warn" if used_pct >= 80 else "ok"},
            "running": running, "composer": self._composer(conv, running, spent, status, status_view),
            "messages": [{"seq": m.seq, "role": m.role, "turn": m.turn, "at": m.at, "text": clean_text(m.text, MAX_TEXT),
                          "by": (m.actor or "").removeprefix("operator:") or None} for m in messages],
            "last_message": messages[-1].seq if messages else after_message,
            "events": events, "last_event": last_event, "turn_starts": turn_starts,
            "plan": self._plan(plan), "status": status,
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
            raise ConsoleApiError("that conversation no longer exists", "not_found") from None
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
            raise ConsoleApiError("that conversation no longer exists", "not_found") from None
        except ConsoleError as exc:
            raise ConsoleApiError(str(exc)) from None

    def archive(self, conversation_id: str, operator: str) -> dict:
        self._conversation(conversation_id)
        try:
            return {"archiving": self.service.archive(conversation_id, operator)}
        except ConversationNotFound:
            raise ConsoleApiError("that conversation no longer exists", "not_found") from None
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

    # ── internals ─────────────────────────────────────────────────────────────────────────
    def _conversation(self, conversation_id: str) -> Conversation:
        try:
            return self.console.get(conversation_id)
        except ConversationNotFound:
            raise ConsoleApiError("that conversation no longer exists", "not_found") from None

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
