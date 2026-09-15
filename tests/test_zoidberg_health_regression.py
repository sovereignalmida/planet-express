"""
CRITICAL regression tests (landing 1a, T2): pin casa_zoidberg's canary health-check
semantics BEFORE the T5 extraction moves them into planet_express/execution/actions.py.

Faked at the subprocess.run seam on purpose. Zoidberg's _run() (shlex.split, no shell)
and the bender.run_argv() these checks move onto in T5 both end in subprocess.run with an
argv list, so this file must keep passing UNCHANGED across the extraction. casa_zoidberg
keeps _is_healthy_now / _watch_until_stable / _container_name_for importable after T5 for
exactly that reason.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_zoidberg as zoidberg

# The guard that makes `docker inspect` work at all on containers without a healthcheck.
# Without it the whole inspect call errors, and every such container fails verification.
HEALTH_GUARD = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"


class FakeDocker:
    """Scripted stand-in for subprocess.run, dispatching on the docker argv."""

    def __init__(self, health=None, ps=("abc123", 0, ""), name=("/fixture-healthy", 0, "")):
        self.health = list(health or [])  # [(stdout, returncode, stderr), ...] per inspect call
        self.ps = ps
        self.name = name
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        assert isinstance(cmd, list), f"health checks must use argv, never a shell string: {cmd!r}"
        assert not kwargs.get("shell"), "health checks must never run with shell=True"
        self.calls.append(cmd)
        if cmd[:2] == ["docker", "inspect"] and any("State.Status" in part for part in cmd):
            out, rc, err = self.health.pop(0)
        elif cmd[:2] == ["docker", "compose"] and "ps" in cmd:
            out, rc, err = self.ps
        elif cmd[:2] == ["docker", "inspect"]:
            out, rc, err = self.name
        else:
            raise AssertionError(f"unexpected command in health check: {cmd!r}")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def health_calls(self) -> list[list[str]]:
        return [c for c in self.calls if any("State.Status" in part for part in c)]


def _install(monkeypatch, fake: FakeDocker) -> FakeDocker:
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return fake


# ── _is_healthy_now ─────────────────────────────────────────────────────────────
def test_no_healthcheck_running_container_is_ok(monkeypatch):
    fake = _install(monkeypatch, FakeDocker(health=[("running\t0\tnone", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-healthy") == (True, "ok")
    assert HEALTH_GUARD in " ".join(fake.health_calls()[0]), "Health guard template must be preserved"
    assert fake.health_calls()[0][-1] == "fixture-healthy"


def test_healthy_container_is_ok(monkeypatch):
    _install(monkeypatch, FakeDocker(health=[("running\t0\thealthy", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-healthy") == (True, "ok")


def test_starting_healthcheck_is_not_a_failure(monkeypatch):
    # slow-start containers sit in "starting" during their grace period; not unhealthy.
    _install(monkeypatch, FakeDocker(health=[("running\t0\tstarting", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-slow-start") == (True, "ok")


def test_unhealthy_container_fails(monkeypatch):
    _install(monkeypatch, FakeDocker(health=[("running\t0\tunhealthy", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-unhealthy") == (False, "healthcheck failing")


def test_not_running_container_fails(monkeypatch):
    _install(monkeypatch, FakeDocker(health=[("exited\t0\tnone", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-crash-loop") == (False, "status=exited")


def test_restart_during_watch_window_fails(monkeypatch):
    _install(monkeypatch, FakeDocker(health=[("running\t2\thealthy", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-crash-loop") == (False, "restarted 2x during watch window")


def test_non_numeric_restart_count_is_treated_as_zero(monkeypatch):
    _install(monkeypatch, FakeDocker(health=[("running\t?\thealthy", 0, "")]))
    assert zoidberg._is_healthy_now("fixture-healthy") == (True, "ok")


def test_inspect_failure_fails_with_reason(monkeypatch):
    _install(monkeypatch, FakeDocker(health=[("", 1, "Error: No such container: ghost")]))
    assert zoidberg._is_healthy_now("ghost") == (False, "inspect failed: Error: No such container: ghost")


# ── _watch_until_stable ─────────────────────────────────────────────────────────
def test_watch_until_stable_passes_when_every_poll_is_ok(monkeypatch):
    polls = 3
    fake = _install(monkeypatch, FakeDocker(health=[("running\t0\thealthy", 0, "")] * polls))
    seconds = zoidberg.CANARY_POLL_INTERVAL_SECONDS * polls
    assert zoidberg._watch_until_stable("fixture-healthy", seconds) == (True, "ok")
    assert len(fake.health_calls()) == polls


def test_watch_until_stable_stops_at_first_failure(monkeypatch):
    fake = _install(monkeypatch, FakeDocker(health=[
        ("running\t0\thealthy", 0, ""),
        ("running\t0\tunhealthy", 0, ""),
        ("running\t0\thealthy", 0, ""),
    ]))
    seconds = zoidberg.CANARY_POLL_INTERVAL_SECONDS * 3
    assert zoidberg._watch_until_stable("fixture-unhealthy", seconds) == (False, "healthcheck failing")
    assert len(fake.health_calls()) == 2, "must not keep polling after a failed check"


# ── _container_name_for ─────────────────────────────────────────────────────────
def test_container_name_for_resolves_and_strips_leading_slash(monkeypatch):
    _install(monkeypatch, FakeDocker(ps=("abc123\n", 0, ""), name=("/fixture-healthy", 0, "")))
    assert zoidberg._container_name_for(Path("/home/casaroot/stacks/healthy"), "web") == "fixture-healthy"


def test_container_name_for_none_when_service_has_no_container(monkeypatch):
    _install(monkeypatch, FakeDocker(ps=("", 0, "")))
    assert zoidberg._container_name_for(Path("/home/casaroot/stacks/healthy"), "web") is None


def test_container_name_for_none_when_compose_ps_fails(monkeypatch):
    _install(monkeypatch, FakeDocker(ps=("", 1, "no such file")))
    assert zoidberg._container_name_for(Path("/home/casaroot/stacks/missing"), "web") is None
