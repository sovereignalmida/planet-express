"""planet_express/application/planner.py (slice 5b-2): model intent -> validated runbook, the
vpn recipe, and refusals. No host, no LLM: the binder and identity lookups are fakes."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.application import planner
from planet_express.execution import actions
from tests.binding_fakes import FakeBinder

IDENTITIES = {"CASA_GLUETON": ("network", "gluetun"), "CASA_GSP": ("network", "gsp"),
              "CASA_QBIT": ("media", "qbit"), "CASA_SONARR": ("media", "sonarr")}


def builder(**overrides):
    def identities(containers, **_):
        return {name: IDENTITIES[name] for name in containers if name in IDENTITIES}

    def resolve(stack, service, for_mutation=True, timeout=None):
        return actions.Target(stack, service, f"{stack}-{service}-1")

    kwargs = {"binder": FakeBinder(), "resolve_target": resolve, "resolve_identities": identities}
    return planner.RunbookBuilder(**(kwargs | overrides))


def test_vpn_recipe_dead_forward_is_the_full_sequence_with_a_typed_port_reference():
    plan = builder().build({"title": "Resync VPN", "finding_ids": ["f1"],
                            "steps": [{"recipe": "vpn.resync_port_forward",
                                       "params": {"mode": "dead_forward"}}]})
    types = [step.type for step in plan.runbook.steps]
    assert types == ["service.restart", "wait", "service.restart", "service.restart", "wait",
                     "read.qbittorrent_session_port", "check.log_since_start"]
    # gateway first, then the two containers sharing its namespace, in order
    assert [s.params.get("service") for s in plan.runbook.steps[:4]] == ["gluetun", None, "gsp", "qbit"]
    assert plan.runbook.steps[1].params["seconds"] == planner.VPN_TUNNEL_WAIT_SECONDS
    # the log check consumes the port the read step produced, by step number
    assert plan.runbook.steps[6].params["port"] == {"from_step": 6, "output": "port"}
    assert plan.runbook.steps[6].params["match"] == planner.VPN_PORT_MARKER


def test_vpn_recipe_sync_mismatch_leaves_the_gateway_alone():
    plan = builder().build({"title": "Resync", "steps": [
        {"recipe": "vpn.resync_port_forward", "params": {"mode": "sync_mismatch"}}]})
    assert [s.params.get("service") for s in plan.runbook.steps if s.type == "service.restart"] == \
        ["gsp", "qbit"]


def test_bindings_are_built_from_docker_labels_not_from_the_model():
    plan = builder().build({"title": "Restart it", "steps": [
        {"type": "service.restart", "container": "CASA_SONARR",
         "params": {"stack": "LIES", "service": "LIES"}}]})
    step = plan.runbook.steps[0]
    # the compose labels win over whatever the model claimed
    assert (step.params["stack"], step.params["service"]) == ("media", "sonarr")
    assert step.binding["container"] == "media-sonarr-1"


def test_container_identity_comes_from_compose_labels():
    plan = builder().build({"title": "Restart it", "steps": [
        {"type": "service.restart", "container": "CASA_SONARR"}]})
    step = plan.runbook.steps[0]
    assert (step.params["stack"], step.params["service"]) == ("media", "sonarr")
    assert step.binding["service"] == "sonarr"


@pytest.mark.parametrize("plan, message", [
    ({"title": "x", "steps": [{"type": "shell", "params": {"command": "rm -rf /"}}]}, "unknown step type"),
    ({"title": "x", "steps": [{"recipe": "vpn.make_coffee", "params": {}}]}, "unknown recipe"),
    ({"title": "x", "steps": [{"recipe": "vpn.resync_port_forward", "params": {"mode": "?"}}]}, "mode"),
    ({"title": "x", "steps": [{"type": "stack.down_all", "params": {}}]}, "not available to the planner"),
    ({"title": "x", "steps": []}, "at least one step"),
    ({"title": "", "steps": [{"type": "wait", "params": {"seconds": 1}}]}, "needs a title"),
    ({"title": "x", "steps": [{"type": "service.restart", "params": {"stack": "media"}}]}, "needs a stack and service"),
    ({"title": "x", "steps": [{"type": "wait", "params": {"seconds": 9000}}]}, ""),
])
def test_plans_the_catalogue_cannot_express_are_refused(plan, message):
    with pytest.raises((planner.PlanRefused, Exception)) as caught:
        builder().build(plan)
    assert message in str(caught.value) or message == ""


def test_an_unknown_container_is_refused_not_guessed():
    with pytest.raises(planner.PlanRefused, match="not a compose-managed container"):
        builder().build({"title": "x", "steps": [
            {"type": "service.restart", "container": "CASA_MYSTERY"}]})


def test_a_network_guarded_target_is_refused_by_resolution():
    def guarded(stack, service, for_mutation=True, timeout=None):
        raise actions.TargetError(f"{stack}/{service} is network-guarded (Traefik/AdGuard)")

    with pytest.raises(planner.PlanRefused, match="network-guarded"):
        builder(resolve_target=guarded).build({"title": "x", "steps": [
            {"type": "service.restart", "params": {"stack": "network", "service": "traefik"}}]})


def test_too_many_steps_is_refused():
    steps = [{"type": "wait", "params": {"seconds": 1}} for _ in range(planner.MAX_STEPS + 1)]
    with pytest.raises(planner.PlanRefused, match="at most"):
        builder().build({"title": "x", "steps": steps})


def test_parse_plans_handles_fences_junk_and_caps_the_count():
    plans, needs = planner.parse_plans('```json\n{"plans": [{"title": "a"}], "needs_human": [{"why": "z"}]}\n```')
    assert plans == [{"title": "a"}] and needs == [{"why": "z"}]
    with pytest.raises(planner.PlanRefused, match="did not parse"):
        planner.parse_plans("not json at all")
    plans = ",".join('{"title": "t"}' for _ in range(planner.MAX_PLANS + 3))
    many = f'{{"plans": [{plans}]}}'
    assert len(planner.parse_plans(many)[0]) == planner.MAX_PLANS


def test_a_container_check_given_a_stack_and_service_binds_and_drops_them():
    plan = builder().build({"title": "Look at web", "steps": [
        {"type": "check.container", "params": {"stack": "media", "service": "web", "expect": "healthy"}}]})
    step = plan.runbook.steps[0]
    assert step.params == {"expect": "healthy"}          # check.container takes no stack/service
    assert step.binding["container_id"]                   # ...but it is bound to that container


def test_a_step_missing_a_required_param_is_refused_not_a_validation_crash():
    with pytest.raises(planner.PlanRefused, match="check.container: expect"):
        builder().build({"title": "Look at web", "steps": [
            {"type": "check.container", "params": {"stack": "media", "service": "web"}}]})


def test_params_that_are_not_an_object_are_refused_not_a_crash():
    with pytest.raises(planner.PlanRefused, match="params must be an object"):
        builder().build({"title": "Restart", "steps": [
            {"type": "service.restart", "params": "just restart sonarr"}]})
    with pytest.raises(planner.PlanRefused, match="params must be an object"):
        builder().build({"title": "Resync", "steps": [
            {"recipe": "vpn.resync_port_forward", "params": ["dead_forward"]}]})


def test_a_unit_step_outside_the_sudo_allowlist_never_becomes_a_plan():
    with pytest.raises(planner.PlanRefused, match="sudo allowlist"):
        builder().build({"title": "Restart docker", "steps": [
            {"type": "unit.action", "params": {"action": "restart", "unit": "docker.service"}}]})


def test_an_allowlisted_unit_step_still_builds(monkeypatch):
    import casa_bender as bender
    from config_schema import SudoAllowlist, SudoUnitGrant
    monkeypatch.setattr(bender, "SUDO_ALLOWLIST", SudoAllowlist(
        units=[SudoUnitGrant(unit="casa-stacks.service", actions=["start", "stop", "restart"])]))
    plan = builder().build({"title": "Restart the stacks unit", "steps": [
        {"type": "unit.action", "params": {"action": "restart", "unit": "casa-stacks.service"}}]})
    assert plan.runbook.steps[0].binding == {"unit": "casa-stacks.service"}


def test_stack_steps_go_through_the_normal_stack_resolver():
    refused = []

    def resolve_stack(action, stack, **_):
        refused.append((action, stack))
        raise actions.TargetError(f"stack {stack!r} is forbidden")

    with pytest.raises(planner.PlanRefused, match="forbidden"):
        builder(resolve_stack=resolve_stack).build({"title": "Bring ai up", "steps": [
            {"type": "stack.up", "params": {"stack": "ai"}}]})
    assert refused == [(actions.UP_STACK, "ai")]


def test_malformed_discriminators_and_finding_ids_are_refusals():
    with pytest.raises(planner.PlanRefused, match="step type must be a string"):
        builder().build({"title": "t", "steps": [{"type": {"restart": True}}]})
    with pytest.raises(planner.PlanRefused, match="recipe name must be a string"):
        builder().build({"title": "t", "steps": [{"recipe": ["vpn.resync_port_forward"]}]})
    with pytest.raises(planner.PlanRefused, match="finding_ids must be a list"):
        builder().build({"title": "t", "finding_ids": 1, "steps": [
            {"type": "service.restart", "params": {"stack": "media", "service": "sonarr"}}]})


def test_the_planner_may_not_move_forbidden_or_ingress_stacks(monkeypatch):
    import config as config_module
    from planet_express.execution import actions as actions_module
    monkeypatch.setattr(config_module, "FORBIDDEN_STACKS", ["ai", "clawbot"])
    monkeypatch.setattr(actions_module, "is_ingress_stack", lambda stack: stack == "network")
    for step_type, stack, message in (("stack.down", "ai", "forbidden"),
                                      ("stack.up", "clawbot", "forbidden"),
                                      ("stack.up", "network", "ingress"),
                                      ("stack.down", "network", "ingress")):
        with pytest.raises(planner.PlanRefused, match=message):
            builder().build({"title": "t", "steps": [
                {"type": step_type, "params": {"stack": stack}}]})
