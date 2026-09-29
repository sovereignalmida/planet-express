"""planet_express/execution/policy.py (landing 1b): fail closed, R0 automatic, above R0 approval."""

import os
import sys
from pathlib import Path

import pytest

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


def test_the_default_ceilings_differ_by_surface():
    """Telegram keeps R1. The dashboard reaches R3, because its stack controls need R2 to
    exist at all -- what holds the line there is the passphrase (requires_elevation), not a
    lower ceiling.
    """
    for risk in (*policy.RISK_LEVELS, 'R9', '', None):
        assert policy.allows_direct_request(risk, 'telegram-direct') is (risk == 'R1')
        assert policy.allows_direct_request(risk, 'dashboard-direct') is (
            risk in {'R1', 'R2', 'R3'})


def test_everything_above_r1_still_needs_the_passphrase():
    """The ceiling says what the dashboard may reach; this says what it may reach without
    asking again. Raising the first without the second would be the gate undone."""
    assert not policy.requires_elevation('R1', 'dashboard-direct')
    for risk in ('R2', 'R3'):
        assert policy.allows_direct_request(risk, 'dashboard-direct')
        assert policy.requires_elevation(risk, 'dashboard-direct')


def test_configurable_policy(monkeypatch):
    import config
    from config_schema import AutonomyConfig

    autonomy = AutonomyConfig(direct_request_risks=['R2'], forbidden_risks=['R3', 'R4'])
    monkeypatch.setattr(config, 'AUTONOMY', autonomy)
    monkeypatch.setitem(actions.REGISTRY, 'test', actions.ActionSpec('test', 'R3', 'test'))
    assert not policy.decide('test').allowed
    assert policy.decide('test', autonomy=AutonomyConfig()).needs_approval
    # The shared list is Telegram's: R2 and nothing else.
    assert not policy.allows_direct_request('R1', 'telegram-direct')
    assert policy.allows_direct_request('R2', 'telegram-direct')
    # The dashboard keeps its own default until a per-origin entry says otherwise.
    assert policy.allows_direct_request('R1', 'dashboard-direct')
    # A config that says nothing gets the built-in defaults instead: the dashboard reaches R2
    # (behind the passphrase), Telegram does not reach it at all.
    assert policy.allows_direct_request('R2', 'dashboard-direct', autonomy=AutonomyConfig())
    assert not policy.allows_direct_request('R2', 'telegram-direct', autonomy=AutonomyConfig())
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


# ── per-origin direct-request ceilings (T47) ─────────────────────────────────────

def _autonomy(**kwargs):
    from config_schema import AutonomyConfig
    return AutonomyConfig(**kwargs)


def test_an_override_raises_one_surface_and_not_the_other():
    """The point of the whole change. An elevated session is something a browser can ask for
    and prove; a chat message cannot, so the two must not share one number."""
    autonomy = _autonomy(direct_request_risks_by_origin={
        "dashboard-direct": ["R1", "R2", "R3"]})
    for risk in ("R1", "R2", "R3"):
        assert policy.allows_direct_request(risk, "dashboard-direct", autonomy=autonomy)
    assert policy.allows_direct_request("R1", "telegram-direct", autonomy=autonomy)
    for risk in ("R2", "R3"):
        assert not policy.allows_direct_request(risk, "telegram-direct", autonomy=autonomy)


def test_an_origin_with_no_override_falls_back_to_the_shared_ceiling():
    autonomy = _autonomy(direct_request_risks=["R1", "R2"],
                         direct_request_risks_by_origin={"dashboard-direct": ["R1"]})
    assert policy.allows_direct_request("R2", "telegram-direct", autonomy=autonomy)
    # An override can lower a ceiling as well as raise it.
    assert not policy.allows_direct_request("R2", "dashboard-direct", autonomy=autonomy)


def test_an_unknown_origin_gets_the_shared_ceiling():
    """Not a silent allow-all. A caller naming something that cannot originate anything is a
    bug, and it should behave no more permissively than the default."""
    autonomy = _autonomy(direct_request_risks_by_origin={
        "dashboard-direct": ["R1", "R2", "R3"]})
    assert policy.allows_direct_request("R1", "planner", autonomy=autonomy)
    assert not policy.allows_direct_request("R3", "planner", autonomy=autonomy)
    assert not policy.allows_direct_request("R3", "", autonomy=autonomy)


@pytest.mark.parametrize("override,message", [
    ({"nonsense": ["R1"]}, "is not a direct origin"),
    ({"dashboard": ["R1"]}, "is not a direct origin"),
    ({"dashboard-direct": ["R0", "R1"]}, "R0 must not be directly requestable"),
    ({"dashboard-direct": ["R4"]}, "cannot be both forbidden and directly requestable"),
])
def test_a_nonsensical_override_is_refused_at_load(override, message):
    """Refused rather than ignored: a ceiling written for an origin that cannot originate
    anything looks like it is in force and is not."""
    with pytest.raises(ValueError, match=message):
        _autonomy(direct_request_risks_by_origin=override)


def test_a_runbook_is_decided_against_its_own_origin():
    """The ceiling has to reach decide_runbook, not only the helper -- that is the path a real
    request takes, and it is where the origin was previously thrown away."""
    autonomy = _autonomy(direct_request_risks_by_origin={
        "dashboard-direct": ["R1", "R2", "R3"]})
    plan = _runbook("service.stop")                 # R2

    allowed = policy.decide_runbook(plan, "dashboard-direct", autonomy=autonomy)
    assert allowed.allowed and not allowed.needs_approval, allowed.reason

    refused = policy.decide_runbook(plan, "telegram-direct", autonomy=autonomy)
    assert not refused.allowed, "telegram-direct kept the dashboard's raised ceiling"
