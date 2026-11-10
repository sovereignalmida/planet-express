"""Actions and Missions see each other (rc13). Before this, a mission's Docker writes took no lock and v3 never looked at
mission leases: an Actions-tab restart, an update pass or safe-prune could run in the middle of a Repair on the same stack.

  - While a mission holds any lease, no v3 change starts (PipelineState.try_begin_mutation), and the busy message says which.
  - A Repair start waits while a v3 change already holds the lock (checked after its leases are taken, so nothing slips in).
Trust mode removed the per-session Docker proxy, and with it the per-write lock: a session's own Docker calls are its record's."""
import pytest
from repair_fakes import build, update

import casa_farnsworth as fw
from planet_express.missions import leases


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, behaviours=[{"script": [update()] * 12}], **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def go(st):
    """Propose, approve, start: a live Repair session. Returns (mission id, session id)."""
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    sid = st.runtimes[0].context.session.session_id
    return mid, sid


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
    mid, _sid = go(st)
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


def test_core_registers_the_claims_before_mission_control_starts_and_whether_or_not_it_does():
    """Mission control's thread can take leases as soon as it starts, and a lease from an earlier run counts even if it
    fails to start: the claims check is wired first, unconditionally (Codex review, rc13)."""
    import inspect
    src = inspect.getsource(fw.run_bot)
    register = src.index("state.set_mission_claims(")
    assert register < src.index("start_mission_runner(")
    # And before the dashboard RPC, which takes the same lock for an Action, a config apply or an update rollback
    # (rc14.7.4: it used to start first, so for a moment it did not see a lease left from before the restart).
    assert register < src.index("_start_dashboard_rpc(")
    line = next(ln for ln in src.splitlines() if "state.set_mission_claims(" in ln)
    assert line.startswith("    state.") and not line.startswith("     "), "at run_bot's top level, not under a condition"
    assert "host_lock=state" in src


def test_a_mission_handed_to_the_operator_no_longer_blocks_the_operators_tools(stack):
    """A take-over, a held cancel or a failed run leaves the mission in needs_operator with its leases (they still fence
    other missions out) and no live session: the Actions tab, config apply and update rollbacks must work again."""
    st = stack()
    mid, sid = go(st)
    assert leases.claim_reason(st.store) is not None
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET status='needs_operator' WHERE id=?", (mid,))
    assert leases.claim_reason(st.store) is not None, "a session that may still be running keeps the claim"
    # The rows as production leaves them (rc14.7.4): nothing ever moves `state` off 'live'; a session's end is its
    # revocation, and it is provably dead once its uid is released. The test used to write state='ended' by hand,
    # which production never does, so the exemption it proved never applied on a real host.
    with st.store._write() as conn:
        conn.execute("UPDATE mission_sessions SET session_uid=NULL, uid_released_at=NULL, revoked_at=1 WHERE id=?", (sid,))
    assert leases.claim_reason(st.store) is not None, "revoked, but with no uid there is no proof it is dead"
    with st.store._write() as conn:
        conn.execute("UPDATE mission_sessions SET session_uid=200001, uid_released_at=NULL WHERE id=?", (sid,))
    assert leases.claim_reason(st.store) is not None, "revoked, but its uid is not released: it may still be running"
    with st.store._write() as conn:
        conn.execute("UPDATE mission_sessions SET uid_released_at=2 WHERE id=?", (sid,))
    with st.store._connect() as conn:
        assert conn.execute("SELECT state FROM mission_sessions WHERE id=?", (sid,)).fetchone()[0] in ("live", "launching")
    assert leases.claim_reason(st.store) is None
    with st.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mission_leases WHERE mission_id=?", (mid,)).fetchone()[0] > 0
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET status='executing' WHERE id=?", (mid,))
    assert leases.claim_reason(st.store) is not None, "only needs_operator hands the host back"
