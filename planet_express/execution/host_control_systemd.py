"""SystemdHostControlProvider -- the one `HostControlProvider` implementation that exists
today (v3 Phase 3, `docs/designs/phase-3-host-control-provider.md`).

A faithful extraction, not new behavior: `is_service_running`/`start_service`/`stop_service`/
`restart_service` reproduce exactly what `engine.py`'s `unit_active()`/`_unit_action()` already
do (same `systemctl` invocations, same sudo-allowlist gate, same before/after active-state
verification); `get_uptime_seconds`/`get_metrics` reproduce what `casa_leela.py`'s
`check_system()` already collects (`free -h`, `uptime`). The parsing regexes are duplicated
from `dashboard_data.py` rather than imported from it -- `execution/` must not depend on the
application layer, and the parsing itself is small enough that duplication is cheaper than a
layering violation.

No existing call site is migrated to use this in this landing (see the design doc's
non-goals) -- this type exists, is tested, and nothing calls it yet.
"""

import re

import casa_bender as bender
from planet_express.core.host_control import ServiceActionResult
from planet_express.core.hosts import HostMetrics
from planet_express.core.redact import redact
from planet_express.execution import actions

# is_service_running/start_service/stop_service/restart_service use the exact same timeout
# engine.py's unit_active()/_unit_action() already use for both reads and mutations -- codex
# review caught an earlier version of this file inventing a shorter 20s value here, which would
# have been a real (if subtle) behavior change from the thing this module claims to extract.
SERVICE_CONTROL_TIMEOUT_SECONDS = actions.DOCKER_TIMEOUT_SECONDS

# Matches engine.py's own REASON_LIMIT -- not imported directly (that module is much larger
# than the one constant this file needs), but must track the same value so a diagnostic clipped
# here is clipped the same way it would be through the engine.
REASON_LIMIT = 300


def _clip(text: str) -> str:
    """Same redaction + length limit as engine.py's `_clip()`: a systemctl/journalctl error can
    contain secrets (an env var dump, a path with a token in it), and this is the one place that
    text passes through before landing in a `ServiceActionResult.detail` or a log line -- codex
    review caught an earlier version of this file skipping redaction entirely."""
    return redact((text or "").strip())[:REASON_LIMIT]

# get_host_logs/get_uptime_seconds/get_metrics are a different family (monitoring, not the
# typed-action engine) -- matches casa_leela.py's own _run()'s default timeout, since that's
# what check_system() actually runs these commands with today. No shared constant exists to
# import for this one; casa_leela.py's default is a bare literal in its own signature.
MONITORING_READ_TIMEOUT_SECONDS = 30

# Reproduced from dashboard_data.py's _MEM_SIZE_RE/_UPTIME_RE (not imported -- see module
# docstring). Keep these two in sync by hand if either changes; they parse the same `free -h`/
# `uptime` output by construction, not by coincidence.
_MEM_SIZE_RE = re.compile(r"^([\d.]+)([KMGT]?)i?B?$", re.IGNORECASE)
_UPTIME_RE = re.compile(
    r"^(?P<now>\d{1,2}:\d{2}:\d{2})\s+up\s+"
    r"(?:(?P<days>\d+)\s+days?,\s*)?"
    r"(?:(?P<hh>\d+):(?P<mm>\d+)|(?P<minonly>\d+)\s*min)\s*,\s*"
    r"(?P<users>\d+)\s+users?,\s*"
    r"load average:\s*(?P<load1>[\d.]+),\s*(?P<load5>[\d.]+),\s*(?P<load15>[\d.]+)"
)


def _parse_mem_size(token: str) -> float | None:
    m = _MEM_SIZE_RE.match(token.strip())
    if not m:
        return None
    value, unit = m.groups()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return float(value) * mult[unit.upper()]


class SystemdHostControlProvider:
    def is_service_running(self, unit: str) -> bool | None:
        state, _detail = self._raw_state(unit)
        return None if state is None else state == "active"

    def _unit_action(self, action: str, unit: str) -> ServiceActionResult:
        """Mirrors engine.py's `_unit_action()` control flow exactly, including its two
        early-refusal points and the effect each one carries: a refusal before the mutating
        command ever runs -- sudo-allowlist or an unreadable before-state -- is `"not_applied"`,
        the same as `_refuse()`'s `StepOutcome("failed", "not_applied", reason)`, because nothing
        ran and that is known for certain. Only a genuinely uncertain outcome -- the command ran
        but the after-read failed -- is `"unknown"`. A first version of this file conflated the
        two (and also ran the mutating command even when `before` was unreadable, and
        unconditionally reported `restart` as "applied" regardless of the after-read; codex
        review caught all of this)."""
        try:
            bender._check_sudo_allowlist(f"sudo systemctl {action} {unit}")
        except bender.SafetyError as exc:
            return ServiceActionResult(
                ok=False, before=None, after=None, effect="not_applied",
                detail=f"not in the sudo allowlist: {_clip(str(exc))}",
            )
        before, before_detail = self._raw_state(unit)
        if before is None:
            return ServiceActionResult(
                ok=False, before=None, after=None, effect="not_applied",
                detail=f"could not read the state of {unit}: {before_detail}",
            )
        rc, out, err = bender.run_argv(
            ["sudo", "-n", "systemctl", action, unit], timeout=SERVICE_CONTROL_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return ServiceActionResult(
                ok=False, before=before, after=None, effect="unknown",
                detail=f"systemctl {action} failed (exit {rc}): {_clip(err or out)}",
            )
        after, after_detail = self._raw_state(unit)
        if after is None:
            return ServiceActionResult(
                ok=False, before=before, after=None, effect="unknown",
                detail=f"could not read the state of {unit}: {after_detail}",
            )
        want_active = action in ("start", "restart")
        ok = (after == "active") == want_active
        if action == "restart":
            effect = "applied"
        else:
            effect = "applied" if (before == "active") != (after == "active") else "not_applied"
        return ServiceActionResult(ok=ok, before=before, after=after, effect=effect, detail=f"{unit} is {after}")

    def _raw_state(self, unit: str) -> tuple[str | None, str]:
        """Returns `(state, detail)`: `detail` is only meaningful (the clipped+redacted error,
        or "no answer") when `state` is `None` -- same diagnostic engine.py's `unit_active()`
        raises (`f"could not read the state of {unit}: {_clip(err) or 'no answer'}"`), carried
        as a return value here instead of an exception."""
        rc, out, err = bender.run_argv(
            ["systemctl", "is-active", unit], timeout=SERVICE_CONTROL_TIMEOUT_SECONDS,
        )
        value = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if rc == bender.RUN_ARGV_TIMEOUT_EXIT or not value:
            return None, (_clip(err) or "no answer")
        return value, ""

    def start_service(self, unit: str) -> ServiceActionResult:
        return self._unit_action("start", unit)

    def stop_service(self, unit: str) -> ServiceActionResult:
        return self._unit_action("stop", unit)

    def restart_service(self, unit: str) -> ServiceActionResult:
        return self._unit_action("restart", unit)

    def get_host_logs(self, unit: str, *, lines: int) -> tuple[str, ...]:
        if lines < 1:
            raise ValueError("lines must be at least 1")
        rc, out, err = bender.run_argv(
            ["journalctl", "-u", unit, "--no-pager", "-n", str(lines)],
            timeout=MONITORING_READ_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return (f"journalctl failed (exit {rc}): {_clip(err or out)}",)
        return tuple(out.splitlines())

    def get_uptime_seconds(self) -> float | None:
        rc, out, _err = bender.run_argv(["uptime"], timeout=MONITORING_READ_TIMEOUT_SECONDS)
        if rc != 0 or not out.strip():
            return None
        m = _UPTIME_RE.match(out.strip())
        if not m:
            return None
        g = m.groupdict()
        days = int(g["days"] or 0)
        if g["hh"] is not None:
            hours, minutes = int(g["hh"]), int(g["mm"])
        else:
            hours, minutes = 0, int(g["minonly"] or 0)
        return float(days * 86400 + hours * 3600 + minutes * 60)

    def get_metrics(self) -> HostMetrics:
        """Only `mem_*`/`load` are populated -- `cpu_pct`/`disk_*`/`temps` have no local
        collector today (see the design doc's non-goals) and are left `None`, never guessed."""
        load: tuple[float, ...] | None = None
        rc, out, _err = bender.run_argv(["uptime"], timeout=MONITORING_READ_TIMEOUT_SECONDS)
        if rc == 0 and out.strip():
            m = _UPTIME_RE.match(out.strip())
            if m:
                g = m.groupdict()
                load = (float(g["load1"]), float(g["load5"]), float(g["load15"]))

        mem_pct = mem_used_gib = mem_total_gib = None
        rc, out, _err = bender.run_argv(["free", "-h"], timeout=MONITORING_READ_TIMEOUT_SECONDS)
        if rc == 0:
            mem_lines = out.splitlines()
            if len(mem_lines) > 1:
                fields = dict(zip(
                    ("total", "used", "free", "shared", "buff_cache", "available"),
                    mem_lines[1].split()[1:],
                ))
                total = _parse_mem_size(fields.get("total", ""))
                used = _parse_mem_size(fields.get("used", ""))
                if total:
                    mem_total_gib = total / 1024**3
                    if used is not None:
                        mem_used_gib = used / 1024**3
                        mem_pct = round(used / total * 100, 1)

        return HostMetrics(
            mem_pct=mem_pct, mem_used_gib=mem_used_gib, mem_total_gib=mem_total_gib, load=load,
        )

    def reboot(self) -> None:
        raise NotImplementedError(
            "no reboot capability exists in this codebase today -- declared per the "
            "Architecture Brief's sketch, not backed by anything (see the design doc's "
            "non-goals); implementing this is a new capability decision, not a refactor"
        )

    def shutdown(self) -> None:
        raise NotImplementedError(
            "no shutdown capability exists in this codebase today -- same as reboot() above"
        )
