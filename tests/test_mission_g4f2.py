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
    from planet_express.missions import approvals as af
    st = stack()
    mid = st.propose()["mission_id"]
    monkeypatch.setattr(af, "PARAM_CARD_MAX", 5)                     # anything longer than 5 chars would be clipped
    card = st.flow.card(mid)
    assert card["verification"]["truncated"] is True and card["ready"] is False
    with pytest.raises(FlowError):
        st.flow.approve("operator:alice", mid, "grant", st.ms.get_mission(mid).grant_hash)
    assert not rows(st, mid)


def test_plan_view_fails_closed_and_shows_non_ascii_unescaped():
    from planet_express.missions.approvals import plan_view
    assert plan_view('["not", "a", "dict"]')[1] is True
    assert plan_view('{"checks": "x"}')[1] is True
    assert plan_view('{"checks": ["x"]}')[1] is True
    assert plan_view('{"checks": [{"id": "a", "check": "http", "params": "x"}]}')[1] is True
    checks, truncated = plan_view('{"checks": [{"id": "a", "check": "exec_probe", "params": {"argv": ["echo", "caf\\u00e9"]}}]}')
    assert not truncated and "café" in checks[0]["params"]["argv"]


def test_truncation_is_measured_after_redaction(monkeypatch):
    from planet_express.missions import approvals
    monkeypatch.setattr(approvals, "redact", lambda text: text + "-pad")      # redaction can lengthen what is shown
    monkeypatch.setattr(approvals, "PARAM_CARD_MAX", 10)
    plan = '{"checks": [{"id": "a", "check": "http", "params": {"url": "123456789"}}]}'
    assert approvals.plan_view(plan)[1] is True


def test_a_clipped_card_is_refused_by_record_approval_and_the_predicate_not_only_the_flow(stack, monkeypatch):
    from planet_express.missions import approvals as ap
    st = stack()
    mid = st.propose()["mission_id"]
    grant_hash = st.ms.get_mission(mid).grant_hash
    ap.record_approval(st.store, mid, "grant", grant_hash, "approve", "operator:me")           # fine while nothing is clipped
    assert ap.execution_predicate(st.store, mid).ok
    monkeypatch.setattr(ap, "PARAM_CARD_MAX", 5)
    verdict = ap.execution_predicate(st.store, mid)
    assert not verdict.ok and any("show an operator in full" in r for r in verdict.reasons)
    mid2 = st.propose(title="second")["mission_id"]
    with pytest.raises(ValueError, match="in full"):
        ap.record_approval(st.store, mid2, "grant", st.ms.get_mission(mid2).grant_hash, "approve", "operator:me")


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


def test_expiry_and_wall_budget_do_not_drift_over_repeated_pauses_and_a_pause_ended_outside_resume(tmp_path):
    from repair_fakes import build, update
    from tripwire_fakes import touch

    from planet_express.missions.state import transition
    st = build(tmp_path, behaviours=[{"script": [update()] * 400}])
    try:
        mid = st.propose(budgets={"wall_s": 1800})["mission_id"]
        st.approve_all(mid)
        st.sup.tick()
        sid = st.runtimes[0].context.session.session_id
        first = st.ms.get_session(sid)["expires_at"]
        run = st.sup._runs[mid]
        started = run.started_at

        def advance(seconds, step=20):
            for _ in range(seconds // step):
                st.clock.t += step
                st.sup.tick()
        advance(100)
        for n in range(2):
            touch(st.rig.fs / "etc" / "hosts", f"10.0.0.{n} changed\n")
            st.monitor.scan_once()
            assert st.status(mid) == "paused_tripwire"
            advance(200)
            if n == 0:
                st.flow.resume("operator:alice", mid)
            else:                                            # the pause ends by a plain transition, not resume()
                transition(st.store, mid, "executing", "operator:alice")
                st.clock.t += 17
                st.sup.tick()
            assert st.status(mid) == "executing"
            advance(100)
        assert run.paused_at is None and run.pause_extended_to is None
        shift = run.started_at - started
        gained = st.ms.get_session(sid)["expires_at"] - first
        assert 360 <= shift <= 440 and abs(gained - shift) <= 40       # both moved by (about) the two pauses
    finally:
        st.close()


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


def test_runner_stop_quiesces_the_tripwire_and_closes_the_supervisor_to_starts_first():
    from types import SimpleNamespace

    from planet_express.missions.runner import MissionRunner
    calls = []
    sup = SimpleNamespace(docker_proxy=None, begin_shutdown=lambda: calls.append("no_starts"),
                          shutdown=lambda: calls.append("sessions_gone"))
    trip = SimpleNamespace(quiesce=lambda: calls.append("quiesce"), stop=lambda: calls.append("tripwire_stop"))
    MissionRunner(sup, None, tripwire=trip).stop()
    assert calls == ["no_starts", "quiesce", "sessions_gone", "tripwire_stop"]


def test_a_quiesced_tripwire_does_not_freeze_a_session_that_is_being_killed(tmp_path):
    from tripwire_fakes import frozen, go_executing, make_rig, status, touch
    rig = make_rig(tmp_path)
    go_executing(rig)
    rig.monitor.quiesce()
    touch(rig.fs / "etc" / "hosts", "10.0.0.1 changed\n")
    rig.monitor.scan_once()
    assert status(rig) == "executing" and not frozen(rig)


def test_a_supervisor_that_is_shutting_down_refuses_every_start(tmp_path):
    from repair_fakes import build
    from planet_express.missions.supervisor import RepairNotReady
    st = build(tmp_path)
    try:
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        st.sup.begin_shutdown()
        with pytest.raises(RepairNotReady):
            st.sup.start(mid)
        st.sup.tick()
        assert st.status(mid) == "awaiting_decision" and st.runtimes == []
    finally:
        st.close()


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


def test_a_start_in_flight_when_shutdown_begins_launches_nothing_and_leaves_no_session(tmp_path):
    from repair_fakes import build

    from planet_express.missions.supervisor import RepairNotReady
    st = build(tmp_path)
    try:
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        real = st.sup.require_repair_preconditions

        def slow_check():                                    # the start has passed its first look at the flag...
            real()
            st.sup.begin_shutdown()                          # ...and shutdown begins before the session is registered
        st.sup.require_repair_preconditions = slow_check
        with pytest.raises(RepairNotReady):
            st.sup.start(mid)
        assert st.runtimes == [] and mid not in st.sup._runs
    finally:
        st.close()


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


def _proxy_event(rig, body, actor="docker-proxy"):
    rig.env.ms.append_event(rig.mission.id, "tool", actor, body)


def test_a_container_seen_while_a_proxy_create_is_in_flight_waits_for_its_record(tmp_path):
    from tripwire_fakes import doc, frozen, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    _proxy_event(rig, {"event": "docker.create_begin", "nonce": "n1"})
    made = doc("media-new-1", project="media", service="new")
    rig.host.containers["media-new-1"] = made
    assert rig.monitor.scan_once()[rig.mission.id] == "ok"             # the record has not landed yet: no verdict, no freeze
    assert status(rig) == "executing" and not frozen(rig)
    _proxy_event(rig, {"event": "docker.created", "created_id": made["Id"], "nonce": "n1"})
    assert rig.monitor.scan_once()[rig.mission.id] == "ok" and status(rig) == "executing"


def test_an_intent_that_never_finishes_stops_protecting_an_unknown_container(tmp_path):
    from tripwire_fakes import doc, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    _proxy_event(rig, {"event": "docker.create_begin", "nonce": "n1"})
    rig.host.containers["media-evil-1"] = doc("media-evil-1", project="media", service="evil")
    rig.env.clock.t += 500
    rig.monitor.scan_once()
    assert status(rig) == "paused_tripwire"


def test_only_the_proxys_own_records_vouch_for_a_container(tmp_path):
    from tripwire_fakes import doc, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    made = doc("media-evil-1", project="media", service="evil")
    rig.host.containers["media-evil-1"] = made
    _proxy_event(rig, {"event": "docker.created", "created_id": made["Id"]}, actor="session")
    rig.monitor.scan_once()
    assert status(rig) == "paused_tripwire"


def test_the_proxy_log_is_read_incrementally(tmp_path):
    from tripwire_fakes import go_executing, make_rig
    rig = make_rig(tmp_path)
    go_executing(rig)
    _proxy_event(rig, {"event": "docker.created", "created_id": "a" * 64, "nonce": "n"})
    first = rig.monitor._proxy_state(rig.mission.id)
    seen = rig.monitor._proxy_seen[rig.mission.id][0]
    assert first[0] == frozenset({"a" * 64}) and seen > 0
    assert rig.env.ms.proxy_creates_since(rig.mission.id, seen) == []


def test_a_container_named_like_an_excluded_one_with_spoofed_labels_is_reported_protected(tmp_path):
    from tripwire_fakes import doc, go_executing, make_rig, paused_event
    rig = make_rig(tmp_path, grant_over={"exclusions": ["container:media-vault-1"]})
    go_executing(rig)
    rig.host.containers["media-vault-1"] = doc("media-vault-1", project="media", service="innocent")
    rig.monitor.scan_once()
    assert paused_event(rig)["cause"] == "protected"


def test_another_missions_creation_does_not_vouch_for_this_one(tmp_path):
    from tripwire_fakes import doc, go_executing, make_rig, status
    rig = make_rig(tmp_path)
    go_executing(rig)
    made = doc("media-new-1", project="media", service="new")
    rig.host.containers["media-new-1"] = made
    assert made["Id"] not in rig.monitor._proxy_state(rig.mission.id)[0]
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
def test_an_enabled_config_with_a_range_overlapping_systemd_dynamic_users_is_refused(low, high):
    from config_schema import MissionsConfig
    with pytest.raises(ValueError, match="DynamicUser"):
        MissionsConfig(enabled=True, session_uid_min=low, session_uid_max=high)


def test_a_config_with_missions_off_still_loads_with_an_old_explicit_range_and_never_reads_passwd(monkeypatch):
    import pwd

    from config_schema import MissionsConfig
    monkeypatch.setattr(pwd, "getpwall", lambda: (_ for _ in ()).throw(AssertionError("passwd read at config load")))
    assert MissionsConfig(session_uid_min=61000, session_uid_max=61999).session_uid_min == 61000
    MissionsConfig()


@pytest.mark.parametrize("low,high", [(61000, 61183), (65520, 65530), (200000, 200999)])
def test_a_range_just_outside_it_is_accepted_when_enabled(low, high):
    from config_schema import MissionsConfig
    assert MissionsConfig(enabled=True, session_uid_min=low, session_uid_max=high).session_uid_min == low


def test_preflight_catches_an_account_in_the_range_and_an_unreadable_passwd():
    from types import SimpleNamespace

    from planet_express.missions.launcher.preflight import PreflightReport, check_range_free
    users = lambda: [SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="a"),      # noqa: E731
                     SimpleNamespace(pw_uid=200500, pw_gid=200500, pw_name="stray")]
    report = PreflightReport("x")
    assert not check_range_free(report, 200000, 200999, getpwall=users, getgrall=lambda: [])
    assert check_range_free(PreflightReport("x"), 300000, 300999, getpwall=users, getgrall=lambda: [])


def test_preflight_refuses_sessions_left_in_the_old_range():
    from planet_express.missions.launcher.preflight import PreflightReport, check_no_stale_uids
    report = PreflightReport("x")
    assert not check_no_stale_uids(report, 200000, 200999, [61003, 200001])
    assert "61003" in report.checks[-1].detail and "finish or cancel" in report.checks[-1].detail
    assert check_no_stale_uids(PreflightReport("x"), 200000, 200999, [200001])
    assert check_no_stale_uids(PreflightReport("x"), 200000, 200999, [])


def test_the_runner_preflight_reads_the_uids_sessions_still_hold(tmp_path):
    from missions_helpers import make_store

    from planet_express.missions.runner import held_session_uids
    store, _ = make_store(tmp_path)
    assert held_session_uids(store) == []
