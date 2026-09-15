"""
Target resolution + restart argv + verifier (landing 1b, planet_express/execution/actions.py).
Refusals that need no docker call must happen before any argv is built.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import actions


class FakeDocker:
    def __init__(self, services="web\n", ids="abc123\n", name="/fixture-healthy"):
        self.services, self.ids, self.name = services, ids, name
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        assert isinstance(argv, list)
        self.calls.append(argv)
        if argv[-2:] == ["config", "--services"]:
            return 0, self.services, ""
        if "ps" in argv:
            return 0, self.ids, ""
        if argv[:3] == ["docker", "inspect", "--format"] and argv[3] == "{{.Name}}":
            return 0, self.name, ""
        raise AssertionError(f"unexpected argv {argv!r}")


@pytest.fixture
def stacks(tmp_path, monkeypatch):
    root = tmp_path / "stacks"
    for name in ("healthy", "traefik", "ai"):
        (root / name).mkdir(parents=True)
        (root / name / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(actions.config, "STACKS_ROOT", root)
    monkeypatch.setattr(actions.config, "FORBIDDEN_STACKS", ["ai"])
    monkeypatch.setattr(actions.config, "PAUSED_CONTAINERS", [])
    return root


def _install(monkeypatch, fake):
    monkeypatch.setattr(actions.bender, "run_argv", fake)
    return fake


def test_happy_path_resolves_exactly_one_container(stacks, monkeypatch):
    fake = _install(monkeypatch, FakeDocker())
    target = actions.resolve_target("healthy", "web")
    assert target == actions.Target("healthy", "web", "fixture-healthy")
    assert target.key == "healthy/web"
    compose = str(stacks / "healthy" / "docker-compose.yml")
    assert fake.calls[0] == ["docker", "compose", "-f", compose, "config", "--services"]
    assert fake.calls[1] == ["docker", "compose", "-f", compose, "ps", "-a", "-q", "web"]


@pytest.mark.parametrize("stack, service", [
    ("../etc", "web"), ("healthy", "../../x"), ("a/b", "web"), ("healthy", "web;rm"),
    ("", "web"), ("healthy", "$(id)"), ("-rf", "web"),
])
def test_invalid_names_are_refused_before_any_docker_call(stacks, monkeypatch, stack, service):
    fake = _install(monkeypatch, FakeDocker())
    with pytest.raises(actions.TargetError, match="invalid"):
        actions.resolve_target(stack, service)
    assert fake.calls == []


def test_forbidden_stack_is_refused_before_any_docker_call(stacks, monkeypatch):
    fake = _install(monkeypatch, FakeDocker())
    with pytest.raises(actions.TargetError, match="forbidden"):
        actions.resolve_target("ai", "web")
    assert fake.calls == []


def test_network_guarded_service_is_refused_before_any_docker_call(stacks, monkeypatch):
    fake = _install(monkeypatch, FakeDocker())
    with pytest.raises(actions.TargetError, match="network-guarded"):
        actions.resolve_target("traefik", "proxy")
    with pytest.raises(actions.TargetError, match="network-guarded"):
        actions.resolve_target("healthy", "adguard-home")
    assert fake.calls == []


def test_missing_stack_is_refused(stacks, monkeypatch):
    fake = _install(monkeypatch, FakeDocker())
    with pytest.raises(actions.TargetError, match="no stack named"):
        actions.resolve_target("ghost", "web")
    assert fake.calls == []


def test_unknown_service_is_refused(stacks, monkeypatch):
    _install(monkeypatch, FakeDocker(services="db\ncache\n"))
    with pytest.raises(actions.TargetError, match="has no service"):
        actions.resolve_target("healthy", "web")


def test_service_without_a_container_is_refused(stacks, monkeypatch):
    _install(monkeypatch, FakeDocker(ids=""))
    with pytest.raises(actions.TargetError, match="has no container"):
        actions.resolve_target("healthy", "web")


def test_ambiguous_service_is_refused(stacks, monkeypatch):
    _install(monkeypatch, FakeDocker(ids="abc\ndef\n"))
    with pytest.raises(actions.TargetError, match="ambiguous"):
        actions.resolve_target("healthy", "web")


def test_paused_container_is_refused_for_mutation_only(stacks, monkeypatch):
    _install(monkeypatch, FakeDocker())
    monkeypatch.setattr(actions.config, "PAUSED_CONTAINERS", ["fixture-healthy"])
    with pytest.raises(actions.TargetError, match="paused"):
        actions.resolve_target("healthy", "web")
    assert actions.resolve_target("healthy", "web", for_mutation=False).container == "fixture-healthy"


def test_restart_argv_is_fixed_and_uses_the_resolved_compose_file(stacks):
    target = actions.Target("healthy", "web", "fixture-healthy")
    assert actions.restart_argv(target) == [
        "docker", "compose", "-f", str(stacks / "healthy" / "docker-compose.yml"), "restart", "web",
    ]


# ── verify_after_restart ────────────────────────────────────────────────────────
class FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _readings(monkeypatch, *outputs):
    queue = list(outputs)

    def fake(argv, timeout):
        assert argv[:3] == ["docker", "inspect", "--format"]
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        return (1, "", out[1]) if isinstance(out, tuple) else (0, out, "")

    monkeypatch.setattr(actions.bender, "run_argv", fake)


def _verify(t, **kw):
    return actions.verify_after_restart("c", kw.pop("baseline", 0), clock=t.clock, sleep=t.sleep, **kw)


def test_verify_passes_after_the_stability_window(monkeypatch):
    _readings(monkeypatch, "running\t0\thealthy")
    t = FakeTime()
    ok, reason = _verify(t)
    assert ok and reason.startswith("healthy for")
    assert t.now <= actions.VERIFY_STABLE_SECONDS + 2 * actions.DEFAULT_POLL_SECONDS


def test_verify_passes_for_a_container_without_healthcheck(monkeypatch):
    _readings(monkeypatch, "running\t0\tnone")
    ok, reason = _verify(FakeTime())
    assert ok and "no healthcheck" in reason


def test_verify_waits_through_starting_then_passes(monkeypatch):
    _readings(monkeypatch, *(["running\t0\tstarting"] * 6), "running\t0\thealthy")
    ok, _ = _verify(FakeTime())
    assert ok


def test_verify_fails_when_unhealthy(monkeypatch):
    _readings(monkeypatch, "running\t0\tstarting", "running\t0\tunhealthy")
    assert _verify(FakeTime()) == (False, "healthcheck failing")


def test_verify_fails_when_not_running(monkeypatch):
    _readings(monkeypatch, "restarting\t3\tnone")
    assert _verify(FakeTime(), baseline=3) == (False, "status=restarting")


def test_verify_fails_on_restarts_beyond_the_baseline(monkeypatch):
    _readings(monkeypatch, "running\t5\thealthy", "running\t6\thealthy")
    assert _verify(FakeTime(), baseline=5) == (False, "restarted 1x during verification")


def test_verify_pre_existing_restarts_do_not_fail(monkeypatch):
    _readings(monkeypatch, "running\t55\thealthy")
    ok, _ = _verify(FakeTime(), baseline=55)
    assert ok


def test_verify_without_a_baseline_uses_the_first_reading(monkeypatch):
    _readings(monkeypatch, "running\t9\thealthy")
    ok, _ = _verify(FakeTime(), baseline=None)
    assert ok


def test_verify_times_out_if_never_healthy(monkeypatch):
    _readings(monkeypatch, "running\t0\tstarting")
    t = FakeTime()
    ok, reason = _verify(t)
    assert not ok and reason.startswith("not healthy within 90s")


def test_verify_fails_when_inspect_fails(monkeypatch):
    _readings(monkeypatch, (1, "No such container: c"))
    assert _verify(FakeTime()) == (False, "inspect failed: No such container: c")
