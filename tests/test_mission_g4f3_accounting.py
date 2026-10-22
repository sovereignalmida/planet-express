"""G4-F3 #19: tool budgets and the decision quota are counted by SQL (or a tally), atomically, from rows only Core writes."""
import contextlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build, update
from test_mission_tools import setup

from planet_express.integrations.rpc import RpcError
from planet_express.missions.sessions import SessionRow
from planet_express.missions.tools import ToolBroker


# ── tools ────────────────────────────────────────────────────────────────────────────────────────────

def test_concurrent_calls_cannot_overshoot_the_tool_call_budget(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, budgets={"tool_calls": 2})
    real = broker.http

    def slow(method, url, headers, timeout):
        time.sleep(0.05)                                   # every call is in flight long enough for the others to arrive
        return real(method, url, headers, timeout)
    broker.http = slow
    results = []

    def call():
        try:
            results.append(broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})["ok"])
        except RpcError:
            results.append("refused")
    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 2 and results.count("refused") == 4
    assert sum(1 for e in ms.events(m.id) if '"tool.invoke"' in e.body_json) == 2


def test_a_second_broker_reads_the_same_tally_from_the_log(tmp_path):
    ms, m, broker, sess, http, runner = setup(tmp_path, budgets={"tool_calls": 2})
    for _ in range(2):
        broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})
    again = ToolBroker(broker.registry, missions=ms, secrets=broker.secrets, sites=broker.sites, http=http,
                       runner=runner, stacks_root=broker.stacks_root, protected=broker.protected)
    assert again._usage(m.id)["invoke"] == 2
    with pytest.raises(RpcError):
        again.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})


def test_an_event_a_session_could_write_never_counts_toward_the_budget(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path, budgets={"tool_calls": 2})
    for _ in range(5):                                      # the worker's own tool notes, shaped like the broker's
        ms.append_event(m.id, "tool", "worker", {"event": "tool.invoke", "bytes": 10**9, "session": sess.id})
    assert broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})["ok"]


def test_the_budget_check_does_not_scan_the_log_on_every_call(tmp_path):
    ms, m, broker, sess, http, _ = setup(tmp_path)
    broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 1})                  # the tally is loaded once
    statements = []
    real = ms.store._connect

    @contextlib.contextmanager
    def tracing(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    ms.store._connect = tracing
    try:
        for i in range(3):
            broker.invoke(sess, "arr.queue_item", {"app": "sonarr", "id": 2 + i})
    finally:
        ms.store._connect = real
    assert not [q for q in statements if "json_extract" in q or "SUM(" in q]      # no per-call sum over the event log
    assert broker._usage(m.id)["invoke"] == 4


# ── the decision quota ───────────────────────────────────────────────────────────────────────────────

def session_row(st, mid):
    row = st.ms.live_session(mid)
    return SessionRow(id=row["id"], mission_id=mid, kind=row["kind"], cgroup_path=row["cgroup_path"], leader_pid=row["leader_pid"],
                      session_uid=row["session_uid"], issued_at=row["issued_at"], expires_at=row["expires_at"], fences={})


@pytest.fixture
def running(tmp_path):
    st = build(tmp_path, behaviours=[{"script": [update()] * 40}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) == "executing"
    yield st, mid
    st.close()


def test_a_worker_note_cannot_use_up_the_decision_quota(running):
    st, mid = running
    row = session_row(st, mid)
    for _ in range(20):                                     # free text from the worker that merely says it
        st.sup.post_event(row, "note", {"event": "decision.requested", "summary": "x"})
    for _ in range(5):
        assert st.sup.request_decision(row, "really asking", ["yes"]) == {"recorded": True}
    with pytest.raises(RpcError):
        st.sup.request_decision(row, "one too many", [])


def test_the_decision_quota_does_not_read_the_event_log(running):
    st, mid = running
    row = session_row(st, mid)
    st.ms.events = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the whole event log was loaded"))
    assert st.sup.request_decision(row, "ok?", []) == {"recorded": True}


def test_the_decision_cards_are_written_under_their_own_actor(running):
    st, mid = running
    st.sup.request_decision(session_row(st, mid), "ok?", ["a"])
    cards = [e for e in st.ms.events(mid) if json.loads(e.body_json).get("event") == "decision.requested"]
    assert [e.actor for e in cards] == ["worker:decision"]
