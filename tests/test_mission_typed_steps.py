"""Slice 10: typed steps from the `mission` origin. The gate's four checks (scope and protected, mission and session state,
step type and approval, the record) and the command service entry that acts on an authorization."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from missions_helpers import advance_to, make_store, new_mission, repair_grant, token

from planet_express.execution import actions, policy, runbook as runbooks
from planet_express.missions import leases
from planet_express.missions.approvals import record_approval
from planet_express.missions.store import MissionStore
from planet_express.missions.typed_steps import (
    ACTOR,
    MISSION_STEP_TYPES,
    MissionStepGate,
    StepAuthorization,
    StepRefused,
)

STACKS_ROOT = str(actions.compose_file("x").parent.parent)


def binding(stack="media", service="sonarr"):
    return {"compose_path": str(actions.compose_file(stack)), "compose_sha256": "a" * 64, "project": stack,
            "service": service, "container": f"{stack}-{service}-1", "container_id": "0123456789ab"}


STACK_SET = {"stacks": [{"stack": "media", "compose_path": str(actions.compose_file("media")), "compose_sha256": "a" * 64,
                         "project": "media", "services": ["web"]}]}


def restart(stack="media", service="sonarr"):
    return {"type": "service.restart", "params": {"stack": stack, "service": service}, "binding": binding(stack, service)}


def book(*steps):
    return runbooks.Runbook.model_validate({"title": "t", "steps": list(steps), "artifacts": {}})


class Rig:
    def __init__(self, tmp_path, *, stack_leases=False, **grant_over):
        self.store, self.clock = make_store(tmp_path)
        self.ms = MissionStore(self.store)
        if stack_leases:
            self.ms.stacks_root = STACKS_ROOT          # as the supervisor does: a scoped service also leases its stack directory
        self.mission = new_mission(self.ms, repair_grant(**grant_over))
        advance_to(self.ms, self.mission.id, "queued", "investigating", "awaiting_decision")
        record_approval(self.store, self.mission.id, "grant", self.mission.grant_hash, "approve", "operator:me")
        self.session = self.ms.start_session(self.mission.id, kind="local", token_hash=self.ms.hash_token(token()),
                                             session_ttl_s=600, lease_ttl_s=600)
        self.identities = {}
        self.gate = MissionStepGate(self.ms, stacks_root=STACKS_ROOT, identities=lambda names: {
            n: self.identities.get(n, tuple(n.rsplit("-", 2)[:2])) for n in names})

    def events(self, name=None):
        out = [json.loads(e.body_json) for e in self.ms.events(self.mission.id) if e.actor == ACTOR]
        return [e for e in out if name is None or e["event"] == name]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def refused(rig, runbook, match, session=None):
    with pytest.raises(StepRefused, match=match):
        rig.gate.authorize(rig.mission.id, runbook, session_id=session or rig.session)


# ── 1. targets: canonical, in scope, not protected ──────────────────────────────────────────────────

def test_an_in_scope_step_is_authorized_and_the_check_is_recorded_first(rig):
    runbook = book(restart())
    auth = rig.gate.authorize(rig.mission.id, runbook, session_id=rig.session)
    assert auth.valid_for(runbook, rig.mission.id)
    assert "host:local/service:media/sonarr" in auth.resources
    (event,) = rig.events("step.authorized")
    assert event["plan_sha256"] == auth.plan_sha256 and event["grant_hash"] == rig.mission.grant_hash
    assert event["session"] == rig.session and event["steps"][0]["type"] == "service.restart"


def test_a_step_outside_the_grants_scope_is_refused_and_recorded(rig):
    refused(rig, book(restart(stack="other")), "outside the grant")
    (event,) = rig.events("step.refused")
    assert "outside the grant" in event["reason"] and event["steps"][0]["params"]["stack"] == "other"
    assert rig.events("step.authorized") == []


def test_a_protected_service_inside_the_scope_is_refused(tmp_path):
    rig = Rig(tmp_path, protected={"resources": ["service:media/sonarr"]})
    refused(rig, book(restart()), "protected")
    rig.gate.authorize(rig.mission.id, book(restart(service="radarr")), session_id=rig.session)   # its neighbour is fine


def test_the_sites_protected_set_wins_too(rig):
    gate = MissionStepGate(rig.ms, stacks_root=STACKS_ROOT, protected=["service:media/sonarr"], identities=rig.gate.identities)
    with pytest.raises(StepRefused, match="protected"):
        gate.authorize(rig.mission.id, book(restart()), session_id=rig.session)


def test_a_binding_that_points_outside_its_stack_cannot_even_be_written():
    step = restart()
    step["binding"] = {**step["binding"], "compose_path": "/etc/elsewhere/docker-compose.yml"}
    with pytest.raises(ValueError, match="compose_path"):
        book(step)


def test_a_mutating_step_with_no_target_is_refused(rig):
    refused(rig, book({"type": "stack.up_all", "params": {}, "binding": STACK_SET}), "not a step a mission may run")
    refused(rig, book({"type": "prune.safe", "params": {}, "binding": {}}), "not a step a mission may run")


def test_policy_refuses_an_untargeted_mutating_step_even_if_the_gate_were_skipped():
    for step in ({"type": "prune.safe", "params": {}, "binding": {}},
                 {"type": "stack.up_all", "params": {}, "binding": STACK_SET}):
        decision = policy.decide_runbook(book(step), "mission", mission_approved=True)
        assert not decision.allowed and "names no target" in decision.reason


# ── 2. the mission may act: executing, live session, current fences ─────────────────────────────────

def test_a_wrong_or_revoked_session_is_refused(rig):
    refused(rig, book(restart()), "no live session", session="S-someone-else")
    rig.ms.revoke_session(rig.session)
    refused(rig, book(restart()), "no live session")


def test_a_paused_mission_is_refused(rig):
    from planet_express.missions.state import transition
    transition(rig.store, rig.mission.id, "paused_tripwire", "tripwire")
    refused(rig, book(restart()), "not executing")


def test_a_pending_cancel_is_refused(rig):
    with rig.store._write() as conn:
        conn.execute("UPDATE missions SET cancel_requested_at=?, cancel_requested_by='operator:me' WHERE id=?",
                     (int(rig.clock()), rig.mission.id))
    refused(rig, book(restart()), "cancel is pending")


def test_a_stale_fence_is_refused(rig):
    leases.release(rig.store, rig.mission.id)                  # the lease the session's fence names is gone
    refused(rig, book(restart()), "lease is stale")


# ── 3. the approval covers the step type ────────────────────────────────────────────────────────────

def test_only_listed_step_types_may_run(rig):
    assert "compose.write" not in MISSION_STEP_TYPES and "stack.down_ingress" not in MISSION_STEP_TYPES
    assert "update.canary" not in MISSION_STEP_TYPES and "prune.safe" not in MISSION_STEP_TYPES
    step = {"type": "stack.down_ingress", "params": {"stack": "media"}, "binding": {"compose_path": str(actions.compose_file("media")),
            "compose_sha256": "a" * 64, "project": "media", "services": ["web"]}}
    refused(rig, book(step), "not a step a mission may run")


def test_a_unit_action_is_not_a_mission_step(tmp_path):
    step = {"type": "unit.action", "params": {"action": "restart", "unit": "casa-stacks.service"},
            "binding": {"unit": "casa-stacks.service"}}
    rig = Rig(tmp_path, sudo={"mode": "scoped", "rules": [{"unit": "casa-stacks.service", "actions": ["restart"]}]})
    refused(rig, book(step), "not a step a mission may run")        # even with a sudo rule for it: R3, and not via this path


def test_a_mutating_step_needs_the_missions_approval(rig):
    with rig.store._write() as conn:
        conn.execute("DELETE FROM mission_approvals WHERE mission_id=?", (rig.mission.id,))
    refused(rig, book(restart()), "no approval")


def test_a_read_needs_no_approval_but_still_needs_a_live_session(rig):
    with rig.store._write() as conn:
        conn.execute("DELETE FROM mission_approvals WHERE mission_id=?", (rig.mission.id,))
    wait = book({"type": "wait", "params": {"seconds": 1}, "binding": {}})
    assert rig.gate.authorize(rig.mission.id, wait, session_id=rig.session).resources == ()
    refused(rig, wait, "no live session", session="S-someone-else")


def test_a_non_repair_mission_may_not_run_a_mutating_step(tmp_path):
    rig = Rig(tmp_path)
    with rig.store._write() as conn:
        conn.execute("UPDATE missions SET tier='change' WHERE id=?", (rig.mission.id,))
    refused(rig, book(restart()), "may not run a mutating step")


# ── the authorization is only good for the document it was minted for ──────────────────────────────

def test_an_authorization_cannot_be_forged_or_reused_for_another_runbook(rig):
    runbook = book(restart())
    auth = rig.gate.authorize(rig.mission.id, runbook, session_id=rig.session)
    assert not auth.valid_for(book(restart(service="radarr")), rig.mission.id)
    assert not auth.valid_for(runbook, "M-999")
    forged = StepAuthorization(rig.mission.id, rig.session, auth.plan_sha256, ())
    assert not forged.valid_for(runbook, rig.mission.id)


def test_a_check_that_cannot_be_recorded_is_a_refusal(rig, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rig.ms, "append_event", broken)
    refused(rig, book(restart()), "could not be recorded")


# ── CommandService.run_mission_runbook ──────────────────────────────────────────────────────────────

@pytest.fixture
def served(tmp_path):
    from test_command_service import Env
    env = Env(tmp_path)
    ms = MissionStore(env.store)
    mission = new_mission(ms, repair_grant(scope={"stacks": ["healthy"], "hosts": ["local"]}))
    advance_to(ms, mission.id, "queued", "investigating", "awaiting_decision")
    record_approval(env.store, mission.id, "grant", mission.grant_hash, "approve", "operator:me")
    session = ms.start_session(mission.id, kind="local", token_hash=ms.hash_token(token()), session_ttl_s=600, lease_ttl_s=600)
    env.service._resolve = lambda stack, service, for_mutation=True, timeout=None, **_: actions.Target(
        stack, service, f"fixture-{service}")
    env.ms, env.mission, env.session = ms, mission, session
    env.gate = MissionStepGate(ms, stacks_root=STACKS_ROOT, identities=lambda names: {
        n: ("healthy", n.replace("fixture-", "")) for n in names})
    return env


def healthy_restart(env, service="web"):
    return book({"type": "service.restart", "params": {"stack": "healthy", "service": service},
                 "binding": env.service._binder.service(actions.Target("healthy", service, f"fixture-{service}"), timeout=4)})


def test_an_authorized_mission_step_runs_through_the_engine_without_a_card(served):
    env = served
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    assert result.outcome == "passed", result.reason
    assert env.notifier.approval_requests == [] and env.store.list_pending() == []
    execution = env.store.get_execution(result.execution_id)
    assert env.store.get_approval(execution["approval_id"])["origin"] == "mission"
    assert len(env.argv_calls) == 1
    assert env.state.mutation_owner is None                      # the lock was released


def test_a_refused_mission_step_runs_nothing_and_releases_the_lock(served):
    env = served
    env.ms.revoke_session(env.session)
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    assert result.outcome == "refused" and "no live session" in result.reason
    assert env.argv_calls == [] and env.state.mutation_owner is None
    assert [e["kind"] for e in env.store.list_events()].count("mission.step_refused") == 1


def test_a_busy_host_refuses_the_step_before_the_gate_is_asked(served):
    env = served
    assert env.state.try_begin_mutation("someone-else")
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    assert result.outcome == "refused" and "busy" in result.reason
    assert env.ms.events(env.mission.id)[-1].actor != ACTOR


def test_a_gate_that_returns_someone_elses_authorization_is_not_trusted(served):
    env = served

    class Lying(MissionStepGate):
        def authorize(self, mission_id, runbook, *, session_id):
            return StepAuthorization(mission_id, session_id, runbooks.plan_sha256(runbook), ())
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=Lying(env.ms, stacks_root=STACKS_ROOT))
    assert result.outcome == "refused" and "does not cover" in result.reason and env.argv_calls == []


def test_t24_limits_still_count_a_mission_step(served, monkeypatch):
    import config as config_module
    from config_schema import AutonomyConfig
    monkeypatch.setattr(config_module, "AUTONOMY", AutonomyConfig(cooldown_seconds=1800, max_attempts_per_day=3))
    env = served
    first = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    again = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    assert first.outcome == "passed" and again.outcome == "refused" and "cooling down" in again.reason


# ── review fixes: container identity, leases, expiry, budget, abort, record ────────────────────────

def test_a_binding_container_that_is_not_the_named_service_is_refused(rig):
    rig.identities["media-sonarr-1"] = ("vault", "vaultwarden")          # the daemon says this container is someone else's
    refused(rig, book(restart()), "container is not media/sonarr")


def test_an_unreadable_container_identity_is_not_proof(rig):
    def broken(names):
        raise RuntimeError("docker down")
    rig.gate.identities = broken
    refused(rig, book(restart()), "Compose identity cannot be read")


def test_a_check_step_is_tied_to_its_service_and_its_container(rig):
    check = {"type": "check.container", "params": {"expect": "running"}, "binding": binding("media", "sonarr")}
    assert rig.gate.authorize(rig.mission.id, book(check), session_id=rig.session).resources
    rig.identities["media-sonarr-1"] = ("media", "radarr")
    refused(rig, book(check), "container is not media/sonarr")
    protected = Rig(rig.store.path.parent / "p", protected={"resources": ["service:media/sonarr"]})
    refused(protected, book(check), "protected")                             # a read of a protected service is refused too


def test_a_step_on_a_resource_this_mission_does_not_lease_is_refused(rig):
    with rig.store._write() as conn:
        conn.execute("UPDATE mission_sessions SET fence_json='{}' WHERE id=?", (rig.session,))
    refused(rig, book(restart()), "not leased")


def test_an_expired_session_is_refused(rig):
    rig.clock.t += 601
    refused(rig, book(restart()), "expired")


def test_the_call_budget_bounds_the_steps_a_mission_may_run(tmp_path):
    rig = Rig(tmp_path, budgets={"tool_calls": 2})
    for _ in range(2):
        rig.gate.authorize(rig.mission.id, book(restart()), session_id=rig.session)
    refused(rig, book(restart()), "call budget")


def test_every_step_of_a_runbook_is_charged_to_the_call_budget(tmp_path):
    rig = Rig(tmp_path, budgets={"tool_calls": 2})
    refused(rig, book(restart(service="a"), restart(service="b"), restart(service="c")), "call budget")
    rig.gate.authorize(rig.mission.id, book(restart(service="a"), restart(service="b")), session_id=rig.session)
    refused(rig, book(restart()), "call budget")                       # two steps in one runbook used both calls


def test_a_service_only_grant_runs_its_own_service_and_nothing_beside_it(tmp_path):
    rig = Rig(tmp_path, stack_leases=True, scope={"services": ["media/sonarr"], "hosts": ["local"]})
    check = {"type": "check.container", "params": {"expect": "running"}, "binding": binding("media", "sonarr")}
    for runbook in (book(restart()), book(check)):
        assert rig.gate.authorize(rig.mission.id, runbook, session_id=rig.session).resources
    refused(rig, book(restart(service="radarr")), "outside the grant")        # its neighbour is not granted


def test_a_service_only_grant_runs_a_restart_with_its_check_in_one_runbook(tmp_path):
    rig = Rig(tmp_path, stack_leases=True, scope={"services": ["media/sonarr"], "hosts": ["local"]})
    check = {"type": "check.container", "params": {"expect": "running"}, "binding": binding("media", "sonarr")}
    assert rig.gate.authorize(rig.mission.id, book(restart(), check), session_id=rig.session).resources


def test_a_service_only_grant_does_not_accept_another_file_as_the_compose_file(tmp_path):
    rig = Rig(tmp_path, stack_leases=True, scope={"services": ["media/sonarr"], "hosts": ["local"]},
              protected={"resources": [f"path:{actions.compose_file('media')}"]})
    other = restart()
    other["binding"] = {**other["binding"], "compose_path": str(actions.compose_file("media").parent / "other.yml")}
    refused(rig, book(other), "outside the grant|protected")


def test_a_service_only_grant_still_refuses_a_protected_compose_file(tmp_path):
    rig = Rig(tmp_path, stack_leases=True, scope={"services": ["media/sonarr"], "hosts": ["local"]},
              protected={"resources": [f"path:{actions.compose_file('media')}"]})
    refused(rig, book(restart()), "protected")


def test_an_oversize_record_still_names_what_was_authorized(rig):
    names = [f"svc{n:02d}" + "x" * 50 for n in range(40)]
    many = book(*[restart(service=name) for name in names])
    for name in names:
        rig.identities[f"media-{name}-1"] = ("media", name)
    rig.gate.authorize(rig.mission.id, many, session_id=rig.session)
    (event,) = rig.events("step.authorized")
    assert event.get("truncated") and event["plan_sha256"] and event["grant_hash"] and len(event["step_types"]) == 40


def test_still_current_follows_the_missions_state(rig):
    assert rig.gate.still_current(rig.mission.id, rig.session)
    assert not rig.gate.still_current(rig.mission.id, "S-other")
    rig.ms.revoke_session(rig.session)
    assert not rig.gate.still_current(rig.mission.id, rig.session)


def test_a_cancel_during_a_run_stops_the_remaining_steps(served):
    env = served
    two = book(*[json.loads(json.dumps(step)) for step in (
        {"type": "service.restart", "params": {"stack": "healthy", "service": s},
         "binding": env.service._binder.service(actions.Target("healthy", s, f"fixture-{s}"), timeout=4)} for s in ("web", "api"))])
    original = env.service._run_argv
    seen = []

    def cancel_after_first(argv, timeout=None):
        seen.append(argv)
        env.ms.revoke_session(env.session)                       # the mission is stopped while the first step runs
        return original(argv, timeout)
    env.service._run_argv = cancel_after_first
    result = env.service.run_mission_runbook(two, mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    assert result.outcome != "passed" and len(seen) == 1


def test_the_outcome_is_written_to_the_missions_own_timeline(served):
    env = served
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    bodies = [json.loads(e.body_json) for e in env.ms.events(env.mission.id) if e.actor == ACTOR]
    assert [b["event"] for b in bodies] == ["step.authorized", "step.ran"]
    assert bodies[1]["execution"] == result.execution_id and bodies[1]["outcome"] == "passed"


def test_a_gate_that_is_not_the_real_one_is_refused(served):
    env = served

    class Permissive:
        def authorize(self, *a, **k):
            raise AssertionError("must not be asked")
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=Permissive())
    assert result.outcome == "refused" and "no mission step gate" in result.reason and env.argv_calls == []


def test_the_approval_is_recomputed_not_taken_from_the_gate(served):
    env = served
    with env.store._write() as conn:
        conn.execute("DELETE FROM mission_approvals WHERE mission_id=?", (env.mission.id,))
    result = env.service.run_mission_runbook(healthy_restart(env), mission_id=env.mission.id, session_id=env.session, gate=env.gate)
    assert result.outcome == "refused" and env.argv_calls == []


def test_concurrent_authorizations_cannot_overrun_the_call_budget(tmp_path):
    import threading
    rig = Rig(tmp_path, budgets={"tool_calls": 2})
    outcomes = []

    def go():
        try:
            rig.gate.authorize(rig.mission.id, book(restart(service="a"), restart(service="b")), session_id=rig.session)
            outcomes.append("ok")
        except StepRefused:
            outcomes.append("refused")
    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("ok") == 1 and len(rig.events("step.authorized")) == 1
