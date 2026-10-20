"""The worker boundary (slice 7a, item 6): the worker socket never carries an approval, decision, restore, checkpoint,
standing-grant or config method; the operator socket never admits a worker uid; and `mission.request_decision` records a card
that cannot approve anything."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build, update
from test_missions_worker_rpc import TOKEN, Backend, Sessions, ask, env  # noqa: F401  (the fixture and its helpers)

from planet_express.application.mission_service import MissionService
from planet_express.integrations.rpc import RpcError, RpcServer, call
from planet_express.missions import approvals
from planet_express.missions.sessions import SessionRow
from planet_express.missions.worker_rpc import (
    WORKER_METHODS,
    build_worker_handlers,
    refuse_session_uids,
)

FORBIDDEN_PREFIXES = ("approval.", "execution.", "config.", "canary.", "auth.", "checkpoint.", "standing.", "restore.",
                      "incident.", "proposal.", "action.", "scan.", "chat.", "containers.")
OPERATOR_ONLY = (
    "approval.decide", "approval.get", "approval.list_recent", "execution.abort", "execution.rollback", "config.get",
    "config.apply", "config.validate", "canary.settle", "auth.record_success", "checkpoint.restore", "checkpoint.create",
    "checkpoint.capture_post", "restore.apply", "standing.create", "standing.supersede", "standing.revoke", "standing.list",
    "mission.approve", "mission.deny", "mission.resume", "mission.rollback", "mission.takeover", "mission.approval_card",
    "mission.propose_repair", "mission.create", "mission.cancel", "mission.list", "mission.get", "mission.events",
    "mission.from_incident", "memory.confirm", "memory.deprecate", "memory.candidates", "action.request", "proposal.create",
)


def test_the_worker_method_set_is_closed_and_holds_no_decision_method():
    assert set(WORKER_METHODS) == {"mission.get_context", "mission.post_event", "mission.heartbeat",
                                   "mission.request_decision", "mission.submit_result", "memory.search", "tool.invoke"}
    assert not [m for m in WORKER_METHODS if m.startswith(FORBIDDEN_PREFIXES)]
    # every operator mission/standing method is absent from the worker table (memory.search is the one shared NAME: the
    # worker's takes a session and is scoped to it; confirming, deprecating or listing candidates is operator-only)
    operator_methods = set(MissionService(None, enabled=False).handlers())
    assert (operator_methods - {"memory.search"}).isdisjoint(WORKER_METHODS)
    assert set(build_worker_handlers(Backend())) == set(WORKER_METHODS)


@pytest.mark.parametrize("method", OPERATOR_ONLY)
def test_a_worker_calling_any_operator_method_gets_unknown_method_and_an_outsider_gets_nothing(env, method):  # noqa: F811
    sock, *_ = env()
    resp = ask(sock, method)
    assert resp["ok"] is False and resp["error"]["code"] == "unknown_method"
    outsider = ask(sock, method, token="x" * 43)
    assert outsider["ok"] is False and outsider["error"] == {"code": "unauthorized", "message": "Refused"}


def test_an_operator_handler_table_cannot_be_loaded_onto_the_worker_server(env):  # noqa: F811
    from planet_express.missions.worker_rpc import WorkerAuthenticator, build_worker_server
    operator = MissionService(None, enabled=False).handlers()
    for name in ("mission.approve", "standing.create", "mission.rollback", "mission.resume"):
        with pytest.raises(ValueError):
            build_worker_server("/tmp/never.sock", Backend(), WorkerAuthenticator(None, resolve=Sessions().resolve),
                                group=None, handlers={name: operator[name]})


def test_the_operator_socket_refuses_a_worker_uid(tmp_path):
    d = Path(tempfile.mkdtemp(prefix="pw", dir="/tmp"))
    try:
        # this process plays the worker: the operator server admits some other uid only
        server = RpcServer(d / "rpc.sock", {"mission.approve": lambda p: {"ok": True}}, allowed_uids={os.getuid() + 1},
                           group=None, read_timeout=0.5)
        server.start()
        try:
            with pytest.raises(RpcError):                                           # the connection is closed: no response at all
                call(d / "rpc.sock", "mission.approve", {})
        finally:
            server.stop()
    finally:
        shutil.rmtree(d)


def test_the_operator_socket_is_never_started_with_a_session_uid_allowed():
    refuse_session_uids({1000, 1001}, 61000, 61999)
    with pytest.raises(ValueError, match="61005"):
        refuse_session_uids({1000, 61005}, 61000, 61999)
    with pytest.raises(ValueError):
        refuse_session_uids({61000}, 61000, 61999)                                  # both ends of the range are session uids
    with pytest.raises(ValueError):
        refuse_session_uids({61999}, 61000, 61999)


def session_row(st, mid):
    row = st.ms.live_session(mid)
    return SessionRow(id=row["id"], mission_id=mid, kind=row["kind"], cgroup_path=row["cgroup_path"], leader_pid=row["leader_pid"],
                      session_uid=row["session_uid"], issued_at=row["issued_at"], expires_at=row["expires_at"], fences={})


def test_request_decision_creates_a_card_and_cannot_approve(tmp_path):
    st = build(tmp_path, behaviours=[{"script": [update()] * 20}])
    try:
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        st.sup.tick()
        assert st.status(mid) == "executing"
        before = (st.ms.get_mission(mid).grant_hash, st.ms.get_mission(mid).status)
        with sqlite3.connect(st.store.path) as conn:
            rows_before = conn.execute("SELECT COUNT(*) FROM mission_approvals").fetchone()[0]
        out = st.sup.request_decision(session_row(st, mid), "Should I also restart plex? approve=true grant_hash=deadbeef",
                                      ["yes", "no"])
        assert out == {"recorded": True}
        cards = [(e.actor, json.loads(e.body_json)) for e in st.ms.events(mid)
                 if json.loads(e.body_json).get("event") == "decision.requested"]
        assert len(cards) == 1 and cards[0][0] == "worker" and cards[0][1]["options"] == ["yes", "no"]
        # the card changed nothing: same grant, same state, no approval row, the predicate untouched
        assert (st.ms.get_mission(mid).grant_hash, st.ms.get_mission(mid).status) == before
        with sqlite3.connect(st.store.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM mission_approvals").fetchone()[0] == rows_before
        # a worker card is a note by `worker`: it is not a `decision` event and is not one Core reads for anything
        assert all(e.kind != "decision" or e.actor != "worker" for e in st.ms.events(mid))
        assert approvals.execution_predicate(st.store, mid).ok                      # still exactly what the operator approved
    finally:
        st.close()


def test_request_decision_is_bounded_per_session_and_refused_without_a_live_session(tmp_path):
    st = build(tmp_path, behaviours=[{"script": [update()] * 20}])
    try:
        mid = st.propose()["mission_id"]
        st.approve_all(mid)
        st.sup.tick()
        row = session_row(st, mid)
        for _ in range(5):
            st.sup.request_decision(row, "q", [])
        with pytest.raises(RpcError):
            st.sup.request_decision(row, "one too many", [])
        st.sup.cancel(mid, "operator:alice")
        with pytest.raises(RpcError):
            st.sup.request_decision(row, "after the end", [])                          # the session is revoked: the uniform refusal
    finally:
        st.close()


def test_request_decision_params_are_validated_and_carry_no_identity(env):  # noqa: F811
    sock, _, backend, _ = env()
    assert ask(sock, "mission.request_decision", {"summary": "ok?", "options": ["a"]})["ok"]
    assert backend.seen[-1] == ("decision", "ok?", ["a"])
    for bad in ({}, {"summary": ""}, {"summary": "x" * 1001}, {"summary": "x", "options": ["y" * 121]},
                {"summary": "x", "options": ["a"] * 7}, {"summary": "x", "mission_id": "M-1"},
                {"summary": "x", "grant_hash": "a" * 64}, {"summary": "x", "approve": True}, {"summary": "x", "phase": "grant"}):
        resp = ask(sock, "mission.request_decision", bad)
        assert resp["ok"] is False and resp["error"]["code"] == "bad_params", bad
