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
        st = build(tmp_path, **kw)
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
    from dataclasses import replace
    st.verifier.env = replace(st.verifier.env, exec_allowlist=frozenset({argv[0]}))     # exec_probe is offered only where it can run
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


# ── #6 stopping the runner: no new starts, then the sessions proven gone ─────────────────────────────

def test_runner_stop_closes_the_supervisor_to_starts_before_the_sessions_are_killed():
    from types import SimpleNamespace

    from planet_express.missions.runner import MissionRunner
    calls = []
    sup = SimpleNamespace(docker_proxy=None, begin_shutdown=lambda: calls.append("no_starts"),
                          shutdown=lambda: calls.append("sessions_gone"))
    MissionRunner(sup, None).stop()
    assert calls == ["no_starts", "sessions_gone"]


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


def test_a_start_in_flight_when_shutdown_begins_launches_nothing_and_leaves_no_session(tmp_path):
    from repair_fakes import build

    from planet_express.missions.supervisor import RepairNotReady
    st = build(tmp_path)
    try:
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        real = st.sup._acquire_early

        def slow_check(mission):                             # the start has passed its first look at the flag...
            out = real(mission)
            st.sup.begin_shutdown()                          # ...and shutdown begins before the session is registered
            return out
        st.sup._acquire_early = slow_check
        with pytest.raises(RepairNotReady):
            st.sup.start(mid)
        assert st.runtimes == [] and mid not in st.sup._runs
    finally:
        st.close()


def test_begin_shutdown_waits_for_a_start_that_is_already_launching(tmp_path):
    import threading

    from repair_fakes import build
    st = build(tmp_path)
    try:
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        inside, release = threading.Event(), threading.Event()
        real = st.sup._acquire_early

        def slow_check(mission):
            out = real(mission)
            inside.set()
            assert release.wait(10)
            return out
        st.sup._acquire_early = slow_check
        starter = threading.Thread(target=st.sup.start, args=(mid,))
        starter.start()
        assert inside.wait(10)
        done = threading.Event()
        stopper = threading.Thread(target=lambda: (st.sup.begin_shutdown(), done.set()))
        stopper.start()
        assert not done.wait(0.5)                    # the quiesce/kill sequence cannot begin under a start in flight
        release.set()
        starter.join(10)
        assert done.wait(10) and mid in st.sup._runs  # the run is registered, so shutdown() will kill it
        stopper.join(10)
    finally:
        st.close()


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

    from planet_express.missions.launcher.preflight import (
        PreflightReport,
        check_range_free,
    )
    users = lambda: [SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="a"),      # noqa: E731
                     SimpleNamespace(pw_uid=200500, pw_gid=200500, pw_name="stray")]
    report = PreflightReport("x")
    assert not check_range_free(report, 200000, 200999, getpwall=users, getgrall=lambda: [])
    assert check_range_free(PreflightReport("x"), 300000, 300999, getpwall=users, getgrall=lambda: [])


def test_preflight_refuses_sessions_left_in_the_old_range():
    from planet_express.missions.launcher.preflight import (
        PreflightReport,
        check_no_stale_uids,
    )
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
