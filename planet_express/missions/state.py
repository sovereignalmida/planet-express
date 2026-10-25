"""Mission state machine (contract §3). Illegal edges raise, and the refusal is logged."""

import sqlite3

from planet_express.core.store import Store

from .approvals import evaluate
from .canonical import CanonicalError, loads_strict, sha256_hex
from .events import insert_event
from .models import Grant
from .verify.plan import parse_result

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
    # `investigating` is the operator's follow-up on a finished observe/propose mission (a Console turn): see transition_in_txn.
    "awaiting_decision": frozenset({"executing", "completed", "failed", "investigating"}),
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


MUTATING_TIERS = frozenset({"repair", "change", "critical"})


def completion_problems(row: sqlite3.Row) -> list[str]:
    """Why this mission may NOT be completed (contract 10). Only Core's own verification can complete a mutating
    mission: the state must be `verifying`, and the stored result must be `passed`, for THIS grant hash, THIS
    checkpoint and THIS plan. An agent's say-so, a result for an older grant and a result for a changed plan all fail.
    This runs inside the transition's own transaction, so a direct store call cannot get around it."""
    if row["tier"] not in MUTATING_TIERS:
        return []
    if row["status"] != "verifying":
        return [f"a {row['tier']} mission completes only from verifying (it is {row['status']})"]
    result = parse_result(row["verification_result_json"])
    if result is None:
        return ["there is no readable verification result"]
    problems = []
    if result.status != "passed":
        problems.append("the verification result is not passed")
    if result.grant_hash != row["grant_hash"]:
        problems.append("the verification result is for a different grant hash")
    if not row["checkpoint_id"] or result.checkpoint_id != row["checkpoint_id"]:
        problems.append("the verification result is for a different checkpoint")
    try:
        grant = Grant.model_validate(loads_strict(row["grant_json"]))
        plan_sha = sha256_hex(loads_strict(row["verification_plan_json"])) if row["verification_plan_json"] else None
    except (CanonicalError, ValueError):
        return [*problems, "the grant or the verification plan is unreadable"]
    if plan_sha is None or plan_sha != grant.verification.plan_sha256 or result.plan_sha256 != plan_sha:
        problems.append("the verification result is not for the plan the grant approved")
    return problems


def release_for_terminal(conn: sqlite3.Connection, mission_id: str, now: int) -> None:
    """Free what a finished mission holds: every lease (suspect ones included) and its live session row.
    `resource_fences` is untouched, so the next holder still gets a higher fence."""
    freed = [r["resource"] for r in conn.execute(
        "SELECT resource FROM mission_leases WHERE mission_id=? ORDER BY resource", (mission_id,))]
    conn.execute("DELETE FROM mission_leases WHERE mission_id=?", (mission_id,))
    cur = conn.execute("UPDATE mission_sessions SET revoked_at=? WHERE mission_id=? AND revoked_at IS NULL",
                       (now, mission_id))
    if freed or cur.rowcount:
        insert_event(conn, mission_id, "decision", "core",
                     {"terminal_release": freed, "sessions_revoked": cur.rowcount}, now)


def transition_in_txn(conn: sqlite3.Connection, mission_id: str, to: str, actor: str, now: int) -> str:
    """Take one edge inside the caller's transaction (status CAS, predicate on `executing`, state event).
    Raises IllegalTransition without logging: the caller decides whether the transaction survives.

    Entering a terminal state also frees the mission's leases and revokes its live session row, in this
    same transaction. That is only safe because the SUPERVISOR MUST HAVE CONFIRMED THE SESSION DEAD (cgroup
    or uid empty, token and endpoints revoked, contract §2.1) BEFORE it asks for a terminal transition:
    the lease is what fences a still-running worker out. A `suspect` lease is freed by this path or by
    leases.release_after_proven_dead, and by nothing else."""
    row = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
    if row is None:
        raise MissionNotFound(mission_id)
    frm, tier = row["status"], row["tier"]
    if not is_legal(frm, to):
        raise IllegalTransition(f"{frm} -> {to} is not a legal transition")
    if tier in ("observe", "propose") and to in EXECUTION_STATES:
        raise IllegalTransition(f"{tier} missions never reach {to}")
    if frm == "needs_operator" and to != "cancelled" and not actor.startswith("operator:"):
        # Resuming is the operator's act. Closing it is not a resumption: Core and the system may cancel a mission that is waiting
        # for the operator (an archived Console conversation whose last turn failed would otherwise never finish archiving).
        raise IllegalTransition("only an operator may resume a needs_operator mission")
    if frm == "awaiting_decision" and to == "investigating":
        # Reopening a finished mission for more work is the operator's act, and only an observe/propose mission can be reopened:
        # a mission that may mutate the host leaves `awaiting_decision` by approval, never by a follow-up.
        if tier not in ("observe", "propose"):
            raise IllegalTransition(f"a {tier} mission cannot be reopened for a follow-up")
        if not actor.startswith("operator:"):
            raise IllegalTransition("only an operator may reopen a finished mission")
    if to == "executing":
        verdict = evaluate(conn, mission_id, now)
        if not verdict.ok:
            raise IllegalTransition("execution predicate not satisfied: " + "; ".join(verdict.reasons))
    if to == "completed":
        problems = completion_problems(row)
        if problems:
            raise IllegalTransition("completion refused: " + "; ".join(problems))
    closed = ", closed_at=?" if to in TERMINAL else ""
    # A result belongs to one pass through `verifying`: leaving it for more work (or entering it afresh) voids it.
    voided = ", verification_result_json=NULL" if to in ("executing", "verifying") else ""
    args = (to, now, *((now,) if to in TERMINAL else ()), mission_id, frm)
    cur = conn.execute(
        f"UPDATE missions SET status=?, updated_at=?{closed}{voided} WHERE id=? AND status=?", args)
    if cur.rowcount != 1:
        raise IllegalTransition(f"{mission_id} changed state concurrently")
    if to in TERMINAL:
        release_for_terminal(conn, mission_id, now)
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
