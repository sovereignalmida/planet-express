"""Contract 5: the `mission` origin. Accepted for typed steps only under an approval row for the mission's current grant
hash; never unattended; never limit-exempt; every other origin unchanged."""
import os

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from missions_helpers import make_store, new_mission

from planet_express.execution import policy
from planet_express.execution.runbook import Runbook
from planet_express.missions.approvals import mission_step_approved, record_approval
from planet_express.missions.store import MissionStore


def runbook(step_type="service.restart"):
    if step_type == "wait":
        step = {"type": "wait", "params": {"seconds": 1}, "binding": {}}
    elif step_type == "prune.safe":
        step = {"type": step_type, "params": {}, "binding": {}}
    else:
        step = {"type": step_type, "params": {"stack": "media", "service": "sonarr"},
                "binding": {"compose_path": "/stacks/media/docker-compose.yml", "compose_sha256": "a" * 64,
                            "project": "media", "service": "sonarr", "container": "sonarr", "container_id": "0123456789ab"}}
    return Runbook.model_validate({"title": "t", "steps": [step], "artifacts": {}})


def test_mission_is_a_known_origin_and_is_never_unattended_or_limit_exempt():
    assert "mission" in policy.RUNBOOK_ORIGINS
    assert "mission" not in policy.LIMIT_EXEMPT_ORIGINS and "mission" not in policy.OPERATOR_ORIGINS
    assert "mission" not in policy.DIRECT_RUNBOOK_ORIGINS and "mission" not in policy.ELEVATABLE_ORIGINS
    # not automatic even for the two steps that are automatic for their own server-assigned origin
    for step in ("prune.safe", "service.restart"):
        decision = policy.decide_runbook(runbook(step), "mission", mission_approved=True)
        assert not decision.automatic


def test_a_mutating_step_without_a_mission_approval_is_refused_not_carded():
    decision = policy.decide_runbook(runbook(), "mission")
    assert not decision.allowed and not decision.needs_approval and "approval" in decision.reason
    assert not policy.decide_runbook(runbook(), "mission", mission_approved=False).allowed


def test_a_mutating_step_with_the_mission_approval_is_allowed_without_a_second_v3_card():
    decision = policy.decide_runbook(runbook(), "mission", mission_approved=True)
    assert decision.allowed and not decision.needs_approval and not decision.automatic


def test_a_read_from_a_mission_needs_no_approval_but_is_still_not_automatic():
    decision = policy.decide_runbook(runbook("wait"), "mission")
    assert decision.allowed and not decision.needs_approval and not decision.automatic


def test_the_flag_changes_nothing_for_other_origins():
    for origin in ("planner", "chat", "incident", "amy", "install", "system", "zoidberg", "telegram"):
        plain = policy.decide_runbook(runbook(), origin)
        flagged = policy.decide_runbook(runbook(), origin, mission_approved=True)
        assert plain == flagged
    assert policy.decide_runbook(runbook(), "planner").needs_approval
    assert policy.decide_runbook(runbook("prune.safe"), "planner").needs_approval


def test_forbidden_risk_still_refuses_a_mission_even_when_approved():
    from config_schema import AutonomyConfig
    decision = policy.decide_runbook(runbook(), "mission", mission_approved=True,
                                     autonomy=AutonomyConfig(forbidden_risks=["R1", "R4"], direct_request_risks=[]))
    assert not decision.allowed


def guarded_runbook(step_type="service.restart", stack="network", service="adguardhome"):
    step = {"type": step_type, "params": {"stack": stack, "service": service},
            "binding": {"compose_path": f"/stacks/{stack}/docker-compose.yml", "compose_sha256": "a" * 64, "project": stack,
                        "service": service, "container": service, "container_id": "0123456789ab"}}
    return Runbook.model_validate({"title": "t", "steps": [step], "artifacts": {}})


@pytest.mark.parametrize("stack, service", [("network", "adguardhome"), ("edge", "traefik"), ("proxy", "my-traefik-2")])
def test_a_mission_step_on_a_network_guarded_service_is_refused_even_when_the_mission_is_approved(stack, service):
    for approved in (False, True):
        decision = policy.decide_runbook(guarded_runbook(stack=stack, service=service), "mission", mission_approved=approved)
        assert not decision.allowed and "protected-touch" in decision.reason and not decision.automatic


def test_an_explicit_protected_touch_approval_lets_the_guarded_step_through_the_mission_path_but_never_automatically():
    decision = policy.decide_runbook(guarded_runbook(), "mission", mission_approved=True, guarded_touch_approved=True)
    assert decision.allowed and not decision.needs_approval and not decision.automatic
    # the touch approval alone is not the mission's own approval
    assert not policy.decide_runbook(guarded_runbook(), "mission", guarded_touch_approved=True).allowed
    # the flag changes nothing for another origin: the AdGuard exception tests' paths are untouched
    assert policy.decide_runbook(guarded_runbook(), "telegram").needs_approval
    assert not policy.decide_runbook(guarded_runbook("service.restart"), "zoidberg").automatic


def test_an_ordinary_service_is_unaffected_by_the_guard():
    assert policy.decide_runbook(runbook(), "mission", mission_approved=True).allowed


def test_only_a_critical_mission_with_a_protected_touch_carries_the_explicit_approval(tmp_path):
    from missions_helpers import advance_to
    from planet_express.missions.models import Grant
    from planet_express.missions.approvals import guarded_touch_approved
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    repair = new_mission(ms)
    record_approval(store, repair.id, "grant", repair.grant_hash, "approve", "operator:me")
    assert mission_step_approved(store, repair.id) and not guarded_touch_approved(store, repair.id)   # a Repair mission never does
    crit = new_mission(ms, Grant.model_validate({"tier": "critical", "scope": {"stacks": ["network"]}}))
    advance_to(ms, crit.id, "queued", "investigating", "awaiting_decision")
    assert not guarded_touch_approved(store, crit.id) and not guarded_touch_approved(store, "M-404")
    plan = {"protected_touches": ["host:local/service:network/adguardhome"], "steps": []}
    crit = ms.set_plan(crit.id, plan)
    record_approval(store, crit.id, "grant", crit.grant_hash, "approve", "operator:me", elevated=True)
    record_approval(store, crit.id, "plan", crit.plan_hash, "approve", "operator:me")
    assert not guarded_touch_approved(store, crit.id)                                              # the touch row is still missing
    record_approval(store, crit.id, "critical:0", crit.plan_hash, "approve", "operator:me", elevated=True)
    assert guarded_touch_approved(store, crit.id)


def test_the_approval_flag_comes_from_the_current_grant_hash_row(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    assert not mission_step_approved(store, m.id)
    record_approval(store, m.id, "grant", m.grant_hash, "deny", "operator:me")
    assert not mission_step_approved(store, m.id)                      # a denial is never a proof
    other = new_mission(ms)
    record_approval(store, other.id, "grant", other.grant_hash, "approve", "operator:me")
    assert mission_step_approved(store, other.id) and not mission_step_approved(store, m.id)   # not shared between missions
    assert not mission_step_approved(store, "M-999")                                           # no such mission
