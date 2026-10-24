"""Hold scheduled work until the host has settled after a reboot.

A reboot brings the stacks up over several minutes: containers start, health checks go from "starting" to "healthy",
mounts arrive. A scan that runs in that window reports things that are not wrong yet. This module answers one
question, "is the host still coming back up?", so the scheduled scan and the weekly update pass can wait instead.

It only ever says "wait" for a bounded time after boot and it fails open: any doubt (no systemd, no Docker, an unreadable
answer) counts as settled, so a broken probe can delay a scan by minutes but never suppress monitoring.

Only scheduled work waits. A scan the operator asks for (`/check`, the dashboard's SCAN button) always runs.
"""

import logging
import subprocess
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

STACKS_UNIT = "casa-stacks.service"
BOOT_WINDOW_SECONDS = 900       # past this uptime the host did not "just reboot", whatever the probes say
MIN_QUIET_SECONDS = 120         # always wait this long after boot: the units may not have been started yet
SETTLE_DELAY_SECONDS = 180      # after the stacks unit finishes, let containers finish their own startup
NEVER_RAN_GIVE_UP_SECONDS = 300  # the stacks unit never ran this boot (disabled, or absent): stop waiting for it
POLL_SECONDS = 15
PROBE_TIMEOUT_SECONDS = 10


def uptime_seconds() -> float:
    try:
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return float("inf")          # unknown: not a boot


def stacks_unit_state(unit: str = STACKS_UNIT) -> dict[str, str]:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-pActiveState", "-pActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def starting_containers() -> list[str]:
    """Containers whose health check has not passed yet."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "health=starting", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in out.splitlines() if line.strip()]


def settling_reason(
    *,
    uptime: Callable[[], float] | None = None,
    unit_state: Callable[[], dict[str, str]] | None = None,
    starting: Callable[[], list[str]] | None = None,
) -> str | None:
    """Why the host is still coming back up after a reboot, or None when it has settled (or never just rebooted).
    The probes are looked up when called, so a test can replace them on the module."""
    uptime, unit_state, starting = uptime or uptime_seconds, unit_state or stacks_unit_state, starting or starting_containers
    up = uptime()
    if up >= BOOT_WINDOW_SECONDS:
        return None
    if up < MIN_QUIET_SECONDS:
        return f"the host rebooted {int(up)}s ago"
    state = unit_state()
    active = state.get("ActiveState")
    if active == "failed":
        return None                                           # a failed bring-up is a real problem: report it
    if active in ("activating", "reloading"):
        return "the stacks are still starting"
    try:
        entered = int(state.get("ActiveEnterTimestampMonotonic", "0")) / 1_000_000
    except ValueError:
        entered = 0.0
    if active == "active" and entered > 0:
        if up - entered < SETTLE_DELAY_SECONDS:
            return "the stacks have only just come up"
    elif up < NEVER_RAN_GIVE_UP_SECONDS:
        return "the stacks have not been started yet"
    pending = starting()
    if pending:
        return f"{len(pending)} container(s) are still starting ({', '.join(pending[:3])})"
    return None


def wait_until_settled(
    *,
    reason: Callable[[], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll: float = POLL_SECONDS,
) -> bool:
    """Block until `reason()` is None. True if it had to wait. The wait is bounded by BOOT_WINDOW_SECONDS: past that
    uptime `reason` is always None."""
    reason = reason or settling_reason
    why = reason()
    if why is None:
        return False
    log.info("Holding scheduled work: %s", why)
    while why is not None:
        sleep(poll)
        why = reason()
    log.info("The host has settled; scheduled work resumes")
    return True
