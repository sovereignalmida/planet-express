import json
import sqlite3

import pytest
from supervisor_fakes import Rig, tool_event, update


def kill_reason_events(rig):
    return [json.loads(e.body_json) for e in rig.ms.events(rig.mid) if "kill" in e.body_json]


def breach(rig, which):
    """Make the NEXT status poll breach one budget."""
    if which == "wall":
        rig.clock.t += 10_000
        return update()
    if which == "tool_calls":
        return update(events=[tool_event() for _ in range(4)])
    return update(output_bytes=10_000)


BUDGETS = {"wall_s": 100, "tool_calls": 3, "output_bytes": 5_000, "retries": 0}


@pytest.mark.parametrize("which", ["wall", "tool_calls", "output_bytes"])
def test_budget_breach_kills_waits_for_empty_then_asks_the_operator(tmp_path, which):
    rig = Rig(tmp_path, budgets=BUDGETS)
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.cont.ignore_kill(sid, 2)                     # the process ignores kill for two polls
    rig.runtimes[0].script = [breach(rig, which)]
    rig.sup.poll(rig.mid)
    assert rig.cont.kill_calls(sid) >= 1
    assert rig.status() == "investigating", "no transition before is_empty()"
    assert rig.ms.live_session(rig.mid) is None, "token is revoked as soon as the kill starts"
    for _ in range(5):
        rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"
    assert not rig.cont.exists(sid)
    reasons = " ".join(rig.dump().split())
    assert f"budget:{which}" in reasons


def test_process_that_ignores_kill_blocks_the_terminal_transition(tmp_path):
    rig = Rig(tmp_path, budgets=BUDGETS)
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.cont.ignore_kill(sid, 4)
    rig.sup.cancel(rig.mid, "operator:me")
    for _ in range(3):
        rig.sup.poll(rig.mid)
        assert rig.status() == "investigating"
    for _ in range(4):
        rig.sup.poll(rig.mid)
    assert rig.status() == "cancelled"


def test_leaked_child_blocks_release_until_it_is_gone(tmp_path):
    rig = Rig(tmp_path, budgets=BUDGETS)
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.cont.leak_child(sid)
    rig.runtimes[0].script = [breach(rig, "tool_calls")]
    for _ in range(8):
        rig.sup.poll(rig.mid)
        assert rig.status() == "investigating"
    assert rig.cont.exists(sid) and rig.cont.kill_calls(sid) > 1, "kill is retried while members remain"
    rig.cont.reap_leaks(sid)
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"


def test_cancel_kills_then_lands_in_cancelled(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    rig.sup.cancel(rig.mid, "operator:me")
    mission = rig.ms.get_mission(rig.mid)
    assert mission.cancel_requested_by == "operator:me" and mission.cancel_requested_at is not None
    assert rig.runtimes[0].cancelled
    rig.sup.poll(rig.mid)
    assert rig.status() == "cancelled"
    assert rig.cont.kill_calls(sid) >= 1 and not rig.cont.exists(sid)


def test_cancel_before_a_session_exists(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.cancel(rig.mid, "operator:me")
    assert rig.status() == "cancelled"


def test_observe_and_propose_can_never_enter_executing_via_the_supervisor(tmp_path):
    for tier in ("observe", "propose"):
        sub = tmp_path / tier
        sub.mkdir()
        rig = Rig(sub, tier=tier, behaviours=[{"script": [update("finished")]}])
        rig.sup.start(rig.mid)
        for _ in range(5):
            rig.sup.poll(rig.mid)
        with sqlite3.connect(rig.store.path) as conn:
            states = [r[0] for r in conn.execute("SELECT body_json FROM mission_events WHERE kind='state'")]
        assert not any('"executing"' in s for s in states)


def test_the_final_result_counts_against_the_output_budget(tmp_path):
    rig = Rig(tmp_path, budgets={"output_bytes": 20, "retries": 0}, behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"
    assert "budget:output_bytes" in rig.dump()
    assert "result.json" not in rig.dump()


def test_a_cancel_that_raced_the_launch_spawns_nothing(tmp_path):
    rig = Rig(tmp_path)
    original = rig.sup.ms.start_session

    def start_then_cancel(*a, **k):
        sid = original(*a, **k)
        _flag(rig)
        return sid
    rig.sup.ms.start_session = start_then_cancel
    rig.sup.start(rig.mid)
    assert rig.status() == "cancelled" and rig.runtimes == []
    assert rig.ms.live_session(rig.mid) is None


def _flag(rig):
    now = int(rig.store._clock())
    with rig.store._write() as conn:
        conn.execute("UPDATE missions SET cancel_requested_at=?, cancel_requested_by='operator:x' WHERE id=?",
                     (now, rig.mid))


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_a_malformed_status_object_is_a_runtime_error_not_a_crash_of_poll(tmp_path):
    from planet_express.missions.runtime import AgentUpdate
    rig = Rig(tmp_path, budgets={"retries": 0})
    rig.sup.start(rig.mid)
    bad = AgentUpdate.model_construct(state="running", events=[{"not": "an event"}], output_bytes=0)
    rig.runtimes[0].script = [bad]
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator" and rig.ms.live_session(rig.mid) is None
