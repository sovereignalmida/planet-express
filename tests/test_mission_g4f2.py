"""G4-F2 safety fixes (from the G4 /code-review): each test failed before its fix."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build

from planet_express.missions import approvals
from planet_express.missions.approval_flow import FlowError
from planet_express.missions.models import Grant


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, start_monitor=False, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def rows(st, mid):
    with sqlite3.connect(st.store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM mission_approvals WHERE mission_id=?", (mid,))]


# ── #1 the card shows the whole plan the hash binds ───────────────────────────────────────────────────

def test_long_http_url_appears_complete_on_the_card(stack):
    st = stack()
    url = "http://localhost/" + "a" * 300 + "/TAIL-OF-THE-URL"
    mid = st.propose(checks=[{"id": "default", "check": "http", "params": {"url": url}}])["mission_id"]
    card = st.flow.card(mid)
    assert card["verification"]["checks"][0]["params"]["url"] == url
    assert card["verification"]["truncated"] is False


def test_long_exec_argv_appears_complete_on_the_card(stack):
    st = stack()
    argv = [f"arg{i}-" + "x" * 200 for i in range(6)]
    chk = {"id": "default", "check": "exec_probe", "params": {"container": "media-app", "argv": argv}}
    mid = st.propose(checks=[chk])["mission_id"]
    shown = st.flow.card(mid)["verification"]["checks"][0]["params"]["argv"]
    assert all(a in shown for a in argv)


def test_a_card_that_would_clip_cannot_be_approved(stack, monkeypatch):
    from planet_express.missions import approval_flow as af
    st = stack()
    mid = st.propose()["mission_id"]
    monkeypatch.setattr(af, "PARAM_CARD_MAX", 5)                     # anything longer than 5 chars would be clipped
    card = st.flow.card(mid)
    assert card["verification"]["truncated"] is True and card["ready"] is False
    with pytest.raises(FlowError):
        st.flow.approve("operator:alice", mid, "grant", st.ms.get_mission(mid).grant_hash)
    assert not rows(st, mid)


# ── #2 a critical mission's grant row must be elevated, in the predicate itself ───────────────────────

def test_a_non_elevated_grant_row_does_not_satisfy_a_critical_mission(stack):
    from missions_helpers import new_mission
    st = stack()
    m = new_mission(st.ms, Grant.model_validate({"tier": "critical", "scope": {"stacks": ["media"]}}))
    with sqlite3.connect(st.store.path) as conn:
        conn.execute("INSERT INTO mission_approvals (id, mission_id, phase, bound_hash, decision, operator, elevated, decided_at) "
                     "VALUES ('A-x', ?, 'grant', ?, 'approve', 'operator:me', 0, 1)", (m.id, m.grant_hash))
    verdict = approvals.execution_predicate(st.store, m.id)
    assert not verdict.ok
    assert any("elevated" in r and "grant" in r for r in verdict.reasons)


# ── #3 exclusions that name a resource are enforced like protected, at every layer ───────────────────

EXCLUDED = "service:media/qbit"


def test_a_resource_exclusion_is_enforced_and_a_tag_is_advisory():
    g = Grant.model_validate({"tier": "repair", "exclusions": [EXCLUDED, "network"]})
    assert g.enforced_exclusions() == ("host:local/service:media/qbit",)
    assert g.advisory_exclusions() == ("network",)


def test_the_card_says_which_exclusions_are_hard(stack):
    st = stack()
    mid = st.propose(exclusions=[EXCLUDED, "network"])["mission_id"]
    prot = st.flow.card(mid)["protected"]
    assert prot["exclusions_enforced"] == ["host:local/service:media/qbit"] and prot["exclusions_advisory"] == ["network"]


def test_tripwire_classifies_an_excluded_resource_as_protected(tmp_path):
    from tripwire_fakes import make_rig
    rig = make_rig(tmp_path, grant_over={"exclusions": [EXCLUDED]})
    ctx = rig.monitor._ctx(rig.mission.grant())
    assert ctx.cls("host:local/service:media/qbit") == "protected"
    assert ctx.cls("host:local/service:media/other") == "in_scope"


def test_the_proxy_scope_treats_an_excluded_resource_as_protected():
    from planet_express.missions.dockerproxy.policy import Scope
    g = Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"]}, "exclusions": [EXCLUDED]})
    scope = Scope.from_grant(g, stacks_root="/srv/stacks")
    assert scope.classify("host:local/service:media/qbit") == "protected"
    assert scope.classify("host:local/service:media/other") == "in_scope"


# ── #6 stopping the runner: no new starts, sessions proven gone, only then the tripwire ──────────────

def test_runner_stop_halts_the_tripwire_only_after_the_sessions_are_gone():
    from types import SimpleNamespace

    from planet_express.missions.runner import MissionRunner
    calls = []
    sup = SimpleNamespace(docker_proxy=None, shutdown=lambda: calls.append("sessions_gone"))
    trip = SimpleNamespace(stop=lambda: calls.append("tripwire_stop"))
    runner = MissionRunner(sup, None, tripwire=trip)
    runner._stop.set = lambda real=runner._stop.set: (calls.append("no_new_starts"), real())[1]
    runner.stop()
    assert calls == ["no_new_starts", "sessions_gone", "tripwire_stop"]


def test_runner_stop_still_stops_the_tripwire_if_the_supervisor_shutdown_fails():
    from types import SimpleNamespace

    from planet_express.missions.runner import MissionRunner
    calls = []

    def boom():
        raise RuntimeError("x")
    runner = MissionRunner(SimpleNamespace(docker_proxy=None, shutdown=boom), None,
                           tripwire=SimpleNamespace(stop=lambda: calls.append("tripwire_stop")))
    with pytest.raises(RuntimeError):
        runner.stop()
    assert calls == ["tripwire_stop"]


# ── #7 a tripwire pause is not session time: the session's expiry moves with the wall budget ──────────

def test_a_session_still_resolves_after_a_long_tripwire_pause_is_resumed(tmp_path):
    from repair_fakes import build, update
    from tripwire_fakes import touch
    st = build(tmp_path, behaviours=[{"script": [update()] * 400}])
    try:
        mid = st.propose(budgets={"wall_s": 1800})["mission_id"]
        st.approve_all(mid)
        st.sup.tick()
        session_id = st.runtimes[0].context.session.session_id
        first = st.ms.get_session(session_id)["expires_at"]

        def advance(seconds, step=20):
            for _ in range(seconds // step):
                st.clock.t += step
                st.sup.tick()
        advance(1500)                                                # most of the wall budget is used before the pause
        touch(st.rig.fs / "etc" / "hosts", "10.0.0.1 changed\n")
        st.monitor.scan_once()
        assert st.status(mid) == "paused_tripwire"
        advance(900)                                                 # 15 minutes paused: past wall_s + pad in real time
        assert st.clock.t > first
        st.flow.resume("operator:alice", mid)
        assert st.status(mid) == "executing"
        row = st.ms.get_session(session_id)
        assert row["expires_at"] > st.clock.t and row["revoked_at"] is None
        assert row["expires_at"] >= first + 850                      # extended by the paused duration
        st.sup.tick()
        assert st.status(mid) == "executing" and st.ms.live_session(mid) is not None
    finally:
        st.close()


# ── review round: exclusions (#5 hosts, #6 carve-out only, #13 one helper) ──────────────────────────

def test_a_bare_exclusion_is_enforced_on_every_host_in_scope():
    g = Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["nas", "local"]},
                              "exclusions": ["service:media/qbit"]})
    assert g.enforced_exclusions() == ("host:nas/service:media/qbit", "host:local/service:media/qbit")


def test_proxy_scope_on_a_non_local_host_enforces_a_bare_exclusion():
    from planet_express.missions.dockerproxy.policy import Scope
    g = Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["nas"]},
                              "exclusions": ["service:media/qbit"]})
    assert Scope.from_grant(g, stacks_root="/srv/stacks").classify("host:nas/service:media/qbit") == "protected"


def test_an_excluded_child_does_not_block_its_in_scope_parent():
    from planet_express.missions.dockerproxy.policy import Scope
    g = Grant.model_validate({"tier": "repair", "scope": {"paths": ["/srv/media"]}, "exclusions": ["path:/srv/media/db"]})
    scope = Scope.from_grant(g, stacks_root="/srv/stacks")
    assert scope.classify("host:local/path:/srv/media") == "in_scope"
    assert scope.classify("host:local/path:/srv/media/other") == "in_scope"
    assert scope.classify("host:local/path:/srv/media/db") == "protected"
    assert scope.classify("host:local/path:/srv/media/db/x") == "protected"
    assert scope.classify_path("host:local/path:/srv/media/db/x") == "protected"


def test_every_consumer_gets_protection_from_the_one_helper():
    """The invariant is structural: the three consumers call Grant.effective_protection and none reads the raw fields."""
    import inspect

    from planet_express.missions import tools
    from planet_express.missions.dockerproxy import policy
    from planet_express.missions.tripwire import monitor
    for module in (tools, policy, monitor):
        src = inspect.getsource(module)
        assert "effective_protection(" in src, module.__name__
        assert "grant.protected.resources" not in src and "grant.exclusions" not in src, module.__name__


# ── #4 a container's own labels are not evidence of scope ────────────────────────────────────────────

def test_a_spoofed_in_scope_label_on_a_new_container_pauses_and_a_proxy_created_one_does_not(tmp_path):
    from tripwire_fakes import doc, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    rig.host.containers["media-evil-1"] = doc("media-evil-1", project="media", service="evil")      # labelled in scope
    assert rig.monitor.scan_once()[rig.mission.id] == "out_of_scope"
    assert status(rig) == "paused_tripwire"


def test_a_container_the_proxy_recorded_as_created_by_this_mission_is_allowed(tmp_path):
    from tripwire_fakes import doc, frozen, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    made = doc("media-new-1", project="media", service="new")
    rig.host.containers["media-new-1"] = made
    rig.env.ms.append_event(rig.mission.id, "tool", "docker-proxy", {"event": "docker.created", "created_id": made["Id"]})
    assert rig.monitor.scan_once()[rig.mission.id] == "ok"
    assert status(rig) == "executing" and not frozen(rig)


def test_another_missions_creation_does_not_vouch_for_this_one(tmp_path):
    from tripwire_fakes import doc, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    made = doc("media-new-1", project="media", service="new")
    rig.host.containers["media-new-1"] = made
    assert made["Id"] not in rig.env.ms.proxy_created_containers(rig.mission.id)
    rig.monitor.scan_once()
    assert status(rig) == "paused_tripwire"


def test_the_tool_broker_refuses_an_excluded_resource(tmp_path):
    from test_mission_tools import setup

    from planet_express.integrations.rpc import RpcError
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, _, broker, sess, *_ = setup(tmp_path / "a", creds=("tool:app.health_get",))
    assert broker.invoke(sess, "app.health_get", {"service": "media/qbit"})["ok"]          # allowed without the exclusion
    ms, m, broker, sess, http, runner = setup(tmp_path / "b", creds=("tool:app.health_get",), exclusions=[EXCLUDED])
    with pytest.raises(RpcError):
        broker.invoke(sess, "app.health_get", {"service": "media/qbit"})


# ── #8 the default session uid range is clear of systemd's DynamicUser range ─────────────────────────

def test_the_schema_default_is_the_launchers_one_default_range():
    from config_schema import MissionsConfig

    from planet_express.missions.launcher.config import DEFAULT_UID_RANGE
    cfg = MissionsConfig()
    assert (cfg.session_uid_min, cfg.session_uid_max) == DEFAULT_UID_RANGE


@pytest.mark.parametrize("low,high", [(61000, 61999), (61184, 61300), (65000, 65600), (60000, 70000), (65519, 65530), (61100, 61184)])
def test_a_range_overlapping_systemd_dynamic_users_is_refused(low, high):
    from config_schema import MissionsConfig
    with pytest.raises(ValueError, match="DynamicUser"):
        MissionsConfig(session_uid_min=low, session_uid_max=high)


@pytest.mark.parametrize("low,high", [(61000, 61183), (65520, 65530), (200000, 200999)])
def test_a_range_just_outside_it_is_accepted(low, high):
    from config_schema import MissionsConfig
    assert MissionsConfig(session_uid_min=low, session_uid_max=high).session_uid_min == low


def test_a_range_containing_an_existing_account_is_refused():
    from types import SimpleNamespace

    from config_schema import uid_range_problem
    users = lambda: [SimpleNamespace(pw_uid=1000), SimpleNamespace(pw_uid=200500)]      # noqa: E731
    assert "200500" in uid_range_problem(200000, 200999, getpwall=users)
    assert uid_range_problem(300000, 300999, getpwall=users) is None
