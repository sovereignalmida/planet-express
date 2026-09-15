"""
planet_express/execution/actions.py (landing 1a, T5). Zoidberg's original semantics are
pinned in tests/test_zoidberg_health_regression.py; this file covers what the extraction
added: the restart-count baseline the 1b verifier will use, the compose-label lookup
Amy's investigation now shares, and that nothing here ever builds a shell string.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import actions


class FakeRunArgv:
    def __init__(self, *responses):
        self.responses = list(responses)  # [(rc, stdout, stderr), ...]
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        assert isinstance(argv, list)
        assert timeout == actions.DOCKER_TIMEOUT_SECONDS
        self.calls.append(argv)
        return self.responses.pop(0)


def _install(monkeypatch, fake):
    monkeypatch.setattr(actions.bender, "run_argv", fake)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return fake


# ── baseline_restarts (used by the 1b typed-action verifier) ────────────────────
def test_restarts_before_the_action_do_not_fail_verification(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "running\t5\thealthy", "")))
    assert actions.container_health("fixture-crash-loop", baseline_restarts=5) == (True, "ok")


def test_restart_after_the_baseline_fails_with_the_delta(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "running\t6\thealthy", "")))
    assert actions.container_health("fixture-crash-loop", baseline_restarts=5) == (
        False, "restarted 1x during watch window",
    )


def test_watch_until_stable_passes_the_baseline_to_every_poll(monkeypatch):
    fake = _install(monkeypatch, FakeRunArgv((0, "running\t3\thealthy", ""), (0, "running\t3\thealthy", "")))
    assert actions.watch_until_stable("web", seconds=10, poll_seconds=5, baseline_restarts=3) == (True, "ok")
    assert len(fake.calls) == 2


# ── container_compose_labels (Amy's investigation) ──────────────────────────────
def test_compose_labels_parsed(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "media\tsonarr\n", "")))
    assert actions.container_compose_labels("CASA_SONARR") == ("media", "sonarr")


def test_compose_labels_fall_back_for_non_compose_container(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "\t", "")))
    assert actions.container_compose_labels("CASA_STANDALONE") == ("unknown", "CASA_STANDALONE")


def test_compose_labels_fall_back_when_inspect_fails(monkeypatch):
    _install(monkeypatch, FakeRunArgv((1, "", "Error: No such object: ghost")))
    assert actions.container_compose_labels("ghost") == ("unknown", "ghost")


def test_hostile_container_name_stays_one_argv_element(monkeypatch):
    hostile = "CASA_X; docker rm -f $(docker ps -aq)"
    fake = _install(monkeypatch, FakeRunArgv((1, "", "no such object")))
    actions.container_compose_labels(hostile)
    assert fake.calls[0][-1] == hostile
    assert fake.calls[0][:3] == ["docker", "inspect", "--format"]


def test_service_container_passes_stack_path_with_spaces_intact(monkeypatch):
    fake = _install(monkeypatch, FakeRunArgv((0, "abc123", ""), (0, "/my-app", "")))
    assert actions.service_container(Path("/home/casaroot/stacks/my stack"), "web") == "my-app"
    assert fake.calls[0] == [
        "docker", "compose", "-f", "/home/casaroot/stacks/my stack/docker-compose.yml", "ps", "-q", "web",
    ]
