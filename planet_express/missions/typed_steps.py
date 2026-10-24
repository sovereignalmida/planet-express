"""Typed steps from the `mission` origin (slice 10).

A mission's agent works with its own tools; a typed step is the fast path for a known repair (restart, start, stop, a stack up
or down). The step is still a typed step: validated per type, bound to its target, run through the engine.
What this module adds is the question the v3 policy cannot answer on its own, "may THIS mission run THIS step now?":

1. every target resolves to a canonical resource id, is inside the grant's scope, is not protected (protected wins), and is
   covered by a lease this mission holds. A binding's container is checked against the daemon's own Compose labels, so it
   cannot name a different container than the stack and service it claims;
2. the mission is `executing`, its session is the live, unexpired, non-revoked one, and the session's fences are current;
3. the approval covers the step type: only the types in MISSION_STEP_TYPES, at most R2, only from a Repair mission, within the
   grant's call budget. A step with no target (a stack-set, a global prune) is never served, and neither is a unit action (R3:
   units are reached through the grant's scoped sudo rules, not through a typed step);
4. the check and its inputs are recorded in the mission's events BEFORE the step is handed back for execution, and the outcome
   after it ran.

`MissionStepGate.authorize` returns a `StepAuthorization` bound to the exact runbook hash, or records the refusal and raises
`StepRefused`. `MissionStepGate.still_current` is asked again between the steps of a running runbook, so a cancel, a pause or a
revoked session stops it. `CommandService.run_mission_runbook` is the only caller that acts on an authorization."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from planet_express.core.redact import redact
from planet_express.execution import actions, runbook as runbooks
from planet_express.missions import approvals, leases
from planet_express.missions.events import redact_value
from planet_express.missions.resources import (
    BadResource,
    canon,
    classify,
    contains,
    split,
)
from planet_express.missions.store import MissionStore, grant_resources

log = logging.getLogger("planetexpress.missions.typed_steps")

ACTOR = "mission-steps"
MAX_RISK = "R2"                  # Change and Critical steps keep their own per-step approvals, which a mission does not carry
_RISK_ORDER = {value: index for index, value in enumerate(("R0", "R1", "R2", "R3", "R4"))}
# The only step types a mission may run. Left out on purpose: stack.up_all / stack.down_all / prune.safe (no target to check),
# stack.down_ingress and unit.action (R3), update.canary (Zoidberg's), compose.write (a mission edits files with its own tools,
# under containment) and compose.restore (built by rollback, never proposed).
MISSION_STEP_TYPES = frozenset({
    "service.restart", "service.start", "service.stop", "stack.up", "stack.down",
    "wait", "check.container", "check.log_since_start", "read.qbittorrent_session_port",
})
_MAX_REASON = 300
_MAX_EVENT_BYTES = 8192
_SEAL = object()                 # only this module mints a StepAuthorization the command service will accept


class StepRefused(Exception):
    """The gate refused the step. The reason has been recorded in the mission's events."""


@dataclass(frozen=True)
class StepAuthorization:
    mission_id: str
    session_id: str
    plan_sha256: str
    resources: tuple[str, ...]
    _seal: object = field(default=None, repr=False, compare=False)

    def valid_for(self, runbook: runbooks.Runbook, mission_id: str) -> bool:
        """Minted by the gate, for this mission, for exactly this runbook document."""
        return (self._seal is _SEAL and self.mission_id == mission_id
                and self.plan_sha256 == runbooks.plan_sha256(runbook))


def _host_identities(containers: list[str]) -> dict[str, tuple[str, str]]:
    from planet_express.execution import actions
    return actions.container_compose_identities(containers)


class MissionStepGate:
    def __init__(self, missions: MissionStore, *, stacks_root: str, protected: Collection[str] = (),
                 identities: Callable[[list[str]], dict[str, tuple[str, str]]] | None = None):
        self.ms = missions
        self.stacks_root = stacks_root
        self.protected = tuple(protected)
        self.identities = identities or _host_identities    # container name -> (compose project, service), from the daemon
        self._lock = threading.Lock()                       # the budget is read, then the authorization recorded: one at a time

    def authorize(self, mission_id: str, runbook: runbooks.Runbook, *, session_id: str) -> StepAuthorization:
        with self._lock:
            steps = [{"type": step.type, "params": step.params} for step in runbook.steps]
            try:
                auth = self._check(mission_id, runbook, session_id)
            except StepRefused as exc:
                self._record(mission_id, "step.refused", {"reason": str(exc)[:_MAX_REASON], "steps": steps, "session": session_id})
                raise
            except Exception as exc:                                   # noqa: BLE001 - a check that cannot be made is a refusal
                log.warning("mission step check failed for %s: %s", mission_id, type(exc).__name__)
                reason = f"the step could not be checked ({type(exc).__name__})"
                self._record(mission_id, "step.refused", {"reason": reason, "steps": steps, "session": session_id})
                raise StepRefused(reason) from None
            self._record(mission_id, "step.authorized", {
                "plan_sha256": auth.plan_sha256, "session": session_id, "steps": steps, "n_steps": len(steps), "resources": list(auth.resources),
                "grant_hash": self.ms.get_mission(mission_id).grant_hash})
            return auth

    def still_current(self, mission_id: str, session_id: str) -> bool:
        """Whether the mission may still act (checked between the steps of a running runbook). False on any doubt."""
        try:
            mission = self.ms.get_mission(mission_id)
            if mission.status != "executing" or mission.cancel_requested_at is not None:
                return False
            self._session_current(mission_id, session_id)
            return True
        except Exception:                                          # noqa: BLE001
            return False

    def record_outcome(self, mission_id: str, auth: StepAuthorization, execution_id: str | None, outcome: str,
                       reason: str) -> None:
        """What the authorized step did, in the mission's own timeline next to the authorization."""
        self._record(mission_id, "step.ran", {"plan_sha256": auth.plan_sha256, "session": auth.session_id,
                                              "execution": execution_id, "outcome": outcome, "reason": reason[:_MAX_REASON]})

    # ── the checks ──────────────────────────────────────────────────────────
    def _check(self, mission_id: str, runbook: runbooks.Runbook, session_id: str) -> StepAuthorization:
        try:
            mission = self.ms.get_mission(mission_id)
        except LookupError:
            raise StepRefused("no such mission") from None
        # 2. the mission may act: executing, no cancel pending, a live and current session
        if mission.status != "executing":
            raise StepRefused(f"the mission is {mission.status}, not executing")
        if mission.cancel_requested_at is not None:
            raise StepRefused("a cancel is pending")
        fences = self._session_current(mission_id, session_id)
        # 3. the approval covers this step type
        unknown = [s.type for s in runbook.steps if s.type not in MISSION_STEP_TYPES]
        if unknown:
            raise StepRefused(f"{unknown[0]} is not a step a mission may run")
        top = runbooks.risk(runbook)
        if _RISK_ORDER[top] > _RISK_ORDER[MAX_RISK]:
            raise StepRefused(f"{top} is above what a mission's approval covers")
        mutating = top != "R0"
        if mutating and mission.tier != "repair":
            raise StepRefused(f"a {mission.tier}-tier mission may not run a mutating step")
        grant = mission.grant()
        if self._authorized_count(mission_id) + len(runbook.steps) > grant.budgets.tool_calls:
            raise StepRefused("the mission's call budget is used up")
        approved = approvals.mission_step_approved(self.ms.store, mission_id)
        if mutating and not approved:
            raise StepRefused("the mission has no approval for its current grant hash")
        # 1. every target resolves, is in scope, is not protected, and is leased by this mission
        try:
            scope = [canon(r) for r in grant_resources(grant)]
            prot = grant.effective_protection(self.protected)
        except BadResource:
            raise StepRefused("the grant's resources are malformed") from None
        resources: list[str] = []
        for step in runbook.steps:
            spec = runbooks.STEP_TYPES[step.type]
            self._container_belongs(step)
            targets = self._targets(step, spec)
            primary_ok = False
            for index, rid in enumerate(targets):
                verdict = classify(rid, scope, prot.protected, stacks_root=self.stacks_root,
                                   parent_of_protected=(spec.risk != "R0"), excluded=prot.excluded)
                if verdict == "out_of_scope" and index > 0 and primary_ok and self._tied_to(step, rid):
                    # The stack a service belongs to and its own compose file are named by the step's binding. When the
                    # service itself is in scope they need no wider grant, but protected still wins for them.
                    verdict = classify(rid, [rid], prot.protected, stacks_root=self.stacks_root,
                                       parent_of_protected=(spec.risk != "R0"), excluded=prot.excluded)
                if index == 0:
                    primary_ok = verdict == "in_scope"
                if verdict != "in_scope":
                    raise StepRefused(f"{rid[:160]} is {'protected' if verdict == 'protected' else 'outside the grant'}")
                if spec.risk != "R0" and not any(contains(held, rid, stacks_root=self.stacks_root) for held in fences):
                    raise StepRefused(f"{rid[:160]} is not leased by this mission")
                resources.append(rid)
        return StepAuthorization(mission_id=mission_id, session_id=session_id, plan_sha256=runbooks.plan_sha256(runbook),
                                 resources=tuple(dict.fromkeys(resources)), _seal=_SEAL)

    def _session_current(self, mission_id: str, session_id: str) -> list[str]:
        """The session is the mission's live one, unexpired, and every fence it holds is current. The leased resources."""
        session = self.ms.live_session(mission_id)
        if session is None or session["id"] != session_id or session.get("state") != "live":
            raise StepRefused("there is no live session for this mission")
        if int(session["expires_at"]) <= self.ms._now():
            raise StepRefused("the session has expired")
        try:
            fences = json.loads(session.get("fence_json") or "{}")
        except ValueError:
            raise StepRefused("the session's fences cannot be read") from None
        if not isinstance(fences, dict):
            raise StepRefused("the session's fences cannot be read")
        for resource, fence in fences.items():
            try:
                leases.check_fence(self.ms.store, str(resource), mission_id, int(fence))
            except (leases.StaleFence, TypeError, ValueError):
                raise StepRefused("the session's lease is stale") from None
        return [str(r) for r in fences]

    def _targets(self, step: runbooks.Step, spec: runbooks.StepType) -> list[str]:
        """The canonical resource ids a step touches. A mutating step that names none is refused: its target is unbounded, so
        no scope check could mean anything (G4 review row 9)."""
        params, binding = step.params, step.binding
        out: list[str] = []
        try:
            if spec.target == "service":
                out.append(canon(f"service:{params['stack']}/{params['service']}"))
            elif spec.target == "stack":
                out.append(canon(f"stack:{params['stack']}"))
            elif isinstance(binding.get("project"), str) and binding["project"]:        # a check names its target in its binding
                if isinstance(binding.get("service"), str) and binding["service"]:
                    out.append(canon(f"service:{binding['project']}/{binding['service']}"))     # the primary target first
                out.append(canon(f"stack:{binding['project']}"))
            compose_path = binding.get("compose_path")
            if isinstance(compose_path, str) and out:
                out.append(canon(f"path:{compose_path}"))                                # the file a step acts on is checked too
        except (BadResource, KeyError, TypeError):
            raise StepRefused(f"a {step.type} target is not a canonical resource") from None
        if not out and spec.risk != "R0":
            raise StepRefused(f"{step.type} names no target a mission may touch")
        return out

    def _tied_to(self, step: runbooks.Step, rid: str) -> bool:
        """Is this derived resource the step's own stack, or a file directly inside that stack's directory?"""
        stack = step.binding.get("project") or step.params.get("stack")
        if not isinstance(stack, str):
            return False
        try:
            _host, kind, name = split(rid)
        except BadResource:
            return False
        if kind == "stack":
            return name == stack
        if kind == "path":
            try:
                return name == canon(f"path:{actions.compose_file(stack)}").partition("/path:")[2]
            except (BadResource, ValueError, TypeError):
                return False
        return False

    def _container_belongs(self, step: runbooks.Step) -> None:
        """The engine acts on the binding's container. Nothing else ties it to the stack and service the step names, so the
        daemon is asked: its Compose labels must say this container is that project's service."""
        binding = step.binding
        container = binding.get("container")
        if container is None:
            return
        project = binding.get("project") or step.params.get("stack")
        service = binding.get("service") or step.params.get("service")
        if not (isinstance(container, str) and isinstance(project, str) and isinstance(service, str)):
            raise StepRefused(f"a {step.type} container cannot be tied to a stack and service")
        try:
            found = self.identities([container]).get(container)
        except Exception:                                          # noqa: BLE001 - unreadable is not proof
            raise StepRefused("the container's Compose identity cannot be read") from None
        if found != (project, service):
            raise StepRefused(f"the container is not {project}/{service}")

    def _authorized_count(self, mission_id: str) -> int:
        with self.ms.store._connect() as conn:
            # every step of an authorized runbook is one call; a record without a count is charged by its steps, or 1
            return int(conn.execute(
                "SELECT COALESCE(SUM(COALESCE(json_extract(body_json, '$.n_steps'), "
                "json_array_length(body_json, '$.steps'), 1)), 0) FROM mission_events "
                "WHERE mission_id=? AND kind='tool' AND actor=? "
                "AND json_extract(body_json, '$.event')='step.authorized'", (mission_id, ACTOR)).fetchone()[0])

    # ── the record ──────────────────────────────────────────────────────────
    def _record(self, mission_id: str, event: str, body: dict[str, Any]) -> None:
        shown = redact_value({**body, "event": event})
        text = redact(json.dumps(shown, sort_keys=True, default=str))
        if len(text) > _MAX_EVENT_BYTES:
            # Keep what identifies the authorization; only the step detail is dropped.
            shown = {"event": event, "truncated": True, "bytes": len(text), "n_steps": body.get("n_steps"), "session": body.get("session"),
                     "plan_sha256": body.get("plan_sha256"), "grant_hash": body.get("grant_hash"),
                     "reason": body.get("reason"), "outcome": body.get("outcome"), "execution": body.get("execution"),
                     "step_types": [s.get("type") for s in body.get("steps", []) if isinstance(s, dict)][:64],
                     "resources": list(body.get("resources", []))[:32]}
        try:
            self.ms.append_event(mission_id, "tool", ACTOR, shown)
        except (LookupError, sqlite3.IntegrityError):               # the mission does not exist: nothing to attach it to
            return
        except Exception:                                           # noqa: BLE001
            if event == "step.authorized":
                raise StepRefused("the check could not be recorded") from None   # no record, no step
            log.warning("could not record %s for %s", event, mission_id)


__all__ = ["ACTOR", "MAX_RISK", "MISSION_STEP_TYPES", "MissionStepGate", "StepAuthorization", "StepRefused"]
