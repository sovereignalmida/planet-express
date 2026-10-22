"""Unique uid per live session (contract 22.1, 22.8.3) and the carried-in Codex P1: the second real session must work."""
import sqlite3
import threading

import pytest
from missions_helpers import advance_to, make_store
from supervisor_fakes import Rig, update

from planet_express.core.store import Store
from planet_express.missions.containment import FakeContainment
from planet_express.missions.models import Grant
from planet_express.missions.schema import DDL_V6
from planet_express.missions.sessions import issue_session
from planet_express.missions.store import MissionStore, NoFreeUid
from planet_express.missions.supervisor import StartDeferred


def missions(tmp_path, count, uid_range=(61000, 61002)):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    out = []
    for n in range(count):
        m = ms.create_mission(title=f"t{n}", origin="operator", goal="g", success_criteria="s", runtime="fake",
                              grant=Grant.model_validate({"tier": "observe"}))
        advance_to(ms, m.id, "queued")
        out.append(m)
    return store, ms, out


def start(ms, mission, sid, uid_range=(61000, 61002)):
    cs, _ = issue_session(ms, mission.id, sid, f"/cg/{sid}", session_ttl_s=60, lease_ttl_s=60, uid_range=uid_range)
    return cs


def test_two_sequential_sessions_get_different_uids(tmp_path):
    _, ms, (a, b) = missions(tmp_path, 2)
    first, second = start(ms, a, "S-a"), start(ms, b, "S-b")
    assert {first.session_uid, second.session_uid} == {61000, 61001}


def test_two_concurrent_starts_never_share_a_uid(tmp_path):
    _, ms, ms_list = missions(tmp_path, 8)
    barrier = threading.Barrier(len(ms_list))
    got, errors = [], []

    def go(mission, index):
        barrier.wait()
        try:
            got.append(start(ms, mission, f"S-{index}", (61000, 61100)).session_uid)
        except Exception as exc:        # noqa: BLE001 -- collected and asserted on below
            errors.append(exc)
    threads = [threading.Thread(target=go, args=(m, i)) for i, m in enumerate(ms_list)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(got)) == len(ms_list)


def test_a_revoked_sessions_uid_is_not_reused_until_it_is_released(tmp_path):
    _, ms, (a, b, c) = missions(tmp_path, 3)
    first = start(ms, a, "S-a", (61000, 61001))
    ms.revoke_session("S-a")
    second = start(ms, b, "S-b", (61000, 61001))
    assert second.session_uid != first.session_uid
    ms.revoke_session("S-b")
    with pytest.raises(NoFreeUid):                      # both uids are revoked but still held: nothing is free
        start(ms, c, "S-c", (61000, 61001))
    assert ms.release_session_uid("S-a")
    assert start(ms, c, "S-c", (61000, 61001)).session_uid == first.session_uid


def test_a_refused_allocation_leaves_the_mission_and_leases_untouched(tmp_path):
    store, ms, (a, b) = missions(tmp_path, 2)
    start(ms, a, "S-a", (61000, 61000))
    with pytest.raises(NoFreeUid):
        start(ms, b, "S-b", (61000, 61000))
    assert ms.get_mission(b.id).status == "queued" and ms.live_session(b.id) is None


def test_the_uid_is_unique_among_held_rows_even_when_chosen_by_hand(tmp_path):
    from planet_express.missions.store import SessionConflict
    _, ms, (a, b) = missions(tmp_path, 2)
    ms.start_session(a.id, kind="local_cgroup", token_hash="a" * 64, session_ttl_s=60, lease_ttl_s=60,
                     session_uid=61500)
    with pytest.raises(SessionConflict):
        ms.start_session(b.id, kind="local_cgroup", token_hash="b" * 64, session_ttl_s=60, lease_ttl_s=60,
                         session_uid=61500)


def test_releasing_is_idempotent_and_only_for_a_session_that_has_a_uid(tmp_path):
    _, ms, (a,) = missions(tmp_path, 1)
    start(ms, a, "S-a")
    assert ms.release_session_uid("S-a") and not ms.release_session_uid("S-a")
    assert not ms.release_session_uid("S-nope")


# ── through the supervisor ──────────────────────────────────────────────────────────────────────

def test_the_row_with_its_uid_is_committed_before_the_launcher_runs(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()]}])
    seen = {}
    real_launch = rig.cont.launch

    def launch(session_id, spec):
        row = rig.ms.get_session(session_id)
        seen.update(state=row["state"], uid=row["session_uid"], spec_uid=spec.uid, revoked=row["revoked_at"])
        return real_launch(session_id, spec)
    rig.cont.launch = launch
    rig.sup.start(rig.mid)
    assert seen == {"state": "launching", "uid": seen["spec_uid"], "spec_uid": seen["spec_uid"], "revoked": None}
    assert rig.ms.get_session(rig.session_ids()[0])["state"] == "live"


def test_a_runtime_cannot_choose_the_uid_or_the_session(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()]}])
    rig.sup.start(rig.mid)
    spawn = rig.cont.spawns(rig.session_ids()[0])[0]
    assert spawn["uid"] == rig.ms.get_session(rig.session_ids()[0])["session_uid"]
    assert 60000 <= spawn["uid"] <= 60999


def test_two_real_shaped_missions_in_a_row_both_start_and_the_uid_is_recycled_after_the_reap(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}, {"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    first = rig.sessions()[0]
    assert rig.status() == "awaiting_decision" and first["uid_released_at"] is not None
    second = rig.ms.create_mission(title="t2", origin="operator", grant=rig.mission.grant(), goal="g2",
                                   success_criteria="s", runtime="fake")
    advance_to(rig.ms, second.id, "queued")
    rig.sup.start(second.id)
    rig.sup.poll(second.id)
    assert rig.ms.get_mission(second.id).status == "awaiting_decision"
    uids = [r["session_uid"] for r in rig.sessions()] + [rig.ms.get_session(s)["session_uid"]
                                                          for s in _session_ids(rig, second.id)]
    assert len(uids) == 2 and uids[0] == uids[1]                      # the first was released, so it is reused


def _session_ids(rig, mission_id):
    with sqlite3.connect(rig.store.path) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM mission_sessions WHERE mission_id=?", (mission_id,))]


def test_two_live_sessions_hold_different_uids_in_the_supervisor(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()] * 5}, {"script": [update()] * 5}])
    other = rig.ms.create_mission(title="t2", origin="operator", grant=rig.mission.grant(), goal="g2",
                                  success_criteria="s", runtime="fake")
    advance_to(rig.ms, other.id, "queued")
    rig.sup.start(rig.mid)
    rig.sup.start(other.id)
    held = {rig.ms.get_session(s)["session_uid"] for s in rig.session_ids()} | \
           {rig.ms.get_session(s)["session_uid"] for s in _session_ids(rig, other.id)}
    assert len(held) == 2


def test_a_straggler_process_keeps_the_uid_held_until_it_is_gone(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    rig.cont.uids_running.add(60000)                    # something still runs as the first uid at reap time
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    row = rig.sessions()[0]
    assert row["session_uid"] == 60000 and row["uid_released_at"] is None
    assert rig.sup.sweep_uids() == 0
    rig.cont.uids_running.clear()
    assert rig.sup.sweep_uids() == 1
    assert rig.ms.get_session(row["id"])["uid_released_at"] is not None


def test_recovery_reaps_a_launching_row_left_by_a_crash_and_releases_its_uid(tmp_path):
    rig = Rig(tmp_path)
    cs, _ = issue_session(rig.ms, rig.mid, "S-crashed", rig.cont.cgroup_path("S-crashed"), session_ttl_s=60,
                          lease_ttl_s=60, uid_range=rig.cont.uid_range)      # committed, then Core died before launch
    assert rig.ms.get_session("S-crashed")["state"] == "launching"
    rig.sup.recover()
    row = rig.ms.get_session("S-crashed")
    assert row["revoked_at"] is not None and row["uid_released_at"] is not None
    assert rig.ms.live_session(rig.mid) is None or rig.ms.live_session(rig.mid)["id"] != "S-crashed"


def test_recovery_kills_a_launched_but_never_live_session_before_freeing_its_uid(tmp_path):
    from planet_express.missions.launcher.spec import LaunchSpec
    rig = Rig(tmp_path, behaviours=[{"script": [update()]}])
    cs, _ = issue_session(rig.ms, rig.mid, "S-half", rig.cont.cgroup_path("S-half"), session_ttl_s=60,
                          lease_ttl_s=60, uid_range=rig.cont.uid_range)
    rig.cont.launch("S-half", LaunchSpec(uid=cs.session_uid, cwd="/w", argv=("/usr/bin/x",), token="t" * 40))
    rig.sup.recover()
    assert rig.cont.kill_calls("S-half") >= 1 and not rig.cont.exists("S-half")
    assert rig.ms.get_session("S-half")["uid_released_at"] is not None


def test_an_exhausted_range_is_a_wait_that_leaves_the_mission_queued(tmp_path):
    rig = Rig(tmp_path)
    rig.cont.uid_range = (60000, 60000)
    other = rig.ms.create_mission(title="t2", origin="operator", grant=rig.mission.grant(), goal="g2",
                                  success_criteria="s", runtime="fake")
    advance_to(rig.ms, other.id, "queued")
    rig.sup.start(rig.mid)                                               # takes the only uid
    with pytest.raises(StartDeferred, match="NoFreeUid"):             # transient: not a failed start (G4-F3 #11)
        rig.sup.start(other.id)
    assert rig.ms.get_mission(other.id).status == "queued"


# ── a v6 database made before slice 3d is repaired in place ─────────────────────────────────────

def test_a_pre_3d_sessions_table_is_rebuilt_keeping_its_rows_and_making_uids_reusable(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    m = ms.create_mission(title="t", origin="operator", goal="g", success_criteria="s", runtime="fake",
                          grant=Grant.model_validate({"tier": "observe"}))
    old_table = DDL_V6[DDL_V6.index("CREATE TABLE IF NOT EXISTS mission_sessions"):DDL_V6.index("CREATE UNIQUE INDEX")]
    old_table = (old_table.replace("session_uid INTEGER,", "session_uid INTEGER UNIQUE,")
                 .replace(",\n    state TEXT NOT NULL DEFAULT 'live',\n    uid_released_at INTEGER", ""))
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP INDEX session_uid_held")
        conn.execute("DROP INDEX one_live_session")
        conn.execute("DROP TABLE mission_sessions")
        conn.execute(old_table)
        conn.execute("INSERT INTO mission_sessions (id, mission_id, token_hash, issued_at, expires_at, kind, "
                     "session_uid, fence_json) VALUES ('S-old', ?, 'h', 1, 2, 'local_cgroup', 61000, '{}')", (m.id,))
    from planet_express.core.store import write_snapshot_marker
    write_snapshot_marker(store.path, now=clock.t)
    again = Store(store.path, clock=clock)
    again.init()
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM mission_sessions WHERE id='S-old'").fetchone())
        names = {r[1] for r in conn.execute("PRAGMA index_list(mission_sessions)")}
    assert row["state"] == "live" and row["session_uid"] == 61000 and row["uid_released_at"] is None
    assert {"one_live_session", "session_uid_held"} <= names
    again2 = MissionStore(again)
    assert again2.get_session("S-old")["session_uid"] == 61000
    assert again2.release_session_uid("S-old")                         # now it can be released and reused


def test_fake_containment_range_and_cgroup_path_contract():
    cont = FakeContainment()
    assert cont.uid_range == (60000, 60999) and cont.cgroup_path("S-1") == "/fake-cgroup/S-1"
