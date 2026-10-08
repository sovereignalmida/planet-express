"""planet_express/execution/host_control_mos.py: MosHostControlProvider.
Every `bender.run_argv` call is faked; no real `service`/`tail`/`free`/`uptime` runs."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_bender as bender
from planet_express.core.hosts import HostMetrics
from planet_express.execution import host_control_mos as hcm


class FakeRunArgv:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]):
        self.responses = responses

    def __call__(self, argv, timeout):
        try:
            return self.responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected argv: {argv!r}") from None


@pytest.fixture
def provider():
    return hcm.MosHostControlProvider()


# --- is_service_running / _raw_state: the exit-code/text cross-check -------------------------


def test_is_service_running_true_when_exit_and_text_agree(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "cron", "status"): (0, "cron is running.", ""),
    }))
    assert provider.is_service_running("cron") is True


def test_is_service_running_false_when_exit_and_text_agree(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "cron", "status"): (3, "cron is not running ... failed!", ""),
    }))
    assert provider.is_service_running("cron") is False


def test_docker_script_bug_is_caught_not_trusted(monkeypatch, provider):
    """The real bug found on the MOS VM: docker's init.d script echoes "Docker is not
    running." but exits 0 -- the same exit code as the running case. Trusting exit code alone
    (the way SystemdHostControlProvider trusts `systemctl is-active`) would misread this as
    running. The cross-check must refuse (None), not pick a side."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "docker", "status"): (0, "Docker is not running.", ""),
    }))
    assert provider.is_service_running("docker") is None
    state, detail = provider._raw_state("docker")
    assert state is None
    assert "disagree" in detail
    assert "docker" in detail


def test_unasserted_exit_code_falls_back_to_text(monkeypatch, provider):
    """An exit code other than 0/3 asserts nothing under LSB -- text is the only signal, so it
    is trusted rather than treated as another disagreement."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "ssh", "status"): (1, "sshd is running.", ""),
    }))
    assert provider.is_service_running("ssh") is True


def test_unrecognized_text_is_unreadable_not_guessed(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "mystery", "status"): (0, "something unexpected happened", ""),
    }))
    assert provider.is_service_running("mystery") is None
    state, detail = provider._raw_state("mystery")
    assert state is None
    assert "unrecognized" in detail


def test_reverse_disagreement_is_also_caught(monkeypatch, provider):
    """Exit 3 (stopped) with "is running" text is the mirror image of the docker bug -- must
    also refuse, not just the 0/"not running" direction."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "cron", "status"): (3, "cron is running.", ""),
    }))
    assert provider.is_service_running("cron") is None
    state, detail = provider._raw_state("cron")
    assert state is None
    assert "disagree" in detail


def test_unasserted_exit_code_falls_back_to_stopped_text(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "docker", "status"): (2, "Docker is not running.", ""),
    }))
    assert provider.is_service_running("docker") is False


def test_exit_code_4_also_falls_back_to_text(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "docker", "status"): (4, "Docker is running.", ""),
    }))
    assert provider.is_service_running("docker") is True


def test_timeout_is_unreadable_not_guessed(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "ghost", "status"): (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out"),
    }))
    assert provider.is_service_running("ghost") is None


def test_status_read_uses_the_service_control_timeout_not_monitoring(monkeypatch, provider):
    """`service <unit> status` is this provider's counterpart to `systemctl is-active`, which
    host_control_systemd.py times at SERVICE_CONTROL_TIMEOUT_SECONDS -- not the shorter
    monitoring-read timeout used for `uptime`/`free`/`tail`."""
    seen = {}

    def fake_run_argv(argv, timeout):
        seen[tuple(argv)] = timeout
        return (0, "x is running.", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    provider.is_service_running("x")
    assert seen[("service", "x", "status")] == hcm.SERVICE_CONTROL_TIMEOUT_SECONDS
    assert hcm.SERVICE_CONTROL_TIMEOUT_SECONDS != hcm.MONITORING_READ_TIMEOUT_SECONDS


def test_not_running_matches_before_running_substring(monkeypatch, provider):
    """"is not running" contains the word "running" -- the not-running check must win, not the
    running regex matching first and misreading the stopped case."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "docker", "status"): (3, "Docker is not running.", ""),
    }))
    assert provider.is_service_running("docker") is False


def test_error_text_is_redacted(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "x", "status"): (0, "Docker is not running. PASSWORD=supersecret123", ""),
    }))
    _state, detail = provider._raw_state("x")
    assert "supersecret123" not in detail
    assert "[REDACTED]" in detail


# --- start/stop/restart: gated through casa_bender._check_sudo_allowlist(), same control -------
# flow as SystemdHostControlProvider._unit_action() -- see docs/designs/mos-sudo-gate-scoping.md.


def test_start_service_applies_when_it_goes_running(monkeypatch, provider):
    """The before and after `service status` reads use identical argv, so a plain
    FakeRunArgv keyed only by argv can't distinguish them -- call-count-aware instead,
    same pattern test_host_control_systemd.py uses for the same reason."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    calls = {"n": 0}
    responses = [(3, "docker is not running.", ""), (0, "docker is running.", "")]

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["service", "docker"]:
            value = responses[calls["n"]]
            calls["n"] += 1
            return value
        assert argv == ["sudo", "-n", "service", "docker", "start"]
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.start_service("docker")
    assert result.ok is True
    assert result.before == "stopped"
    assert result.after == "running"
    assert result.effect == "applied"


def test_start_service_not_applied_when_already_running(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["service", "docker"]:
            return (0, "docker is running.", "")
        assert argv == ["sudo", "-n", "service", "docker", "start"]
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.start_service("docker")
    assert result.ok is True
    assert result.effect == "not_applied"


def test_restart_service_effect_is_always_applied_on_success(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("service", "docker", "status"): (0, "docker is running.", ""),
        ("sudo", "-n", "service", "docker", "restart"): (0, "", ""),
    }))
    result = provider.restart_service("docker")
    assert result.ok is True
    assert result.effect == "applied"


def test_outside_sudo_allowlist_never_runs_the_command(monkeypatch, provider):
    def refuse(command):
        raise bender.SafetyError(f"not allowed: {command}")

    monkeypatch.setattr(bender, "_check_sudo_allowlist", refuse)

    def fake_run_argv(argv, timeout):
        raise AssertionError(f"must not run: {argv!r}")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("forbidden")
    assert result.ok is False
    assert "not in the sudo allowlist" in result.detail
    assert result.effect == "not_applied"


def test_unreadable_before_state_refuses_without_running_the_command(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["service", "docker"]:
            return (0, "something unexpected", "")  # unreadable text
        raise AssertionError(f"must not run the mutating command: {argv!r}")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("docker")
    assert result.ok is False
    assert result.before is None
    assert result.after is None
    assert result.effect == "not_applied"
    assert "could not read the state of docker" in result.detail


def test_command_failure_is_reported_with_before_state_known(monkeypatch, provider):
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["service", "docker"]:
            return (0, "docker is running.", "")
        assert argv == ["sudo", "-n", "service", "docker", "stop"]
        return (1, "", "docker: command failed")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("docker")
    assert result.ok is False
    assert result.before == "running"
    assert result.after is None
    assert "docker: command failed" in result.detail


def test_restart_with_unreadable_after_is_unknown_not_applied(monkeypatch, provider):
    """codex review of the systemd version caught this exact bug: restart must not get
    an exemption from the after-read check just because the command itself succeeded."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    calls = {"n": 0}

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["service", "docker"]:
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "docker is running.", "")
            return (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out")
        assert argv == ["sudo", "-n", "service", "docker", "restart"]
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.restart_service("docker")
    assert result.ok is False
    assert result.after is None
    assert result.effect == "unknown"


def test_sudo_command_built_with_unit_before_action(monkeypatch, provider):
    """The mutating argv must be ["sudo", "-n", "service", <unit>, <action>] -- unit
    before action -- matching _SUDO_SERVICE_RE. Building it the other way round would
    silently fail every allowlist check regardless of what's declared in config.yaml."""
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    seen_mutating = {}

    def fake_run_argv(argv, timeout):
        if argv[:2] == ["service", "docker"]:
            return (0, "docker is running.", "")
        seen_mutating["argv"] = argv
        return (0, "", "")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    provider.restart_service("docker")
    assert seen_mutating["argv"] == ["sudo", "-n", "service", "docker", "restart"]


# --- root without sudo (MOS): same gate, direct `service` argv -------------------------------


def _mutating_recorder(seen, state="docker is running."):
    def fake_run_argv(argv, timeout):
        if argv == ["service", "docker", "status"]:
            return (0, state, "")
        seen.append(argv)
        return (0, "", "")
    return fake_run_argv


def test_root_without_sudo_runs_service_directly(monkeypatch, provider):
    monkeypatch.setattr(hcm.os, "geteuid", lambda: 0)
    monkeypatch.setattr(hcm.shutil, "which", lambda name: None)
    gated = []
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: gated.append(command))
    seen = []
    monkeypatch.setattr(bender, "run_argv", _mutating_recorder(seen))
    result = provider.restart_service("docker")
    assert gated == ["sudo service docker restart"]  # gate semantics unchanged
    assert seen == [["service", "docker", "restart"]]
    assert result.ok is True and result.effect == "applied"


def test_non_root_with_sudo_keeps_the_sudo_path(monkeypatch, provider):
    monkeypatch.setattr(hcm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(hcm.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    seen = []
    monkeypatch.setattr(bender, "run_argv", _mutating_recorder(seen))
    provider.restart_service("docker")
    assert seen == [["sudo", "-n", "service", "docker", "restart"]]


def test_root_with_sudo_keeps_the_sudo_path(monkeypatch, provider):
    monkeypatch.setattr(hcm.os, "geteuid", lambda: 0)
    monkeypatch.setattr(hcm.shutil, "which", lambda name: "/usr/bin/sudo")
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    seen = []
    monkeypatch.setattr(bender, "run_argv", _mutating_recorder(seen))
    provider.restart_service("docker")
    assert seen == [["sudo", "-n", "service", "docker", "restart"]]


def test_non_root_without_sudo_still_tries_sudo_and_fails_honestly(monkeypatch, provider):
    """A non-root process must never fall back to running `service` unprivileged-as-if-allowed."""
    monkeypatch.setattr(hcm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(hcm.shutil, "which", lambda name: None)
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)

    def fake_run_argv(argv, timeout):
        if argv == ["service", "docker", "status"]:
            return (0, "docker is running.", "")
        assert argv[:2] == ["sudo", "-n"]
        return (127, "", "sudo: not found")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    result = provider.stop_service("docker")
    assert result.ok is False and result.effect == "unknown"


def test_root_without_sudo_is_still_refused_outside_the_real_allowlist(monkeypatch, provider):
    """Uses the real gate (not a stub): config's sudo_allowlist still decides, root or not."""
    monkeypatch.setattr(hcm.os, "geteuid", lambda: 0)
    monkeypatch.setattr(hcm.shutil, "which", lambda name: None)
    monkeypatch.setattr(bender, "HOST_CONTROL_PROVIDER", "mos")
    from config_schema import SudoAllowlist, SudoUnitGrant
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", SudoAllowlist(
        units=[SudoUnitGrant(unit="docker", actions=["restart"])]))

    def fake_run_argv(argv, timeout):
        raise AssertionError(f"must not run: {argv!r}")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    for unit, action in (("cron", "restart"), ("docker", "stop")):
        result = getattr(provider, f"{action}_service")(unit)
        assert result.ok is False and result.effect == "not_applied"
        assert "not in the sudo allowlist" in result.detail


# --- get_host_logs ------------------------------------------------------------------------------


def test_get_host_logs_success(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("tail", "-n", "5", "/var/log/docker"): (0, "line1\nline2", ""),
    }))
    assert provider.get_host_logs("docker", lines=5) == ("line1", "line2")


def test_get_host_logs_directory_shaped_log_is_an_honest_failure(monkeypatch, provider):
    """nginx's log is a directory on the live VM, not a file -- tail fails, and that failure is
    surfaced as the line rather than guessing which file inside is "the" log."""
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("tail", "-n", "5", "/var/log/nginx"): (1, "", "tail: error reading '/var/log/nginx': Is a directory"),
    }))
    lines = provider.get_host_logs("nginx", lines=5)
    assert len(lines) == 1
    assert "Is a directory" in lines[0]


def test_get_host_logs_rejects_nonpositive_lines(provider):
    with pytest.raises(ValueError):
        provider.get_host_logs("x", lines=0)


def test_get_host_logs_failure_is_redacted_and_clipped(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("tail", "-n", "5", "/var/log/x"): (1, "", "tail: PASSWORD=supersecret123: No such file"),
    }))
    lines = provider.get_host_logs("x", lines=5)
    assert len(lines) == 1
    assert "supersecret123" not in lines[0]
    assert "[REDACTED]" in lines[0]


# --- get_uptime_seconds / get_metrics: reused from the systemd provider, not reimplemented -----


def test_get_uptime_seconds_reuses_systemd_providers_parsing(monkeypatch, provider):
    monkeypatch.setattr(bender, "run_argv", FakeRunArgv({
        ("uptime",): (0, "16:09:30 up 20 days, 23:43, 5 users, load average: 1.74, 1.31, 1.12", ""),
    }))
    assert provider.get_uptime_seconds() == 20 * 86400 + 23 * 3600 + 43 * 60


def test_get_metrics_reuses_systemd_providers_parsing(monkeypatch, provider):
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
    assert isinstance(metrics, HostMetrics)
    assert metrics.load == (1.50, 1.20, 1.00)
    assert metrics.mem_total_gib == pytest.approx(15.0, abs=0.1)


# --- reboot/shutdown: declared, not implemented -------------------------------------------------


def test_reboot_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.reboot()


def test_shutdown_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.shutdown()
