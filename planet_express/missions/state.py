"""Mission state machine (contract §3). Illegal edges raise, and the refusal is logged."""

import sqlite3

from planet_express.core.store import Store

from .approvals import evaluate
from .events import insert_event

TERMINAL = frozenset({"completed", "failed", "rolled_back", "cancelled"})
NON_TERMINAL = frozenset({
    "draft", "queued", "investigating", "awaiting_decision", "executing", "verifying", "blocked",
    "needs_operator", "recovery_required", "paused_tripwire", "rollback_pending",
})
# Edges only an operator may take (the contract leaves needs_operator exits undefined: see the slice report).
OPERATOR_EXITS = frozenset({"investigating", "awaiting_decision", "executing", "verifying", "rollback_pending"})
# States that only exist because something may have been executed: observe and propose never get there.
EXECUTION_STATES = frozenset({"executing", "paused_tripwire", "rollback_pending", "verifying", "rolled_back"})

_ANY = {"cancelled", "blocked", "needs_operator", "recovery_required"}

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"queued"}),
    "queued": frozenset({"investigating"}),
    "investigating": frozenset({"awaiting_decision", "completed", "failed"}),
    "awaiting_decision": frozenset({"executing", "completed", "failed"}),
    "executing": frozenset({"verifying", "paused_tripwire", "rollback_pending", "failed"}),
    "verifying": frozenset({"completed", "failed", "rollback_pending"}),
    "paused_tripwire": frozenset({"executing", "rolled_back", "rollback_pending"}),
    "rollback_pending": frozenset({"rolled_back"}),
    "blocked": frozenset({"queued", "awaiting_decision"}),
    "recovery_required": frozenset({
        "investigating", "executing", "verifying", "rolled_back", "rollback_pending"}),
    "needs_operator": frozenset(OPERATOR_EXITS),
}
for _state in NON_TERMINAL:
    TRANSITIONS[_state] = frozenset((TRANSITIONS.get(_state, frozenset()) | _ANY) - {_state})
for _state in TERMINAL:
    TRANSITIONS[_state] = frozenset()


class IllegalTransition(RuntimeError):
    """The edge is not in the transition table, or a rule on it is not met."""


class MissionNotFound(LookupError):
    pass


def is_legal(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm, frozenset())


def transition_in_txn(conn: sqlite3.Connection, mission_id: str, to: str, actor: str, now: int) -> str:
    """Take one edge inside the caller's transaction (status CAS, predicate on `executing`, state event).
    Raises IllegalTransition without logging: the caller decides whether the transaction survives."""
    row = conn.execute("SELECT status, tier FROM missions WHERE id=?", (mission_id,)).fetchone()
    if row is None:
        raise MissionNotFound(mission_id)
    frm, tier = row["status"], row["tier"]
    if not is_legal(frm, to):
        raise IllegalTransition(f"{frm} -> {to} is not a legal transition")
    if tier in ("observe", "propose") and to in EXECUTION_STATES:
        raise IllegalTransition(f"{tier} missions never reach {to}")
    if frm == "needs_operator" and not actor.startswith("operator:"):
        raise IllegalTransition("only an operator may resume a needs_operator mission")
    if to == "executing":
        verdict = evaluate(conn, mission_id, now)
        if not verdict.ok:
            raise IllegalTransition("execution predicate not satisfied: " + "; ".join(verdict.reasons))
    closed = ", closed_at=?" if to in TERMINAL else ""
    args = (to, now, *((now,) if to in TERMINAL else ()), mission_id, frm)
    cur = conn.execute(
        f"UPDATE missions SET status=?, updated_at=?{closed} WHERE id=? AND status=?", args)
    if cur.rowcount != 1:
        raise IllegalTransition(f"{mission_id} changed state concurrently")
    insert_event(conn, mission_id, "state", actor, {"from": frm, "to": to}, now)
    return frm


def transition(store: Store, mission_id: str, to: str, actor: str) -> str:
    """Move a mission along one edge. A refused edge is recorded as a state event, then raised."""
    now = int(store._clock())
    refusal: IllegalTransition | None = None
    with store._write() as conn:
        try:
            return transition_in_txn(conn, mission_id, to, actor, now)
        except IllegalTransition as exc:
            frm = conn.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
            insert_event(conn, mission_id, "state", actor,
                         {"refused": True, "from": frm["status"] if frm else None, "to": to,
                          "reason": str(exc)}, now)
            refusal = exc
    raise refusal
