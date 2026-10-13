"""S4: the operator RPC surface for missions and Ship's Memory (planet_express/application/mission_service.py)."""
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest

from missions_helpers import make_store, new_mission, repair_grant  # noqa: E402
from planet_express.application.mission_service import MissionService  # noqa: E402
from planet_express.integrations.rpc import RpcError, build_core_handlers  # noqa: E402
from planet_express.memory import store as memstore  # noqa: E402
from planet_express.memory.models import Card  # noqa: E402
from planet_express.missions.store import MissionStore  # noqa: E402

SECRET = "password=hunter2hunter2"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def card(card_id, **kw):
    data = {"id": card_id, "title": "T " + card_id, "type": "fact", "scope": ["stack:media"],
            "statement": "A statement about media.", "source": "operator-stated", "created_at": NOW}
    if kw.get("type") in ("procedure",):
        data["steps"] = [{"text": "Do it.", "check": "It is done."}]
    data.update(kw)
    return Card(**data)


@pytest.fixture
def env(tmp_path):
    store, clock = make_store(tmp_path)
    incidents = {"c" * 12: {"id": "c" * 12, "kind": "container_health", "resource": "media/plex",
                            "summary": "plex is unhealthy", "severity": "HIGH", "status": "open",
                            "events": [{"kind": "observed", "payload": {"note": SECRET}}]}}
    with store._write() as conn:     # missions.incident_id is a foreign key to a real incident row
        conn.execute("INSERT INTO incident_reconciliations VALUES ('scan1', 'ts', 1, 1)")
        conn.execute("INSERT INTO incidents VALUES (?, 'fp', 1, 'container_health', 'media/plex', 'open', "
                     "'failing', 'HIGH', 'plex is unhealthy', '{}', 'scan1', 1, 1, NULL, 1)", ("c" * 12,))
    svc = MissionService(store, enabled=lambda: True, incident_context=incidents.get)
    return svc, store, MissionStore(store), incidents


@pytest.fixture
def handlers(env):
    svc, store, *_ = env
    return build_core_handlers(None, store, missions=svc)


def call(handlers, method, **params):
    return handlers[method](params)


def err(handlers, method, code, **params):
    with pytest.raises(RpcError) as exc:
        handlers[method](params)
    assert exc.value.code == code, (exc.value.code, str(exc.value))
    return exc.value


def add_card(store, c, actor="operator:alice"):
    with store._connect() as conn:
        memstore.upsert_candidate(conn, c, actor, now=NOW)


# ── gating ────────────────────────────────────────────────────────────────────
ALL = {
    "mission.list": {}, "mission.get": {"mission_id": "M-1"}, "mission.events": {"mission_id": "M-1"},
    "mission.create": {"title": "t", "goal": "g", "operator": "alice"},
    "mission.cancel": {"mission_id": "M-1", "operator": "alice"},
    "mission.from_incident": {"incident_id": "c" * 12, "operator": "alice"},
    "memory.candidates": {}, "memory.confirm": {"card_id": "x", "operator": "alice"},
    "memory.deprecate": {"card_id": "x", "operator": "alice"}, "memory.search": {"query": "x"},
    "memory.cards": {}, "memory.review_due": {},
}


@pytest.mark.parametrize("method", sorted(ALL))
def test_every_method_is_disabled_when_the_flag_is_off(tmp_path, method):
    store, _ = make_store(tmp_path)
    h = build_core_handlers(None, store, missions=MissionService(store, enabled=lambda: False))
    err(h, method, "disabled", **ALL[method])


def test_default_service_is_disabled(tmp_path):
    store, _ = make_store(tmp_path)
    h = build_core_handlers(None, store, missions=MissionService(store))
    err(h, "mission.list", "disabled")


def test_no_missions_handlers_without_a_service(tmp_path):
    store, _ = make_store(tmp_path)
    assert not [k for k in build_core_handlers(None, store) if k.startswith(("mission.", "memory."))]


# ── missions ──────────────────────────────────────────────────────────────────
def test_create_get_list_happy_path(handlers):
    made = call(handlers, "mission.create", title="Why is plex slow", goal="Find out", operator="alice")
    assert made["id"] == "M-1" and made["status"] == "queued" and made["tier"] == "propose"
    assert made["origin"] == "operator"
    got = call(handlers, "mission.get", mission_id="M-1")
    assert got["goal"] == "Find out" and got["grant"]["tier"] == "propose"
    assert got["cancel_requested"] is False and got["artifacts"] == []
    listed = call(handlers, "mission.list")
    assert [m["id"] for m in listed["missions"]] == ["M-1"] and listed["total"] == 1
    assert "grant_json" not in listed["missions"][0] and "goal" not in listed["missions"][0]


def test_create_observe_and_incident_origin(handlers):
    m = call(handlers, "mission.create", title="t", goal="g", operator="alice", tier="observe")
    assert m["tier"] == "observe"
    m = call(handlers, "mission.create", title="t", goal="g", operator="alice", incident_id="c" * 12)
    assert m["origin"] == "incident" and m["incident_id"] == "c" * 12


@pytest.mark.parametrize("tier", ["repair", "change", "critical", "root", 1])
def test_create_refuses_tiers_above_propose(handlers, env, tier):
    err(handlers, "mission.create", "forbidden" if tier in ("repair", "change", "critical") else "bad_request",
        title="t", goal="g", operator="alice", tier=tier)
    assert env[2].list_missions() == []


@pytest.mark.parametrize("params", [
    {"goal": "g", "operator": "alice"},                                   # no title
    {"title": "", "goal": "g", "operator": "alice"},
    {"title": "t" * 257, "goal": "g", "operator": "alice"},
    {"title": "t", "goal": "g" * 9000, "operator": "alice"},
    {"title": "t", "goal": "g", "operator": "alice", "origin": "maintenance"},
    {"title": "t", "goal": "g", "operator": "alice", "origin": "incident"},   # incident origin needs an id
    {"title": "t", "goal": "g", "operator": "alice", "grant": {"tier": "repair"}},
    {"title": "t", "goal": "g", "operator": "alice", "incident_id": "nope"},
    {"title": "t", "goal": "g", "operator": "alice", "scope": {"stacks": "media"}},
    {"title": "t", "goal": "g"},                                          # no operator
    "not a dict", None,
])
def test_create_rejects_bad_params(handlers, env, params):
    with pytest.raises(RpcError) as exc:
        handlers["mission.create"](params)
    assert exc.value.code == "bad_request"
    assert env[2].list_missions() == []


def test_create_with_scope_builds_a_propose_grant(handlers):
    m = call(handlers, "mission.create", title="t", goal="g", operator="alice",
             scope={"stacks": ["media"], "services": ["plex"]})
    g = call(handlers, "mission.get", mission_id=m["id"])["grant"]
    assert g["scope"]["stacks"] == ["media"] and g["scope"]["services"] == ["plex"]


def test_get_unknown_and_malformed_ids(handlers):
    err(handlers, "mission.get", "not_found", mission_id="M-99")
    err(handlers, "mission.get", "bad_request", mission_id="x; DROP")
    err(handlers, "mission.get", "bad_request", mission_id="M-1", extra=1)


def test_list_pagination_bounds_and_filter(handlers, env):
    for i in range(5):
        call(handlers, "mission.create", title=f"t{i}", goal="g", operator="alice")
    page = call(handlers, "mission.list", limit=2, offset=0)
    assert len(page["missions"]) == 2 and page["total"] == 5 and page["limit"] == 2
    page2 = call(handlers, "mission.list", limit=2, offset=4)
    assert len(page2["missions"]) == 1
    assert [m["id"] for m in call(handlers, "mission.list", limit=100)["missions"]][0] == "M-5"
    assert call(handlers, "mission.list", status="draft")["total"] == 0
    assert call(handlers, "mission.list", status="queued")["total"] == 5
    for bad in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"}, {"offset": -1},
                {"offset": 10**9}, {"status": "weird"}, {"status": 5}, {"bogus": 1}):
        err(handlers, "mission.list", "bad_request", **bad)


def test_events_are_paged(handlers):
    call(handlers, "mission.create", title="t", goal="g", operator="alice")
    first = call(handlers, "mission.events", mission_id="M-1", limit=1)
    assert len(first["events"]) == 1 and first["has_more"] is True
    assert first["events"][0]["kind"] == "state" and isinstance(first["events"][0]["body"], dict)
    nxt = call(handlers, "mission.events", mission_id="M-1", after_seq=first["next_after_seq"], limit=200)
    assert nxt["events"] and nxt["has_more"] is False
    assert nxt["events"][0]["seq"] > first["events"][0]["seq"]
    for bad in ({"limit": 0}, {"limit": 201}, {"after_seq": -1}, {"after_seq": "1"}):
        err(handlers, "mission.events", "bad_request", mission_id="M-1", **bad)
    err(handlers, "mission.events", "not_found", mission_id="M-9")


def test_output_is_redacted_and_bounded(handlers, env):
    svc, store, ms, _ = env
    m = ms.create_mission(title=f"t {SECRET}", origin="operator", grant=repair_grant(tier="propose"),
                          goal=f"look at {SECRET} " + "x" * 8000, success_criteria="s", runtime="command")
    ms.append_event(m.id, "note", "core", {"text": SECRET, "big": "y" * 50000})
    got = call(handlers, "mission.get", mission_id=m.id)
    ev = call(handlers, "mission.events", mission_id=m.id)
    listed = call(handlers, "mission.list")
    blob = repr((got, ev, listed))
    assert "hunter2hunter2" not in blob
    assert len(got["goal"]) <= 4096 + 40
    assert all(len(repr(e)) < 20000 for e in ev["events"])
    # a secret stored straight into a row (bypassing the redacting writers) is still masked on the way out
    with store._write() as conn:
        conn.execute("UPDATE missions SET outcome=? WHERE id=?", (SECRET, m.id))
    assert "hunter2hunter2" not in repr(call(handlers, "mission.get", mission_id=m.id))


def test_get_never_returns_session_secrets(handlers, env):
    svc, store, ms, _ = env
    call(handlers, "mission.create", title="t", goal="g", operator="alice")
    ms.start_session("M-1", kind="local_uid", token_hash="a" * 64, session_ttl_s=60, lease_ttl_s=60,
                     session_uid=12345)
    got = call(handlers, "mission.get", mission_id="M-1")
    assert got["session"]["kind"] == "local_uid"
    assert "a" * 64 not in repr(got) and "12345" not in repr(got)


def test_cancel_sets_the_flag_and_is_idempotent(handlers, env):
    svc, store, ms, _ = env
    m = ms.create_mission(title="t", origin="operator", grant=repair_grant(tier="propose"), goal="g",
                          success_criteria="s", runtime="command")
    from planet_express.missions.state import transition
    transition(store, m.id, "queued", "core")
    transition(store, m.id, "investigating", "core")
    out = call(handlers, "mission.cancel", mission_id=m.id, operator="alice")
    assert out["cancel_requested"] is True and out["status"] == "investigating"
    mm = ms.get_mission(m.id)
    assert mm.cancel_requested_by == "operator:alice" and mm.cancel_requested_at is not None
    again = call(handlers, "mission.cancel", mission_id=m.id, operator="bob")
    assert ms.get_mission(m.id).cancel_requested_by == "operator:alice" and again["cancel_requested"]
    assert [e for e in ms.events(m.id) if e.actor == "operator:alice" and "cancel_requested" in e.body_json]


def test_cancel_of_a_not_yet_started_mission_cancels_it(handlers, env):
    call(handlers, "mission.create", title="t", goal="g", operator="alice")
    out = call(handlers, "mission.cancel", mission_id="M-1", operator="alice")
    assert out["status"] == "cancelled"


def test_cancel_refuses_closed_unknown_and_bad(handlers, env):
    call(handlers, "mission.create", title="t", goal="g", operator="alice")
    call(handlers, "mission.cancel", mission_id="M-1", operator="alice")
    err(handlers, "mission.cancel", "conflict", mission_id="M-1", operator="alice")
    err(handlers, "mission.cancel", "not_found", mission_id="M-9", operator="alice")
    err(handlers, "mission.cancel", "bad_request", mission_id="M-1")
    err(handlers, "mission.cancel", "bad_request", mission_id="M-1", operator="alice", actor="core")


# ── operator identity ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("operator", ["runtime:s1", "operator:alice", "?", "", "A B", "x" * 33, 5, None,
                                      "system:scan"])
def test_operator_must_be_a_plain_dashboard_identity(handlers, operator):
    for method, params in (("mission.create", {"title": "t", "goal": "g"}),
                           ("memory.confirm", {"card_id": "x"})):
        err(handlers, method, "bad_request", operator=operator, **params)


def test_actor_is_never_taken_from_params(handlers, env):
    svc, store, ms, _ = env
    err(handlers, "mission.create", "bad_request", title="t", goal="g", operator="alice", actor="runtime:x")
    call(handlers, "mission.create", title="t", goal="g", operator="alice")
    assert {e.actor for e in ms.events("M-1")} == {"operator:alice"}


# ── incident -> mission ───────────────────────────────────────────────────────
def test_from_incident_seeds_a_propose_mission_with_redacted_evidence(handlers, env):
    svc, store, ms, incidents = env
    out = call(handlers, "mission.from_incident", incident_id="c" * 12, operator="alice")
    assert out["created"] is True
    m = ms.get_mission(out["mission"]["id"])
    assert m.tier == "propose" and m.origin == "incident" and m.incident_id == "c" * 12 and m.status == "queued"
    assert "plex is unhealthy" in m.goal and "media/plex" in m.goal
    blob = repr([e.body_json for e in ms.events(m.id)]) + m.goal
    assert "hunter2hunter2" not in blob
    assert any("incident_evidence" in e.body_json for e in ms.events(m.id))


def test_from_incident_reuses_an_open_mission_and_handles_unknown(handlers, env):
    first = call(handlers, "mission.from_incident", incident_id="c" * 12, operator="alice")
    second = call(handlers, "mission.from_incident", incident_id="c" * 12, operator="alice")
    assert second["created"] is False and second["mission"]["id"] == first["mission"]["id"]
    err(handlers, "mission.from_incident", "not_found", incident_id="d" * 12, operator="alice")
    err(handlers, "mission.from_incident", "bad_request", incident_id="zzz", operator="alice")


def test_from_incident_without_a_context_source_is_unavailable(tmp_path):
    store, _ = make_store(tmp_path)
    h = build_core_handlers(None, store, missions=MissionService(store, enabled=lambda: True))
    err(h, "mission.from_incident", "unavailable", incident_id="c" * 12, operator="alice")


# ── memory ────────────────────────────────────────────────────────────────────
def test_candidates_confirm_and_search(handlers, env):
    svc, store, *_ = env
    add_card(store, card("fact-one", statement="plex needs the nas mounted"))
    add_card(store, card("proc-one", type="procedure"))
    c = call(handlers, "memory.candidates")
    assert {x["id"] for x in c["cards"]} == {"fact-one", "proc-one"} and c["total"] == 2
    # candidates never reach search
    assert call(handlers, "memory.search", query="plex nas")["cards"] == []
    out = call(handlers, "memory.confirm", card_id="proc-one", operator="alice")
    assert out["status"] == "confirmed"
    assert [x["id"] for x in call(handlers, "memory.candidates")["cards"]] == ["fact-one"]
    call(handlers, "memory.confirm", card_id="fact-one", operator="alice")
    hits = call(handlers, "memory.search", query="plex nas")
    assert [x["id"] for x in hits["cards"]] == ["fact-one"]
    by_scope = call(handlers, "memory.search", query="", scope=["stack:media"])["cards"]
    assert {x["id"] for x in by_scope} == {"fact-one", "proc-one"}
    with store._connect() as conn:
        assert {h.actor for h in memstore.history(conn, "proc-one") if h.action == "confirm"} == {"operator:alice"}
    assert {x["id"] for x in call(handlers, "memory.cards")["cards"]} == {"fact-one", "proc-one"}


def test_confirm_of_constraint_needs_an_operator_and_other_actors_cannot_reach_it(handlers, env):
    svc, store, *_ = env
    add_card(store, card("c-one", type="constraint", source="inferred"), actor="runtime:s1")
    err(handlers, "memory.confirm", "bad_request", card_id="c-one", operator="alice", actor="runtime:s1")
    err(handlers, "memory.confirm", "bad_request", card_id="c-one", operator="runtime:s1")
    assert call(handlers, "memory.candidates")["total"] == 1
    # the store itself still refuses a non-operator for a constraint, which the service never passes
    with store._connect() as conn:
        with pytest.raises(memstore.ActorNotPermitted):
            memstore.confirm(conn, "c-one", "runtime:s1")
    assert call(handlers, "memory.confirm", card_id="c-one", operator="alice")["status"] == "confirmed"


def test_confirm_errors_are_typed(handlers, env):
    svc, store, *_ = env
    err(handlers, "memory.confirm", "not_found", card_id="ghost", operator="alice")
    add_card(store, card("a-one"))
    call(handlers, "memory.confirm", card_id="a-one", operator="alice")
    err(handlers, "memory.confirm", "conflict", card_id="a-one", operator="alice")
    err(handlers, "memory.confirm", "bad_request", card_id="Bad Id!", operator="alice")


def test_deprecate(handlers, env):
    svc, store, *_ = env
    add_card(store, card("a-one"))
    call(handlers, "memory.confirm", card_id="a-one", operator="alice")
    out = call(handlers, "memory.deprecate", card_id="a-one", operator="alice", reason="obsolete")
    assert out["status"] == "deprecated"
    assert call(handlers, "memory.search", query="statement media")["cards"] == []
    err(handlers, "memory.deprecate", "conflict", card_id="a-one", operator="alice")
    err(handlers, "memory.deprecate", "not_found", card_id="ghost", operator="alice")
    err(handlers, "memory.deprecate", "bad_request", card_id="a-one", operator="alice", reason="r" * 600)
    with store._connect() as conn:
        assert [h.actor for h in memstore.history(conn, "a-one") if h.action == "deprecate"] == ["operator:alice"]


def test_memory_pagination_bounds_and_search_params(handlers, env):
    svc, store, *_ = env
    for i in range(5):
        add_card(store, card(f"card-{i}"))
    assert len(call(handlers, "memory.candidates", limit=2)["cards"]) == 2
    assert len(call(handlers, "memory.candidates", limit=2, offset=4)["cards"]) == 1
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}):
        err(handlers, "memory.candidates", "bad_request", **bad)
    for bad in ({"query": 5}, {"query": "x" * 501}, {"query": "x", "limit": 21}, {"query": "x", "limit": 0},
                {"query": "x", "scope": "stack:media"}, {"query": "x", "scope": ["a"] * 17}, {}):
        err(handlers, "memory.search", "bad_request", **bad)


def test_review_due(handlers, env):
    svc, store, *_ = env
    add_card(store, card("old-one"))
    with store._connect() as conn:
        memstore.confirm(conn, "old-one", "operator:alice", now=datetime(2020, 1, 1, tzinfo=UTC))
    assert [c["id"] for c in call(handlers, "memory.review_due")["cards"]] == ["old-one"]


def test_memory_output_is_redacted(handlers, env):
    svc, store, *_ = env
    add_card(store, card("a-one"))
    with store._write() as conn:     # a secret that got into a row some other way
        conn.execute("UPDATE memory_cards SET statement=? WHERE id='a-one'", ("see " + SECRET,))
    assert "hunter2hunter2" not in repr(call(handlers, "memory.candidates"))


def test_memory_unavailable_without_fts_tables(handlers, env):
    svc, store, *_ = env
    with store._write() as conn:
        conn.execute("DROP TABLE memory_history")
        conn.execute("DROP TABLE memory_edges")
        conn.execute("DROP TABLE memory_cards")
    err(handlers, "memory.candidates", "unavailable")


# ── transport bound: a response must always fit the RPC frame ─────────────────
def test_event_pages_are_cut_to_the_frame_budget_and_still_page_through(handlers, env):
    import json
    svc, store, ms, _ = env
    call(handlers, "mission.create", title="t", goal="g", operator="alice")
    for i in range(130):
        ms.append_event("M-1", "note", "core", {"i": i, "blob": "\u00e9" * 1500})   # escapes to 6 bytes each
    seen, after = [], 0
    for _ in range(100):
        page = call(handlers, "mission.events", mission_id="M-1", after_seq=after, limit=200)
        assert len(json.dumps(page)) < 1024 * 1024
        seen += [e["seq"] for e in page["events"]]
        after = page["next_after_seq"]
        if not page["has_more"]:
            break
    assert len(seen) == len(set(seen)) == 132        # 130 notes + the two creation events, none skipped or repeated
    assert seen == sorted(seen)


def test_card_pages_are_cut_to_the_frame_budget(handlers, env):
    import json
    svc, store, *_ = env
    for i in range(30):
        steps = [{"text": "t" * 990, "check": "c" * 490} for _ in range(20)]
        add_card(store, card(f"big-{i:03d}", type="procedure", steps=steps))
    page = call(handlers, "memory.candidates", limit=100)
    assert len(json.dumps(page)) < 1024 * 1024
    assert page["truncated"] is True and 0 < len(page["cards"]) < 30 and page["total"] == 30
    nxt = call(handlers, "memory.candidates", limit=100, offset=len(page["cards"]))
    assert nxt["cards"][0]["id"] == f"big-{len(page['cards']):03d}"


def test_create_for_an_unknown_incident_is_not_found_not_internal(handlers, env):
    err(handlers, "mission.create", "not_found", title="t", goal="g", operator="alice", incident_id="d" * 12)
    assert env[2].list_missions() == []


def test_concurrent_confirms_have_exactly_one_winner(handlers, env):
    import threading
    svc, store, *_ = env
    add_card(store, card("race-one"))
    barrier, results = threading.Barrier(6), []

    def go(name):
        barrier.wait()
        try:
            call(handlers, "memory.confirm", card_id="race-one", operator=name)
            results.append("ok")
        except RpcError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=go, args=(f"op{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["conflict"] * 5 + ["ok"]
    with store._connect() as conn:
        assert len([h for h in memstore.history(conn, "race-one") if h.action == "confirm"]) == 1
