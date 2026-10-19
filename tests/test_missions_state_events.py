import inspect
import itertools
import sqlite3

import pytest
from missions_helpers import make_store, new_mission

from planet_express.missions import events as events_module, store as store_module
from planet_express.missions.approvals import record_approval
from planet_express.missions.models import Grant
from planet_express.missions.state import (
    NON_TERMINAL,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    is_legal,
    transition,
)
from planet_express.missions.store import MissionStore

STATES = sorted(NON_TERMINAL | TERMINAL)


def force_status(store, mission_id, status):
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE missions SET status=? WHERE id=?", (status, mission_id))


def approve_all(store, m):
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")


def test_table_covers_contract_edges():
    must = {("draft", "queued"), ("queued", "investigating"), ("investigating", "awaiting_decision"),
            ("awaiting_decision", "executing"), ("executing", "verifying"), ("verifying", "completed"),
            ("investigating", "completed"), ("awaiting_decision", "completed"),
            ("executing", "paused_tripwire"), ("paused_tripwire", "executing"),
            ("paused_tripwire", "rolled_back"), ("paused_tripwire", "needs_operator"),
            ("executing", "rollback_pending"), ("verifying", "rollback_pending"),
            ("verifying", "needs_operator"), ("rollback_pending", "rolled_back"),
            ("rollback_pending", "needs_operator"), ("blocked", "queued"), ("blocked", "awaiting_decision"),
            ("blocked", "cancelled"), ("paused_tripwire", "rollback_pending"),
            ("recovery_required", "rollback_pending")}
    must |= {("recovery_required", t) for t in
             ("investigating", "executing", "verifying", "rolled_back", "needs_operator")}
    must |= {(s, t) for s in NON_TERMINAL for t in ("cancelled", "blocked", "needs_operator", "recovery_required")
             if s != t}
    for edge in must:
        assert is_legal(*edge), edge


def test_terminal_states_have_no_exits_and_no_self_edges():
    for s in TERMINAL:
        assert TRANSITIONS[s] == frozenset()
    for s in STATES:
        assert s not in TRANSITIONS[s]


@pytest.mark.parametrize(("frm", "to"), [(a, b) for a, b in itertools.product(STATES, STATES) if a != b])
def test_every_edge_matches_table(tmp_path, frm, to):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, Grant.model_validate({"tier": "repair"}))
    approve_all(store, m)
    force_status(store, m.id, frm)
    legal = is_legal(frm, to)
    if legal and to == "completed":
        # In the table, but a repair mission completes only on a passed verification result (slice 8): none here.
        with pytest.raises(IllegalTransition, match="completion refused"):
            transition(store, m.id, to, "operator:me")
        assert ms.get_mission(m.id).status == frm
    elif legal:
        transition(store, m.id, to, "operator:me")
        assert ms.get_mission(m.id).status == to
        last = ms.events(m.id)[-1]
        assert last.kind == "state" and '"to": "%s"' % to in last.body_json
        assert (ms.get_mission(m.id).closed_at is not None) == (to in TERMINAL)
    else:
        with pytest.raises(IllegalTransition):
            transition(store, m.id, to, "operator:me")
        assert ms.get_mission(m.id).status == frm
        last = ms.events(m.id)[-1]
        assert '"refused": true' in last.body_json and to in last.body_json


def test_needs_operator_exit_requires_operator_actor(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    force_status(store, m.id, "needs_operator")
    with pytest.raises(IllegalTransition):
        transition(store, m.id, "investigating", "core")
    transition(store, m.id, "investigating", "operator:me")


@pytest.mark.parametrize("tier", ["observe", "propose"])
def test_observe_and_propose_never_reach_executing(tmp_path, tier):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, Grant.model_validate({"tier": tier}))
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    for frm in ("awaiting_decision", "paused_tripwire", "recovery_required", "needs_operator"):
        force_status(store, m.id, frm)
        for to in ("executing", "verifying", "rollback_pending"):
            if is_legal(frm, to):
                with pytest.raises(IllegalTransition):
                    transition(store, m.id, to, "operator:me")
    assert ms.get_mission(m.id).status != "executing"


def test_entering_executing_needs_the_predicate(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    force_status(store, m.id, "awaiting_decision")
    with pytest.raises(IllegalTransition, match="predicate"):
        transition(store, m.id, "executing", "core")
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    transition(store, m.id, "executing", "core")


# ── events ──────────────────────────────────────────────────────────────────
def test_planted_secrets_never_reach_the_table(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    ms.append_event(m.id, "tool", "worker", {
        "stdout": "starting\nAPI_KEY=sk-live-PLANTED-1\ndone",
        "env": {"DB_PASSWORD": "PLANTED-2", "plain": "fine", "TOKEN": ["PLANTED-3"]},
        "nested": [{"Authorization": "Bearer PLANTED-4"}, "https://user:PLANTED-5@host/x"],
    })
    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(r[0] for r in conn.execute("SELECT body_json FROM mission_events"))
    for n in range(1, 6):
        assert f"PLANTED-{n}" not in dump
    assert "fine" in dump and "[REDACTED]" in dump
    assert ms.events(m.id)[-1].redaction_applied == 1


def test_event_api_is_insert_only():
    for module in (events_module, store_module):
        src = inspect.getsource(module)
        assert "UPDATE mission_events" not in src and "DELETE FROM mission_events" not in src
    public = {n for n in dir(MissionStore) if not n.startswith("_")}
    assert not {n for n in public if "event" in n} - {"append_event", "events"}
    import pathlib

    import planet_express.missions as pkg
    for path in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        text = path.read_text()
        assert "UPDATE mission_events" not in text and "DELETE FROM mission_events" not in text, path
