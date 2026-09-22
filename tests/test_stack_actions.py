"""T37 compose stack targets, argv builders, and state-based verification."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import config
from planet_express.execution import actions


def _stacks(tmp_path, monkeypatch, *names, forbidden=()):
    root = tmp_path / "stacks"
    for name in names:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(config, "STACKS_ROOT", root)
    monkeypatch.setattr(config, "FORBIDDEN_STACKS", list(forbidden))
    return root


@pytest.mark.parametrize("name", ["", "../media", "bad/name", ".hidden", "a" * 65, None])
def test_stack_target_rejects_invalid_names(tmp_path, monkeypatch, name):
    _stacks(tmp_path, monkeypatch, "media")
    with pytest.raises(actions.TargetError, match="invalid stack name"):
        actions.resolve_stack_target(actions.UP_STACK, name)


def test_forbidden_stack_up_refused_and_down_allowed(tmp_path, monkeypatch):
    _stacks(tmp_path, monkeypatch, "ai", forbidden=("ai",))
    with pytest.raises(actions.TargetError, match="forbidden"):
        actions.resolve_stack_target(actions.UP_STACK, "ai")
    assert actions.resolve_stack_target(actions.DOWN_STACK, "ai").as_dict() == {"stack": "ai"}


def test_ingress_requires_the_r3_action(tmp_path, monkeypatch):
    _stacks(tmp_path, monkeypatch, "network", "my-traefik", "media")
    for name in ("network", "my-traefik"):
        with pytest.raises(actions.TargetError, match=actions.DOWN_INGRESS):
            actions.resolve_stack_target(actions.DOWN_STACK, name)
        assert actions.resolve_stack_target(actions.DOWN_INGRESS, name).stack == name
    with pytest.raises(actions.TargetError, match=actions.DOWN_STACK):
        actions.resolve_stack_target(actions.DOWN_INGRESS, "media")


def test_all_target_snapshots_order_and_refuses_a_changed_set(tmp_path, monkeypatch):
    root = _stacks(tmp_path, monkeypatch, "zeta", "network", "ai", "alpha", forbidden=("ai",))
    up = actions.resolve_stack_target(actions.UP_ALL, "all")
    down = actions.resolve_stack_target(actions.DOWN_ALL, "all")
    assert up.as_dict() == {"scope": "all", "stacks": ["network", "alpha", "zeta"]}
    assert down.as_dict() == {"scope": "all", "stacks": ["ai", "alpha", "zeta", "network"]}
    (root / "new" / "docker-compose.yml").parent.mkdir()
    (root / "new" / "docker-compose.yml").write_text("services: {}\n")
    with pytest.raises(actions.TargetError, match="stack set changed"):
        actions.resolve_stack_target(actions.UP_ALL, "all", approved_target=up.as_dict())


def test_stack_argv_builders(tmp_path, monkeypatch):
    root = _stacks(tmp_path, monkeypatch, "media")
    compose = str(root / "media" / "docker-compose.yml")
    assert actions.stack_argv(actions.UP_STACK, "media") == [
        "docker", "compose", "-f", compose, "up", "-d",
    ]
    assert actions.stack_argv(actions.DOWN_STACK, "media") == [
        "docker", "compose", "-f", compose, "down",
    ]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _reading(status="running", restarts=0, health="healthy", error=None):
    return actions.HealthReading(error, status, restarts, health)


def test_up_verification_passes_and_handles_no_healthcheck(monkeypatch):
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: (0, ["a", "b"]))
    readings = {"a": _reading(), "b": _reading(health="none")}
    monkeypatch.setattr(actions, "read_health", lambda container, **_kwargs: readings[container])
    clock = Clock()
    ok, reason = actions.verify_stack_up(
        "media", timeout=10, poll_seconds=1, stable_seconds=2,
        clock=clock, sleep=clock.sleep,
    )
    assert ok and "2 containers stable" in reason


@pytest.mark.parametrize(("reading", "reason"), [
    (_reading(status="exited"), "status=exited"),
    (_reading(health="unhealthy"), "healthcheck failing"),
])
def test_up_verification_fails_fast(monkeypatch, reading, reason):
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: (0, ["a"]))
    monkeypatch.setattr(actions, "read_health", lambda _container, **_kwargs: reading)
    assert reason in actions.verify_stack_up("media", timeout=5, poll_seconds=1)[1]


def test_up_verification_fails_on_restart_increase(monkeypatch):
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: (0, ["a"]))
    queue = [_reading(restarts=2), _reading(restarts=3)]
    monkeypatch.setattr(actions, "read_health", lambda _container, **_kwargs: queue.pop(0))
    clock = Clock()
    result = actions.verify_stack_up(
        "media", timeout=5, poll_seconds=1, stable_seconds=5,
        clock=clock, sleep=clock.sleep,
    )
    assert result == (False, "a: restarted 1x during verification")


def test_up_verification_zero_and_timeout(monkeypatch):
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: (0, []))
    assert actions.verify_stack_up("media") == (False, "no containers started")
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: (0, ["a"]))
    monkeypatch.setattr(actions, "read_health", lambda _container, **_kwargs: _reading(health="starting"))
    clock = Clock()
    ok, reason = actions.verify_stack_up(
        "media", timeout=3, poll_seconds=1, clock=clock, sleep=clock.sleep,
    )
    assert not ok and "not healthy within 3s" in reason


def test_down_verification_passes_and_times_out(monkeypatch):
    queue = [(0, ["a"]), (0, [])]
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: queue.pop(0))
    clock = Clock()
    assert actions.verify_stack_down(
        "media", timeout=3, poll_seconds=1, clock=clock, sleep=clock.sleep,
    ) == (True, "no containers remain")
    monkeypatch.setattr(actions, "stack_container_ids", lambda *a, **k: (0, ["a"]))
    clock = Clock()
    ok, reason = actions.verify_stack_down(
        "media", timeout=2, poll_seconds=1, clock=clock, sleep=clock.sleep,
    )
    assert not ok and "still present" in reason
