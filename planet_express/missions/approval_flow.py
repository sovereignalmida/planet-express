"""The approval flow (v4 slice 7a): proposing a Repair mission, the approval card, approve and deny bound to the grant hash,
and the operator's rollback of a Repair mission (standing grants were removed in 4.2).

Everything here takes an ACTOR that the caller built from the authenticated dashboard or Telegram identity
(`operator:<name>`); there is no parameter anywhere that lets a request name its own actor. Approval is proven only by a
`mission_approvals` row (`approvals.record_approval`), bound to the hash the operator saw; editing the grant afterwards changes
the hash and every earlier row stops matching. Standing grants (an unattended start) were removed in 4.2: nothing attached one to a
mission, and nothing here can now.
"""

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from planet_express.core.redact import redact
from planet_express.core.store import Store

from . import approvals
from .approvals import ApprovalConflict, phase_demands_elevation, record_approval
from .canonical import CanonicalError, loads_strict
from .events import insert_event
from .models import (
    LEGACY_GRANT_TEXT,
    Budgets,
    Grant,
    GrantScope,
    Mission,
    VerificationPlan,
    is_legacy_grant,
)
from .state import IllegalTransition, MissionNotFound, transition, transition_in_txn
from .store import MissionStore
from .verify.plan import PlanInvalid, validate_plan

log = logging.getLogger("planetexpress.missions.approval_flow")

MAX_LIST = 100
MAX_CARD_TEXT = 2000
OPERATOR_PREFIX = "operator:"
GrantValidator = Callable[[Grant, VerificationPlan], list[str]]


class FlowError(Exception):
    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _clip(text: Any, limit: int = MAX_CARD_TEXT) -> str:
    return redact(str(text))[:limit]


def plain_error(exc: BaseException, limit: int = MAX_CARD_TEXT) -> str:
    """An exception as an operator reads it. A pydantic error becomes "field: what is wrong" for its first two problems, with no
    input values and no documentation links (the raw form put both, and a type name, in the Console's footer)."""
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False, include_input=False, include_context=False)
        parts = []
        for err in errors[:2]:
            where = ".".join(str(p) for p in err.get("loc", ())) or "the request"
            msg = str(err.get("msg", "is not valid")).removeprefix("Value error, ").removeprefix("Assertion failed, ")
            parts.append(f"{where}: {msg}")
        text = "; ".join(parts) + (f" (and {len(errors) - 2} more)" if len(errors) > 2 else "")
    else:
        text = str(exc)
    return _clip(text, limit)


def require_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.startswith(OPERATOR_PREFIX) or len(actor) <= len(OPERATOR_PREFIX):
        raise FlowError("this is an operator decision", "forbidden")
    return actor


def required_phases(mission: Mission) -> list[dict[str, Any]]:
    """Every approval row this mission's current grant needs, in the order an operator is asked. `elevated` rows need an
    authenticated elevated session (the dashboard); Telegram can never give them."""
    grant = mission.grant()
    out = [{"phase": "grant", "bound": mission.grant_hash, "elevated": grant.tier == "critical"}]
    if grant.tier in ("change", "critical") and mission.plan_hash:
        out.append({"phase": "plan", "bound": mission.plan_hash, "elevated": False})
        try:
            plan = loads_strict(mission.plan_json or "{}")
            touches = plan.get("protected_touches", []) if isinstance(plan, dict) else []
        except CanonicalError:
            touches = []
        for index in range(len(touches) if isinstance(touches, list) else 0):
            out.append({"phase": f"critical:{index}", "bound": mission.plan_hash, "elevated": True})
    return out


def _decisions(store: Store, mission: Mission) -> dict[str, str]:
    """phase -> 'approve' | 'deny' for the rows bound to the CURRENT hashes (an older hash's rows are not shown: they prove
    nothing)."""
    with store._connect() as conn:
        rows = conn.execute("SELECT phase, bound_hash, decision FROM mission_approvals WHERE mission_id=?",
                            (mission.id,)).fetchall()
    current = {mission.grant_hash, mission.plan_hash}
    return {r["phase"]: r["decision"] for r in rows if r["bound_hash"] in current}


ACCESS_TEXT = ("FULL ACCESS: the session runs with your access to this host (the Docker socket, sudo, the logs), as you would "
               "at a terminal. PE takes a checkpoint first, records everything it does, checks the result itself and can roll "
               "the checkpointed files back. It refuses only wiping disks, deleting Docker volumes or data, and touching PE's "
               "own config, secrets and database.")


def build_card(store: Store, mission: Mission) -> dict[str, Any]:
    """The approval card an operator reads before deciding. Elevated risks come FIRST (`warnings`), then what the mission works
    on and how it will be judged. Everything is redacted and bounded; nothing here is a secret."""
    grant = mission.grant()
    warnings: list[dict[str, str]] = []
    if is_legacy_grant(mission.grant_json):
        warnings.append({"kind": "legacy", "text": "MADE BEFORE AN UPDATE: " + LEGACY_GRANT_TEXT})
    warnings.append({"kind": "full_access", "text": ACCESS_TEXT})
    if grant.tier == "critical":
        warnings.append({"kind": "critical", "text": "CRITICAL tier: every protected touch in its plan needs its own elevated approval."})
    decisions = _decisions(store, mission)
    required = [{**r, "state": decisions.get(r["phase"], "pending")} for r in required_phases(mission)]
    plan_checks, truncated = approvals.plan_view(mission.verification_plan_json)
    return {
        "mission_id": mission.id, "title": _clip(mission.title, 256), "goal": _clip(mission.goal),
        "status": mission.status, "tier": grant.tier, "grant_hash": mission.grant_hash,
        "plan_hash": mission.plan_hash, "standing_grant_id": mission.standing_grant_id,
        "warnings": warnings,
        "scope": grant.scope.model_dump(mode="json"),
        "budgets": grant.budgets.model_dump(mode="json"),
        "verification": {"plan_sha256": grant.verification.plan_sha256, "checks": plan_checks, "truncated": truncated,
                         "on_verify_fail": grant.on_verify_fail},
        "credentials": [_clip(c, 120) for c in grant.credentials[:32]],
        "window": grant.window.model_dump(mode="json"),
        "required": required, "ready": bool(approvals.execution_predicate(store, mission.id)) and not truncated,
    }


class ApprovalFlow:
    def __init__(self, store: Store, *, supervisor: Any = None, validator: GrantValidator | None = None,
                 budgets: Callable[[], Budgets] | None = None, runtime: str = "command"):
        self.store = store
        self.ms = MissionStore(store)
        self.supervisor = supervisor
        self.validator = validator                  # None: no Repair mission can be proposed (nothing could validate it)
        self.budgets = budgets
        self.runtime = runtime

    # ── helpers ─────────────────────────────────────────────────────────────
    def _mission(self, mission_id: str) -> Mission:
        try:
            return self.ms.get_mission(mission_id)
        except MissionNotFound:
            raise FlowError("Mission not found", "not_found") from None

    def _audit(self, kind: str, **fields: Any) -> None:
        try:
            self.store.record_event(kind, **{k: _clip(v, 300) for k, v in fields.items()})
        except Exception:                           # noqa: BLE001 - the mission/standing rows are the record; this is a copy
            log.warning("could not write the %s audit event", kind)

    # ── the card ────────────────────────────────────────────────────────────
    def card(self, mission_id: str) -> dict[str, Any]:
        mission = self._mission(mission_id)
        if mission.tier in ("observe", "propose"):
            raise FlowError("only a mission that can execute has an approval card", "conflict")
        return build_card(self.store, mission)

    # ── proposing ───────────────────────────────────────────────────────────
    def propose_repair(self, actor: str, *, title: str, goal: str, scope: dict, checks: list[dict],
                       success_criteria: str | None = None, budgets: dict | None = None, credentials: list[str] | None = None,
                       on_verify_fail: str = "needs_operator", incident_id: str | None = None) -> dict[str, Any]:
        """Create a Repair mission waiting in `awaiting_decision` with its approval card. The grant is validated by Core
        (scope within the inventory, budgets within site maximums, a known verification plan) BEFORE anyone is asked."""
        require_actor(actor)
        if self.validator is None:
            raise FlowError("Repair missions are not configured on this host", "unavailable")
        try:
            plan = validate_plan(VerificationPlan.model_validate({"catalogue_v": 1, "checks": checks}))
        except (ValidationError, PlanInvalid, ValueError) as exc:
            raise FlowError(f"Invalid verification plan: {plain_error(exc, 200)}") from None
        if not plan.checks:
            raise FlowError("A repair mission needs at least one verification check")
        available = self._check_availability()
        if available is not None:
            for check in plan.checks:
                why = available.problem(check.check, check.params)
                if why is not None:
                    raise FlowError(f"A verification check cannot run on this host: {why}")
        try:
            default = self.budgets() if self.budgets is not None else Budgets()
            body = {"tier": "repair", "scope": GrantScope.model_validate(scope).model_dump(mode="json"),
                    "credentials": list(credentials or []),
                    "budgets": {**default.model_dump(mode="json"), **(budgets or {})},
                    "on_verify_fail": on_verify_fail,
                    "verification": {"checks": [c.id for c in plan.checks], "agreed_at": int(self.store._clock()),
                                     "plan_sha256": plan.hash()}}
            grant = Grant.model_validate(body)
        except (ValidationError, ValueError) as exc:
            raise FlowError(f"Invalid grant: {plain_error(exc, 300)}") from None
        reasons = self.validator(grant, plan)
        if reasons:
            raise FlowError("The grant is not valid: " + "; ".join(_clip(r, 160) for r in reasons[:6]), "invalid_grant")
        origin = "incident" if incident_id else "operator"
        who = actor
        try:
            mission = self.ms.create_mission(
                title=redact(title), origin=origin, grant=grant, goal=redact(goal),
                success_criteria=redact(success_criteria or "The pre-agreed verification checks pass."),
                runtime=self.runtime, incident_id=incident_id, actor=who)
        except sqlite3.IntegrityError:
            raise FlowError("Incident not found", "not_found") from None
        except ValidationError:
            raise FlowError("Mission text is too long") from None
        try:
            self.ms.set_verification_plan(mission.id, plan, who)
            # draft -> queued -> investigating -> awaiting_decision in ONE transaction: a Repair mission is proposed with its
            # plan already agreed, so it spends no time `investigating` (no session exists); the contract's edges are used
            # as they are and nothing can observe, or crash in, the half-way state.
            now = int(self.store._clock())
            with self.store._write() as conn:
                for state in ("queued", "investigating", "awaiting_decision"):
                    transition_in_txn(conn, mission.id, state, who, now)
        except Exception:
            self._cancel_quietly(mission.id)
            raise
        mission = self.ms.get_mission(mission.id)
        self.ms.append_event(mission.id, "decision", "core", {
            "event": "approval.requested", "grant_hash": mission.grant_hash, "tier": mission.tier, "by": who})
        return {"mission_id": mission.id, "status": mission.status, "grant_hash": mission.grant_hash}

    def _cancel_quietly(self, mission_id: str) -> None:
        try:
            transition(self.store, mission_id, "cancelled", "core")
        except Exception:                           # noqa: BLE001
            log.warning("could not cancel the half-made mission %s", mission_id)

    # ── deciding ────────────────────────────────────────────────────────────
    def approve(self, actor: str, mission_id: str, phase: str, bound_hash: str, *, elevated: bool = False) -> dict[str, Any]:
        """Approve ONE phase for the hash the operator saw. A stale hash (the grant or plan changed since the card was
        drawn) is refused, not re-bound. Elevated phases need an authenticated elevated session (`elevated`, set by the
        dashboard route from its own session, never by the caller)."""
        actor = require_actor(actor)
        mission = self._mission(mission_id)
        if mission.tier in ("observe", "propose"):
            raise FlowError("this mission never executes: nothing to approve", "conflict")
        if mission.status != "awaiting_decision":
            raise FlowError(f"Mission is {mission.status}; it can be approved only while awaiting a decision", "conflict")
        if is_legacy_grant(mission.grant_json):
            raise FlowError(LEGACY_GRANT_TEXT[0].upper() + LEGACY_GRANT_TEXT[1:], "conflict")
        if approvals.plan_view(mission.verification_plan_json)[1]:
            raise FlowError("The verification plan is too large to show in full, so it cannot be approved", "invalid_grant")
        phases = {r["phase"]: r for r in required_phases(mission)}
        if phase not in phases:
            raise FlowError(f"This mission does not need a {phase!r} approval")
        need = phases[phase]
        if bound_hash != need["bound"]:
            raise FlowError("That approval is for an older version of this mission. Reload the card.", "stale")
        wants_elevation = need["elevated"] or phase_demands_elevation(phase)
        if wants_elevation and elevated is not True:
            raise FlowError("This approval needs an elevated dashboard session", "elevation_required")
        if self.validator is not None and phase == "grant":
            try:
                plan = VerificationPlan.model_validate(loads_strict(mission.verification_plan_json or "{}"))
            except (ValidationError, CanonicalError, ValueError):
                raise FlowError("The verification plan is unreadable", "invalid_grant") from None
            reasons = self.validator(mission.grant(), plan)
            if reasons:
                raise FlowError("The grant is no longer valid: " + "; ".join(_clip(r, 160) for r in reasons[:6]), "invalid_grant")
        try:
            record_approval(self.store, mission_id, phase, bound_hash, "approve", actor, elevated=wants_elevation)
        except ApprovalConflict:
            raise FlowError(f"{phase} was already decided for this version", "conflict") from None
        except ValueError as exc:
            raise FlowError(_clip(exc, 300)) from None
        self.ms.append_event(mission_id, "decision", actor, {
            "event": "approval.recorded", "phase": phase, "bound_hash": bound_hash, "elevated": wants_elevation})
        if phase == "grant" and mission.standing_grant_id:
            # The operator approved it themselves: it runs on their approval, like any mission, not on the standing grant. Without
            # this, an approval never counted once a standing grant was attached, so a mission whose standing grant was revoked or
            # expired before it started could never start, while its card asked for this very approval (rc14.7.10, M8). The record
            # says who authorised it.
            now = int(self.store._clock())
            with self.store._write() as conn:
                conn.execute("UPDATE missions SET standing_grant_id=NULL, updated_at=? WHERE id=? AND standing_grant_id=?",
                             (now, mission_id, mission.standing_grant_id))
                insert_event(conn, mission_id, "decision", actor,
                             {"event": "standing.replaced_by_approval", "standing_grant_id": mission.standing_grant_id}, now)
        self._audit("mission.approved", mission=mission_id, phase=phase, operator=actor)
        return self.card(mission_id)

    def deny(self, actor: str, mission_id: str, bound_hash: str) -> dict[str, Any]:
        """Deny the grant: a denial row is recorded (a record, never a proof) and the mission is closed `cancelled`."""
        actor = require_actor(actor)
        mission = self._mission(mission_id)
        if mission.status != "awaiting_decision" or mission.tier in ("observe", "propose"):
            raise FlowError(f"Mission is {mission.status}; it can be denied only while awaiting a decision", "conflict")
        if bound_hash != mission.grant_hash:
            raise FlowError("That decision is for an older version of this mission. Reload the card.", "stale")
        try:
            record_approval(self.store, mission_id, "grant", bound_hash, "deny", actor)
        except ApprovalConflict:
            raise FlowError("grant was already decided for this version", "conflict") from None
        self.ms.append_event(mission_id, "decision", actor, {"event": "approval.denied", "bound_hash": bound_hash})
        try:
            transition(self.store, mission_id, "cancelled", actor)
        except IllegalTransition as exc:
            raise FlowError(_clip(exc, 200), "conflict") from None
        self._audit("mission.denied", mission=mission_id, operator=actor)
        return {"mission_id": mission_id, "status": "cancelled"}

    # ── operator actions on a Repair mission (trust mode keeps only rollback; pause, resume and take-over are gone) ──
    def _check_availability(self) -> Any:
        """The checks this host's verifier can run (`verify.checks.Availability`), or None when there is no verifier to ask."""
        env = getattr(getattr(self.supervisor, "verifier", None), "env", None)
        if env is None:
            return None
        from .verify.checks import CheckEnv, availability
        return availability(env) if isinstance(env, CheckEnv) else None

    def _supervisor(self) -> Any:
        if self.supervisor is None:
            raise FlowError("Repair missions are not running on this host", "unavailable")
        return self.supervisor

    def _operator_action(self, name: str, mission_id: str, actor: str, **kw: Any) -> Any:
        from .supervisor import ResumeRefused
        actor = require_actor(actor)
        sup = self._supervisor()
        self._mission(mission_id)
        try:
            out = getattr(sup, name)(mission_id, actor, **kw)
        except ResumeRefused as exc:
            raise FlowError(_clip(exc, 300), "conflict") from None
        except IllegalTransition as exc:
            raise FlowError(_clip(exc, 300), "conflict") from None
        self._audit(f"mission.{name}", mission=mission_id, operator=actor)
        return out

    def roll_back(self, actor: str, mission_id: str, *, ack_drift: bool = False) -> dict[str, Any]:
        report = self._operator_action("roll_back", mission_id, actor, ack_drift=ack_drift)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status,
                "restore": _clip(getattr(report, "status", "unknown"), 40),
                "reason": _clip(getattr(report, "reason", "") or "", 200)}

    def roll_back_start(self, actor: str, mission_id: str, *, ack_drift: bool = False) -> dict[str, Any]:
        """Start the rollback and return at once (the Console): the restore runs on the supervisor's rollback worker. The same
        operator checks and refusals as `roll_back`; the outcome is read afterwards from the mission's state and record."""
        self._operator_action("roll_back_start", mission_id, actor, ack_drift=ack_drift)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status, "restore": "started", "reason": ""}

    def recheck(self, actor: str, mission_id: str) -> dict[str, Any]:
        """rc14.7: the operator's "Check again" on a held run: PE's own verification runs again (in the background)."""
        self._operator_action("recheck", mission_id, actor)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status}

    def keep_and_close(self, actor: str, mission_id: str) -> dict[str, Any]:
        """rc14.7: the operator keeps what a held run changed and closes it (`failed`, flagged kept_by_operator)."""
        self._operator_action("keep_and_close", mission_id, actor)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status}

    def rollback_in_flight(self, mission_id: str) -> bool:
        """Is a rollback started by `roll_back_start` still running? False where Repair is not running here."""
        sup = self.supervisor
        try:
            return bool(sup is not None and sup.rollback_in_flight(mission_id))
        except Exception:                                  # noqa: BLE001 - a status read never breaks the page
            return False

__all__ = ["ApprovalFlow", "FlowError", "build_card", "require_actor", "required_phases"]
