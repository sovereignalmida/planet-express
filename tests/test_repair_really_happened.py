"""rc13.1's live Prowlarr restart (M-11) completed with nothing restarted: the hook refused `docker restart` (an "ask" that a
headless session cannot answer), `docker compose` could not read the stack, the only check was "the container is running",
and the agent's own "the restart was NOT performed" decided nothing. rc13.2 closes each of those. Trust mode removed the
restart-only grant; a plan may still ask for the restart check (`started_during_mission`), and the agent's "not done" still stops a pass."""
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


# ── a plan may carry that check ─────────────────────────────────────────────────────────────────────

def test_a_plan_can_ask_that_the_container_really_restarted_and_the_card_says_so():
    doc = plan_doc()
    doc["options"][0]["checks"] = [{"id": "up", "check": "container_state",
                                    "params": {"container": "CASA_PROWLARR", "started_during_mission": True}}]
    up = plan_mod.parse(doc).options[0].checks[0]
    assert plan_mod.check_phrase(up) == "CASA_PROWLARR is running and stable and was restarted by this run"


def test_the_planner_is_told_to_add_the_restart_check():
    text = plan_mod.instructions()
    assert "started_during_mission: true" in text and "container_state" in text


# ── the hook lets the restart through (trust mode: there is no ask, for any grant) ────────────────────

@pytest.mark.parametrize("command", ["docker restart CASA_PROWLARR", "docker container restart CASA_PROWLARR",
                                     "docker stop -t 30 app", "docker start app && docker ps", "docker restart --time=20 a b",
                                     "docker kill app", "docker rm -f app", "docker compose restart app", "docker restart -t 0 app",
                                     "docker restart $(docker ps -q)", "sudo docker restart app", "docker update --restart=no app"])
def test_restarts_and_other_container_lifecycle_calls_are_allowed_whatever_the_grant(command):
    assert classify("Bash", {"command": command}, HookPolicy()).verdict == "allow"


def test_an_old_policy_with_the_lifecycle_flag_still_loads_and_the_runner_policy_does_not_depend_on_the_grant(tmp_path):
    from types import SimpleNamespace

    from planet_express.missions.runner import _hook_policy
    assert HookPolicy.from_json(json.dumps({"docker_lifecycle": True, "live_trees": []})) == HookPolicy()
    cfg = SimpleNamespace(hooks=None)
    store = SimpleNamespace(path=tmp_path / "casa.db")
    policy = _hook_policy(cfg, store, "/srv/stacks")
    assert policy.stacks_root == "/srv/stacks" and str(tmp_path / "casa.db") in policy.pe_paths


# ── the brief says how ──────────────────────────────────────────────────────────────────────────────

def test_a_repair_brief_says_it_has_full_access_and_how_to_restart_and_asks_for_work_done(tmp_path):
    from test_supervisor_brief import mission
    m = mission(tmp_path)
    m = m.model_copy(update={"tier": "repair", "grant_json": Grant.model_validate(
        {**json.loads(m.grant_json), "tier": "repair"}).canonical()})
    text = build_brief(m, [], []).text
    assert "full access to this host" in text and "`docker restart <container>`" in text and "work_done" in text
    assert "protected" not in text.lower()


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


# Nothing on the host changed in these runs, so a run the agent says it did not finish closes `failed` (stage 5) rather than waiting.
@pytest.mark.parametrize("done, final", [("none", "failed"), ("some", "failed"), ("all", "completed"),
                                          (None, "completed")])
def test_the_agents_word_that_it_did_not_finish_stops_a_pass(stack, done, final):
    result = dict(GOOD_RESULT, **({"work_done": done} if done else {}))
    st = stack(behaviours=[{"script": [update("finished")], "result": result}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    assert st.drive(mid) == final
    stored = st.ms.get_mission(mid)
    if final == "failed":
        assert "the repair agent reported that" in json.loads(stored.verification_result_json)["reason"]


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

def test_a_restart_in_the_first_second_of_the_run_counts():
    """Codex, rc13.2: both times are whole seconds, so the same second must count as during the run."""
    env, _ = state_env(inspect_json(ago=60), inspect_json(ago=60))
    from test_verify_checks import started
    from planet_express.missions.verify.checks import _started_epoch
    assert fresh(env, float(_started_epoch(started(60)))).ok


def test_a_re_entry_into_executing_from_elsewhere_does_not_move_the_start_of_the_run(stack):
    st = stack(behaviours=[{"script": [update("finished")], "result": GOOD_RESULT}])
    mid = st.propose()["mission_id"]
    for to, frm, at in (("executing", "awaiting_decision", 100), ("executing", "needs_operator", 900)):
        with st.store._write() as conn:
            conn.execute("INSERT INTO mission_events (mission_id, at, kind, actor, body_json) VALUES (?, ?, 'state', 'supervisor', ?)",
                         (mid, at, json.dumps({"from": frm, "to": to})))
    since, _seq = st.verifier._executing_events(st.ms.get_mission(mid))
    assert since == 100.0


# ── check_restarted stays available: every container of a service restarted ───────────────────────

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
