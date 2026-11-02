"""Actions and Missions see each other (rc13). Before this, a mission's Docker writes took no lock and v3 never looked at
mission leases: an Actions-tab restart, an update pass or safe-prune could run in the middle of a Repair on the same stack.

  - While a mission holds any lease, no v3 change starts (PipelineState.try_begin_mutation), and the busy message says which.
  - A Repair start waits while a v3 change already holds the lock (checked after its leases are taken, so nothing slips in).
  - Each proxied mission write holds the same lock while the daemon acts; a v3 change already under way is waited for."""
import threading

import pytest
from test_dockerproxy_service import events_of, go, restart, stack  # noqa: F401  (the fixture)

import casa_farnsworth as fw
from planet_express.missions import leases
from planet_express.missions.dockerproxy.service import StoreGate


@pytest.fixture(autouse=True)
def _isolate_state_files(monkeypatch, tmp_path):
    monkeypatch.setattr(fw.config, "STATE_STATUS", tmp_path / "run_status.json")
    monkeypatch.setattr(fw.config, "STATE_MONITOR", tmp_path / "monitor.json")


def _state(claims=None):
    return fw.PipelineState(maintenance=lambda: None, mission_claims=claims)


# ── the v3 side: the lock ───────────────────────────────────────────────────────────────────────────

def test_a_mission_claim_refuses_every_v3_change_and_the_busy_message_names_it():
    s = _state(lambda: "M-5 is working on media (stack)")
    for owner in ("act:abc", "plan:p1", "zoidberg-weekly", "zoidberg-patchnow", "stack:up:media"):
        assert not s.try_begin_mutation(owner)
    assert not s.try_begin_mutation("safe-prune", during_scan=True)
    assert not s.try_begin_mutation("zoidberg-weekly", require_idle=True)
    assert s.mutation_owner is None
    assert "M-5 is working on media" in s.busy_reason


def test_a_missions_own_change_is_not_refused_by_mission_claims():
    s = _state(lambda: "M-5 is working on media (stack)")
    assert s.try_begin_mutation("mission:M-5:ab12")
    assert not s.try_begin_mutation("mission:M-6:cd34"), "still one change at a time"
    s.end_mutation("mission:M-5:ab12")
    assert s.mutation_owner is None


def test_unreadable_claims_refuse_rather_than_run_over_a_mission():
    def broken():
        raise OSError("database is locked")
    s = _state(broken)
    assert not s.try_begin_mutation("act:abc")
    assert "could not be read" in s.busy_reason


def test_scans_still_run_while_a_mission_holds_leases():
    s = _state(lambda: "M-5 is working on media (stack)")
    assert s.try_start_run(), "a scan observes; what it would change waits on the claim instead"


def test_with_no_claims_wired_or_none_held_v3_is_unchanged():
    assert _state().try_begin_mutation("act:abc")
    assert _state(lambda: None).try_begin_mutation("act:abc")


# ── the claim itself, from real leases ───────────────────────────────────────────────────────────────

def test_a_running_repair_is_a_claim_in_words_and_v3_is_refused_until_it_ends(stack):
    st = stack()
    assert leases.claim_reason(st.store) is None
    mid, *_ = go(st)
    reason = leases.claim_reason(st.store)
    assert reason.startswith(f"{mid} is working on ") and "media" in reason
    s = _state(lambda: leases.claim_reason(st.store))
    assert not s.try_begin_mutation("act:abc") and mid in s.busy_reason
    leases.release(st.store, mid)
    assert s.try_begin_mutation("act:abc")


# ── the mission side: starting ───────────────────────────────────────────────────────────────────────

def test_a_repair_start_waits_while_a_v3_change_holds_the_lock_and_keeps_no_leases(stack):
    st = stack()
    s = _state(lambda: leases.claim_reason(st.store))
    st.sup.host_lock = s
    assert s.try_begin_mutation("plan:p1")
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert not st.runtimes, "no session while v3 is changing the host"
    assert st.sup.ms.get_mission(mid).status == "awaiting_decision"
    assert leases.claim_reason(st.store) is None, "the early leases were given back"
    assert any(e.body_json and "waiting for plan:p1" in e.body_json for e in st.sup.ms.events(mid))
    s.end_mutation("plan:p1")
    st.clock.t += 11                                        # past START_DEFER_RETRY_S
    st.sup.tick()
    assert st.runtimes and st.sup.ms.get_mission(mid).status == "executing"


# ── the mission side: each write ─────────────────────────────────────────────────────────────────────

def test_a_mission_write_holds_the_host_lock_while_the_daemon_acts(stack):
    st = stack()
    mid, _sid, token, path, _ = go(st)
    s = _state()
    st.svc.host_lock = s
    seen = []
    original = st.daemon._route

    def spy(conn, rec):
        if rec["method"] == "POST":
            seen.append(s.mutation_owner)
        return original(conn, rec)
    st.daemon._route = spy
    assert restart(path, token)[0] == 204
    assert len(seen) == 1 and seen[0].startswith(f"mission:{mid}:proxy:")
    assert s.mutation_owner is None, "released once the daemon has answered"


def test_a_write_waits_for_a_v3_change_then_refuses_and_nothing_reaches_the_daemon(stack):
    st = stack()
    mid, _sid, token, path, _ = go(st)
    s = _state()
    st.svc.host_lock, st.svc.host_wait_s, st.svc.host_poll_s = s, 0.3, 0.05
    assert s.try_begin_mutation("act:abc")
    posts = []
    original = st.daemon._route
    st.daemon._route = lambda conn, rec: (posts.append(rec) if rec["method"] == "POST" else None) or original(conn, rec)
    status, _headers, body = restart(path, token)
    assert status == 409 and b"busy with another change" in body
    assert b"act:abc" not in body, "who holds the lock is not the session's to read"
    assert posts == [] and s.mutation_owner == "act:abc"
    assert not [e for e in events_of(st, mid) if e.get("event") == "docker.write"], "not recorded as a write either"
    assert any(e.get("event") == "docker.refused" for e in events_of(st, mid))


def test_a_write_proceeds_once_the_v3_change_finishes(stack):
    st = stack()
    _mid, _sid, token, path, _ = go(st)
    s = _state()
    assert s.try_begin_mutation("act:abc")
    st.svc.host_lock, st.svc.host_wait_s, st.svc.host_poll_s = s, 5.0, 0.01
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            s.end_mutation("act:abc")
    st.svc.sleep = sleep
    assert restart(path, token)[0] == 204
    assert len(waits) == 3 and s.mutation_owner is None


def test_a_stream_write_only_proves_the_host_is_quiet_as_it_starts(stack):
    """exec and attach can stay open as long as a shell; holding the lock for that would stop every v3 change."""
    st = stack()
    s = _state()
    st.svc.host_lock = s
    gate = StoreGate(st.svc, "S-x", "M-1", "0" * 64, authenticator=None)
    with gate.host(hold=False):
        assert s.mutation_owner is None
    with gate.host(hold=True):
        assert s.mutation_owner.startswith("mission:M-1:proxy:")
    assert s.mutation_owner is None


def test_two_writes_from_one_session_are_serialised(stack):
    st = stack()
    s = _state()
    st.svc.host_lock, st.svc.host_wait_s, st.svc.host_poll_s = s, 5.0, 0.01
    gate = StoreGate(st.svc, "S-x", "M-1", "0" * 64, authenticator=None)
    inside, overlap = threading.Event(), []

    def second():
        with gate.host(hold=True):
            overlap.append(inside.is_set())
    with gate.host(hold=True):
        inside.set()
        t = threading.Thread(target=second)
        t.start()
        t.join(0.2)
        assert t.is_alive(), "the second write waits"
        inside.clear()
    t.join(5)
    assert overlap == [False]


def test_core_registers_the_claims_before_mission_control_starts_and_whether_or_not_it_does():
    """Mission control's thread can take leases as soon as it starts, and a lease from an earlier run counts even if it
    fails to start: the claims check is wired first, unconditionally (Codex review, rc13)."""
    import inspect
    src = inspect.getsource(fw.run_bot)
    register = src.index("state.set_mission_claims(")
    assert register < src.index("start_mission_runner(")
    line = next(ln for ln in src.splitlines() if "state.set_mission_claims(" in ln)
    assert line.startswith("    state.") and not line.startswith("     "), "at run_bot's top level, not under a condition"
    assert "host_lock=state" in src


def test_a_maintenance_window_refuses_a_write_at_once_instead_of_after_the_wait(stack):
    st = stack()
    _mid, _sid, token, path, _ = go(st)
    window = []
    s = fw.PipelineState(maintenance=lambda: window[0] if window else None)
    st.svc.host_lock, st.svc.host_wait_s = s, 60.0
    st.svc.sleep = lambda _s: pytest.fail("a maintenance window is not waited out write by write")
    window.append(object())
    status, _headers, body = restart(path, token)
    assert status == 409 and b"maintenance window" in body


def test_a_mission_handed_to_the_operator_no_longer_blocks_the_operators_tools(stack):
    """A take-over, a held cancel or a failed run leaves the mission in needs_operator with its leases (they still fence
    other missions out) and no live session: the Actions tab, config apply and update rollbacks must work again."""
    st = stack()
    mid, sid, *_ = go(st)
    assert leases.claim_reason(st.store) is not None
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET status='needs_operator' WHERE id=?", (mid,))
    assert leases.claim_reason(st.store) is not None, "a session that may still be running keeps the claim"
    with st.store._write() as conn:
        conn.execute("UPDATE mission_sessions SET revoked_at=1, state='ended' WHERE id=?", (sid,))
    assert leases.claim_reason(st.store) is None
    with st.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mission_leases WHERE mission_id=?", (mid,)).fetchone()[0] > 0
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET status='executing' WHERE id=?", (mid,))
    assert leases.claim_reason(st.store) is not None, "only needs_operator hands the host back"
