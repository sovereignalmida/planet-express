"""MosHostControlProvider -- the second `HostControlProvider` implementation (v3,
`docs/designs/mos-host-control-provider.md`), proving the Protocol against a real non-systemd
host (Devuan/sysvinit, via MOS) for the first time.

`start_service`/`stop_service`/`restart_service` gate through `casa_bender._check_sudo_allowlist()`
exactly like `SystemdHostControlProvider` does -- the gate itself now exists
(`docs/designs/mos-sudo-gate-scoping.md`): `casa_bender.py`'s `_SUDO_SERVICE_RE` plus
`config.HOST_CONTROL_PROVIDER == "mos"` branching, and `scripts/setup_wizard.py` renders the
matching `/etc/sudoers.d/planetexpress` grant as `sudo service <unit> <action>` instead of
`sudo systemctl <action> <unit>` -- note the unit and action swap position between the two, this
module's sudo command must build the string in that order or the regex won't match.

`get_uptime_seconds`/`get_metrics` reuse `host_control_systemd`'s parsing directly -- `uptime` and
`free -h` behave identically on Devuan and Ubuntu, and both modules live in the same execution
layer, so importing is reuse, not the cross-layer duplication `host_control_systemd.py`'s own
docstring explains for its relationship to `dashboard_data.py`.
"""

import re

import casa_bender as bender
from planet_express.core.host_control import ServiceActionResult
from planet_express.core.hosts import HostMetrics
from planet_express.core.redact import redact
from planet_express.execution.host_control_systemd import (
    MONITORING_READ_TIMEOUT_SECONDS,
    REASON_LIMIT,
    SERVICE_CONTROL_TIMEOUT_SECONDS,
    SystemdHostControlProvider,
)

# Exit codes sysvinit's `status)` case can assert something by, per LSB -- confirmed on the MOS
# VM that not every init.d script honors this (see the design doc's docker example), which is
# exactly why these are only half of the cross-check, never trusted alone.
_EXIT_RUNNING = 0
_EXIT_STOPPED = 3

# MOS's own init.d scripts phrase status consistently (confirmed across docker/cron/ssh on the
# live VM) -- "is not running" is checked first since it contains "running" as a substring of
# "is running" would otherwise also match.
_NOT_RUNNING_RE = re.compile(r"is not running|not running", re.IGNORECASE)
_RUNNING_RE = re.compile(r"is running", re.IGNORECASE)


def _clip(text: str) -> str:
    """Same redaction + length limit as `host_control_systemd._clip()` -- duplicated rather than
    imported since that one isn't re-exported for cross-module use, and it's a one-line body."""
    return redact((text or "").strip())[:REASON_LIMIT]


def _text_state(output: str) -> str | None:
    if _NOT_RUNNING_RE.search(output):
        return "stopped"
    if _RUNNING_RE.search(output):
        return "running"
    return None


class MosHostControlProvider:
    def is_service_running(self, unit: str) -> bool | None:
        state, _detail = self._raw_state(unit)
        return None if state is None else state == "running"

    def _raw_state(self, unit: str) -> tuple[str | None, str]:
        """Returns `(state, detail)` -- `state` is `"running"`/`"stopped"`/`None`. `detail` is
        only meaningful when `state` is `None`: either the text didn't parse, or it parsed but
        disagreed with an exit code that actually asserted something (see the design doc's
        decision table) -- refusing rather than guessing which signal is right. Uses
        `SERVICE_CONTROL_TIMEOUT_SECONDS`, not the monitoring timeout -- `service <unit> status`
        is this provider's counterpart to `systemctl is-active`, which `_raw_state()` in
        `host_control_systemd.py` also times at that value, not the monitoring one."""
        rc, out, err = bender.run_argv(
            ["service", unit, "status"], timeout=SERVICE_CONTROL_TIMEOUT_SECONDS,
        )
        text = out.strip()
        text_state = _text_state(text)
        if text_state is None:
            return None, f"unrecognized status output for {unit}: {_clip(text or err)}"

        exit_state = {
            _EXIT_RUNNING: "running",
            _EXIT_STOPPED: "stopped",
        }.get(rc)
        if exit_state is not None and exit_state != text_state:
            return None, (
                f"status text/exit code disagree for {unit} "
                f"(exit {rc} implies {exit_state!r}, text says {text_state!r}): {_clip(text)}"
            )
        return text_state, ""

    def _service_action(self, action: str, unit: str) -> ServiceActionResult:
        """Mirrors `SystemdHostControlProvider._unit_action()`'s control flow and effect
        semantics exactly -- same two early-refusal points (`not_applied`: sudo-allowlist, or an
        unreadable before-state), same "only a genuinely uncertain outcome is `unknown`" rule,
        same unconditional-after-read requirement for `restart`. The sudo command is built as
        `sudo service <unit> <action>` -- unit before action -- matching `casa_bender.py`'s
        `_SUDO_SERVICE_RE`; building it the other way round would silently fail every allowlist
        check regardless of what's declared in config.yaml."""
        try:
            bender._check_sudo_allowlist(f"sudo service {unit} {action}")
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
            ["sudo", "-n", "service", unit, action], timeout=SERVICE_CONTROL_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return ServiceActionResult(
                ok=False, before=before, after=None, effect="unknown",
                detail=f"service {unit} {action} failed (exit {rc}): {_clip(err or out)}",
            )
        after, after_detail = self._raw_state(unit)
        if after is None:
            return ServiceActionResult(
                ok=False, before=before, after=None, effect="unknown",
                detail=f"could not read the state of {unit}: {after_detail}",
            )
        want_running = action in ("start", "restart")
        ok = (after == "running") == want_running
        if action == "restart":
            effect = "applied"
        else:
            effect = "applied" if (before == "running") != (after == "running") else "not_applied"
        return ServiceActionResult(ok=ok, before=before, after=after, effect=effect, detail=f"{unit} is {after}")

    def start_service(self, unit: str) -> ServiceActionResult:
        return self._service_action("start", unit)

    def stop_service(self, unit: str) -> ServiceActionResult:
        return self._service_action("stop", unit)

    def restart_service(self, unit: str) -> ServiceActionResult:
        return self._service_action("restart", unit)

    def get_host_logs(self, unit: str, *, lines: int) -> tuple[str, ...]:
        """Only correct for a unit whose log is a single flat file (confirmed for `docker`,
        `cron`, `api` on the live VM) -- a directory-shaped log (`nginx`, `samba`, `wsddn`)
        surfaces `tail`'s own "Is a directory" error as the line, the same honest-failure shape
        `host_control_systemd.get_host_logs` already uses for a `journalctl` failure. See the
        design doc's non-goals: no directory-log tailing in this landing."""
        if lines < 1:
            raise ValueError("lines must be at least 1")
        rc, out, err = bender.run_argv(
            ["tail", "-n", str(lines), f"/var/log/{unit}"],
            timeout=MONITORING_READ_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return (f"tail failed (exit {rc}): {_clip(err or out)}",)
        return tuple(out.splitlines())

    def get_uptime_seconds(self) -> float | None:
        return SystemdHostControlProvider().get_uptime_seconds()

    def get_metrics(self) -> HostMetrics:
        return SystemdHostControlProvider().get_metrics()

    def reboot(self) -> None:
        raise NotImplementedError(
            "no reboot capability exists in this codebase today -- same as "
            "SystemdHostControlProvider.reboot()"
        )

    def shutdown(self) -> None:
        raise NotImplementedError(
            "no shutdown capability exists in this codebase today -- same as "
            "SystemdHostControlProvider.shutdown()"
        )
