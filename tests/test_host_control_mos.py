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


# --- start/stop/restart: declared, not implemented --------------------------------------------


def test_start_service_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.start_service("docker")


def test_stop_service_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.stop_service("docker")


def test_restart_service_raises_not_implemented(provider):
    with pytest.raises(NotImplementedError):
        provider.restart_service("docker")


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
