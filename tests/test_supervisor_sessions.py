# ruff: noqa: RUF059
import sqlite3

import pytest
from missions_helpers import advance_to, make_store, new_mission

from planet_express.missions.containment import FakeContainment
from planet_express.missions.models import Grant
from planet_express.missions.sessions import (
    issue_session,
    resolve_session,
    revoke_session,
)
from planet_express.missions.store import MissionStore, SessionConflict


def setup(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, Grant.model_validate({"tier": "observe"}))
    advance_to(ms, m.id, "queued")
    cont = FakeContainment()
    return store, clock, ms, m, cont


def issue(ms, m, cont, sid="S-abc", **kw):
    kw.setdefault("session_ttl_s", 600)
    kw.setdefault("lease_ttl_s", 60)
    return issue_session(ms, m.id, sid, cont.cgroup_path(sid), uid_range=cont.uid_range, **kw)


def live(ms, sid):
    assert ms.mark_session_live(sid)


def test_only_the_hash_is_stored_and_the_right_caller_resolves(tmp_path):
    store, clock, ms, m, cont = setup(tmp_path)
    cs, token = issue(ms, m, cont)
    live(ms, cs.session_id)
    assert cs.session_id == "S-abc" and len(token) == 64 and int(token, 16) >= 0
    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM mission_sessions"))
    assert token not in dump
    row = resolve_session(store, MissionStore.hash_token(token), peer_cgroup=cs.cgroup_path, peer_uid=cs.session_uid)
    assert row and row.id == "S-abc" and row.mission_id == m.id and row.session_uid == cs.session_uid


def test_wrong_token_wrong_cgroup_wrong_uid_and_missing_binding_are_rejected(tmp_path):
    store, clock, ms, m, cont = setup(tmp_path)
    cs, token = issue(ms, m, cont)
    live(ms, cs.session_id)
    h = MissionStore.hash_token(token)
    ok = {"peer_cgroup": cs.cgroup_path, "peer_uid": cs.session_uid}
    assert resolve_session(store, h, **ok)
    assert resolve_session(store, MissionStore.hash_token("0" * 64), **ok) is None
    assert resolve_session(store, h, peer_cgroup="/fake-cgroup/other", peer_uid=cs.session_uid) is None
    assert resolve_session(store, h, peer_cgroup=cs.cgroup_path, peer_uid=cs.session_uid + 1) is None
    assert resolve_session(store, h, peer_cgroup=cs.cgroup_path) is None          # cgroup alone is not enough
    assert resolve_session(store, h, peer_uid=cs.session_uid) is None             # nor is the uid alone
    assert resolve_session(store, h) is None


def test_a_launching_session_authenticates_only_with_its_full_identity_so_the_first_connect_cannot_race(tmp_path):
    """The agent may connect the instant it is exec'd, before Core has read the helper's status line and marked the
    row live. The identity it is bound to (cgroup, uid, token) only exists once the launcher has run, so a launching row
    is as safe to authenticate as a live one."""
    store, clock, ms, m, cont = setup(tmp_path)
    cs, token = issue(ms, m, cont)
    h = MissionStore.hash_token(token)
    ok = {"peer_cgroup": cs.cgroup_path, "peer_uid": cs.session_uid}
    assert ms.get_session("S-abc")["state"] == "launching"
    assert resolve_session(store, h, **ok).id == "S-abc"
    assert resolve_session(store, h, peer_cgroup=cs.cgroup_path, peer_uid=cs.session_uid + 1) is None
    assert resolve_session(store, h, peer_cgroup="/fake-cgroup/other", peer_uid=cs.session_uid) is None
    assert ms.mark_session_live("S-abc", leader_pid=4242)
    assert resolve_session(store, h, **ok).leader_pid == 4242
    assert not ms.mark_session_live("S-abc")                                      # only launching -> live, once


def test_a_revoked_session_cannot_go_live(tmp_path):
    store, clock, ms, m, cont = setup(tmp_path)
    cs, _ = issue(ms, m, cont)
    revoke_session(ms, cs.session_id)
    assert not ms.mark_session_live(cs.session_id)


def test_revoked_and_expired_sessions_do_not_resolve(tmp_path):
    store, clock, ms, m, cont = setup(tmp_path)
    cs, token = issue(ms, m, cont)
    live(ms, cs.session_id)
    h = MissionStore.hash_token(token)
    ok = {"peer_cgroup": cs.cgroup_path, "peer_uid": cs.session_uid}
    clock.t += 599
    assert resolve_session(store, h, **ok)
    clock.t += 1
    assert resolve_session(store, h, **ok) is None       # expired
    clock.t -= 100
    revoke_session(ms, cs.session_id)
    assert resolve_session(store, h, **ok) is None       # revoked


def test_one_live_session_per_mission(tmp_path):
    store, clock, ms, m, cont = setup(tmp_path)
    issue(ms, m, cont)
    with pytest.raises(SessionConflict):
        issue(ms, m, cont, sid="S-def")
