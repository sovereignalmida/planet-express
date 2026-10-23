"""G4-F3 functional and robustness fixes (rows 11-21 of the G4 review): each test failed before its fix."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build, update
from tripwire_fakes import touch

from planet_express.missions import leases
from planet_express.missions import supervisor as sup_mod
from planet_express.missions.store import NoFreeUid


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        kw.setdefault("start_monitor", False)
        st = build(tmp_path, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def notes(st, mid):
    return [json.loads(e.body_json) for e in st.ms.events(mid)]


def lease_count(st):
    with st.store._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM mission_leases").fetchone()[0]


def last_heartbeat(st, mission_id):
    with st.store._connect() as conn:
        row = conn.execute("SELECT last_heartbeat FROM mission_leases WHERE mission_id=?", (mission_id,)).fetchone()
    return None if row is None else row[0]


# ── #11 a start that must wait for a lease or a uid is a wait, not a failed start ─────────────────────

def test_a_lease_conflict_waits_and_spends_no_checkpoint(stack):
    st = stack()
    st.rig.monitor.start()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    other = st.propose()["mission_id"]
    leases.acquire(st.store, ["host:local/stack:media"], other, 3600)
    for _ in range(sup_mod.START_ATTEMPTS + 3):
        st.clock.t += sup_mod.START_DEFER_RETRY_S + 1
        st.sup.tick()
    mission = st.ms.get_mission(mid)
    assert mission.status == "awaiting_decision" and mission.checkpoint_id is None      # no checkpoint was spent
    assert [b for b in notes(st, mid) if "start_waiting" in b]
    assert not [b for b in notes(st, mid) if "start_failed" in b]
    leases.release(st.store, other)
    st.clock.t += sup_mod.START_DEFER_RETRY_S + 1
    assert st.drive(mid) == "completed"


def test_a_lease_wait_is_bounded_and_then_goes_to_the_operator(stack):
    st = stack()
    st.rig.monitor.start()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    other = st.propose()["mission_id"]
    leases.acquire(st.store, ["host:local/stack:media"], other, 10**9)
    st.sup.tick()
    st.clock.t += sup_mod.START_DEFER_MAX_S + 1
    st.sup.tick()
    assert st.status(mid) == "needs_operator"
    assert [b for b in notes(st, mid) if "start_wait_expired" in b]


def test_a_failed_start_after_the_early_lease_gives_the_lease_back(stack):
    st = stack()
    st.rig.monitor.start()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.env.svc.baseline_provider = lambda: (_ for _ in ()).throw(RuntimeError("scan failed"))
    st.sup.tick()
    assert st.status(mid) == "awaiting_decision"
    assert lease_count(st) == 0


def test_no_free_uid_is_a_wait_not_a_failed_start(stack, monkeypatch):
    st = stack()
    st.rig.monitor.start()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)

    def full(*a, **kw):
        raise NoFreeUid("all session uids are held")
    monkeypatch.setattr(st.sup.ms, "start_session", full)
    for _ in range(sup_mod.START_ATTEMPTS + 2):
        st.clock.t += sup_mod.START_DEFER_RETRY_S + 1
        st.sup.tick()
    assert st.status(mid) == "awaiting_decision"
    assert not [b for b in notes(st, mid) if "start_failed" in b]
    assert [b for b in notes(st, mid) if "start_waiting" in b]
    monkeypatch.undo()
    st.clock.t += sup_mod.START_DEFER_RETRY_S + 1
    assert st.drive(mid) == "completed"


# ── #12 a slow start keeps every live lease alive ────────────────────────────────────────────────────

def test_the_helper_thread_refreshes_live_leases_during_a_start(stack):
    st = stack()
    st.sup.heartbeat_interval_s = 0.01
    live = st.propose()["mission_id"]
    leases.acquire(st.store, ["host:local/stack:other"], live, 60)
    st.sup._runs[live] = sup_mod._Run(live, None, None, None, None, 0.0)
    st.clock.t += 40
    try:
        with st.sup._heartbeating("M-x"):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and last_heartbeat(st, live) != st.clock.t:
                time.sleep(0.01)
        assert last_heartbeat(st, live) == st.clock.t
    finally:
        st.sup._runs.clear()


def test_a_checkpoint_longer_than_the_lease_ttl_does_not_lose_the_early_lease(stack):
    st = stack()
    st.rig.monitor.start()
    st.sup.heartbeat_interval_s = 0.01
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    original = st.env.svc.create

    def slow(mission_id, scope):
        for _ in range(6):                               # the checkpoint takes three lease lifetimes, in half-lifetime steps
            st.clock.t += st.sup.lease_ttl_s // 2
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and last_heartbeat(st, mission_id) != st.clock.t:
                time.sleep(0.01)
        return original(mission_id, scope)
    st.env.svc.create = slow
    assert st.drive(mid) == "completed"


# ── #17 a failed resume does not lose the baseline that tripped ──────────────────────────────────────

def test_a_failed_resume_keeps_the_original_baseline(stack):
    st = stack(behaviours=[{"script": [update()] * 6}], with_proxy=True)
    st.rig.monitor.start()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    original = st.monitor.state.read(mid, "baseline.json")
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    assert st.status(mid) == "paused_tripwire"

    def boom(mission_id, grant_hash):
        raise RuntimeError("proxy down")
    real = st.proxy.resume
    st.proxy.resume = boom
    with pytest.raises(sup_mod.ResumeRefused):
        st.sup.resume(mid, "operator:alice")
    assert st.status(mid) == "paused_tripwire"
    assert st.monitor.state.read(mid, "baseline.json") == original
    assert st.monitor.state.read(mid, "baseline.prev.json") is None
    st.proxy.resume = real
    st.sup.resume(mid, "operator:alice")
    after = st.monitor.state.read(mid, "baseline.json")
    assert after != original and st.monitor.state.read(mid, "baseline.prev.json") is None


def test_a_stale_set_aside_baseline_is_not_kept_when_there_is_nothing_to_set_aside(stack):
    st = stack(behaviours=[{"script": [update()] * 6}], with_proxy=True)
    st.rig.monitor.start()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.monitor.state.write(mid, "baseline.prev.json", {"v": 1, "stale": True})
    st.monitor.state.remove(mid, "baseline.json")                     # nothing readable to set aside
    st.monitor.begin(mid, keep_previous=True)
    assert st.monitor.state.read(mid, "baseline.prev.json") is None
    assert st.monitor.restore_previous_baseline(mid) is False


# ── #18 a passing preflight is not cached forever ────────────────────────────────────────────────────

def _preflight(state):
    from planet_express.missions.launcher.preflight import Check, PreflightReport

    def pre():
        state["calls"] = state.get("calls", 0) + 1
        report = PreflightReport("launcher")
        report.checks.append(Check("x", state.get("ok", True), "" if state.get("ok", True) else "broke"))
        return report
    return pre


def test_a_passing_preflight_is_proven_again(stack):
    st = stack()
    state = {}
    st.sup.preflight_fn = _preflight(state)
    assert st.sup.launcher_ready() and st.sup.launcher_ready() and state["calls"] == 1
    st.clock.t += sup_mod.PREFLIGHT_REPROVE_S + 1
    assert st.sup.launcher_ready() and state["calls"] == 2


def test_every_repair_start_re_proves_the_launcher(stack):
    st = stack()
    st.rig.monitor.start()
    state = {}
    st.sup.preflight_fn = _preflight(state)
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    assert st.sup.launcher_ready()
    state["ok"] = False                                  # the launcher breaks after the cached pass
    st.sup.tick()
    assert st.status(mid) == "awaiting_decision" and st.runtimes == []
    assert not [b for b in notes(st, mid) if "start_failed" in b]


# ── Codex: a crash between the early lease and the session leaves no orphaned lease ──────────────────

def test_a_pre_start_lease_left_by_a_crash_is_released_at_recovery(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup._acquire_early(st.ms.get_mission(mid))               # Core dies right here: no session row exists
    assert lease_count(st) > 0
    st.sup.recover()
    assert lease_count(st) == 0
    st.rig.monitor.start()
    assert st.drive(mid) == "completed"                          # and the mission can start normally afterwards
