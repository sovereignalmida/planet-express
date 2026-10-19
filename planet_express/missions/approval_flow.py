"""The approval flow (v4 slice 7a): proposing a Repair mission, the approval card, approve and deny bound to the grant hash,
standing grants, and the operator's actions on a tripwire card.

Everything here takes an ACTOR that the caller built from the authenticated dashboard or Telegram identity
(`operator:<name>`); there is no parameter anywhere that lets a request name its own actor. Approval is proven only by a
`mission_approvals` row (`approvals.record_approval`), bound to the hash the operator saw; editing the grant afterwards changes
the hash and every earlier row stops matching. An unattended start is decided solely by `approvals.evaluate`, through
`start_under_standing`, and recorded on the mission with `standing_grant_id`.
"""

import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from planet_express.core.redact import redact
from planet_express.core.store import Store

from . import approvals, window
from .approvals import ApprovalConflict, phase_demands_elevation, record_approval
from .canonical import CanonicalError, loads_strict
from .events import insert_event
from .grants import with_site_protected
from .models import Budgets, Grant, GrantScope, Mission, VerificationPlan
from .state import IllegalTransition, MissionNotFound, transition, transition_in_txn
from .store import MissionStore
from .verify.plan import PlanInvalid, validate_plan

log = logging.getLogger("planetexpress.missions.approval_flow")

MAX_STANDING_TTL_S = 30 * 86400
MAX_LIST = 100
MAX_CARD_TEXT = 2000
STANDING_TIERS = ("repair",)
OPERATOR_PREFIX = "operator:"
GrantValidator = Callable[[Grant, VerificationPlan], list[str]]


class FlowError(Exception):
    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


def _clip(text: Any, limit: int = MAX_CARD_TEXT) -> str:
    return redact(str(text))[:limit]


def require_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.startswith(OPERATOR_PREFIX) or len(actor) <= len(OPERATOR_PREFIX):
        raise FlowError("this is an operator decision", "forbidden")
    return actor


def required_phases(mission: Mission) -> list[dict[str, Any]]:
    """Every approval row this mission's current grant needs, in the order an operator is asked. `elevated` rows need an
    authenticated elevated session (the dashboard); Telegram can never give them."""
    grant = mission.grant()
    out = [{"phase": "grant", "bound": mission.grant_hash, "elevated": grant.tier == "critical"}]
    if grant.sudo.mode == "broad" or grant.docker == "raw":
        out.append({"phase": "broad_sudo", "bound": mission.grant_hash, "elevated": True})
    if grant.backup.acknowledged_no_backup:
        out.append({"phase": "no_backup", "bound": mission.grant_hash, "elevated": True})
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


def _plan_view(mission: Mission) -> list[dict[str, Any]]:
    try:
        raw = loads_strict(mission.verification_plan_json) if mission.verification_plan_json else {}
    except CanonicalError:
        return []
    out = []
    for check in (raw.get("checks", []) if isinstance(raw, dict) else [])[:64]:
        if isinstance(check, dict):
            out.append({"id": _clip(check.get("id", ""), 128), "check": _clip(check.get("check", ""), 32),
                        "params": {_clip(k, 64): _clip(v, 200) for k, v in list((check.get("params") or {}).items())[:16]}})
    return out


def build_card(store: Store, mission: Mission, *, backup: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The approval card an operator reads before deciding. Elevated risks come FIRST (`warnings`), then what the mission may
    touch and how it will be judged. Everything is redacted and bounded; nothing here is a secret."""
    grant = mission.grant()
    warnings: list[dict[str, str]] = []
    if grant.sudo.mode == "broad":
        warnings.append({"kind": "broad_sudo", "text": "BROAD SUDO: this worker may run any privileged command. Needs an elevated approval."})
    if grant.docker == "raw":
        warnings.append({"kind": "docker_raw", "text": "RAW DOCKER: the worker talks to the Docker daemon without the scope-enforcing proxy."})
    if grant.backup.acknowledged_no_backup:
        warnings.append({"kind": "no_backup", "text": "NO BACKUP: the scope has no recent verified backup and the grant accepts running without one."})
    if grant.tier == "critical":
        warnings.append({"kind": "critical", "text": "CRITICAL tier: may touch protected resources. Every touch needs its own elevated approval."})
    backup_view = {"covered": grant.backup.covered, "recent_s": grant.backup.recent_s, **(dict(backup) if backup else {}),
                   "acknowledged_no_backup": grant.backup.acknowledged_no_backup}       # the grant, not a status source, says this
    decisions = _decisions(store, mission)
    required = [{**r, "state": decisions.get(r["phase"], "pending")} for r in required_phases(mission)]
    return {
        "mission_id": mission.id, "title": _clip(mission.title, 256), "goal": _clip(mission.goal),
        "status": mission.status, "tier": grant.tier, "grant_hash": mission.grant_hash,
        "plan_hash": mission.plan_hash, "standing_grant_id": mission.standing_grant_id,
        "warnings": warnings, "backup": backup_view,
        "scope": grant.scope.model_dump(mode="json"),
        "protected": {"resources": [_clip(r, 200) for r in grant.protected.resources[:64]],
                      "count": len(grant.protected.resources),
                      "exclusions": [_clip(x, 120) for x in grant.exclusions[:32]]},
        "budgets": grant.budgets.model_dump(mode="json"),
        "verification": {"plan_sha256": grant.verification.plan_sha256, "checks": _plan_view(mission),
                         "on_verify_fail": grant.on_verify_fail},
        "sudo": grant.sudo.model_dump(mode="json"), "docker": grant.docker,
        "credentials": [_clip(c, 120) for c in grant.credentials[:32]],
        "window": grant.window.model_dump(mode="json"),
        "required": required, "ready": bool(approvals.execution_predicate(store, mission.id)),
    }


class ApprovalFlow:
    def __init__(self, store: Store, *, supervisor: Any = None, validator: GrantValidator | None = None,
                 backup_status: Callable[[Grant], Mapping[str, Any]] | None = None,
                 protected: Callable[[], Any] | None = None, budgets: Callable[[], Budgets] | None = None,
                 named_windows: Callable[[], Mapping[str, Any]] | None = None, runtime: str = "command"):
        self.store = store
        self.ms = MissionStore(store)
        self.supervisor = supervisor
        self.validator = validator                  # None: no Repair mission can be proposed (nothing could validate it)
        self.backup_status = backup_status          # Grant -> the checkpoint backup floor's view of the scope
        self.protected = protected                  # () -> ProtectedSet, copied into every grant
        self.budgets = budgets
        self.named_windows = named_windows or dict
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

    def _backup_view(self, grant: Grant) -> Mapping[str, Any] | None:
        if self.backup_status is None:
            return None
        try:
            return self.backup_status(grant)
        except Exception:                           # noqa: BLE001 - an unreadable backup status is shown as unverifiable
            return {"covered": False, "state": "unverifiable", "acknowledged_no_backup": grant.backup.acknowledged_no_backup}

    # ── the card ────────────────────────────────────────────────────────────
    def card(self, mission_id: str) -> dict[str, Any]:
        mission = self._mission(mission_id)
        if mission.tier in ("observe", "propose"):
            raise FlowError("only a mission that can execute has an approval card", "conflict")
        out = build_card(self.store, mission, backup=self._backup_view(mission.grant()))
        try:
            from .tripwire.monitor import pause_card
            out["tripwire"] = pause_card(self.ms, mission_id) if mission.status == "paused_tripwire" else None
        except Exception:                           # noqa: BLE001
            out["tripwire"] = None
        return out

    # ── proposing ───────────────────────────────────────────────────────────
    def propose_repair(self, actor: str, *, title: str, goal: str, scope: dict, checks: list[dict],
                       success_criteria: str | None = None, budgets: dict | None = None, sudo: dict | None = None,
                       docker: str = "proxy", exclusions: list[str] | None = None, credentials: list[str] | None = None,
                       on_verify_fail: str = "needs_operator", acknowledged_no_backup: bool = False,
                       incident_id: str | None = None, standing_grant_id: str | None = None) -> dict[str, Any]:
        """Create a Repair mission waiting in `awaiting_decision` with its approval card. The grant is validated by Core
        (scope within the inventory, no protected resource, budgets within site maximums, backup floor) BEFORE anyone is asked.
        With `standing_grant_id` the unattended path runs (`approvals.evaluate`); a refusal there is NOT an error: the mission
        is made without the standing grant and the card says why, so an operator decides instead."""
        unattended = standing_grant_id is not None
        if not unattended:
            require_actor(actor)
        if self.validator is None or self.protected is None:
            raise FlowError("Repair missions are not configured on this host", "unavailable")
        try:
            plan = validate_plan(VerificationPlan.model_validate({"catalogue_v": 1, "checks": checks}))
        except (ValidationError, PlanInvalid, ValueError) as exc:
            raise FlowError(f"Invalid verification plan: {_clip(exc, 200)}") from None
        if not plan.checks:
            raise FlowError("A repair mission needs at least one verification check")
        try:
            default = self.budgets() if self.budgets is not None else Budgets()
            body = {"tier": "repair", "scope": GrantScope.model_validate(scope).model_dump(mode="json"),
                    "exclusions": list(exclusions or []), "credentials": list(credentials or []),
                    "budgets": {**default.model_dump(mode="json"), **(budgets or {})},
                    "docker": docker, "on_verify_fail": on_verify_fail,
                    "sudo": sudo or {"mode": "scoped", "rules": []},
                    "verification": {"checks": [c.id for c in plan.checks], "agreed_at": int(self.store._clock()),
                                     "plan_sha256": plan.hash()},
                    "backup": {"covered": False, "acknowledged_no_backup": bool(acknowledged_no_backup)}}
            grant = Grant.model_validate(body)
            covered = bool((self._backup_view(grant) or {}).get("covered", False))
            grant = Grant.model_validate({**body, "backup": {**body["backup"], "covered": covered}})
            grant = with_site_protected(grant, self.protected())
        except (ValidationError, ValueError) as exc:
            raise FlowError(f"Invalid grant: {_clip(exc, 300)}") from None
        reasons = self.validator(grant, plan)
        if reasons:
            raise FlowError("The grant is not valid: " + "; ".join(_clip(r, 160) for r in reasons[:6]), "invalid_grant")
        origin = "incident" if incident_id else "operator"
        who = actor if not unattended else "core"
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
        decision: dict[str, Any] = {"unattended": False, "reasons": []}
        if unattended:
            decision = self._attach_standing(mission.id, standing_grant_id)
        mission = self.ms.get_mission(mission.id)
        self.ms.append_event(mission.id, "decision", "core", {
            "event": "approval.requested", "grant_hash": mission.grant_hash, "tier": mission.tier,
            "by": who, "standing": mission.standing_grant_id, "standing_refused": decision["reasons"]})
        return {"mission_id": mission.id, "status": mission.status, "grant_hash": mission.grant_hash,
                "unattended": decision["unattended"], "standing_refused": decision["reasons"]}

    def _cancel_quietly(self, mission_id: str) -> None:
        try:
            transition(self.store, mission_id, "cancelled", "core")
        except Exception:                           # noqa: BLE001
            log.warning("could not cancel the half-made mission %s", mission_id)

    def _attach_standing(self, mission_id: str, standing_id: str) -> dict[str, Any]:
        """Atomically: name the standing grant on the mission, ask the one predicate, keep it only if it holds. Refusal detaches
        it again in the same transaction, so a mission is never observable half-way and an operator's approval path stays open."""
        now = int(self.store._clock())
        with self.store._write() as conn:
            conn.execute("UPDATE missions SET standing_grant_id=?, updated_at=? WHERE id=?", (standing_id, now, mission_id))
            verdict = approvals.evaluate(conn, mission_id, now)
            if verdict.ok:
                insert_event(conn, mission_id, "decision", "core",
                             {"event": "standing.accepted", "standing_grant_id": standing_id}, now)
                return {"unattended": True, "reasons": []}
            conn.execute("UPDATE missions SET standing_grant_id=NULL, updated_at=? WHERE id=?", (now, mission_id))
            reasons = [_clip(r, 200) for r in verdict.reasons][:8]
            insert_event(conn, mission_id, "decision", "core",
                         {"event": "standing.refused", "standing_grant_id": standing_id, "reasons": reasons}, now)
            return {"unattended": False, "reasons": reasons}

    def start_under_standing(self, standing_grant_id: str, **kwargs: Any) -> dict[str, Any]:
        """The unattended path: same proposal, no operator. In scope, incident-backed, unexpired, unrevoked and outside any
        denied window it is accepted (the supervisor then starts it); otherwise it waits as an ordinary approval card."""
        if not kwargs.get("incident_id"):
            raise FlowError("a standing grant covers incident-backed missions only", "forbidden")
        return self.propose_repair("core", standing_grant_id=standing_grant_id, **kwargs)

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

    # ── tripwire card actions ───────────────────────────────────────────────
    def _supervisor(self) -> Any:
        if self.supervisor is None:
            raise FlowError("Repair missions are not running on this host", "unavailable")
        return self.supervisor

    def _tripwire_action(self, name: str, mission_id: str, actor: str, **kw: Any) -> Any:
        from .supervisor import ResumeRefused
        from .tripwire.monitor import RollbackRefused
        actor = require_actor(actor)
        sup = self._supervisor()
        self._mission(mission_id)
        try:
            out = getattr(sup, name)(mission_id, actor, **kw)
        except (ResumeRefused, RollbackRefused) as exc:
            raise FlowError(_clip(exc, 300), "conflict") from None
        except IllegalTransition as exc:
            raise FlowError(_clip(exc, 300), "conflict") from None
        self._audit(f"mission.{name}", mission=mission_id, operator=actor)
        return out

    def resume(self, actor: str, mission_id: str) -> dict[str, Any]:
        self._tripwire_action("resume", mission_id, actor)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status}

    def roll_back(self, actor: str, mission_id: str, *, ack_drift: bool = False) -> dict[str, Any]:
        report = self._tripwire_action("roll_back", mission_id, actor, ack_drift=ack_drift)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status,
                "restore": _clip(getattr(report, "status", "unknown"), 40),
                "reason": _clip(getattr(report, "reason", "") or "", 200)}

    def take_over(self, actor: str, mission_id: str) -> dict[str, Any]:
        self._tripwire_action("take_over", mission_id, actor)
        return {"mission_id": mission_id, "status": self.ms.get_mission(mission_id).status}

    # ── standing grants ─────────────────────────────────────────────────────
    def _window(self, raw: dict | None) -> dict | None:
        try:
            parsed = window.resolve_names(raw, dict(self.named_windows())) if raw else None
        except (TypeError, ValueError):
            raise FlowError("Invalid window") from None
        bad = window.unreadable(parsed)
        if bad:
            raise FlowError("The window holds entries that are not times: " + ", ".join(b[:40] for b in bad[:4]))
        return parsed

    def _check_standing(self, scope: dict, tier: str, ttl_s: int, window_raw: dict | None) -> tuple[dict, dict | None, int]:
        if tier not in STANDING_TIERS:
            raise FlowError("Standing grants cover the repair tier only, never change or critical", "forbidden")
        if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or not 60 <= ttl_s <= MAX_STANDING_TTL_S:
            raise FlowError(f"A standing grant must expire within {MAX_STANDING_TTL_S // 86400} days (and last at least a minute)")
        try:
            scope_model = GrantScope.model_validate(scope)
        except ValidationError:
            raise FlowError("Invalid scope") from None
        if not (scope_model.stacks or scope_model.services or scope_model.paths or scope_model.mounts):
            raise FlowError("A standing grant needs a non-empty scope")
        return scope_model.model_dump(mode="json"), self._window(window_raw), int(self.store._clock()) + ttl_s

    def standing_create(self, actor: str, *, scope: dict, ttl_s: int, window_spec: dict | None = None,
                        tier: str = "repair", elevated: bool = False) -> dict[str, Any]:
        actor = require_actor(actor)
        if elevated is not True:
            raise FlowError("Creating a standing grant needs an elevated dashboard session", "elevation_required")
        scope_d, win, expires = self._check_standing(scope, tier, ttl_s, window_spec)
        with self.store._write() as conn:
            n = conn.execute("SELECT COUNT(*) FROM standing_grants").fetchone()[0] + 1
            while conn.execute("SELECT 1 FROM standing_grants WHERE id LIKE ?", (f"SG-{n}-v%",)).fetchone():
                n += 1
        grant_id = f"SG-{n}-v1"
        self.ms.create_standing_grant(grant_id=grant_id, scope=scope_d, tier=tier, created_by=actor, expires_at=expires,
                                      window=win, version=1)
        self._audit("mission.standing_created", standing=grant_id, operator=actor, expires_at=expires)
        return self._standing_row(grant_id)

    def standing_supersede(self, actor: str, grant_id: str, *, scope: dict, ttl_s: int, window_spec: dict | None = None,
                           elevated: bool = False) -> dict[str, Any]:
        """A changed standing grant is a NEW version: the old one is revoked in the same step, so a mission can never start
        under a version nobody currently stands behind."""
        actor = require_actor(actor)
        if elevated is not True:
            raise FlowError("Changing a standing grant needs an elevated dashboard session", "elevation_required")
        with self.store._connect() as conn:
            old = conn.execute("SELECT * FROM standing_grants WHERE id=?", (grant_id,)).fetchone()
        if old is None:
            raise FlowError("Standing grant not found", "not_found")
        if old["revoked_at"] is not None:
            raise FlowError("That standing grant version is already revoked", "conflict")
        scope_d, win, expires = self._check_standing(scope, old["tier"], ttl_s, window_spec)
        base = grant_id.rsplit("-v", 1)[0]
        new_id = f"{base}-v{old['version'] + 1}"
        try:
            self.ms.create_standing_grant(grant_id=new_id, scope=scope_d, tier=old["tier"], created_by=actor,
                                          expires_at=expires, window=win, version=old["version"] + 1)
        except sqlite3.IntegrityError:
            raise FlowError("That version already exists", "conflict") from None
        self.ms.revoke_standing_grant(grant_id)
        self._audit("mission.standing_superseded", standing=new_id, replaces=grant_id, operator=actor)
        return self._standing_row(new_id)

    def standing_revoke(self, actor: str, grant_id: str) -> dict[str, Any]:
        actor = require_actor(actor)
        with self.store._connect() as conn:
            if conn.execute("SELECT 1 FROM standing_grants WHERE id=?", (grant_id,)).fetchone() is None:
                raise FlowError("Standing grant not found", "not_found")
        self.ms.revoke_standing_grant(grant_id)          # idempotent; takes effect for every later start at once
        self._audit("mission.standing_revoked", standing=grant_id, operator=actor)
        return self._standing_row(grant_id)

    def _standing_row(self, grant_id: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM standing_grants WHERE id=?", (grant_id,)).fetchone()
        now = int(self.store._clock())
        state = "revoked" if row["revoked_at"] is not None else ("expired" if row["expires_at"] <= now else "active")
        return {"id": row["id"], "version": row["version"], "tier": row["tier"], "state": state,
                "scope": loads_strict(row["scope_json"]), "window": loads_strict(row["window_json"]) if row["window_json"] else None,
                "created_by": row["created_by"], "created_at": row["created_at"], "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"]}

    def standing_list(self, *, include_inactive: bool = False, limit: int = MAX_LIST) -> dict[str, Any]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT id FROM standing_grants ORDER BY created_at DESC, id DESC LIMIT ?",
                                (min(limit, MAX_LIST),)).fetchall()
        out = [self._standing_row(r["id"]) for r in rows]
        if not include_inactive:
            out = [r for r in out if r["state"] == "active"]
        return {"standing_grants": out}


__all__ = ["ApprovalFlow", "FlowError", "build_card", "require_actor", "required_phases"]
