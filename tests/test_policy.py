"""planet_express/execution/policy.py (landing 1b): fail closed, R0 automatic, above R0 approval."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import actions, policy


def test_unknown_action_is_denied():
    d = policy.decide("docker.rm_everything")
    assert not d.allowed and "unknown action" in d.reason


def test_restart_needs_approval():
    d = policy.decide(actions.RESTART_SERVICE)
    assert d.allowed and d.needs_approval and d.risk == "R1"


def test_refused_target_is_denied_even_for_a_known_action():
    d = policy.decide(actions.RESTART_SERVICE, target_error="stack 'ai' is forbidden")
    assert not d.allowed and "stack 'ai' is forbidden" in d.reason


def test_r0_action_runs_automatically(monkeypatch):
    monkeypatch.setitem(actions.REGISTRY, "docker.logs_tail", actions.ActionSpec("docker.logs_tail", "R0", "logs"))
    d = policy.decide("docker.logs_tail")
    assert d.allowed and not d.needs_approval


def test_r4_action_is_never_allowed(monkeypatch):
    monkeypatch.setitem(actions.REGISTRY, "host.format_disk", actions.ActionSpec("host.format_disk", "R4", "no"))
    d = policy.decide("host.format_disk")
    assert not d.allowed


def test_unknown_risk_class_is_denied(monkeypatch):
    monkeypatch.setitem(actions.REGISTRY, "weird.thing", actions.ActionSpec("weird.thing", "R9", "?"))
    d = policy.decide("weird.thing")
    assert not d.allowed and "unknown risk class" in d.reason


def test_restart_action_declares_no_abort_rollback_or_resume():
    spec = actions.REGISTRY[actions.RESTART_SERVICE]
    assert (spec.abortable, spec.rollbackable, spec.resumable) == (False, False, False)


def test_only_r1_allows_direct_request():
    for risk in (*policy.RISK_LEVELS, 'R9', '', None):
        assert policy.allows_direct_request(risk) is (risk == 'R1')


def test_configurable_policy(monkeypatch):
    import config
    from config_schema import AutonomyConfig

    autonomy = AutonomyConfig(direct_request_risks=['R2'], forbidden_risks=['R3', 'R4'])
    monkeypatch.setattr(config, 'AUTONOMY', autonomy)
    monkeypatch.setitem(actions.REGISTRY, 'test', actions.ActionSpec('test', 'R3', 'test'))
    assert not policy.decide('test').allowed
    assert policy.decide('test', autonomy=AutonomyConfig()).needs_approval
    assert not policy.allows_direct_request('R1')
    assert policy.allows_direct_request('R2')
    assert not policy.allows_direct_request('R2', autonomy=AutonomyConfig())
    for rollbackable in (False, True):
        monkeypatch.setitem(actions.REGISTRY, 'test', actions.ActionSpec(
            'test', 'R1', 'test', rollbackable=rollbackable,
        ))
        for settings in (AutonomyConfig(), autonomy, AutonomyConfig(direct_request_risks=[])):
            decision = policy.decide('test', autonomy=settings)
            assert decision.allowed and decision.needs_approval


def test_limit_boundaries():
    from config_schema import AutonomyConfig

    now = 100_000
    settings = AutonomyConfig()
    check = lambda attempts: policy.limit_refusal(attempts, now, autonomy=settings)
    assert check([]) is None
    assert check([now - 1799]) == 'cooling down: last attempt 29m ago, cooldown 30m'
    assert check([now - 1800]) is None
    assert check([now - 4000, now - 2000]) is None
    assert check([now - 86400, now - 4000, now - 2000]) == 'attempt cap reached: 3 in 24h (max 3)'
    assert check([now - 86401, now - 4000, now - 2000]) is None
    assert check([now - 60, now - 2000, now - 4000]) == (
        'cooling down: last attempt 1m ago, cooldown 30m'
    )
    assert policy.limit_refusal([now], now, autonomy=AutonomyConfig(cooldown_seconds=0)) is None


def test_limit_lookback_covers_the_longer_of_cooldown_and_a_day():
    from config_schema import AutonomyConfig

    assert policy.limit_lookback_seconds(autonomy=AutonomyConfig()) == 86400
    assert policy.limit_lookback_seconds(autonomy=AutonomyConfig(cooldown_seconds=48 * 3600)) == 48 * 3600
