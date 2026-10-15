"""Assemble a `TripwireMonitor` from `missions.*` configuration. With missions disabled nothing is built."""

from collections.abc import Callable
from typing import Any

from planet_express.missions.protected import (
    Discovery,
    ProtectedOverride,
    ProtectedSet,
    build_protected,
)
from planet_express.missions.resources import canon

from .monitor import TripwireMonitor
from .signals import (
    DEFAULT_PACKAGE_FILES,
    BoundedRunner,
    Runner,
    ScanEnv,
    Scanner,
    volume_paths,
)


def build_protected_set(cfg: Any, *, stacks_root: str, storage_mounts=(), volumes=(), ingress=(), dns=(),
                        credentials=(), backup=(), record: Callable[[dict], None] | None = None) -> ProtectedSet:
    """The site's protected set: the built-in defaults, what setup discovered, and `missions.protected`."""
    discovery = Discovery(
        stacks_root=stacks_root, storage_mounts=tuple(storage_mounts), volumes=tuple(volumes), ingress=tuple(ingress),
        dns=tuple(dns), credentials=tuple(credentials), backup=tuple(backup), package_paths=tuple(cfg.package_paths),
        live_trees=tuple(cfg.live_trees), cross_host=tuple((c.name, c.path) for c in cfg.cross_host))
    overrides = [ProtectedOverride(o.resource, o.reason, o.operator, o.at) for o in cfg.protected_overrides]
    return build_protected(site=cfg.protected, discovery=discovery, overrides=overrides, record=record)


def build_tripwire(cfg: Any, *, missions, containment, checkpoint, provider, stacks_root: str, state_dir: str,
                   protected: ProtectedSet, runner: Runner | None = None, bounded: BoundedRunner | None = None,
                   enabled: Callable[[], bool] | None = None,
                   revoke_endpoints: Callable[[str], None] | None = None) -> TripwireMonitor | None:
    """None when missions are off: nothing is constructed and no thread can start."""
    if not cfg.enabled:
        return None
    from planet_express.missions.checkpoint.provider import (
        default_bounded_runner,
        default_runner,
    )
    env = ScanEnv(runner=runner or default_runner, bounded=bounded or default_bounded_runner, provider=provider,
                  protected=protected, package_paths=(*DEFAULT_PACKAGE_FILES, *cfg.package_paths),
                  volume_paths=volume_paths(protected))
    return TripwireMonitor(
        missions=missions, containment=containment, checkpoint=checkpoint, scanner=Scanner(env), state_dir=state_dir,
        enabled=enabled or (lambda: cfg.enabled), stacks_root=stacks_root, interval_s=cfg.tripwire_interval_s,
        investigating_interval_s=cfg.tripwire_investigating_interval_s, revoke_endpoints=revoke_endpoints,
        accept_blind=[canon(r) for r in cfg.tripwire_accept_blind])


__all__ = ["build_protected_set", "build_tripwire"]
