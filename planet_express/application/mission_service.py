"""Operator surfaces for missions and Ship's Memory (v4 slice 4).

Everything an operator can do to a mission or a memory card through the dashboard RPC or Telegram goes
through here. It reads, creates Observe/Propose missions, asks for a cancel and confirms or deprecates memory cards. It has no
worker-facing code and never touches the execution engine, policy or the host-mutation lock. Since slice 7a it also
fronts the approval flow (`missions.approval_flow.ApprovalFlow`): the card, approve/deny bound to the grant hash,
standing grants and the operator's rollback of a Repair mission. The flow is attached by the daemon; without it those
methods answer "unavailable". Elevated approvals need `elevated: true`, which only the dashboard sends and only from an
authenticated elevated session; Telegram never can.

Rules every method follows:
- Gated by `enabled()`; when the flag is off every call raises MissionError("disabled").
- Output is redacted (strings are masked, secret-looking keys withheld) and bounded (page sizes, text and
  JSON size), whatever the stored row holds.
- The acting identity is built here as `operator:<name>` from the authenticated dashboard or Telegram
  identity. No caller-supplied `actor` exists, and a `kind:name` string is not a valid operator name.
"""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, get_args

from pydantic import ValidationError

from planet_express.core.redact import redact
from planet_express.core.store import Store
from planet_express.memory import retrieval as memory_retrieval, store as memory_store
from planet_express.memory.models import SLUG_RE, Card
from planet_express.missions import holds
from planet_express.missions.approval_flow import ApprovalFlow, FlowError
from planet_express.missions.events import insert_event, redact_value
from planet_express.missions.models import (
    Budgets,
    Grant,
    GrantScope,
    Mission,
    MissionEvent,
    Status,
)
from planet_express.missions.state import (
    TERMINAL,
    IllegalTransition,
    MissionNotFound,
    transition_in_txn,
)
from planet_express.missions.store import MissionStore

log = logging.getLogger("planetexpress.missions.service")

MAX_PAGE = 100
MAX_LIST = 500               # mission.list only: the board needs every open mission, not just a page of them
MAX_EVENT_PAGE = 200
MAX_OFFSET = 100_000
MAX_SEARCH = 20
MAX_TEXT = 4096              # longest free text returned for one field
MAX_JSON = 8192              # longest JSON document returned for one field
MAX_ARTIFACTS = 50
# The RPC frame is 1 MiB. A page is cut to this many bytes of encoded JSON (leaving room for the rest of the
# response), and says so, so a well-populated store never turns an operator call into an internal error.
RESPONSE_BUDGET = 512 * 1024
DEFAULT_RUNTIME = "command"
CREATABLE_TIERS = ("observe", "propose")

# Defined once; the dashboard imports these rather than keeping copies.
MISSION_ID_RE = re.compile(r"M-[0-9]{1,9}")
INCIDENT_ID_RE = re.compile(r"[0-9a-f]{12}")
CONVERSATION_ID_RE = re.compile(r"[0-9a-f]{16}")
_OPERATOR = re.compile(r"[a-z0-9_.-]{1,32}")
_PREFERRED_ORDER = (
    "draft", "queued", "investigating", "awaiting_decision", "blocked", "needs_operator", "executing", "verifying",
    "recovery_required", "rollback_pending", "completed", "failed", "rolled_back", "cancelled")
# Board order: the preferred order, then any status the model gains later, so a new status shows up on its own.
STATUS_ORDER = _PREFERRED_ORDER + tuple(x for x in get_args(Status) if x not in _PREFERRED_ORDER)
_STATUSES = frozenset(STATUS_ORDER)
TERMINAL_STATUSES = frozenset(TERMINAL)
_TERMINAL_SQL = ",".join(f"'{x}'" for x in sorted(TERMINAL_STATUSES))


class MissionError(Exception):
    """A typed refusal. `code` is one of disabled, bad_request, not_found, forbidden, conflict,
    unavailable, timeout, and (approval flow) stale, elevation_required, invalid_grant."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


# ── output shaping ────────────────────────────────────────────────────────────
def _field_max(name: str) -> int:
    """The `le` bound the Budgets model declares for `name`: the grant model, not the config, owns the maxima."""
    for meta in Budgets.model_fields[name].metadata:
        if getattr(meta, "le", None) is not None:
            return meta.le
    raise AssertionError(f"Budgets.{name} has no upper bound")


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _text(value: str | None, limit: int = MAX_TEXT) -> str | None:
    """Redact the whole string first, then clip, so a cut can never leave half a secret behind."""
    return None if value is None else _clip(redact(value), limit)


def _fit(items: list[Any]) -> tuple[list[Any], bool]:
    """The longest prefix of `items` whose encoded JSON fits RESPONSE_BUDGET (always at least one item),
    and whether anything was left out."""
    kept, used = [], 0
    for item in items:
        size = len(json.dumps(item))
        if kept and used + size > RESPONSE_BUDGET:
            return kept, True
        kept.append(item)
        used += size
    return kept, False


def _json(raw: str | None, limit: int = MAX_JSON) -> Any:
    """A stored JSON document, redacted and bounded. Unreadable or oversize documents become a marker.
    Size is measured as encoded on the wire (non-ASCII escaped), which is what the RPC frame counts."""
    if raw is None:
        return None
    try:
        value = redact_value(json.loads(raw))
    except ValueError:
        return {"unreadable": True}
    dumped = json.dumps(value, sort_keys=True)
    if len(dumped) > limit:
        return {"truncated": True, "preview": _clip(dumped, limit // 2)}
    return value


MAX_PROTECTED_SHOWN = 32    # protected resources returned with a mission's grant; the count says how many there are


def _console_owners(conn, mission_ids: list[str]) -> dict[str, str]:
    """mission id -> the Console conversation it belongs to (the conversation's own mission, or a run it started). Empty where
    the Console's tables do not exist (missions on, Console never enabled)."""
    if not mission_ids:
        return {}
    marks = ",".join("?" * len(mission_ids))
    out: dict[str, str] = {}
    try:
        for row in conn.execute(f"SELECT mission_id, id FROM console_conversations WHERE mission_id IN ({marks})", mission_ids):
            out[row[0]] = row[1]
        for row in conn.execute(f"SELECT mission_id, conversation_id FROM console_executions WHERE mission_id IN ({marks})", mission_ids):
            out[row[0]] = row[1]
    except sqlite3.OperationalError:                       # no Console tables on this install
        return {}
    return out


def _grant_out(raw: str) -> Any:
    """The stored grant for display. A grant stored before trust mode carries the site's protected set, which can run to hundreds
    of volume paths and would push the whole grant past MAX_JSON and hide its scope; it is shortened, with its count."""
    try:
        grant = json.loads(raw)
        resources = grant["protected"]["resources"]
    except (ValueError, KeyError, TypeError):
        return _json(raw)
    if isinstance(resources, list) and len(resources) > MAX_PROTECTED_SHOWN:
        grant["protected"] = {"resources": resources[:MAX_PROTECTED_SHOWN], "count": len(resources), "shortened": True}
        return _json(json.dumps(grant))
    return _json(raw)


def _event_out(event: MissionEvent) -> dict[str, Any]:
    return {"seq": event.seq, "at": event.at, "kind": event.kind, "actor": _text(event.actor, 128),
            "body": _json(event.body_json)}


def _mission_summary(m: Mission) -> dict[str, Any]:
    return {"id": m.id, "title": _text(m.title, 256), "status": m.status, "tier": m.tier, "origin": m.origin,
            "incident_id": m.incident_id, "priority": m.priority, "runtime": m.runtime,
            "created_at": m.created_at, "updated_at": m.updated_at, "closed_at": m.closed_at,
            "cancel_requested": m.cancel_requested_at is not None, "outcome": _text(m.outcome, 512),
            "has_checkpoint": bool(m.checkpoint_id)}


def _card_out(card: Card) -> dict[str, Any]:
    return redact_value(card.model_dump(mode="json"))


# ── parameter validation ──────────────────────────────────────────────────────
def _params(params: Any, required: dict[str, Callable], optional: dict[str, Callable] | None = None) -> dict:
    """Exact-key validation: every required key present, no key outside required+optional, each value
    passing its checker. Checkers return the (possibly normalised) value or raise ValueError."""
    optional = optional or {}
    if not isinstance(params, dict):
        raise MissionError("Invalid params")
    unknown = set(params) - set(required) - set(optional)
    missing = set(required) - set(params)
    if unknown or missing:
        raise MissionError("Invalid params")
    out = {}
    for name, check in {**required, **optional}.items():
        if name not in params:
            continue
        try:
            out[name] = check(params[name])
        except ValueError as exc:
            raise MissionError(f"Invalid {name}: {exc}") from None
    return out


def _str(max_len: int, *, blank: bool = False) -> Callable[[Any], str]:
    def check(value):
        if not isinstance(value, str) or len(value) > max_len or (not blank and not value.strip()):
            raise ValueError(f"expected text of at most {max_len} characters")
        return value
    return check


def _int(low: int, high: int) -> Callable[[Any], int]:
    def check(value):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"expected an integer from {low} to {high}")
        return value
    return check


def _pattern(regex: re.Pattern, what: str) -> Callable[[Any], str]:
    def check(value):
        if not isinstance(value, str) or regex.fullmatch(value) is None:
            raise ValueError(f"expected {what}")
        return value
    return check


def _choice(*allowed: str) -> Callable[[Any], str]:
    def check(value):
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("unsupported value")
        return value
    return check


def _tags(value):
    if (not isinstance(value, list) or len(value) > 16
            or not all(isinstance(t, str) and 0 < len(t) <= 160 for t in value)):
        raise ValueError("expected at most 16 tags")
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError("expected true or false")
    return value


_PHASE = re.compile(r"(grant|plan|broad_sudo|no_backup|critical:[0-9]{1,6})")


_OPERATOR_CHECK = _pattern(_OPERATOR, "a plain operator name")
_MISSION_CHECK = _pattern(MISSION_ID_RE, "a mission id such as M-12")
_PAGE = {"limit": _int(1, MAX_PAGE), "offset": _int(0, MAX_OFFSET)}


def _actor(operator: str) -> str:
    return f"operator:{operator}"


from planet_express.console.telegram import COMMANDS as CONSOLE_COMMANDS  # noqa: E402


class MissionService:
    def __init__(self, store: Store, *, enabled: Callable[[], bool] | bool = False,
                 runtime: str = DEFAULT_RUNTIME, budgets: Callable[[], Any] | None = None):
        self.store = store
        self._budgets = budgets          # `missions.budgets.*`, read when a grant is created (so a reload is honoured)
        self.missions = MissionStore(store)
        self._enabled = enabled if callable(enabled) else (lambda: bool(enabled))
        self._runtime = runtime
        self.console_api = None          # `console.api.ConsoleApi`, attached by the daemon when missions.console is enabled
        self.console_telegram = None     # `console.telegram.ConsoleTelegram`, attached with it when a Telegram notifier exists
        self.provider_usage = None       # `missions.provider_usage.ProviderUsage`, attached by the daemon when the broker is on
        self.flow: ApprovalFlow | None = None      # attached by the daemon (slice 7a); None: approvals are unavailable

    # ── gate ────────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        return bool(self._enabled())

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise MissionError("Missions are disabled on this host (missions.enabled is false)", "disabled")

    def _mission(self, mission_id: str) -> Mission:
        try:
            return self.missions.get_mission(mission_id)
        except MissionNotFound:
            raise MissionError("Mission not found", "not_found") from None

    # ── missions ────────────────────────────────────────────────────────────
    def list_missions(self, *, status: str | None = None, scope: str | None = None, limit: int = 50,
                      offset: int = 0) -> dict:
        """`scope` is "active" (every non-terminal mission) or "closed" (terminal ones), newest first. The
        board asks for all active ones so an old awaiting-decision mission never drops off behind recent
        closed ones."""
        self._require_enabled()
        if status and scope:
            raise MissionError("Give a status or a scope, not both")
        if status:
            where, args = "WHERE status=?", (status,)
        elif scope:
            op = "NOT IN" if scope == "active" else "IN"
            where, args = f"WHERE status {op} ({_TERMINAL_SQL})", ()
        else:
            where, args = "", ()
        with self.store._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM missions {where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM missions {where} ORDER BY updated_at DESC, CAST(SUBSTR(id, 3) AS INTEGER) DESC "
                "LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            # Which conversation a mission belongs to (4.0.1): a Console conversation is a mission, and so is each run it
            # starts. NEEDS YOU asks the Console about those instead of reading their mission status (an answered
            # conversation sits in awaiting_decision, which is "your turn to reply", not a decision).
            owner = _console_owners(conn, [r["id"] for r in rows])
        summaries = []
        for r in rows:
            summary = _mission_summary(Mission(**dict(r)))
            summary["conversation_id"] = owner.get(r["id"])
            summaries.append(summary)
        missions, cut = _fit(summaries)
        return {"missions": missions, "total": total, "limit": limit, "offset": offset, "truncated": cut}

    # What NEEDS YOU counts among missions (4.2: the rule lives here, in Core, and every count reads it).
    WAITING_ON_YOU = ("awaiting_decision", "needs_operator", "recovery_required", "paused_tripwire")

    def needs_missions(self) -> dict:
        """The missions and Console conversations waiting on the operator: a grant to decide, a run held for them, a recovery to
        choose, a plan to choose or authorise, a turn that did not finish. A Console conversation and its runs are judged by the
        Console's own grouping (an answered conversation sits in awaiting_decision, which is "your turn to reply"); a run whose
        conversation shows "Working" or cannot be shown (Console off or unreadable, conversation archived) is judged as a mission,
        so a held run never drops out of sight. `truncated`: there were more open missions than were read."""
        self._require_enabled()
        listed: dict[str, str | None] = {}
        items: list[dict] = []
        if self.console_api is not None:
            try:
                conversations = self.console_api.list().get("conversations") or []
            except Exception:
                log.warning("Could not read the Console's conversations for NEEDS YOU", exc_info=True)
                conversations = []
            for conv in conversations:
                if not isinstance(conv, dict) or not isinstance(conv.get("id"), str):
                    continue                             # one malformed entry is skipped, never the whole list (review)
                status = conv.get("status") if isinstance(conv.get("status"), dict) else {}
                listed[conv["id"]] = status.get("group")
                if status.get("group") == "Needs you":
                    items.append({"id": "Console", "title": _text(conv.get("title") or "conversation", 256),
                                  "href": f"/console/{conv['id']}", "meta": _text(status.get("text") or "needs you", 200),
                                  "status": "console", "tier": "console"})
        active = self.list_missions(scope="active", limit=500)
        waiting = []
        for m in active["missions"]:
            if m["status"] not in self.WAITING_ON_YOU:
                continue
            owner = m.get("conversation_id")
            own = owner is not None and m["runtime"] == "console"
            if own and owner in listed:
                continue                         # the conversation itself: the Console says whether it needs you
            if owner is not None and listed.get(owner) == "Needs you":
                continue                         # a run the Console shows as needing you: listed once, as the conversation
            if own and m["status"] == "awaiting_decision":
                continue                         # the conversation itself, answered: your turn to reply, never a decision
            waiting.append({**m, "href": f"/missions/{m['id']}", "meta": f"{m['status'].replace('_', ' ')} · {m['tier']}"})
        truncated = bool(active.get("truncated")) or active["total"] > len(active["missions"])
        return {"missions": waiting + items, "truncated": truncated}

    def _owner(self, mission_id: str) -> str | None:
        """The Console conversation a mission belongs to (its own mission, or a run it started), or None."""
        with self.store._connect() as conn:
            return _console_owners(conn, [mission_id]).get(mission_id)

    def get_mission(self, mission_id: str) -> dict:
        self._require_enabled()
        m = self._mission(mission_id)
        with self.store._connect() as conn:
            artifacts = conn.execute(
                "SELECT * FROM mission_events WHERE mission_id=? AND kind='artifact' ORDER BY seq DESC LIMIT ?",
                (mission_id, MAX_ARTIFACTS)).fetchall()
        artifact_events, _ = _fit([_event_out(MissionEvent(**dict(r))) for r in artifacts])
        live = self.missions.live_session(mission_id)
        out = _mission_summary(m)
        out.update({
            "goal": _text(m.goal), "success_criteria": _text(m.success_criteria),
            "grant": _grant_out(m.grant_json), "grant_hash": m.grant_hash,
            "plan": _json(m.plan_json), "plan_hash": m.plan_hash,
            "verification_plan": _json(m.verification_plan_json),
            "verification_result": _json(m.verification_result_json),
            "outcome_flags": _text(m.outcome_flags, 512),
            "cancel_requested_by": _text(m.cancel_requested_by, 128),
            "cancel_requested_at": m.cancel_requested_at,
            # rc14.7: Check again / Keep apply, by the supervisor's own rule: held because PE's checks failed (rc14.7.5: the hold's
            # recorded reason, so a failed rollback of a failed run no longer offers Keep).
            "hold_reason": (hold := holds.hold_reason(self.store, m)),
            "held_after_failed_checks": hold == holds.FAILED_CHECKS,
            "result": {"outcome": _text(m.outcome, 512), "flags": _text(m.outcome_flags, 512),
                       "verification": _json(m.verification_result_json)},
            # Never the token hash, cgroup path, pid or uid: those are the session's identity.
            "session": (None if live is None else {"id": live["id"], "kind": live["kind"],
                                                   "issued_at": live["issued_at"],
                                                   "expires_at": live["expires_at"]}),
            "artifacts": list(reversed(artifact_events)),
            "spend": self.missions.model_spend([mission_id]),
            "conversation_id": self._owner(mission_id),
            # The open conversation whose controls act on this run (4.2), or None: the mission page then offers its own.
            "decided_in_console": self.console_decides(mission_id),
        })
        return out

    def mission_events(self, mission_id: str, *, after_seq: int = 0, limit: int = 100, tail: bool = False) -> dict:
        """Events oldest-first from `after_seq`, or with `tail` the latest `limit` events (still oldest-first)."""
        self._require_enabled()
        self._mission(mission_id)
        if tail and after_seq:
            raise MissionError("tail and after_seq cannot be combined")
        with self.store._connect() as conn:
            if tail:
                rows = conn.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT ?",
                                    (mission_id, limit + 1)).fetchall()
                rows = rows[::-1]
                earlier, rows = len(rows) > limit, rows[-limit:]
                outs, cut = _fit([_event_out(MissionEvent(**dict(r))) for r in reversed(rows)])
                outs.reverse()          # keep the newest if the budget forces a cut
                return {"events": outs, "has_more": earlier or cut, "tail": True,
                        "next_after_seq": outs[-1]["seq"] if outs else 0,
                        "first_seq": outs[0]["seq"] if outs else None}
            rows = conn.execute("SELECT * FROM mission_events WHERE mission_id=? AND seq>? ORDER BY seq LIMIT ?",
                                (mission_id, after_seq, limit + 1)).fetchall()
        events, cut = _fit([_event_out(MissionEvent(**dict(r))) for r in rows[:limit]])
        more = cut or len(rows) > limit
        return {"events": events, "has_more": more, "tail": False,
                "next_after_seq": events[-1]["seq"] if events else after_seq,
                "first_seq": events[0]["seq"] if events else None}

    def _grant_budgets(self) -> Budgets:
        """The configured per-mission ceilings as a grant's budgets, each clamped to the grant model's own maximum
        (the config allows any positive number; the grant model is the authority on what a grant may hold)."""
        if self._budgets is None:
            return Budgets()
        cfg = self._budgets()
        limits = {name: _field_max(name) for name in ("wall_s", "tool_calls", "output_bytes", "retries")}
        return Budgets(wall_s=min(int(cfg.wall_seconds), limits["wall_s"]),
                       tool_calls=min(int(cfg.tool_calls), limits["tool_calls"]),
                       output_bytes=min(int(cfg.output_bytes), limits["output_bytes"]),
                       retries=min(int(cfg.retries), limits["retries"]))

    def _create_queued(self, *, operator: str, title: str, goal: str, tier: str, origin: str,
                       incident_id: str | None, success_criteria: str, scope: dict | None,
                       evidence: dict | None = None) -> dict:
        """Create the mission, queue it and (optionally) record incident evidence in ONE transaction, so no
        failure can leave a half-made mission behind."""
        try:
            grant = Grant(tier=tier, scope=GrantScope.model_validate(scope or {}), budgets=self._grant_budgets())
        except ValidationError:
            raise MissionError("Invalid scope") from None
        actor = _actor(operator)
        try:
            with self.store._write() as conn:
                mission_id = self.missions.create_in_txn(
                    conn, title=redact(title), origin=origin, grant=grant, goal=redact(goal),
                    success_criteria=redact(success_criteria), runtime=self._runtime,
                    incident_id=incident_id, actor=actor)
                now = int(self.store._clock())
                transition_in_txn(conn, mission_id, "queued", actor, now)
                if evidence is not None:
                    insert_event(conn, mission_id, "note", actor, {"incident_evidence": evidence}, now)
        except ValidationError:
            raise MissionError("Mission text is too long") from None
        except sqlite3.IntegrityError:
            raise MissionError("Incident not found", "not_found") from None   # missions.incident_id is a foreign key
        return _mission_summary(self.missions.get_mission(mission_id))

    def create_mission(self, *, operator: str, title: str, goal: str, tier: str = "propose",
                       origin: str | None = None, incident_id: str | None = None,
                       success_criteria: str | None = None, scope: dict | None = None) -> dict:
        """An Observe or Propose mission. No operator surface reaches this since 4.2 (`mission.create` and INVESTIGATE were removed;
        the Console makes its own missions): it is kept as the service-level constructor the tests build missions with. Do not
        put it back on the RPC table without the second review."""
        self._require_enabled()
        if tier not in CREATABLE_TIERS:
            raise MissionError(f"Operator surfaces can only create {' or '.join(CREATABLE_TIERS)} missions; "
                               f"{tier} needs the approval flow", "forbidden")
        origin = origin or ("incident" if incident_id else "operator")
        if (origin == "incident") != (incident_id is not None):
            raise MissionError("An incident mission needs an incident_id, and only an incident mission has one")
        return self._create_queued(
            operator=operator, title=title, goal=goal, tier=tier, origin=origin, incident_id=incident_id,
            success_criteria=success_criteria or "A diagnosis with evidence; nothing is changed.", scope=scope)

    def cancel_mission(self, mission_id: str, *, operator: str) -> dict:
        """With a live session, set `cancel_requested`: the supervisor owns stopping it and confirming it
        dead. With none (never started, or its session revoked and reaped, uid released) there is
        nothing to stop, so the mission is cancelled through the state machine at once."""
        self._require_enabled()
        actor = _actor(operator)
        now = int(self.store._clock())
        with self.store._write() as conn:
            row = conn.execute("SELECT status, cancel_requested_at, tier, checkpoint_id FROM missions WHERE id=?",
                               (mission_id,)).fetchone()
            if row is None:
                raise MissionError("Mission not found", "not_found")
            if row["status"] in TERMINAL:
                raise MissionError(f"Mission is already {row['status']}", "conflict")
            # Is there a session the supervisor still has to stop? Not revoked, or revoked with its uid still held (the supervisor
            # revokes the token BEFORE it has proven the cgroup empty). A revoked session with no uid has nothing the supervisor can
            # stop or prove, so it is not one: counted as running here, the cancel would only be recorded, and nothing acts on a
            # recorded cancel of an Observe/Propose mission that is not queued (review, rc14.7.7). The lease question is not this
            # one: there it must fail safe (session_state.MAY_RUN_SQL).
            live = conn.execute("SELECT 1 FROM mission_sessions WHERE mission_id=? AND (revoked_at IS NULL OR "
                                "(session_uid IS NOT NULL AND uid_released_at IS NULL))", (mission_id,)).fetchone()
            # A Repair mission that has a checkpoint may have changed the host: only the supervisor, which reads the host against
            # the checkpoint, closes it as cancelled (or holds it for the operator). Here the request is just recorded.
            may_have_changed = (row["tier"] not in ("observe", "propose") and row["checkpoint_id"] is not None
                                and row["status"] in ("executing", "paused_tripwire", "verifying", "needs_operator",
                                                      "recovery_required", "rollback_pending"))
            if live is None and not may_have_changed:
                try:
                    transition_in_txn(conn, mission_id, "cancelled", actor, now)
                except IllegalTransition as exc:
                    raise MissionError(str(exc), "conflict") from None
            elif row["cancel_requested_at"] is None:
                conn.execute("UPDATE missions SET cancel_requested_at=?, cancel_requested_by=?, updated_at=? "
                             "WHERE id=?", (now, actor, now, mission_id))
                insert_event(conn, mission_id, "decision", actor, {"cancel_requested": True}, now)
        return _mission_summary(self._mission(mission_id))

    # ── memory ──────────────────────────────────────────────────────────────
    def _memory(self, conn) -> None:
        self._require_enabled()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_cards'").fetchone() is None:
            raise MissionError("Ship's Memory is unavailable (SQLite has no FTS5)", "unavailable")

    def _cards(self, status: str, limit: int, offset: int) -> dict:
        with self.store._connect() as conn:
            self._memory(conn)
            total = conn.execute("SELECT COUNT(*) FROM memory_cards WHERE status=?", (status,)).fetchone()[0]
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM memory_cards WHERE status=? ORDER BY seq LIMIT ? OFFSET ?",
                (status, limit, offset))]
            cards = [_card_out(c) for c in (memory_store.get_card(conn, i) for i in ids) if c is not None]
            # A card a Console conversation offered says so and links there (4.2): one lesson, findable from both places.
            offered = {}
            if ids:
                marks = ",".join("?" * len(ids))
                try:
                    offered = {r[0]: {"conversation_id": r[1], "title": _text(r[2], 120)} for r in conn.execute(
                        "SELECT l.card_id, c.id, c.title FROM console_lessons l JOIN console_conversations c ON c.id = l.conversation_id "
                        f"WHERE l.card_id IN ({marks})", ids)}
                except sqlite3.OperationalError:
                    offered = {}                         # no Console tables on this install
            for c in cards:
                c["offered_in"] = offered.get(c["id"])
        cards, cut = _fit(cards)
        return {"cards": cards, "total": total, "limit": limit, "offset": offset, "truncated": cut}

    def memory_candidates(self, *, limit: int = 50, offset: int = 0) -> dict:
        return self._cards("candidate", limit, offset)

    def memory_cards(self, *, status: str = "confirmed", limit: int = 50, offset: int = 0) -> dict:
        return self._cards(status, limit, offset)

    def memory_review_due(self, *, limit: int = 50, offset: int = 0) -> dict:
        with self.store._connect() as conn:
            self._memory(conn)
            due = memory_store.review_due(conn, datetime.now(UTC))
        cards, cut = _fit([_card_out(c) for c in due[offset:offset + limit]])
        return {"cards": cards, "total": len(due), "limit": limit, "offset": offset, "truncated": cut}

    def memory_search(self, *, query: str, scope: list[str] | None = None, limit: int = 5) -> dict:
        with self.store._connect() as conn:
            self._memory(conn)
            found = memory_retrieval.retrieve(conn, scope or [], query, limit=limit)
        cards, cut = _fit([_card_out(c) for c in found.cards])
        return {"cards": cards, "truncated": found.truncated or cut}

    def _card_change(self, change: Callable[..., Card], card_id: str, *args) -> dict:
        # The write lock is taken up front so the status precondition is read under it: two operators
        # confirming the same candidate cannot both succeed.
        with self.store._write() as conn:
            self._memory(conn)
            try:
                return _card_out(change(conn, card_id, *args))
            except memory_store.UnknownCard:
                raise MissionError("Card not found", "not_found") from None
            except memory_store.ActorNotPermitted as exc:
                raise MissionError(str(exc), "forbidden") from None
            except memory_store.InvalidTransition as exc:
                raise MissionError(str(exc), "conflict") from None
            except memory_store.MemoryStoreError as exc:
                raise MissionError(str(exc)) from None

    def memory_confirm(self, card_id: str, *, operator: str) -> dict:
        return self._card_change(memory_store.confirm, card_id, _actor(operator))

    def memory_reverify(self, card_id: str, *, operator: str) -> dict:
        return self._card_change(memory_store.reverify, card_id, _actor(operator))

    def memory_deprecate(self, card_id: str, *, operator: str, reason: str = "") -> dict:
        return self._card_change(memory_store.deprecate, card_id, _actor(operator), redact(reason))

    # ── approval flow (slice 7a) ────────────────────────────────────────────
    def _flow(self) -> ApprovalFlow:
        if self.flow is None:
            raise MissionError("Repair missions are not available on this host", "unavailable")
        return self.flow

    def _flowed(self, call: Callable[[ApprovalFlow], Any]) -> Any:
        self._require_enabled()
        try:
            return call(self._flow())
        except FlowError as exc:
            raise MissionError(str(exc), exc.code) from None

    def approval_card(self, mission_id: str) -> dict:
        return self._flowed(lambda f: f.card(mission_id))

    def _decided_in_conversation(self, mission_id: str) -> None:
        """One place to act (4.2, docs/designs/missions-memory.md): a run a Console conversation started is approved, denied, checked
        again, kept and rolled back in that conversation, which tells its thread what happened. The dashboard's mission page and
        Telegram's /approve, /deny and /rollback refuse it and say where to go. With the Console off, nothing can show the
        conversation, so the mission is decided here as before. Cancel is not a decision and stays (the Console cannot stop a run)."""
        owner = self.console_decides(mission_id)
        if owner is not None:
            raise MissionError(f"{mission_id} is a run of a Console conversation: decide it in the conversation "
                               f"(/console/{owner}).", "in_conversation")

    def console_decides(self, mission_id: str) -> str | None:
        """The open Console conversation whose controls act on this run, or None. The Console shows and acts on a conversation's
        latest run only, and only while the conversation is open: an earlier run (its Undo, a hold) and a run a closed or archived
        conversation left behind are decided on their mission page, as before 4.2 (review). With the Console off, none."""
        if self.console_api is None:
            return None
        with self.store._connect() as conn:
            try:
                row = conn.execute("SELECT conversation_id FROM console_executions WHERE mission_id=?", (mission_id,)).fetchone()
                if row is None:
                    return None
                latest = conn.execute(
                    "SELECT mission_id FROM console_executions WHERE conversation_id=? AND status IN ('proposed', 'authorised', 'ended') "
                    "ORDER BY id DESC LIMIT 1", (row[0],)).fetchone()
                conv = conn.execute("SELECT * FROM console_conversations WHERE id=?", (row[0],)).fetchone()
            except sqlite3.OperationalError:
                return None                              # no Console tables on this install
        if latest is None or latest[0] != mission_id or conv is None:
            return None
        try:
            own_status = self._mission(conv["mission_id"]).status
        except MissionError:
            return None                                  # unreadable: the mission page decides, never a run nobody can reach
        # `console.store.is_closed`, the Console's one rule: archived or archiving, or its own mission finished.
        return None if conv["state"] != "open" or own_status in TERMINAL_STATUSES else conv["id"]

    def approve_mission(self, mission_id: str, *, operator: str, phase: str, grant_hash: str, elevated: bool = False) -> dict:
        self._decided_in_conversation(mission_id)
        return self._flowed(lambda f: f.approve(_actor(operator), mission_id, phase, grant_hash, elevated=elevated))

    def deny_mission(self, mission_id: str, *, operator: str, grant_hash: str) -> dict:
        self._decided_in_conversation(mission_id)
        return self._flowed(lambda f: f.deny(_actor(operator), mission_id, grant_hash))

    def rollback_mission(self, mission_id: str, *, operator: str, ack_drift: bool = False) -> dict:
        self._decided_in_conversation(mission_id)
        return self._flowed(lambda f: f.roll_back(_actor(operator), mission_id, ack_drift=ack_drift))

    def recheck_mission(self, mission_id: str, *, operator: str) -> dict:
        self._decided_in_conversation(mission_id)
        return self._flowed(lambda f: f.recheck(_actor(operator), mission_id))

    def keep_mission(self, mission_id: str, *, operator: str) -> dict:
        self._decided_in_conversation(mission_id)
        return self._flowed(lambda f: f.keep_and_close(_actor(operator), mission_id))

    def provider_usage_view(self) -> dict[str, Any]:
        """Credit and spend as the model provider reports them (rc14.7), or why there are none."""
        if self.provider_usage is None:
            return {"source": "none", "available": False, "state": "off",
                    "note": "Provider usage is shown when the model broker is on (missions.model_broker)."}
        return self.provider_usage.snapshot()

    def handlers(self) -> dict[str, Callable[[Any], Any]]:
        """The RPC method table. Each wrapper checks the flag before it looks at params, so a disabled host
        answers "disabled" to everything and parses nothing."""
        from planet_express.integrations.rpc import RpcError

        def wrap(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
            def handler(params):
                try:
                    self._require_enabled()
                    return fn(params)
                except MissionError as exc:
                    raise RpcError(str(exc), exc.code) from None
            return handler

        op = {"operator": _OPERATOR_CHECK}
        mid = {"mission_id": _MISSION_CHECK}
        card = {"card_id": _pattern(SLUG_RE, "a card id")}
        status_checks = {"status": _choice(*sorted(_STATUSES))}

        def list_(p):
            return self.list_missions(**_params(p, {}, {**status_checks, "scope": _choice("active", "closed"),
                                                        "limit": _int(1, MAX_LIST), "offset": _PAGE["offset"]}))

        def get(p):
            return self.get_mission(_params(p, mid)["mission_id"])

        def events(p):
            return self.mission_events(**_params(p, mid, {"after_seq": _int(0, 2**62),
                                                          "limit": _int(1, MAX_EVENT_PAGE), "tail": _bool}))

        def cancel(p):
            v = _params(p, {**mid, **op})
            return self.cancel_mission(v["mission_id"], operator=v["operator"])

        def candidates(p):
            return self.memory_candidates(**_params(p, {}, _PAGE))

        def cards(p):
            return self.memory_cards(**_params(p, {}, {"status": _choice("confirmed", "deprecated"), **_PAGE}))

        def review_due(p):
            return self.memory_review_due(**_params(p, {}, _PAGE))

        def confirm(p):
            v = _params(p, {**card, **op})
            return self.memory_confirm(v["card_id"], operator=v["operator"])

        def reverify(p):
            v = _params(p, {**card, **op})
            return self.memory_reverify(v["card_id"], operator=v["operator"])

        def deprecate(p):
            v = _params(p, {**card, **op}, {"reason": _str(500, blank=True)})
            return self.memory_deprecate(v["card_id"], operator=v["operator"], reason=v.get("reason", ""))

        def search(p):
            return self.memory_search(**_params(p, {"query": _str(500, blank=True)},
                                                {"scope": _tags, "limit": _int(1, MAX_SEARCH)}))

        elev = {"elevated": _bool}
        grant_hash = {"grant_hash": _pattern(re.compile(r"[0-9a-f]{64}"), "a grant hash")}

        def card_(p):
            return self.approval_card(_params(p, mid)["mission_id"])

        def approve(p):
            v = _params(p, {**mid, **op, **grant_hash, "phase": _pattern(_PHASE, "an approval phase")}, elev)
            return self.approve_mission(v["mission_id"], operator=v["operator"], phase=v["phase"],
                                        grant_hash=v["grant_hash"], elevated=v.get("elevated", False))

        def deny(p):
            v = _params(p, {**mid, **op, **grant_hash})
            return self.deny_mission(v["mission_id"], operator=v["operator"], grant_hash=v["grant_hash"])

        def rollback(p):
            v = _params(p, {**mid, **op}, {"ack_drift": _bool})
            return self.rollback_mission(v["mission_id"], operator=v["operator"], ack_drift=v.get("ack_drift", False))

        def recheck(p):
            v = _params(p, {**mid, **op})
            return self.recheck_mission(v["mission_id"], operator=v["operator"])

        def keep(p):
            v = _params(p, {**mid, **op})
            return self.keep_mission(v["mission_id"], operator=v["operator"])

        def provider_usage(p):
            _params(p, {})
            return self.provider_usage_view()

        # 4.2: standing.*, mission.create, mission.from_incident and mission.propose_repair are gone (no caller: the Console makes
        # conversations and their runs in Core itself; standing grants never had an effect).
        table = {"mission.provider_usage": provider_usage, "mission.approval_card": card_, "mission.approve": approve,
                 "mission.deny": deny, "mission.rollback": rollback, "mission.recheck": recheck, "mission.keep": keep,
                 "mission.list": list_, "mission.get": get, "mission.events": events,
                 "mission.cancel": cancel,
                 "memory.candidates": candidates, "memory.cards": cards, "memory.review_due": review_due,
                 "memory.confirm": confirm, "memory.reverify": reverify, "memory.deprecate": deprecate, "memory.search": search}
        handlers = {name: wrap(fn) for name, fn in table.items()}
        handlers.update({name: wrap(fn) for name, fn in self._console_table().items()})
        return handlers

    # ── Console (the Operator Console's read models and actions; see console/api.py) ─────────────────
    def _console_table(self) -> dict[str, Callable[[Any], Any]]:
        from planet_express.console.api import ConsoleApiError
        op = {"operator": _OPERATOR_CHECK}
        conv = {"conversation_id": _pattern(CONVERSATION_ID_RE, "a conversation id")}

        def api():
            if self.console_api is None:
                raise MissionError("The Console is not enabled on this host.", "unavailable")
            return self.console_api

        def guarded(fn):
            def call(params):
                try:
                    return fn(params)
                except ConsoleApiError as exc:
                    raise MissionError(str(exc), exc.code) from None
            return call

        def status(p):
            _params(p, {})
            return api().status()

        def list_(p):
            _params(p, {})
            return api().list()

        def feed(p):
            v = _params(p, conv, {"after_message": _int(0, 2**62), "after_event": _int(0, 2**62)})
            return api().feed(v["conversation_id"], v.get("after_message", 0), v.get("after_event", 0))

        def create(p):
            v = _params(p, {**op, "text": _str(8000)}, {"incident_id": _pattern(INCIDENT_ID_RE, "an incident id")})
            return api().create(v["operator"], v["text"], v.get("incident_id"))

        def post(p):
            v = _params(p, {**conv, **op, "text": _str(8000)})
            return api().post(v["conversation_id"], v["operator"], v["text"])

        def interrupt(p):
            v = _params(p, {**conv, **op})
            return api().interrupt(v["conversation_id"], v["operator"])

        def archive(p):
            v = _params(p, {**conv, **op})
            return api().archive(v["conversation_id"], v["operator"])

        def scrap(p):
            v = _params(p, {**conv, **op})
            return api().scrap_plan(v["conversation_id"], v["operator"])

        def lesson(p):
            v = _params(p, {**conv, **op, "card_id": _pattern(SLUG_RE, "a card id"),
                            "decision": _choice("keep", "discard")}, {"statement": _str(600, blank=True)})
            return api().lesson_decide(v["conversation_id"], v["card_id"], v["decision"], v["operator"], v.get("statement"))

        grant_hash = {"grant_hash": _pattern(re.compile(r"[0-9a-f]{64}"), "a grant hash")}
        execution = {"execution_id": _int(1, 2**31)}

        def confirm_prepare(p):
            v = _params(p, {**conv, **op, "plan_id": _int(1, 2**31), "option": _int(0, 2)}, {"no_backup": _bool})
            return api().confirm_prepare(v["conversation_id"], v["plan_id"], v["option"], v["operator"], v.get("no_backup", False))

        def confirm_authorise(p):
            v = _params(p, {**conv, **op, **execution, **grant_hash}, {"elevated": _bool})
            return api().confirm_authorise(v["conversation_id"], v["execution_id"], v["grant_hash"], v["operator"], v.get("elevated", False))

        def confirm_withdraw(p):
            v = _params(p, {**conv, **op, **execution})
            return api().confirm_withdraw(v["conversation_id"], v["execution_id"], v["operator"])

        def execution_action(p):
            v = _params(p, {**conv, **op, **execution, "action": _choice("rollback", "recheck", "keep")}, {"ack_drift": _bool})
            return api().execution_action(v["conversation_id"], v["execution_id"], v["action"], v["operator"], ack_drift=v.get("ack_drift", False))

        table = {"console.status": status, "console.list": list_, "console.feed": feed, "console.create": create, "console.post": post,
                 "console.interrupt": interrupt, "console.archive": archive, "console.scrap_plan": scrap,
                 "console.confirm_prepare": confirm_prepare, "console.confirm_authorise": confirm_authorise,
                 "console.confirm_withdraw": confirm_withdraw, "console.execution_action": execution_action,
                 "console.lesson": lesson}
        return {name: guarded(fn) for name, fn in table.items()}

    # ── Telegram ────────────────────────────────────────────────────────────
    TELEGRAM_COMMANDS = frozenset({"/missions", "/mission", "/cancel_mission", "/mission_card", "/approve", "/deny",
                                  "/rollback"} | CONSOLE_COMMANDS)
    _CONSOLE_COMMANDS = CONSOLE_COMMANDS
    # Approval-flow commands. The operator is the Telegram sender (never a parameter), no command here can be elevated, and
    # /approve is bound to the first 8 hex digits of the grant hash printed on the card, so it cannot approve a card that
    # changed after it was read.
    _FLOW_COMMANDS = frozenset({"/mission_card", "/approve", "/deny", "/rollback"})

    def telegram(self, text: str, operator: str) -> str:
        """Answer /missions, /mission <id> and /cancel_mission <id> in plain text. The caller has already
        established that the message came from the configured operator chat."""
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in self._CONSOLE_COMMANDS:
            if self.console_telegram is None:
                return "The Console is not available on this host."
            if _OPERATOR.fullmatch(operator) is None:
                return "Cannot identify the operator."
            return self.console_telegram.command(text, operator)
        if cmd in self._FLOW_COMMANDS:
            return self._telegram_flow(cmd, args, operator)
        try:
            if cmd == "/missions":
                if args:
                    return "Usage: /missions"
                return self._telegram_list()
            if len(args) != 1 or MISSION_ID_RE.fullmatch(args[0]) is None:
                return f"Usage: {cmd} M-<number>"
            if cmd == "/mission":
                return self._telegram_detail(args[0])
            if _OPERATOR.fullmatch(operator) is None:
                return "Cannot identify the operator."
            owner = self._owner(args[0])
            if owner is not None and self.console_api is not None and self._mission(args[0]).runtime == "console":
                # A conversation answered or not wanted any more is done, not cancelled (4.0.1, operator).
                return (f"{args[0]} is a Console conversation: /close {owner[:8]} marks it done (nothing on the host changes). "
                        "Cancel is for work you want stopped.")
            out = self.cancel_mission(args[0], operator=operator)
            note = "cancelled" if out["status"] == "cancelled" else (
                f"cancel requested; the supervisor will stop it (now {out['status']})")
            return f"{out['id']}: {note}."
        except MissionError as exc:
            return f"{exc}" if exc.code != "not_found" else f"No such mission: {args[0] if args else ''}".strip()

    def _telegram_flow(self, cmd: str, args: list[str], operator: str) -> str:
        wants_hash = cmd in ("/approve", "/deny")
        if (len(args) != (2 if wants_hash else 1) or MISSION_ID_RE.fullmatch(args[0]) is None
                or (wants_hash and re.fullmatch(r"[0-9a-f]{8}", args[1]) is None)):
            return f"Usage: {cmd} M-<number>" + (" <first 8 characters of the grant hash on the card>" if wants_hash else "")
        if _OPERATOR.fullmatch(operator) is None:
            return "Cannot identify the operator."
        mission_id = args[0]
        try:
            self._require_enabled()
            owner = self.console_decides(mission_id) if cmd != "/mission_card" else None
            if owner is not None:
                return (f"{mission_id} is a run of a Console conversation: decide it there. /plan {owner[:8]} shows it; "
                        f"/authorise, /withdraw and /run {owner[:8]} rollback|recheck|keep act on it.")
            if cmd == "/mission_card":
                return _render_card(self.approval_card(mission_id))
            card = self.approval_card(mission_id)
            if wants_hash:
                if not card["grant_hash"].startswith(args[1]):
                    return "That hash is not this mission's current grant hash. Read the card again: /mission_card " + mission_id
                if cmd == "/deny":
                    out = self.deny_mission(mission_id, operator=operator, grant_hash=card["grant_hash"])
                    return f"{mission_id}: denied; the mission is {out['status']}."
                pending = [r for r in card["required"] if r["state"] == "pending"]
                if not pending:
                    return f"{mission_id}: nothing left to approve."
                if pending[0]["elevated"]:
                    return (f"{mission_id}: the {pending[0]['phase']} approval needs an elevated dashboard session. "
                            "Telegram cannot give it.")
                out = self.approve_mission(mission_id, operator=operator, phase=pending[0]["phase"],
                                           grant_hash=card["grant_hash"], elevated=False)
                left = [r["phase"] for r in out["required"] if r["state"] == "pending"]
                return (f"{mission_id}: {pending[0]['phase']} approved." +
                        (f" Still needed (dashboard): {', '.join(left)}." if left else " It will start shortly."))
            out = self.rollback_mission(mission_id, operator=operator)                  # /rollback
            return f"{mission_id}: rollback {out['restore']}; the mission is {out['status']}."
        except MissionError as exc:
            return f"{exc}" if exc.code != "not_found" else f"No such mission: {mission_id}"

    def _telegram_list(self) -> str:
        self._require_enabled()
        with self.store._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM missions WHERE status NOT IN ({_TERMINAL_SQL}) "
                "ORDER BY updated_at DESC, CAST(SUBSTR(id, 3) AS INTEGER) DESC LIMIT 10").fetchall()
        if not rows:
            return "No active missions."
        return "\n".join(f"{m.id} [{m.status}/{m.tier}] {_text(m.title, 80)}"
                         for m in (Mission(**dict(r)) for r in rows))

    def _telegram_detail(self, mission_id: str) -> str:
        m = self.get_mission(mission_id)
        lines = [f"{m['id']}: {m['title']}", f"status {m['status']} - tier {m['tier']} - origin {m['origin']}"]
        if m["incident_id"]:
            lines.append(f"incident {m['incident_id']}")
        if m["cancel_requested"]:
            lines.append(f"cancel requested by {m['cancel_requested_by']}")
        if m["outcome"]:
            lines.append(f"outcome: {m['outcome']}")
        last = self._last_event(mission_id)
        if last:
            lines.append(f"last event: {last['kind']} by {last['actor']}")
        return "\n".join(lines)

    def _last_event(self, mission_id: str) -> dict | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT 1",
                               (mission_id,)).fetchone()
        return None if row is None else _event_out(MissionEvent(**dict(row)))


def _render_card(card: dict) -> str:
    """The approval card as plain text, elevated risks first. The first 8 characters of the grant hash bind /approve."""
    lines = [f"{card['mission_id']} [{card['status']}/{card['tier']}] {card['title']}"]
    lines += [f"!! {w['text']}" for w in card["warnings"]]
    scope = card["scope"]
    lines.append("scope: " + ", ".join(f"{k}={','.join(v)}" for k, v in scope.items() if v and k != "files"))
    if scope.get("files"):                       # rc14.5: bounded, so the grant-hash line below always fits Telegram's limit
        files = [f[:120] for f in scope["files"][:6]]
        more = len(scope["files"]) - len(files)
        lines.append("files Undo covers: " + ", ".join(files) + (f" and {more} more" if more > 0 else ""))
    bud = card["budgets"]
    lines.append(f"budgets: {bud['wall_s']}s, {bud['tool_calls']} tools, {bud['output_bytes']} bytes, retries {bud['retries']}")
    lines.append("verification: " + (", ".join(f"{c['id']}({c['check']})" for c in card["verification"]["checks"]) or "none")
                 + f"; on failure {card['verification']['on_verify_fail']}")
    lines.append("approvals: " + ", ".join(f"{r['phase']}={r['state']}{'*' if r['elevated'] else ''}" for r in card["required"])
                 + " (* needs the dashboard)")
    lines.append(f"grant hash: {card['grant_hash'][:8]}")
    return "\n".join(lines)


__all__ = ["MissionError", "MissionService"]
