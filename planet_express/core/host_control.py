"""HostControlProvider -- the interface between PE's reasoning and whatever actually controls
the host a given install runs on (v3 Phase 3, `docs/designs/phase-3-host-control-provider.md`).

Two implementations exist: `SystemdHostControlProvider` (`planet_express/execution/
host_control_systemd.py`) and `MosHostControlProvider` (`planet_express/execution/
host_control_mos.py`, `docs/designs/mos-host-control-provider.md`), proven against a real
Devuan/sysvinit MOS VM -- this closes the Architecture Brief Addendum's "provider testability"
item. Both providers implement the full Protocol, reads and mutation -- `MosHostControlProvider`'s
mutating actions gate through `casa_bender._check_sudo_allowlist()` exactly like the systemd
provider's, under `config.yaml`'s `host_control_provider: mos`
(`docs/designs/mos-sudo-gate-scoping.md`).

`reboot()`/`shutdown()` are declared because the brief names them, not because anything backs
them: no reboot/shutdown capability exists anywhere in this codebase today. A provider that
doesn't support a capability raises `NotImplementedError`, never silently returns a placeholder
-- the same "absent is not zero" discipline `core/hosts.py` applies to missing data applies just
as much to missing capability here.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from planet_express.core.hosts import HostMetrics

ServiceEffect = Literal["applied", "not_applied", "unknown"]


@dataclass(frozen=True)
class ServiceActionResult:
    """The outcome of start/stop/restart, mirroring what `engine.py`'s `_unit_action` already
    verifies today: active-state before and after the action, not just the command's exit code.
    `before`/`after` are `None` only when that specific read failed -- an action whose own
    command succeeded but whose after-read failed is not the same as one that never ran."""

    ok: bool
    before: str | None
    after: str | None
    effect: ServiceEffect
    detail: str


class HostControlProvider(Protocol):
    """Everything in this project that currently assumes `systemctl`/`journalctl`/`free`/
    `uptime` exist should eventually ask one of these methods instead -- not in this landing
    (see the design doc's non-goals; no call site is migrated yet), but this is the shape that
    migration targets."""

    def is_service_running(self, unit: str) -> bool | None:
        """`None` means the read itself failed -- `systemctl is-active` already treats an
        empty answer or a timeout as "could not tell," never as "stopped" (see
        `engine.py`'s `unit_active()`), and this interface keeps that distinction rather
        than collapsing it to a bare bool."""
        ...

    def start_service(self, unit: str) -> ServiceActionResult: ...

    def stop_service(self, unit: str) -> ServiceActionResult: ...

    def restart_service(self, unit: str) -> ServiceActionResult: ...

    def get_host_logs(self, unit: str, *, lines: int) -> tuple[str, ...]: ...

    def get_uptime_seconds(self) -> float | None: ...

    def get_metrics(self) -> HostMetrics:
        """Reuses `core.hosts.HostMetrics` rather than a parallel local-only type -- "one
        reading" (CPU/mem/disk/load/temps) means the same thing whether it came from a live
        local read or a remote Beszel bucket. A provider that can't read a given field leaves it
        `None`; it never fills in a measurement it didn't actually take."""
        ...

    def reboot(self) -> None: ...

    def shutdown(self) -> None: ...


def get_host_control_provider(provider_type: str) -> HostControlProvider:
    """Factory function to instantiate the appropriate HostControlProvider based on config.

    Args:
        provider_type: "systemd" or "mos" from config.host_control_provider

    Returns:
        An instantiated provider (SystemdHostControlProvider or MosHostControlProvider)

    Raises:
        ValueError: if provider_type is not recognized
    """
    if provider_type == "systemd":
        from planet_express.execution.host_control_systemd import SystemdHostControlProvider
        return SystemdHostControlProvider()
    elif provider_type == "mos":
        from planet_express.execution.host_control_mos import MosHostControlProvider
        return MosHostControlProvider()
    else:
        raise ValueError(f"Unknown host control provider: {provider_type}. Must be 'systemd' or 'mos'.")
