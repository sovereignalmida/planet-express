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
    cs = cont.create("S-abc")
    return store, clock, ms, m, cont, cs


def test_only_the_hash_is_stored_and_the_right_caller_resolves(tmp_path):
    store, clock, ms, m, cont, cs = setup(tmp_path)
    sid, token = issue_session(ms, m.id, cs, session_ttl_s=600, lease_ttl_s=60)
    assert sid == "S-abc" and len(token) == 64 and int(token, 16) >= 0
    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM mission_sessions"))
    assert token not in dump
    row = resolve_session(store, MissionStore.hash_token(token), peer_cgroup=cs.cgroup_path)
    assert row and row.id == sid and row.mission_id == m.id


def test_wrong_token_wrong_cgroup_and_missing_binding_are_rejected(tmp_path):
    store, clock, ms, m, cont, cs = setup(tmp_path)
    _, token = issue_session(ms, m.id, cs, session_ttl_s=600, lease_ttl_s=60)
    h = MissionStore.hash_token(token)
    assert resolve_session(store, MissionStore.hash_token("0" * 64), peer_cgroup=cs.cgroup_path) is None
    assert resolve_session(store, h, peer_cgroup="/fake-cgroup/other") is None
    assert resolve_session(store, h) is None
    assert resolve_session(store, h, peer_uid=1234) is None


def test_revoked_and_expired_sessions_do_not_resolve(tmp_path):
    store, clock, ms, m, cont, cs = setup(tmp_path)
    sid, token = issue_session(ms, m.id, cs, session_ttl_s=600, lease_ttl_s=60)
    h = MissionStore.hash_token(token)
    clock.t += 599
    assert resolve_session(store, h, peer_cgroup=cs.cgroup_path)
    clock.t += 1
    assert resolve_session(store, h, peer_cgroup=cs.cgroup_path) is None       # expired
    clock.t -= 100
    revoke_session(ms, sid)
    assert resolve_session(store, h, peer_cgroup=cs.cgroup_path) is None       # revoked


def test_uid_bound_session_requires_the_uid(tmp_path):
    store, clock, ms, m, cont, cs = setup(tmp_path)
    from planet_express.missions.containment import ContainedSession
    uid_cs = ContainedSession("S-uid", "local_uid", None, 64001)
    _, token = issue_session(ms, m.id, uid_cs, session_ttl_s=600, lease_ttl_s=60)
    h = MissionStore.hash_token(token)
    assert resolve_session(store, h, peer_uid=64001)
    assert resolve_session(store, h, peer_uid=64002) is None
    assert resolve_session(store, h, peer_cgroup="/anything") is None


def test_one_live_session_per_mission(tmp_path):
    store, clock, ms, m, cont, cs = setup(tmp_path)
    issue_session(ms, m.id, cs, session_ttl_s=600, lease_ttl_s=60)
    cs2 = cont.create("S-def")
    with pytest.raises(SessionConflict):
        issue_session(ms, m.id, cs2, session_ttl_s=600, lease_ttl_s=60)
