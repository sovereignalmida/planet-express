"""planet_express/execution/host_control_systemd.py (v3 Phase 3): SystemdHostControlProvider.
Every `bender.run_argv` call is faked; no real systemctl/journalctl/free/uptime runs."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_bender as bender
from planet_express.core.host_control import ServiceActionResult
from planet_express.core.hosts import HostMetrics
from planet_express.execution import host_control_systemd as hcs


class FakeRunArgv:
    """Dispatches on the argv tuple to a canned (rc, out, err); unlisted argvs fail the test
    loudly rather than silently returning something plausible-looking."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        try:
            return self.responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected argv: {argv!r}") from None


@pytest.fixture
def provider():
    return hcs.SystemdHostControlProvider()


# --- is_service_running ---------------------------------------------------------------------


def test_is_service_running_true(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("systemctl", "is-active", "casa-stacks"): (0, "active", ""),
    }))
    assert provider.is_service_running("casa-stacks") is True


def test_is_service_running_false(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("systemctl", "is-active", "casa-stacks"): (3, "inactive", ""),
    }))
    assert provider.is_service_running("casa-stacks") is False


def test_is_service_running_unreadable_is_none_not_false(monkeypatch, provider):
    """The existing invariant engine.py's unit_active() states explicitly: an empty answer or
    timeout means "could not tell," never "stopped"."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("systemctl", "is-active", "ghost"): (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out"),
    }))
    assert provider.is_service_running("ghost") is None


def test_is_service_running_empty_output_without_a_timeout_is_also_none(monkeypatch, provider):
    """Unreadable isn't only a timeout -- an empty answer with an ordinary exit code is the
    same "could not tell" case, not "stopped"."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("systemctl", "is-active", "ghost"): (0, "", ""),
    }))
    assert provider.is_service_running("ghost") is None


# --- start/stop/restart_service -------------------------------------------------------------


def test_start_service_applies_when_it_goes_active(monkeypatch, provider):
    """The before and after is-active reads use identical argv, so a FakeRunArgv keyed purely
    by argv can't distinguish them -- this needs a call-count-aware fake instead."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    calls = {"n": 0}
    responses = [(3, "inactive", ""), (0, "active", "")]

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            value = responses[calls["n"]]
            calls["n"] += 1
            return value
        assert argv == ["sudo", "-n", "systemctl", "start", "media-sonarr"]
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.start_service("media-sonarr")
    assert result == ServiceActionResult(
        ok=True, before="inactive", after="active", effect="applied", detail="media-sonarr is active",
    )


def test_start_service_not_applied_when_already_active(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active", "")
        assert argv == ["sudo", "-n", "systemctl", "start", "media-sonarr"]
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.start_service("media-sonarr")
    assert result.ok is True
    assert result.effect == "not_applied"


def test_restart_service_effect_is_always_applied_on_success(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("systemctl", "is-active", "x"): (0, "active", ""),
        ("sudo", "-n", "systemctl", "restart", "x"): (0, "", ""),
    }))
    result = provider.restart_service("x")
    assert result.ok is True
    assert result.effect == "applied"


def test_outside_sudo_allowlist_never_runs_the_command(monkeypatch, provider):
    def refuse(command):
        raise bender.SafetyError(f"not allowed: {command}")

    monkeypatch.setattr(bender, "_check_sudo_allowlist", refuse)

    def fake_run_argv(argv, timeout):
        raise AssertionError(f"must not run: {argv!r}")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("forbidden-unit")
    assert result.ok is False
    assert "not in the sudo allowlist" in result.detail
    # A refusal before anything ran is known for certain to be not_applied, not merely
    # "unknown" -- matches engine.py's _refuse() (codex review).
    assert result.effect == "not_applied"


def test_command_failure_is_reported_with_before_state_known(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active", "")
        assert argv == ["sudo", "-n", "systemctl", "stop", "x"]
        return (1, "", "Failed to stop unit")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("x")
    assert result.ok is False
    assert result.before == "active"
    assert result.after is None
    assert "Failed to stop unit" in result.detail


def test_error_text_is_redacted_before_it_reaches_detail(monkeypatch, provider):
    """codex review: an earlier version surfaced systemctl's raw stderr directly -- a
    secret-shaped value in there (an env var dump, a token in a path) must be redacted the
    same way engine.py's _clip() redacts it, not passed straight through."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active", "")
        assert argv == ["sudo", "-n", "systemctl", "stop", "x"]
        return (1, "", "connection failed: PASSWORD=supersecret123")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("x")
    assert "supersecret123" not in result.detail
    assert "[REDACTED]" in result.detail


def test_after_read_failure_is_unknown_effect_not_a_crash(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    calls = {"n": 0}

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "active", "")
            return (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out")
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("x")
    assert result.ok is False
    assert result.after is None
    assert result.effect == "unknown"
    # codex review: _raw_state() must carry the real systemctl error into the detail message,
    # not just a generic "could not read" with no diagnostic -- matching engine.py's
    # unit_active() (f"...: {_clip(err) or 'no answer'}").
    assert "timed out" in result.detail


def test_restart_with_unreadable_after_is_unknown_not_applied(monkeypatch, provider):
    """codex review: an earlier version unconditionally called restart "applied" on command
    success, even when the after-read itself failed -- matching engine.py means restart gets
    no special exemption from the after-read check that every other action already has."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    calls = {"n": 0}

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "active", "")
            return (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out")
        assert argv == ["sudo", "-n", "systemctl", "restart", "x"]
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.restart_service("x")
    assert result.ok is False
    assert result.after is None
    assert result.effect == "unknown"


def test_unreadable_before_state_refuses_without_running_the_command(monkeypatch, provider):
    """codex review: an earlier version continued into the mutating systemctl call even when
    the before-state couldn't be read -- engine.py refuses before dispatching or running
    anything in this case, and this must match."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "", "no answer")  # unreadable
        raise AssertionError(f"must not run the mutating command: {argv!r}")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("x")
    assert result.ok is False
    assert result.before is None
    assert result.after is None
    # Known for certain nothing ran -- not_applied, not "unknown" (codex review).
    assert result.effect == "not_applied"
    assert "could not read the state of x" in result.detail


# --- get_host_logs ---------------------------------------------------------------------------


def test_get_host_logs_success(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("journalctl", "-u", "casa-stacks", "--no-pager", "-n", "5"): (0, "line1\nline2", ""),
    }))
    assert provider.get_host_logs("casa-stacks", lines=5) == ("line1", "line2")


def test_get_host_logs_failure_returns_the_error_as_a_line(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("journalctl", "-u", "ghost", "--no-pager", "-n", "5"): (1, "", "no such unit"),
    }))
    lines = provider.get_host_logs("ghost", lines=5)
    assert len(lines) == 1
    assert "no such unit" in lines[0]


def test_get_host_logs_rejects_nonpositive_lines(provider):
    with pytest.raises(ValueError):
        provider.get_host_logs("x", lines=0)


# --- get_uptime_seconds -----------------------------------------------------------------------


def test_get_uptime_seconds_with_days_and_hhmm(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("uptime",): (0, "16:09:30 up 20 days, 23:43, 5 users, load average: 1.74, 1.31, 1.12", ""),
    }))
    seconds = provider.get_uptime_seconds()
    assert seconds == 20 * 86400 + 23 * 3600 + 43 * 60


def test_get_uptime_seconds_minutes_only_no_days(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("uptime",): (0, "16:09:30 up 5 min, 1 user, load average: 0.10, 0.05, 0.01", ""),
    }))
    assert provider.get_uptime_seconds() == 5 * 60


def test_get_uptime_seconds_unparseable_is_none(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({("uptime",): (0, "garbage", "")}))
    assert provider.get_uptime_seconds() is None


def test_get_uptime_seconds_command_failure_is_none(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({("uptime",): (1, "", "boom")}))
    assert provider.get_uptime_seconds() is None


# --- get_metrics -------------------------------------------------------------------------------


def test_get_metrics_parses_mem_and_load(monkeypatch, provider):
    def fake_run_argv(argv, timeout):
        if argv == ["uptime"]:
            return (0, "16:09:30 up 1 day, 2:00, 1 user, load average: 1.50, 1.20, 1.00", "")
        if argv == ["free", "-h"]:
            header = "              total        used        free      shared  buff/cache   available"
            mem_line = "Mem:           15Gi        10Gi       556Mi       662Mi       5.5Gi       5.1Gi"
            return (0, f"{header}\n{mem_line}", "")
        raise AssertionError(argv)

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    metrics = provider.get_metrics()
    assert metrics.load == (1.50, 1.20, 1.00)
    assert metrics.mem_total_gib == pytest.approx(15.0, abs=0.1)
    assert metrics.mem_used_gib == pytest.approx(10.0, abs=0.1)
    assert metrics.mem_pct == pytest.approx(66.7, abs=0.5)
    # not collected locally -- absent, never guessed
    assert metrics.cpu_pct is None
    assert metrics.disk_pct is None
    assert metrics.temps is None


def test_get_metrics_partial_failure_leaves_fields_none_not_zero(monkeypatch, provider):
    """free fails; uptime succeeds. mem_* must be None, not 0 -- and load must still populate."""
    def fake_run_argv(argv, timeout):
        if argv == ["uptime"]:
            return (0, "16:09:30 up 1 day, 2:00, 1 user, load average: 1.50, 1.20, 1.00", "")
        if argv == ["free", "-h"]:
            return (1, "", "boom")
        raise AssertionError(argv)

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    metrics = provider.get_metrics()
    assert metrics.load == (1.50, 1.20, 1.00)
    assert metrics.mem_pct is None
    assert metrics.mem_used_gib is None
    assert metrics.mem_total_gib is None
    assert isinstance(metrics, HostMetrics)


# --- timeouts genuinely match their claimed source, not just a coincidentally-equal literal --


def test_service_control_timeout_matches_engines_constant():
    """codex review: an earlier version hardcoded 20s here, diverging from the 120s
    engine.py's unit_active()/_unit_action() actually use -- this must track the real
    constant, not a copy of its current value, so the two can't silently drift apart again."""
    from planet_express.execution import actions
    assert hcs.SERVICE_CONTROL_TIMEOUT_SECONDS == actions.DOCKER_TIMEOUT_SECONDS


def test_service_control_actions_actually_use_that_timeout(monkeypatch, provider):
    seen = {}

    def fake_run_argv(argv, timeout):
        seen[tuple(argv)] = timeout
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active", "")
        return (0, "", "")

    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    provider.restart_service("x")
    assert all(t == hcs.SERVICE_CONTROL_TIMEOUT_SECONDS for t in seen.values())


# --- reboot/shutdown: declared, not implemented -----------------------------------------------


def test_reboot_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.reboot()


def test_shutdown_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.shutdown()
