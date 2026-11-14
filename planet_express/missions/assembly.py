"""Construction of the Repair-tier controls (v4 slice 7a): the pieces slices 5, 6 and 8 built and left unconstructed.

`build_repair_controls` is called by `runner.build_mission_runner` only when missions are enabled and the daemon passes a
`RepairWiring`. It builds, from `missions.*` config:

    CheckpointService   root `<state>/checkpoints` (or missions.checkpoint.root), a baseline provider that normalises Leela's
                        full scan
    Verifier            Core-run verification over the same scan
    validator           the grant checks (scope within the inventory, budgets within the site's, a known verification plan)

Trust mode (rc14) removed the side-door gate, the backup floor and the protected set: a Repair session has full access to the
host, and what answers for it is the confirmed plan, the checkpoint, the record and PE's own verification. With
`missions.enabled` false nothing here is called. A control that cannot be built is a refusal to build ALL of them: a partial set
would be a Repair mission without a checkpoint or a verifier, so the caller falls back to no Repair capability.
"""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from planet_express.core.store import Store

from .approval_flow import ApprovalFlow
from .checkpoint import CheckpointService, CheckpointSettings
from .grants import Inventory, SiteLimits, validate_grant
from .models import Budgets, Grant, VerificationPlan
from .hooks import policy_for
from .runner import _pe_paths
from .store import MissionStore
from .tools import default_registry
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


@dataclass
class RepairControls:
    checkpoint: CheckpointService
    verifier: Any
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
        return Inventory(hosts=("local",), stacks=(), services=())
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
    return Inventory(hosts=("local",), stacks=tuple(stacks), services=tuple(services))


def _own_paths(cfg: Any, store: Store, stacks_root: str) -> tuple[str, ...]:
    """PE's own paths as the session hook sees them (config, database, hook root), so Undo and the hook agree on what is PE."""
    try:
        return tuple(policy_for(cfg, stacks_root=stacks_root, pe_paths=_pe_paths(store)).pe_paths)
    except Exception:                                  # noqa: BLE001 - a config without hook settings: the install's own paths
        return _pe_paths(store)


def build_repair_controls(store: Store, cfg: Any, wiring: RepairWiring, *, containment: Any, broker: Any, stacks_root: str,
                          enabled: Callable[[], bool], model_broker: Any = None) -> RepairControls:
    ms = MissionStore(store)
    state_dir = Path(store.path).parent
    checkpoint = CheckpointService(
        mission_store=ms, root=cfg.checkpoint.root or str(state_dir / "checkpoints"), stacks_root=stacks_root,
        provider=wiring.provider, settings=CheckpointSettings.from_config(cfg.checkpoint), enabled=enabled,
        baseline_provider=lambda: normalize(wiring.scan()),
        # Undo never writes PE back (rc14.5): its config and database, its state directory and its own code.
        pe_paths=(*_own_paths(cfg, store, stacks_root), str(state_dir), str(Path(__file__).resolve().parents[2])))
    verifier = verifier_from_config(cfg, missions=ms, checkpoint=checkpoint, provider=wiring.provider, broker=broker,
                                    scan=wiring.scan, stacks_root=stacks_root, enabled=enabled)
    known_tools = default_registry().names()
    limits = SiteLimits.from_config(cfg.budgets)

    def inventory() -> Inventory:
        return discover_inventory(stacks_root)

    def validator(grant: Grant, plan: VerificationPlan) -> list[str]:
        return validate_grant(grant, inventory=inventory(), known_checks={c.id for c in plan.checks}, known_tools=known_tools,
                              limits=limits, verification_plan=plan)

    return RepairControls(checkpoint=checkpoint, verifier=verifier, validator=validator, inventory=inventory)


def build_approval_flow(store: Store, cfg: Any, runner: Any, controls: RepairControls | None, *,
                        runtime: str = "command") -> ApprovalFlow | None:
    """The approval flow over a built runner. None without Repair controls: nothing could validate or run a Repair mission, so
    the surfaces answer 'unavailable' rather than offering an approval that could never execute."""
    if controls is None or runner is None:
        return None
    return ApprovalFlow(
        store, supervisor=runner.supervisor, validator=controls.validator,
        budgets=lambda: Budgets(wall_s=min(cfg.budgets.wall_seconds, 86400 * 7), tool_calls=min(cfg.budgets.tool_calls, 100000),
                                output_bytes=min(cfg.budgets.output_bytes, 2**40), retries=min(cfg.budgets.retries, 100)),
        runtime=runtime)


__all__ = ["RepairControls", "RepairWiring", "build_approval_flow", "build_repair_controls", "discover_inventory",
           "mission_write_gate"]
