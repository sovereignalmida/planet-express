"""S3c: the Supervisor as the worker socket's backend, candidate persistence, config-driven enablement,
and the queued-mission tick that the daemon's poll thread calls."""
import json
from datetime import UTC, datetime

import pytest
from missions_helpers import advance_to
from supervisor_fakes import GOOD_RESULT, Rig, update

from planet_express.integrations.rpc import RpcError
from planet_express.memory import store as memory_store
from planet_express.memory.models import Card
from planet_express.missions.sessions import resolve_session
from planet_express.missions.store import MissionStore
from planet_express.missions.supervisor import MissionsDisabled

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SECRET_LINE = "API_TOKEN=hunter2-planted-secret"


def card(card_id, **kw):
    data = {"id": card_id, "title": "T " + card_id, "type": "fact", "scope": ["stack:media"],
            "statement": "Media lives on the NAS share.", "source": "operator-stated", "created_at": NOW}
    data.update(kw)
    return Card(**data)


def session_of(rig, index=0):
    rt = rig.runtimes[index]
    return resolve_session(rig.store, MissionStore.hash_token(rt.token), peer_cgroup=rt.context.session.cgroup_path)


def started(tmp_path, **kw):
    rig = Rig(tmp_path, **kw)
    rig.sup.start(rig.mid)
    return rig, session_of(rig)


def candidates(rig):
    with rig.store._connect() as conn:
        return memory_store.list_cards(conn, "candidate")


# ── enablement ──────────────────────────────────────────────────────────────

def test_enabled_may_be_a_callable_read_at_use_time(tmp_path):
    rig = Rig(tmp_path, enabled=True)
    flag = {"on": False}
    sup = type(rig.sup)(rig.store, rig.cont, rig.factory, rig.clock, enabled=lambda: flag["on"],
                        artifact_dir=tmp_path / "a", workspace_dir=tmp_path / "w")
    with pytest.raises(MissionsDisabled):
        sup.start(rig.mid)
    flag["on"] = True
    sup.start(rig.mid)
    assert rig.status() == "investigating"


# ── WorkerBackend: get_context ──────────────────────────────────────────────

def test_get_context_is_scoped_to_the_callers_own_mission(tmp_path):
    rig, row = started(tmp_path)
    ctx = rig.sup.get_context(row)
    assert ctx["mission_id"] == rig.mid and ctx["goal"] == "find out why media is down"
    assert "brief" in ctx and "data" in ctx["brief"].lower()
    assert "token" not in json.dumps(ctx).lower() or rig.runtimes[0].token not in json.dumps(ctx)


def test_backend_refuses_a_session_that_is_not_the_live_run(tmp_path):
    rig, row = started(tmp_path)
    rig.sup.cancel(rig.mid, "operator:me")
    for call in (lambda: rig.sup.get_context(row), lambda: rig.sup.heartbeat(row),
                 lambda: rig.sup.post_event(row, "note", {"a": 1})):
        with pytest.raises(RpcError) as exc:
            call()
        assert exc.value.code == "unauthorized"


# ── post_event ──────────────────────────────────────────────────────────────

def test_post_event_records_a_worker_event_redacted(tmp_path):
    rig, row = started(tmp_path)
    rig.sup.post_event(row, "note", {"text": SECRET_LINE})
    events = [e for e in rig.ms.events(rig.mid) if e.actor == "worker"]
    assert events and "hunter2-planted-secret" not in events[-1].body_json


@pytest.mark.parametrize("kind", ["state", "decision", "artifact", "approval"])
def test_post_event_cannot_forge_core_event_kinds(tmp_path, kind):
    rig, row = started(tmp_path)
    with pytest.raises(RpcError) as exc:
        rig.sup.post_event(row, kind, {"x": 1})
    assert exc.value.code == "bad_params"
    assert not [e for e in rig.ms.events(rig.mid) if e.actor == "worker"]


def test_worker_tool_events_count_against_the_tool_budget(tmp_path):
    rig, row = started(tmp_path, budgets={"tool_calls": 2})
    for _ in range(3):
        rig.sup.post_event(row, "tool", {"tool": "read_file"})
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"
    assert rig.cont.is_empty(rig.runtimes[0].context.session)


def test_worker_event_bytes_count_against_the_output_budget(tmp_path):
    rig, row = started(tmp_path, budgets={"output_bytes": 500})
    rig.sup.post_event(row, "note", {"text": "x" * 2000})
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator"


# ── heartbeat ───────────────────────────────────────────────────────────────

def test_heartbeat_answers_ok_for_a_live_session(tmp_path):
    rig, row = started(tmp_path)
    assert rig.sup.heartbeat(row)["ok"] is True


# ── submit_result ───────────────────────────────────────────────────────────

def test_submitted_result_finishes_the_mission_into_awaiting_decision(tmp_path):
    from planet_express.missions.runtime import AgentResult
    rig, row = started(tmp_path, behaviours=[{"script": [update(), update()]}])
    out = rig.sup.submit_result(row, AgentResult(**{**GOOD_RESULT, "diagnosis": "via the socket"}))
    assert out["accepted"] is True
    rig.sup.poll(rig.mid)
    assert rig.status() == "awaiting_decision"
    stored = next(p for p in (tmp_path / "artifacts" / rig.mid).iterdir() if "result.json" in p.name)
    assert json.loads(stored.read_text())["diagnosis"] == "via the socket"
    assert rig.ms.live_session(rig.mid) is None


def test_a_second_submission_is_refused(tmp_path):
    from planet_express.missions.runtime import AgentResult
    rig, row = started(tmp_path)
    rig.sup.submit_result(row, AgentResult(**GOOD_RESULT))
    with pytest.raises(RpcError) as exc:
        rig.sup.submit_result(row, AgentResult(**GOOD_RESULT))
    assert exc.value.code == "conflict"


# ── memory.search ───────────────────────────────────────────────────────────

def test_memory_search_returns_confirmed_cards_only(tmp_path):
    rig, row = started(tmp_path)
    with rig.store._write() as conn:
        memory_store.upsert_candidate(conn, card("confirmed-one", statement="The NAS share holds media."),
                                      "operator:me")
        memory_store.confirm(conn, "confirmed-one", "operator:me")
        memory_store.upsert_candidate(conn, card("still-candidate", statement="The NAS share is flaky."),
                                      "operator:me")
    found = rig.sup.memory_search(row, "NAS share", 5)
    ids = [c["id"] for c in found["cards"]]
    assert ids == ["confirmed-one"]
    assert found["cards"][0]["status"] == "confirmed"


# ── memory candidates ───────────────────────────────────────────────────────

def test_memory_candidates_become_quarantined_inferred_cards(tmp_path):
    result = {**GOOD_RESULT, "memory_candidates": ["The NAS share is mounted at the media path."]}
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")], "result": result}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert rig.status() == "awaiting_decision"
    cards = candidates(rig)
    assert len(cards) == 1 and cards[0].status == "candidate" and cards[0].source == "inferred"
    assert cards[0].scope == ["stack:media"]
    with rig.store._connect() as conn:
        history = memory_store.history(conn, cards[0].id)
        from planet_express.memory.retrieval import retrieve
        assert retrieve(conn, ["stack:media"], "NAS share mounted").cards == []     # quarantined
    assert history and all(h.actor == f"runtime:{rig.mid}" for h in history)


def test_secret_shaped_and_oversize_candidates_are_dropped_and_recorded(tmp_path):
    result = {**GOOD_RESULT, "memory_candidates": [SECRET_LINE, "ok fact about media", "é" * 3000]}
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")], "result": result}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert rig.status() == "awaiting_decision", "a bad candidate never fails the mission"
    assert [c.statement for c in candidates(rig)] == ["ok fact about media"]
    notes = [e.body_json for e in rig.ms.events(rig.mid)]
    assert any("candidate_dropped" in n for n in notes)
    assert "hunter2-planted-secret" not in rig.dump()


def test_candidates_never_claim_the_operator_as_source(tmp_path):
    result = {**GOOD_RESULT, "memory_candidates": ["a fact"]}
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")], "result": result}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert all(c.source != "operator-stated" for c in candidates(rig))


# ── tick: start queued missions, poll running ones ──────────────────────────

def test_tick_starts_queued_observe_and_propose_missions_and_runs_them_to_the_end(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update(), update("finished")]}])
    for _ in range(4):
        rig.sup.tick()
    assert rig.status() == "awaiting_decision"
    assert len(rig.runtimes) == 1


def test_tick_leaves_mutating_tiers_queued(tmp_path):
    rig = Rig(tmp_path, tier="repair")
    rig.sup.tick()
    assert rig.status() == "queued" and rig.runtimes == []


def test_tick_does_nothing_when_disabled(tmp_path):
    rig = Rig(tmp_path, enabled=False)
    rig.sup.tick()
    assert rig.status() == "queued" and rig.runtimes == []


def test_a_mission_that_cannot_start_repeatedly_is_handed_to_the_operator(tmp_path):
    rig = Rig(tmp_path)

    def broken(session_id):
        raise OSError("no privilege")
    rig.cont.create = broken
    for _ in range(5):
        rig.sup.tick()
    assert rig.status() == "needs_operator"


def test_tick_survives_one_bad_mission(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    other = rig.ms.create_mission(title="t2", origin="operator", grant=rig.mission.grant(), goal="g2",
                                  success_criteria="s", runtime="fake")
    advance_to(rig.ms, other.id, "queued")
    rig.sup.poll_all = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    rig.sup.tick()                 # does not raise
    assert len(rig.runtimes) >= 1


# ── shutdown ────────────────────────────────────────────────────────────────

def test_shutdown_kills_sessions_and_leaves_missions_for_recover(tmp_path):
    rig, row = started(tmp_path)
    cs = rig.runtimes[0].context.session
    rig.sup.shutdown()
    assert rig.cont.is_empty(cs) and not rig.cont.exists(cs.session_id)
    assert rig.ms.live_session(rig.mid) is None            # token revoked
    assert rig.status() == "investigating"                 # recover() decides, not shutdown
    sup2 = rig.new_supervisor()
    assert sup2.recover() == [rig.mid]


def test_shutdown_with_a_stubborn_session_leaves_it_tracked(tmp_path):
    rig, row = started(tmp_path)
    cs = rig.runtimes[0].context.session
    rig.cont.ignore_kill(cs.session_id, 3)
    rig.sup.shutdown()
    assert rig.cont.exists(cs.session_id)                  # not provably dead: not cleaned up


def test_token_fd_env_name_is_pe_session_token_fd(tmp_path):
    rig, row = started(tmp_path)
    env = rig.cont.spawns(rig.runtimes[0].context.session.session_id)[0]["env"]
    assert "PE_SESSION_TOKEN_FD" in env and "PE_TOKEN_FD" not in env
    assert rig.runtimes[0].token not in json.dumps(env)
    assert env["PE_MISSION_ID"] == rig.mid
