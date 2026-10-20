"""Construction of the Repair-tier controls (v4 slice 7a): the pieces slices 5, 6 and 8 built and left unconstructed.

`build_repair_controls` is called by `runner.build_mission_runner` only when missions are enabled and the daemon passes a
`RepairWiring`. It builds, from `missions.*` config:

    CheckpointService   root `<state>/checkpoints` (or missions.checkpoint.root), backup freshness from
                        missions.checkpoint.backup_sources, a baseline provider that normalises Leela's full scan
    TripwireMonitor     over the protected set, the checkpoint service and the containment; closing the brokered tool endpoints
                        (and, with slice 7b, the session's Docker proxy) when it pauses a session
    Verifier            Core-run verification over the same scan
    repair gate         the side-door report (worker identity, no Docker TCP, only generator-managed sudoers)

With `missions.enabled` false nothing here is called. A control that cannot be built is a refusal to build ALL of them: a
partial set would be a Repair mission without a safety net, so the caller falls back to no Repair capability.
"""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from planet_express.core.store import Store

from .approval_flow import ApprovalFlow
from .checkpoint import CheckpointService, CheckpointSettings, ConfiguredBackupFreshness
from .checkpoint.backup import BackupSource
from .grants import Inventory, SiteLimits, validate_grant
from .models import Budgets, Grant, VerificationPlan
from .store import MissionStore
from .sudo_gen import outside_ceiling
from .tools import default_registry
from .tripwire.build import build_tripwire
from .verify.baseline import normalize
from .verify.runner import verifier_from_config

log = logging.getLogger(__name__)

_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
MAX_COMPOSE_BYTES = 1 << 20


@dataclass(frozen=True)
class RepairWiring:
    """What the daemon supplies and Core code cannot derive: the host's checkpoint provider and its scan."""
    provider: Any                                   # a `CheckpointProvider` (Systemd or Mos) over the stacks root
    scan: Callable[[], Mapping[str, Any]]           # `casa_leela.run_full`
    core_user: str                                  # the account Core runs as (the side-door gate compares against it)


@dataclass
class RepairControls:
    checkpoint: CheckpointService
    tripwire: Any
    verifier: Any
    repair_gate: Callable[[], Any]
    protected: Any
    backup_status: Callable[[Grant], Mapping[str, Any]]
    validator: Callable[[Grant, VerificationPlan], list[str]]
    inventory: Callable[[], Inventory]


def mission_write_gate(store: Store) -> Callable[[str, str, dict], bool]:
    """The brokered-tool write gate (slice 6 left it for 7): a write tool runs only for a Repair mission that is `executing`
    and whose current grant hash is approved (the one execution predicate). Anything else, or anything unreadable, refuses."""
    from . import approvals
    ms = MissionStore(store)

    def gate(mission_id: str, tool: str, params: dict) -> bool:
        try:
            mission = ms.get_mission(mission_id)
            return mission.tier == "repair" and mission.status == "executing" and bool(approvals.execution_predicate(store, mission_id))
        except Exception:                           # noqa: BLE001 - fail closed
            return False
    return gate


def discover_inventory(stacks_root: str) -> Inventory:
    """Stacks are the directories under the stacks root holding a compose file; services are that file's `services` keys.
    Unreadable or oversized files contribute no services (a grant naming one is then refused, never widened)."""
    import yaml
    stacks: list[str] = []
    services: list[str] = []
    try:
        entries = sorted(os.scandir(stacks_root), key=lambda e: e.name)
    except OSError:
        return Inventory(hosts=("local",), stacks=(), services=(), mounts=(), path_roots=(stacks_root,))
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        for name in _COMPOSE_NAMES:
            path = os.path.join(entry.path, name)
            try:
                if os.path.getsize(path) > MAX_COMPOSE_BYTES:
                    stacks.append(entry.name)
                    break
                with open(path, encoding="utf-8") as handle:
                    doc = yaml.safe_load(handle)
            except (OSError, yaml.YAMLError, UnicodeDecodeError):
                continue
            stacks.append(entry.name)
            found = doc.get("services") if isinstance(doc, dict) else None
            services += [f"{entry.name}/{svc}" for svc in (found if isinstance(found, dict) else {})]
            break
    return Inventory(hosts=("local",), stacks=tuple(stacks), services=tuple(services), mounts=(), path_roots=(stacks_root,))


def build_repair_controls(store: Store, cfg: Any, wiring: RepairWiring, *, containment: Any, broker: Any, protected: Any,
                          stacks_root: str, enabled: Callable[[], bool], proxy: Any = None) -> RepairControls:
    ms = MissionStore(store)
    state_dir = Path(store.path).parent
    sources = [BackupSource(s.path, s.marker) for s in cfg.checkpoint.backup_sources]
    backup = ConfiguredBackupFreshness(sources)
    checkpoint = CheckpointService(
        mission_store=ms, root=cfg.checkpoint.root or str(state_dir / "checkpoints"), stacks_root=stacks_root,
        provider=wiring.provider, backup=backup, settings=CheckpointSettings.from_config(cfg.checkpoint), enabled=enabled,
        baseline_provider=lambda: normalize(wiring.scan()))

    def revoke(mission_id: str) -> None:
        """The monitor's pause closes every capability endpoint the session holds."""
        if broker is not None:
            broker.revoke(mission_id)
        if proxy is not None:
            proxy.pause(mission_id)

    tripwire = build_tripwire(cfg, missions=ms, containment=containment, checkpoint=checkpoint, provider=wiring.provider,
                              stacks_root=stacks_root, state_dir=str(state_dir / "tripwire"), protected=protected,
                              enabled=enabled, revoke_endpoints=revoke)
    if tripwire is None:
        raise RuntimeError("the tripwire monitor was not built")
    verifier = verifier_from_config(cfg, missions=ms, checkpoint=checkpoint, provider=wiring.provider, broker=broker,
                                    scan=wiring.scan, stacks_root=stacks_root, enabled=enabled)

    from .launcher.side_doors import SideDoorPaths, repair_gate_report, side_door_steps
    paths = SideDoorPaths(core_user=wiring.core_user, uid_min=cfg.session_uid_min, uid_max=cfg.session_uid_max,
                          proxy_ports=tuple(cfg.docker_proxy_ports), managed=managed_sudoers(cfg, wiring.core_user))
    steps = side_door_steps(paths)

    def gate() -> Any:
        return repair_gate_report(steps)

    known_tools = default_registry().names()
    limits = SiteLimits.from_config(cfg.budgets)

    def inventory() -> Inventory:
        return discover_inventory(stacks_root)

    def validator(grant: Grant, plan: VerificationPlan) -> list[str]:
        problems = validate_grant(grant, inventory=inventory(), protected=protected, known_checks={c.id for c in plan.checks},
                                  known_tools=known_tools, limits=limits, stacks_root=stacks_root, verification_plan=plan)
        if grant.sudo.mode == "scoped":              # broad sudo is a separate, elevated approval and is never generated
            problems += outside_ceiling(cfg.worker_sudo_rules if cfg.worker_sudo_all_sessions else [], grant.sudo.rules)
        return problems

    def backup_status(grant: Grant) -> Mapping[str, Any]:
        """The backup floor's view of the grant's scope, for the approval card and the grant's `covered` flag."""
        now, states = int(store._clock()), []
        targets = [os.path.join(stacks_root, s) for s in grant.scope.stacks]
        targets += [os.path.join(stacks_root, s.split("/", 1)[0]) for s in grant.scope.services]
        targets += list(grant.scope.mounts) + list(grant.scope.paths)
        for path in dict.fromkeys(targets):
            st = backup.status(path, now=now, recent_s=grant.backup.recent_s)
            states.append({"path": path, "state": st.state, "last_success_at": st.last_success_at})
        rank = {"ok": 0, "stale": 1, "unverifiable": 2, "absent": 3}               # the worst path decides, not the first
        worst = max((x["state"] for x in states), key=lambda st_: rank.get(st_, 3)) if states else "unverifiable"
        return {"covered": bool(states) and all(x["state"] == "ok" for x in states), "state": worst,
                "acknowledged_no_backup": grant.backup.acknowledged_no_backup, "paths": states[:16]}

    return RepairControls(checkpoint=checkpoint, tripwire=tripwire, verifier=verifier, repair_gate=gate, protected=protected,
                          backup_status=backup_status, validator=validator, inventory=inventory)


def managed_sudoers(cfg: Any, core_user: str) -> dict[str, bytes]:
    """Sudoers path -> the exact bytes setup's generators write there: the side-door check treats these, and only these, as
    managed (a byte of difference is an unmanaged rule). The launcher rule is Core's own; the worker file is the generated
    ceiling (absent when no rules are configured)."""
    from .launcher.setup_steps import LauncherSudoers, Paths, WorkerSudoers
    paths = Paths(core_uid=0, core_gid=0, workspace_root="", core_user=core_user, uid_min=cfg.session_uid_min,
                  uid_max=cfg.session_uid_max,
                  worker_sudo_rules=tuple((r.unit, tuple(r.actions)) for r in cfg.worker_sudo_rules),
                  worker_sudo_all_sessions=cfg.worker_sudo_all_sessions)
    managed: dict[str, bytes] = {}
    launcher = LauncherSudoers(paths)
    if not launcher.unsafe():
        managed[paths.sudoers] = launcher.wanted()
    worker = WorkerSudoers(paths)
    if worker.applies() and not worker.unsafe():
        managed[paths.worker_sudoers] = worker.wanted()
    return managed


def build_approval_flow(store: Store, cfg: Any, runner: Any, controls: RepairControls | None, *,
                        runtime: str = "command") -> ApprovalFlow | None:
    """The approval flow over a built runner. None without Repair controls: nothing could validate or run a Repair mission, so
    the surfaces answer 'unavailable' rather than offering an approval that could never execute."""
    if controls is None or runner is None:
        return None
    return ApprovalFlow(
        store, supervisor=runner.supervisor, validator=controls.validator, backup_status=controls.backup_status,
        protected=lambda: controls.protected,
        budgets=lambda: Budgets(wall_s=min(cfg.budgets.wall_seconds, 86400 * 7), tool_calls=min(cfg.budgets.tool_calls, 100000),
                                output_bytes=min(cfg.budgets.output_bytes, 2**40), retries=min(cfg.budgets.retries, 100)),
        named_windows=lambda: dict(cfg.windows), runtime=runtime)


__all__ = ["RepairControls", "RepairWiring", "build_approval_flow", "build_repair_controls", "discover_inventory",
           "managed_sudoers", "mission_write_gate"]
