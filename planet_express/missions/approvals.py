"""Approvals and the one execution predicate (contract §2 rules, §3).

An approval is proven only by a `mission_approvals` row. Rows bind to a hash, so editing the grant or the
plan changes the hash and every old row stops matching. A denial row is a record, never a proof.
"""

import sqlite3
import uuid
from typing import NamedTuple

from planet_express.core.store import Store

from .canonical import CanonicalError, loads_strict, sha256_hex
from .models import PHASE_RE, Budgets, Grant, GrantScope, StandingCeilings, StandingGrant, VerificationPlan
from .verify.plan import binding_problems
from .window import allowed as window_allowed

TIER_RANK = {"observe": 0, "propose": 1, "repair": 2, "change": 3, "critical": 4}
ELEVATED_PHASES = ("broad_sudo", "no_backup")


class ApprovalConflict(RuntimeError):
    """A decision for this mission, phase and hash was already recorded."""


class Verdict(NamedTuple):
    ok: bool
    reasons: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.ok


def phase_demands_elevation(phase: str) -> bool:
    return phase in ELEVATED_PHASES or phase.startswith("critical:")


def record_approval(store: Store, mission_id: str, phase: str, bound_hash: str, decision: str,
                    operator: str, elevated: bool = False) -> str:
    if not PHASE_RE.match(phase):
        raise ValueError(f"unknown approval phase {phase!r}")
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny")
    approval_id = f"A-{uuid.uuid4().hex[:16]}"
    with store._write() as conn:
        if decision == "approve":
            row = conn.execute("SELECT tier, grant_json, verification_plan_json FROM missions WHERE id=?",
                               (mission_id,)).fetchone()
            problems = verification_plan_problems(row) if row is not None else []
            if problems:
                raise ValueError("cannot approve: " + "; ".join(problems))
        try:
            conn.execute(
                "INSERT INTO mission_approvals (id, mission_id, phase, bound_hash, decision, operator, "
                "elevated, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, mission_id, phase, bound_hash, decision, operator, int(elevated),
                 int(store._clock())),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise ApprovalConflict(f"{phase} already decided for this hash") from exc
            raise
    return approval_id


def verification_plan_problems(row: sqlite3.Row) -> list[str]:
    """Contract 10 at the store level: a repair/change/critical mission is approvable and executable only with a grant that
    carries `verification.plan_sha256` AND a stored plan that is valid and hashes to it. Not a `validate_grant` courtesy:
    this runs inside `record_approval` and the execution predicate, whoever calls them."""
    if TIER_RANK.get(row["tier"], 0) < TIER_RANK["repair"]:
        return []
    try:
        grant = Grant.model_validate(loads_strict(row["grant_json"]))
        plan = (VerificationPlan.model_validate(loads_strict(row["verification_plan_json"]))
                if row["verification_plan_json"] else None)
    except (CanonicalError, ValueError):
        return ["the grant or the verification plan is unreadable"]
    return binding_problems(grant, plan)


def _approved(conn: sqlite3.Connection, mission_id: str, phase: str, bound_hash: str,
              elevated: bool) -> bool:
    row = conn.execute(
        "SELECT 1 FROM mission_approvals WHERE mission_id=? AND phase=? AND bound_hash=? "
        "AND decision='approve' AND (?=0 OR elevated=1)",
        (mission_id, phase, bound_hash, int(elevated)),
    ).fetchone()
    return row is not None


def has_approving_row(conn: sqlite3.Connection, mission_id: str, bound_hash: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM mission_approvals WHERE mission_id=? AND bound_hash=? AND decision='approve'",
        (mission_id, bound_hash),
    ).fetchone() is not None


def _scope_within(inner: GrantScope, outer: GrantScope) -> bool:
    return all(set(getattr(inner, f)) <= set(getattr(outer, f))
               for f in ("stacks", "services", "paths", "mounts", "hosts"))


def _standing_reasons(conn: sqlite3.Connection, m: sqlite3.Row, grant: Grant, now: int) -> list[str]:
    standing_id = m["standing_grant_id"]
    row = conn.execute("SELECT * FROM standing_grants WHERE id=?", (standing_id,)).fetchone()
    if row is None:
        return ["standing grant does not exist"]
    reasons = []
    # Incident-backed only (brief 5.2): an unattended start answers one open incident, never a free-standing request.
    if not m["incident_id"] or m["origin"] != "incident":
        reasons.append("standing grants cover incident-backed missions only")
    try:
        window = loads_strict(row["window_json"]) if row["window_json"] else None
        ok, why = window_allowed(window, now)
    except (CanonicalError, ValueError, AttributeError, TypeError):
        ok, why = False, "the standing grant's window is unreadable"
    if not ok:
        reasons.append(f"standing grant window: {why}")
    if row["revoked_at"] is not None:
        reasons.append("standing grant is revoked")
    if row["expires_at"] <= now:
        reasons.append("standing grant is expired")
    try:
        raw_scope = loads_strict(row["scope_json"])
        ceilings = StandingCeilings.model_validate(raw_scope.pop("ceilings", None) or {})
        scope = GrantScope.model_validate(raw_scope)
    except (CanonicalError, ValueError, AttributeError):
        return [*reasons, "the standing grant's scope is unreadable"]
    sg = StandingGrant(id=row["id"], scope=scope,
                       tier=row["tier"], created_by=row["created_by"], created_at=row["created_at"],
                       expires_at=row["expires_at"], revoked_at=row["revoked_at"], version=row["version"])
    if TIER_RANK[sg.tier] > TIER_RANK["repair"] or TIER_RANK[grant.tier] > TIER_RANK["repair"]:
        reasons.append("standing grants never cover change or critical")
    if TIER_RANK[grant.tier] > TIER_RANK[sg.tier]:
        reasons.append("grant tier exceeds the standing grant tier")
    if not _scope_within(grant.scope, sg.scope):
        reasons.append("grant scope exceeds the standing grant scope")
    if grant.sudo.mode == "broad" or grant.docker == "raw":
        reasons.append("standing grants refuse broad_sudo")
    if grant.backup.acknowledged_no_backup:
        reasons.append("standing grants refuse no_backup")
    # What the standing grant did not name does not ride along with an unattended start.
    extra = sorted(set(grant.credentials) - set(ceilings.credentials))
    if extra:
        reasons.append(f"credentials the standing grant does not allow: {', '.join(x[:40] for x in extra[:4])}")
    units = sorted({r.unit for r in grant.sudo.rules} - set(ceilings.sudo_units))
    if units:
        reasons.append(f"sudo rules the standing grant does not allow: {', '.join(x[:40] for x in units[:4])}")
    limit = ceilings.budgets or Budgets()
    over = [f for f in ("wall_s", "tool_calls", "output_bytes", "retries") if getattr(grant.budgets, f) > getattr(limit, f)]
    if over:
        reasons.append(f"budgets above the standing grant's ceiling: {', '.join(over)}")
    missing = sorted(set(ceilings.exclusions) - set(grant.exclusions))
    if missing:
        reasons.append(f"exclusions the standing grant requires are missing: {', '.join(x[:40] for x in missing[:4])}")
    return reasons


def evaluate(conn: sqlite3.Connection, mission_id: str, now: int) -> Verdict:
    """The single execution predicate, evaluated inside the caller's transaction."""
    m = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
    if m is None:
        return Verdict(False, ("mission does not exist",))
    if TIER_RANK[m["tier"]] < TIER_RANK["repair"]:
        return Verdict(False, (f"{m['tier']} missions never execute",))
    try:
        grant = Grant.model_validate(loads_strict(m["grant_json"]))
        grant_hash = sha256_hex(grant.model_dump(mode="json"))
    except (CanonicalError, ValueError) as exc:
        return Verdict(False, (f"grant is invalid: {exc}",))
    if grant_hash != m["grant_hash"]:
        return Verdict(False, ("grant_hash does not match grant_json",))
    if grant.tier != m["tier"]:
        return Verdict(False, ("grant tier does not match mission tier",))

    reasons: list[str] = list(verification_plan_problems(m))
    if m["standing_grant_id"]:
        reasons += _standing_reasons(conn, m, grant, now)
    else:
        # (a) the current-grant approval
        if not _approved(conn, mission_id, "grant", grant_hash, grant.tier == "critical"):
            reasons.append("no approval for the current grant hash" if grant.tier != "critical"
                           else "a critical grant needs an elevated grant approval")
        # (d) elevated rows where the grant demands them
        if ((grant.sudo.mode == "broad" or grant.docker == "raw")
                and not _approved(conn, mission_id, "broad_sudo", grant_hash, True)):
            reasons.append("broad sudo needs an elevated broad_sudo approval")
        if (grant.backup.acknowledged_no_backup
                and not _approved(conn, mission_id, "no_backup", grant_hash, True)):
            reasons.append("no backup needs an elevated no_backup approval")

    if TIER_RANK[grant.tier] >= TIER_RANK["change"]:
        reasons += _plan_reasons(conn, m)
    return Verdict(not reasons, tuple(reasons))


def _plan_reasons(conn: sqlite3.Connection, m: sqlite3.Row) -> list[str]:
    if not m["plan_json"] or not m["plan_hash"]:
        return ["change and critical missions need a plan"]
    try:
        plan = loads_strict(m["plan_json"])
        if sha256_hex(plan) != m["plan_hash"]:
            return ["plan_hash does not match plan_json"]
        touches = plan.get("protected_touches", []) if isinstance(plan, dict) else None
        if not isinstance(touches, list):
            return ["plan protected_touches is not a list"]
    except (CanonicalError, ValueError, AttributeError) as exc:
        return [f"plan is invalid: {exc}"]
    reasons = []
    if not _approved(conn, m["id"], "plan", m["plan_hash"], False):
        reasons.append("no approval for the current plan hash")
    for index in range(len(touches)):
        if not _approved(conn, m["id"], f"critical:{index}", m["plan_hash"], True):
            reasons.append(f"protected touch {index} needs an elevated critical:{index} approval")
    return reasons


def mission_step_approved(store: Store, mission_id: str) -> bool:
    """Whether a typed step from origin `mission` may run mutating: the mission's CURRENT grant hash has an approving row
    (or a valid standing grant), judged by the one predicate. This is what `policy.decide_runbook(mission_approved=)` takes."""
    try:
        return bool(execution_predicate(store, mission_id))
    except Exception:                                    # noqa: BLE001 - anything unreadable is "not approved"
        return False


def guarded_touch_approved(store: Store, mission_id: str) -> bool:
    """Whether the mission carries an explicit protected-touch approval, which is what a step on a network-guarded service needs
    on top of the mission's own approval: a critical-tier mission whose predicate holds and whose plan names at least one protected
    touch (so every `critical:<i>` row it needs has been given, elevated). A Repair mission never has one."""
    try:
        with store._connect() as conn:
            m = conn.execute("SELECT tier, plan_json FROM missions WHERE id=?", (mission_id,)).fetchone()
        if m is None or m["tier"] != "critical" or not m["plan_json"]:
            return False
        touches = loads_strict(m["plan_json"]).get("protected_touches", [])
        return isinstance(touches, list) and len(touches) > 0 and bool(execution_predicate(store, mission_id))
    except Exception:                                    # noqa: BLE001 - anything unreadable is "not approved"
        return False


def execution_predicate(store: Store, mission_id: str) -> Verdict:
    with store._connect() as conn:
        return evaluate(conn, mission_id, int(store._clock()))
