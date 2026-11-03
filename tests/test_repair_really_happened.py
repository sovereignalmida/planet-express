"""rc13.1's live Prowlarr restart (M-11) completed with nothing restarted: the hook refused `docker restart` (an "ask" that a
headless session cannot answer), `docker compose` could not read the stack, the only check was "the container is running",
and the agent's own "the restart was NOT performed" decided nothing. rc13.2 closes each of those."""
import json
import time

import pytest
from repair_fakes import GOOD_RESULT, build, update
from test_console_confirm import plan_doc
from test_verify_checks import chk, inspect_json, state_env

from planet_express.console import plan as plan_mod
from planet_express.missions.brief import build_brief
from planet_express.missions.hooks import HookPolicy, classify
from planet_express.missions.models import Grant
from planet_express.missions.runtime import AgentResult
from planet_express.missions.verify.checks import run_check


# ── a restart check is bound to the run ─────────────────────────────────────────────────────────────

def fresh(env, since):
    return run_check(chk("container_state", container="app", started_during_mission=True), env.__class__(
        **{**env.__dict__, "executing_since": since}))


def test_a_container_that_did_not_restart_during_the_run_fails():
    env, _ = state_env(inspect_json(ago=3600), inspect_json(ago=3600))
    r = fresh(env, time.time() - 600)
    assert not r.ok and "not restarted during this mission" in r.detail


def test_a_container_that_restarted_during_the_run_passes():
    env, _ = state_env(inspect_json(ago=60), inspect_json(ago=60))
    assert fresh(env, time.time() - 600).ok


def test_without_a_known_start_of_the_run_it_fails_closed():
    env, _ = state_env(inspect_json(ago=60), inspect_json(ago=60))
    assert not fresh(env, None).ok


def test_an_ordinary_running_check_is_unchanged():
    env, _ = state_env(inspect_json(ago=3600), inspect_json(ago=3600))
    assert run_check(chk("container_state", container="app"), env).ok


# ── a restart-only plan carries that check ──────────────────────────────────────────────────────────

def restart_doc(checks):
    doc = plan_doc()
    doc["options"][0]["restart_only"] = True
    doc["options"][0]["checks"] = checks
    return doc


def test_a_restart_only_plan_needs_a_container_check_and_gets_the_fresh_start_bound():
    with pytest.raises(plan_mod.PlanError, match="needs a container_state check"):
        plan_mod.parse(restart_doc([{"id": "web", "check": "http", "params": {"url": "http://localhost/"}}]))
    plan = plan_mod.parse(restart_doc([{"id": "up", "check": "container_state", "params": {"container": "CASA_PROWLARR"}},
                                       {"id": "web", "check": "http", "params": {"url": "http://localhost/"}}]))
    up, web = plan.options[0].checks
    assert up.params["started_during_mission"] is True and "started_during_mission" not in web.params
    assert plan_mod.check_phrase(up) == "CASA_PROWLARR is running and stable and was restarted by this run"


def test_an_agent_cannot_switch_the_bound_off():
    plan = plan_mod.parse(restart_doc([{"id": "up", "check": "container_state",
                                        "params": {"container": "c", "started_during_mission": False}}]))
    assert plan.options[0].checks[0].params["started_during_mission"] is True


def test_the_planner_is_told_to_restart_by_name():
    assert "docker restart <container>" in plan_mod.PLAN_INSTRUCTIONS and "docker compose restart" not in plan_mod.PLAN_INSTRUCTIONS


# ── the hook lets the approved restart through ──────────────────────────────────────────────────────

LIFECYCLE = HookPolicy(docker_lifecycle=True)


@pytest.mark.parametrize("command", ["docker restart CASA_PROWLARR", "docker container restart CASA_PROWLARR",
                                     "docker stop -t 30 app", "docker start app && docker ps", "docker restart --time=20 a b"])
def test_under_a_restart_only_grant_lifecycle_calls_are_not_asked(command):
    assert classify("Bash", {"command": command}, LIFECYCLE).verdict == "allow"


@pytest.mark.parametrize("command", ["docker kill app", "docker rm -f app", "docker compose restart app",
                                     "docker restart $(docker ps -q)", "podman restart app", "docker update --restart=no app"])
def test_everything_else_irreversible_is_still_asked_or_blocked(command):
    assert classify("Bash", {"command": command}, LIFECYCLE).verdict in ("ask", "block")


def test_without_a_restart_only_grant_a_restart_is_still_asked():
    assert classify("Bash", {"command": "docker restart app"}, HookPolicy()).verdict == "ask"


def test_the_flag_survives_the_trip_through_the_environment():
    assert HookPolicy.from_json(LIFECYCLE.to_json()).docker_lifecycle is True
    assert HookPolicy.from_json(json.dumps({"live_trees": []})).docker_lifecycle is False
    with pytest.raises(ValueError):
        HookPolicy.from_json(json.dumps({"docker_lifecycle": "yes"}))


def test_the_runner_builds_it_from_the_missions_grant():
    from types import SimpleNamespace

    from planet_express.missions.runner import _hook_policy
    cfg = SimpleNamespace(live_trees=(), hooks=None)
    for docker, want in (("lifecycle", True), ("proxy", False)):
        grant = Grant.model_validate({"tier": "repair", "docker": docker})
        assert _hook_policy(cfg, SimpleNamespace(grant=lambda g=grant: g)).docker_lifecycle is want


# ── the brief says how ──────────────────────────────────────────────────────────────────────────────

def test_a_restart_only_brief_says_to_restart_by_name_and_asks_for_work_done(tmp_path):
    from test_supervisor_brief import mission
    m = mission(tmp_path)
    m = m.model_copy(update={"grant_json": Grant.model_validate(
        {**json.loads(m.grant_json), "docker": "lifecycle", "sudo": {"mode": "scoped"}}).canonical()})
    text = build_brief(m, [], []).text
    assert "use `docker restart <container name>`" in text and "work_done" in text


# ── the agent's own "not done" stops a pass ─────────────────────────────────────────────────────────

@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


@pytest.mark.parametrize("done, final", [("none", "needs_operator"), ("some", "needs_operator"), ("all", "completed"),
                                          (None, "completed")])
def test_the_agents_word_that_it_did_not_finish_stops_a_pass(stack, done, final):
    result = dict(GOOD_RESULT, **({"work_done": done} if done else {}))
    st = stack(behaviours=[{"script": [update("finished")], "result": result}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    assert st.drive(mid) == final
    stored = st.ms.get_mission(mid)
    if final == "needs_operator":
        assert "the repair agent reported that" in json.loads(stored.verification_result_json)["reason"]
        assert stored.outcome_flags == "partially_applied"


def test_only_the_supervisors_note_counts(stack):
    st = stack(behaviours=[{"script": [update("finished")], "result": GOOD_RESULT}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    original = st.sup._settle_repair

    def forged(mission, reason, result):
        # a worker-authored note landing between the executing transition and the verification
        st.ms.append_event(mission.id, "note", "worker", {"agent_reported": "none"})
        return original(mission, reason, result)
    st.sup._settle_repair = forged
    assert st.drive(mid) == "completed"


def test_the_result_schema_allows_it_and_rejects_anything_else():
    assert AgentResult(**GOOD_RESULT, work_done="some").work_done == "some"
    with pytest.raises(ValueError):
        AgentResult(**GOOD_RESULT, work_done="mostly")


# ── review fixes ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", ["docker stop --signal KILL app", "docker stop -s KILL app", "docker restart -t 0 app",
                                     "docker restart --time 5 app", "docker restart --time=abc app", "docker restart -t", "docker restart"])
def test_a_kill_signal_or_short_grace_is_still_asked(command):
    assert classify("Bash", {"command": command}, LIFECYCLE).verdict in ("ask", "block")


def test_the_hooks_grace_floor_is_the_proxys():
    from planet_express.missions import hooks
    from planet_express.missions.dockerproxy import policy
    assert hooks._LIFECYCLE_MIN_GRACE_S == policy.LIFECYCLE_MIN_GRACE_S


def test_a_restart_in_the_first_second_of_the_run_counts():
    """Codex, rc13.2: both times are whole seconds, so the same second must count as during the run."""
    env, _ = state_env(inspect_json(ago=60), inspect_json(ago=60))
    from test_verify_checks import started
    from planet_express.missions.verify.checks import _started_epoch
    assert fresh(env, float(_started_epoch(started(60)))).ok


def test_a_tripwire_resume_does_not_move_the_start_of_the_run(stack):
    st = stack(behaviours=[{"script": [update("finished")], "result": GOOD_RESULT}])
    mid = st.propose()["mission_id"]
    for to, frm, at in (("executing", "awaiting_decision", 100), ("executing", "paused_tripwire", 900)):
        with st.store._write() as conn:
            conn.execute("INSERT INTO mission_events (mission_id, at, kind, actor, body_json) VALUES (?, ?, 'state', 'supervisor', ?)",
                         (mid, at, json.dumps({"from": frm, "to": to})))
    since, _seq = st.verifier._executing_events(st.ms.get_mission(mid))
    assert since == 100.0


def test_a_restart_only_plan_stored_before_rc13_2_is_refused_not_run(tmp_path):
    from test_console_confirm import Rig

    from planet_express.console.api import ConsoleApiError
    rig = Rig(tmp_path)
    try:
        doc = restart_doc([{"id": "up", "check": "container_state", "params": {"container": "c"}}])
        stored = plan_mod.Plan.model_validate(doc)                 # as stored before: no bound on the check
        assert plan_mod.restart_unbound(stored.options[0])
        plan_id = rig.console.add_plan(rig.conv.id, 1, plan_mod.canonical(stored), plan_mod.plan_hash(stored))
        with pytest.raises(ConsoleApiError, match="made before an update"):
            rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    finally:
        rig.close()


# ── PE's own bound: every in-scope service of a restart-only grant restarted ───────────────────────

class Provider:
    def __init__(self, project="media"):
        self.project = project

    def project_name(self, stack):
        return self.project


def lifecycle_env(ids, docs, *, provider=Provider(), since=None):
    from verify_fakes import FakeRun, check_env
    it = iter(docs)
    run = FakeRun([(["docker", "ps"], (0, "\n".join(ids), "")), (["docker", "inspect"], lambda argv: next(it))])
    return check_env(run, provider=provider, executing_since=since if since is not None else time.time() - 600), run


def test_every_container_of_the_service_must_have_restarted():
    from planet_express.missions.verify.checks import check_restarted
    env, run = lifecycle_env(["a", "b"], [inspect_json(ago=60), inspect_json(ago=3600)])
    got = check_restarted("media", "prowlarr", env)
    assert not got.ok and "not restarted" in got.detail
    assert run.calls[0][-4:] == ["--filter", "label=com.docker.compose.project=media",
                                 "--filter", "label=com.docker.compose.service=prowlarr"]
    env, _ = lifecycle_env(["a", "b"], [inspect_json(ago=60), inspect_json(ago=30)])
    assert check_restarted("media", "prowlarr", env).ok


@pytest.mark.parametrize("ids, provider, since, why", [
    ([], Provider(), None, "no containers"), (["a"], None, None, "project is unknown"),
    (["a"], Provider(project=None), None, "project is unknown"), (["a"], Provider(), "unset", "start of this run")])
def test_the_restart_bound_fails_closed(ids, provider, since, why):
    from planet_express.missions.verify.checks import check_restarted
    env, _ = lifecycle_env(ids, [inspect_json(ago=60)], provider=provider)
    if since == "unset":
        env = env.__class__(**{**env.__dict__, "executing_since": None})
    got = check_restarted("media", "prowlarr", env)
    assert not got.ok and why in got.detail


def test_a_stopped_container_fails_the_bound():
    from planet_express.missions.verify.checks import check_restarted
    env, _ = lifecycle_env(["a"], [inspect_json(status="exited", ago=60)])
    assert not check_restarted("media", None, env).ok


def lifecycle_stack(stack, *, restarted):
    st = stack(behaviours=[{"script": [update("finished")], "result": dict(GOOD_RESULT, work_done="all")}])
    st.verifier.env = st.verifier.env.__class__(**{**st.verifier.env.__dict__, "provider": Provider()})
    st.run_log.on(["docker", "ps"], (0, "cid1\n", ""))
    def inspect(argv):
        from datetime import datetime, timezone
        # relative to the rig's clock (the run's start is a fake-clock time): just after it, or long before it
        at = st.clock.t + 5 if restarted else st.clock.t - 86_400
        code, out, err = inspect_json()
        doc = json.loads(out)
        doc[0]["State"]["StartedAt"] = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.5Z")
        return code, json.dumps(doc), err
    st.run_log.on(["docker", "inspect"], inspect)
    mid = st.flow.propose_repair("operator:me", title="restart prowlarr", goal="restart it", docker="lifecycle",
                                 scope={"stacks": ["media"], "hosts": ["local"]},
                                 checks=[{"id": "web", "check": "http", "params": {"url": "http://localhost/"}}])["mission_id"]
    st.approve_all(mid)
    return st, mid


def test_a_restart_only_mission_whose_service_did_not_restart_does_not_complete(stack):
    """M-11 again, even with a plan that only checks HTTP and an agent that says it did everything."""
    st, mid = lifecycle_stack(stack, restarted=False)
    assert st.drive(mid) == "needs_operator"
    checks = json.loads(st.ms.get_mission(mid).verification_result_json)["checks"]
    assert [(c["id"], c["ok"]) for c in checks if c["check"] == "restarted"] == [("restarted:media", False)]


def test_a_restart_only_mission_whose_service_restarted_completes(stack):
    st, mid = lifecycle_stack(stack, restarted=True)
    assert st.drive(mid) == "completed"



@pytest.mark.parametrize("scope, want", [
    ({"stacks": ["media"], "services": ["media/prowlarr"]}, [("media", "prowlarr")]),
    ({"stacks": ["media", "network"], "services": ["media/prowlarr"]}, [("media", "prowlarr"), ("network", None)]),
    ({"stacks": ["media"]}, [("media", None)]),
    ({"services": ["media/a", "media/b"]}, [("media", "a"), ("media", "b")])])
def test_what_a_restart_only_grant_must_have_restarted(scope, want):
    """Codex, rc13.2: a stack named without any of its services must not be dropped."""
    from planet_express.missions.verify.runner import restart_targets
    grant = Grant.model_validate({"tier": "repair", "docker": "lifecycle", "scope": {**scope, "hosts": ["local"]}})
    assert restart_targets(grant) == want
