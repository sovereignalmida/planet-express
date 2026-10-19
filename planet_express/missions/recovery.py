"""Crash classification (contract 11, brief 14.8): after any crash Core reads HOST STATE and decides; it never asks the
runtime and never believes the agent's own events.

    state == checkpoint            -> needs_operator, offering resume (an automatic restart only for Observe/Propose)
    state matches the end expected -> verifying (verification is read-only, so running it again is safe)
    anything else, or unreadable   -> recovery_required, then needs_operator or rollback_pending

Nothing here replays anything. Intent is journaled by Core before a session starts and at each phase change
(`journal_intent`); an intent with no `intent.done` is one whose effect is UNKNOWN, and recovery lists it under
`refused_replays` and leaves it to an operator decision. Only events written by `core` or `supervisor` are read: an
event stream the agent can influence (worker or tool events) has no say, so a hostile stream claiming success changes
nothing.
"""

import logging
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import Grant, Mission, MissionEvent
from .state import IllegalTransition, is_legal, transition
from .store import MissionStore

log = logging.getLogger("planetexpress.missions.recovery")

TRUSTED_ACTORS = frozenset({"core", "supervisor"})
RESTORABLE = ("image_id", "command", "env_hash", "mounts", "networks", "labels", "restart_policy", "health")
PHASES = ("investigation", "execution", "verification")
MUTATING = ("repair", "change", "critical")


# ── the journal ─────────────────────────────────────────────────────────────────────────────────────

def journal_intent(ms: MissionStore, mission_id: str, phase: str, summary: str = "", *, actor: str = "core") -> str:
    """Write the intent BEFORE acting. Returns its id; `journal_done` closes it once the effect is known."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    intent_id = "I-" + secrets.token_hex(6)
    ms.append_event(mission_id, "note", actor, {"event": "intent", "id": intent_id, "phase": phase,
                                                "summary": summary[:200]})
    return intent_id


def journal_done(ms: MissionStore, mission_id: str, intent_id: str, effect: str, *, actor: str = "core") -> None:
    """Close an intent whose effect Core has established: `applied`, `not_applied` or `rolled_back`."""
    if effect not in ("applied", "not_applied", "rolled_back"):
        raise ValueError(f"unknown effect {effect!r}")
    ms.append_event(mission_id, "note", actor, {"event": "intent.done", "id": intent_id, "effect": effect})


def _body(event: MissionEvent) -> dict[str, Any]:
    import json
    try:
        body = json.loads(event.body_json)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def open_intents(events: Iterable[MissionEvent]) -> list[str]:
    """Ids of journaled intents with no `intent.done`: their effect is unknown. Trusted actors only."""
    opened: dict[str, str] = {}
    for event in events:
        if event.kind != "note" or event.actor not in TRUSTED_ACTORS:
            continue
        body = _body(event)
        if body.get("event") == "intent" and isinstance(body.get("id"), str):
            opened[body["id"]] = str(body.get("phase", ""))
        elif body.get("event") == "intent.done":
            opened.pop(str(body.get("id")), None)
    return sorted(opened)


# ── host state (Core's own reading) ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HostState:
    """What Core read from the host just now. `readable` is False when any part could not be read: classification then
    treats the state as diverged (it cannot be shown equal to anything)."""
    readable: bool = True
    files: Mapping[str, str | None] = field(default_factory=dict)          # path -> sha256 (None: absent)
    services: Mapping[str, dict[str, Any] | None] = field(default_factory=dict)   # <stack>/<svc> -> restorable facts
    units: Mapping[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def collect_host_state(checkpoint_service: Any, manifest: Mapping[str, Any]) -> HostState:
    """Read every file and service the manifest covers, through the S5 service and its provider. A read that fails
    makes the whole state unreadable. Ports and the mount table are NOT compared: they move without anyone changing
    anything, and the tripwire collectors judge them while a session runs."""
    problems: list[str] = []
    files: dict[str, str | None] = {}
    for entry in manifest.get("files", []):
        sha, _data, problem, _meta = checkpoint_service._current(entry)
        if problem is not None:
            problems.append(f"{entry['path']}: unreadable")
        files[entry["path"]] = sha
    services: dict[str, dict[str, Any] | None] = {}
    for name in manifest.get("facts", {}).get("services", {}):
        try:
            facts = checkpoint_service.provider.inspect(name)
        except Exception:                                    # noqa: BLE001 - provider failures are unreadable state
            problems.append(f"{name}: facts unreadable")
            continue
        services[name] = None if facts is None else {k: v for k, v in facts.to_dict().items() if k in RESTORABLE}
    units: dict[str, str] = {}
    wanted = list(manifest.get("facts", {}).get("units", {}))
    if wanted:
        try:
            units = dict(checkpoint_service.provider.unit_hashes(wanted))
        except Exception:                                    # noqa: BLE001
            problems.append("units unreadable")
    return HostState(readable=not problems, files=files, services=services, units=units, problems=tuple(problems))


def matches_checkpoint(host: HostState, manifest: Mapping[str, Any]) -> bool:
    """Is the host exactly as the checkpoint recorded it? Anything unknown is "no"."""
    if not host.readable:
        return False
    for entry in manifest.get("files", []):
        if entry["path"] not in host.files or host.files[entry["path"]] != entry["sha256"]:
            return False
    for name, facts in manifest.get("facts", {}).get("services", {}).items():
        if name not in host.services:
            return False
        have = host.services[name]
        if (facts is None) != (have is None):
            return False
        if facts is not None and any(facts.get(k) != have.get(k) for k in RESTORABLE):    # type: ignore[union-attr]
            return False
    for name, digest in manifest.get("facts", {}).get("units", {}).items():
        if host.units.get(name) != digest:
            return False
    return True


# ── the end expectation (from the APPROVED plan only) ───────────────────────────────────────────────

@dataclass(frozen=True)
class Expectation:
    files: Mapping[str, str] = field(default_factory=dict)                    # path -> sha256
    services: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)   # service -> subset of restorable facts

    def __bool__(self) -> bool:
        return bool(self.files or self.services)


def end_expectation(mission: Mission) -> Expectation | None:
    """The end state the approved plan declares under `expected_state`, or None. Only a Change/Critical plan carries
    one, and only the plan Core hashed and an operator approved: nothing the runtime reports can set it."""
    if mission.tier not in ("change", "critical") or not mission.plan_json:
        return None
    from .canonical import CanonicalError, loads_strict
    try:
        plan = loads_strict(mission.plan_json)
    except CanonicalError:
        return None
    raw = plan.get("expected_state") if isinstance(plan, dict) else None
    if not isinstance(raw, dict):
        return None
    files, services = raw.get("files", {}), raw.get("services", {})
    if (not isinstance(files, dict) or not isinstance(services, dict)
            or any(not isinstance(v, str) for v in files.values())
            or any(not isinstance(v, dict) or not set(v) <= set(RESTORABLE) for v in services.values())):
        return None
    expectation = Expectation(files, services)
    return expectation or None


def matches_expectation(host: HostState, expected: Expectation | None) -> bool:
    if expected is None or not host.readable:
        return False
    if any(host.files.get(path, object()) != sha for path, sha in expected.files.items()):
        return False
    for name, facts in expected.services.items():
        have = host.services.get(name)
        if have is None or any(have.get(k) != v for k, v in facts.items()):
            return False
    return True


# ── classification ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Classification:
    state: str                         # needs_operator | verifying | recovery_required
    reason: str
    phase: str
    next_state: str | None = None      # for recovery_required: needs_operator | rollback_pending
    offers: tuple[str, ...] = ()       # what an operator may choose; "replay" is never among them
    auto_restart: bool = False         # only ever True for Observe/Propose
    partially_applied: bool = False
    refused_replays: tuple[str, ...] = ()      # journaled intents whose effect is unknown: never replayed


def _phase(status: str) -> str:
    return {"investigating": "investigation", "verifying": "verification"}.get(status, "execution")


def classify(mission: Mission, host_state: HostState | None, checkpoint: Mapping[str, Any] | None,
             events: Sequence[MissionEvent]) -> Classification:
    """Decide what a crashed mission is, from the host and the trusted journal. `events` is read only for Core's own
    journal; `host_state` must come from `collect_host_state` (Core's reading), never from the runtime."""
    if mission.status in ("completed", "failed", "rolled_back", "cancelled"):
        raise ValueError(f"{mission.id} is already {mission.status}")
    phase = _phase(mission.status)
    if mission.tier in ("observe", "propose"):
        # No mutation is possible: the one automatic restart there is.
        return Classification("needs_operator", "an observe/propose session cannot have changed anything", phase,
                              offers=("restart", "cancel"), auto_restart=True)
    unknown = tuple(open_intents(events))
    grant: Grant = mission.grant()
    if checkpoint is None or host_state is None:
        return Classification("recovery_required", "there is no checkpoint or no host reading to compare with", phase,
                              next_state="needs_operator", offers=("take_over", "cancel"), partially_applied=True,
                              refused_replays=unknown)
    if matches_checkpoint(host_state, checkpoint):
        return Classification("needs_operator", "the host is as the checkpoint recorded it: nothing was applied", phase,
                              offers=("resume", "cancel"), refused_replays=unknown)
    if matches_expectation(host_state, end_expectation(mission)):
        return Classification("verifying", "the host matches the end state the approved plan expects", phase,
                              partially_applied=False, refused_replays=unknown)
    reason = ("the host could not be read completely" if not host_state.readable
              else "the host differs from both the checkpoint and the expected end state")
    rollback = grant.on_verify_fail == "rollback"
    return Classification("recovery_required", reason, phase,
                          next_state="rollback_pending" if rollback else "needs_operator",
                          offers=("rollback", "take_over", "cancel"), partially_applied=True,
                          refused_replays=unknown)


def apply_classification(ms: MissionStore, mission_id: str, c: Classification, *, actor: str = "core") -> str:
    """Take the classification's edge(s) and record it. Returns the state the mission is in afterwards. A refused
    edge leaves the mission where it was (it is already non-terminal and recoverable by an operator)."""
    ms.append_event(mission_id, "decision", actor, {
        "event": "recovery.classified", "state": c.state, "next": c.next_state, "phase": c.phase, "reason": c.reason,
        "offers": list(c.offers), "partially_applied": c.partially_applied,
        "refused_replays": list(c.refused_replays)})
    if c.partially_applied:
        ms.set_outcome(mission_id, ms.get_mission(mission_id).outcome, "partially_applied", actor)
    path = [c.state] if c.next_state is None else [c.state, c.next_state]
    for target in path:
        current = ms.get_mission(mission_id).status
        if current == target:
            continue
        if not is_legal(current, target) and target == "verifying":
            path_via = "recovery_required"
            if is_legal(current, path_via):
                transition(ms.store, mission_id, path_via, actor)
        try:
            transition(ms.store, mission_id, target, actor)
        except IllegalTransition as exc:
            log.warning("recovery edge %s -> %s refused for %s: %s", current, target, mission_id, exc)
            break
    return ms.get_mission(mission_id).status
