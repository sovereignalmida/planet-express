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


def test_stack_action_risks_and_capabilities():
    expected = {
        actions.UP_STACK: "R1", actions.DOWN_STACK: "R2", actions.UP_ALL: "R2",
        actions.DOWN_ALL: "R3", actions.DOWN_INGRESS: "R3",
    }
    for name, risk in expected.items():
        spec = actions.REGISTRY[name]
        assert spec.risk == risk
        assert (spec.abortable, spec.rollbackable, spec.resumable) == (False, False, False)


def test_telegram_direct_is_an_operator_origin():
    assert "telegram-direct" in policy.OPERATOR_ORIGINS


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


def _runbook(step_type="service.restart"):
    from planet_express.execution.runbook import Runbook

    if step_type == "prune.safe":
        step = {"type": step_type, "params": {}, "binding": {}}
    else:
        step = {
            "type": step_type,
            "params": {"stack": "media", "service": "sonarr"},
            "binding": {
                "compose_path": "/stacks/media/docker-compose.yml",
                "compose_sha256": "a" * 64,
                "project": "media",
                "service": "sonarr",
                "container": "sonarr",
                "container_id": "0123456789ab",
            },
        }
    return Runbook.model_validate({"title": "test", "steps": [step], "artifacts": {}})


def test_runbook_policy_accepts_every_declared_origin():
    for origin in policy.RUNBOOK_ORIGINS:
        runbook = _runbook()
        decision = policy.decide_runbook(runbook, origin)
        if origin in policy.DIRECT_RUNBOOK_ORIGINS:
            assert decision.allowed and not decision.needs_approval and not decision.automatic
        else:
            assert decision.allowed


def test_runbook_policy_automatic_exception_is_exact():
    automatic = policy.decide_runbook(_runbook("prune.safe"), "system")
    assert automatic.allowed and automatic.automatic and not automatic.needs_approval
    planner = policy.decide_runbook(_runbook("prune.safe"), "planner")
    assert planner.allowed and planner.needs_approval and not planner.automatic
    zoidberg = policy.decide_runbook(_runbook("prune.safe"), "zoidberg")
    assert zoidberg.allowed and zoidberg.needs_approval and not zoidberg.automatic


def test_runbook_policy_forbidden_risk_and_unknown_origin():
    from config_schema import AutonomyConfig

    forbidden = policy.decide_runbook(
        _runbook("service.restart"), "planner",
        autonomy=AutonomyConfig(forbidden_risks=["R1", "R4"], direct_request_risks=[]),
    )
    assert not forbidden.allowed and forbidden.risk == "R1"
    unknown = policy.decide_runbook(_runbook(), "model-supplied")
    assert not unknown.allowed and "unknown origin" in unknown.reason


def test_runbook_policy_refuses_unknown_registry_risk(monkeypatch):
    from planet_express.execution import runbook as module

    original = module.STEP_TYPES["service.restart"]
    monkeypatch.setitem(module.STEP_TYPES, "service.restart", original.__class__(
        original.params_model, original.binding_model, "R9", original.rollback,
        original.outputs, original.reference_fields, original.target,
    ))
    decision = policy.decide_runbook(_runbook(), "planner")
    assert not decision.allowed and decision.risk == "R9"
    assert "unknown risk class" in decision.reason
