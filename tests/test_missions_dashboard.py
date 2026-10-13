"""S4: the dashboard's Missions and Memory pages, and the incident "Investigate" action."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest

import casa_scruffy
import config
import web_auth
from test_scruffy_routes import ENV, PASSPHRASE, SECRET, csrf, login  # noqa: F401

INC = "c" * 12


def mission(mid, title, status="investigating", tier="propose", **kw):
    return {"id": mid, "title": title, "status": status, "tier": tier, "origin": "operator",
            "incident_id": None, "priority": 50, "runtime": "command", "created_at": 1, "updated_at": 2,
            "closed_at": None, "cancel_requested": False, "outcome": None, **kw}


def detail(m, **kw):
    return {**m, "goal": "Find <b>why</b>", "success_criteria": "a diagnosis", "grant": {"tier": "propose"},
            "grant_hash": "0" * 64, "plan": None, "plan_hash": None, "verification_plan": None,
            "verification_result": None, "outcome_flags": None, "cancel_requested_by": None,
            "cancel_requested_at": None, "result": {"outcome": "done", "flags": None, "verification": None},
            "session": None,
            "artifacts": [{"seq": 5, "at": 9, "kind": "artifact", "actor": "worker", "body": {"name": "report.md"}}],
            **kw}


def card(cid, status="candidate", ctype="fact"):
    return {"id": cid, "title": f"Title {cid}", "type": ctype, "scope": ["stack:media"], "statement": "S <i>x</i>",
            "steps": [], "evidence": [], "source": "inferred", "status": status, "created_at": "2026-10-13T12:00:00Z",
            "last_verified_at": None, "review_after": None, "supersedes": [], "conflicts_with": []}


class Core:
    """Stands in for the core socket: records calls, answers from `results`, can answer an error code."""

    def __init__(self):
        self.calls = []
        self.results = {
            "mission.list": self.list_pool,
            "mission.get": detail(mission("M-2", "Why slow")),
            "mission.events": {"events": [{"seq": 1, "at": 1, "kind": "state", "actor": "operator:alice",
                                           "body": {"from": None, "to": "draft"}}],
                               "has_more": True, "next_after_seq": 1},
            "mission.cancel": mission("M-2", "x", cancel_requested=True),
            "mission.from_incident": {"created": True, "mission": mission("M-7", "Investigate: plex")},
            "memory.candidates": {"cards": [card("cand-one"), card("cand-two", ctype="constraint")], "total": 2,
                                  "limit": 50, "offset": 0},
            "memory.cards": {"cards": [card("conf-one", "confirmed")], "total": 1, "limit": 50, "offset": 0},
            "memory.review_due": {"cards": [card("due-one", "confirmed")], "total": 1, "limit": 50, "offset": 0},
            "memory.confirm": card("cand-one", "confirmed"),
            "memory.deprecate": card("conf-one", "deprecated"),
            "canary.candidates": [], "proposal.list_pending": [],
        }
        self.errors = {}
        self.pool = [mission("M-2", "Why is <script>x</script> slow"),
                     mission("M-1", "Old one", status="completed")]

    def list_pool(self, params):
        """mission.list as core answers it: scope active/closed, newest first, bounded by limit."""
        closed = {"completed", "failed", "rolled_back", "cancelled"}
        rows = [m for m in self.pool if (m["status"] in closed) == (params.get("scope") == "closed")]
        return {"missions": rows[:params["limit"]], "total": len(rows), "limit": params["limit"], "offset": 0,
                "truncated": False}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method in self.errors:
            return {"ok": False, "error": {"code": self.errors[method], "message": "x"}}
        if method.startswith("auth."):
            return {"ok": True, "result": {"locked": False, "remaining_attempts": 2, "locked_until": None,
                                           "accepted": True, "epoch": 0, "sent": True}}
        result = self.results[method]
        return {"ok": True, "result": result(params) if callable(result) else result}

    def methods(self):
        return [m for m, _ in self.calls if not m.startswith("auth.")]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(config, "MISSIONS_ENABLED", True, raising=False)
    core = Core()
    now = [1800000000.0]
    app = casa_scruffy.create_app(ENV, rpc_call=core, clock=lambda: now[0])
    app.testing = True
    client = app.test_client()
    assert login(client, now).status_code == 302
    return client, core, now


def token(client):
    client.get("/missions")
    with client.session_transaction() as s:
        return s["csrf_token"]


# ── auth and CSRF ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/missions", "/missions/M-2", "/memory"])
def test_pages_require_auth(path):
    core = Core()
    app = casa_scruffy.create_app(ENV, rpc_call=core, clock=lambda: 1800000000.0)
    app.testing = True
    r = app.test_client().get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert core.methods() == []


@pytest.mark.parametrize("path", ["/missions/M-2/cancel", "/memory/cand-one/confirm", "/memory/cand-one/deprecate"])
def test_posts_need_csrf_and_auth(web, path):
    client, core, now = web
    assert client.post(path).status_code == 400
    assert client.post(path, data={"csrf_token": "wrong"}).status_code == 400
    assert core.methods() == []
    anon = casa_scruffy.create_app(ENV, rpc_call=Core(), clock=lambda: now[0]).test_client()
    t = csrf(anon)
    r = anon.post(path, data={"csrf_token": t})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_investigate_api_needs_csrf_and_auth(web):
    client, core, now = web
    assert client.post(f"/api/incidents/{INC}/investigate").status_code == 400
    assert "mission.from_incident" not in core.methods()
    anon = casa_scruffy.create_app(ENV, rpc_call=Core(), clock=lambda: now[0]).test_client()
    assert anon.post(f"/api/incidents/{INC}/investigate").status_code == 401


# ── Missions ──────────────────────────────────────────────────────────────────
def test_board_renders_groups_and_escapes(web):
    client, core, _ = web
    r = client.get("/missions")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and core.methods() == ["mission.list", "mission.list"]
    assert [p["scope"] for m, p in core.calls if m == "mission.list"] == ["active", "closed"]
    assert "Why is &lt;script&gt;x&lt;/script&gt; slow" in page and "<script>x</script>" not in page
    assert "INVESTIGATING" in page.upper() and "COMPLETED" in page.upper()
    assert 'href="/missions/M-2"' in page
    assert 'name="viewport"' in page and "missions.css" in page


def test_board_empty_state(web):
    client, core, _ = web
    core.pool = []
    assert "No missions yet" in client.get("/missions").get_data(as_text=True)


def test_detail_shows_events_artifacts_result_and_cancel(web):
    client, core, _ = web
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert [m for m in core.methods()] == ["mission.get", "mission.events"]
    assert "Find &lt;b&gt;why&lt;/b&gt;" in page and "<b>why</b>" not in page
    assert "report.md" in page and "operator:alice" in page and "done" in page
    assert 'action="/missions/M-2/cancel"' in page and 'name="csrf_token"' in page
    assert "after=1" in page            # the stream is paged
    assert "view=latest" in page        # and the newest events are one click away


def test_detail_of_a_closed_mission_has_no_cancel(web):
    client, core, _ = web
    core.results["mission.get"] = detail(mission("M-1", "Old", status="completed"))
    assert "/cancel" not in client.get("/missions/M-1").get_data(as_text=True)


def test_detail_paging_and_bad_ids(web):
    client, core, _ = web
    client.get("/missions/M-2?after=7")
    assert ("mission.events", {"mission_id": "M-2", "after_seq": 7, "limit": 100}) in core.calls
    for bad in ("/missions/x", "/missions/M-2?after=-1", "/missions/M-2?after=zz"):
        n = len(core.calls)
        assert client.get(bad).status_code in (400, 404)
        assert len(core.calls) == n


def test_detail_not_found(web):
    client, core, _ = web
    core.errors["mission.get"] = "not_found"
    assert client.get("/missions/M-9").status_code == 404


def test_cancel_uses_the_session_operator_never_the_form(web):
    client, core, _ = web
    r = client.post("/missions/M-2/cancel", data={"csrf_token": token(client), "operator": "bob"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/missions/M-2?notice=cancel_requested")
    assert ("mission.cancel", {"mission_id": "M-2", "operator": "alice"}) in core.calls
    assert not any(p.get("operator") == "bob" for _, p in core.calls)


def test_cancel_refusal_is_shown_not_raised(web):
    client, core, _ = web
    core.errors["mission.cancel"] = "conflict"
    r = client.post("/missions/M-2/cancel", data={"csrf_token": token(client)})
    assert r.status_code == 302 and r.headers["Location"].endswith("notice=conflict")
    assert "already closed" in client.get(r.headers["Location"]).get_data(as_text=True)


def test_cancel_bad_id_never_reaches_core(web):
    client, core, _ = web
    r = client.post("/missions/bad/cancel", data={"csrf_token": token(client)})
    assert r.status_code in (400, 404) and "mission.cancel" not in core.methods()


def test_missions_disabled_state(web):
    client, core, _ = web
    core.errors["mission.list"] = "disabled"
    core.errors["mission.get"] = "disabled"
    for path in ("/missions", "/missions/M-2"):
        r = client.get(path)
        assert r.status_code == 200 and "Missions are disabled" in r.get_data(as_text=True)
    core.errors["mission.cancel"] = "disabled"
    r = client.post("/missions/M-2/cancel", data={"csrf_token": token(client)})
    assert r.status_code == 302


def test_core_down_is_a_503_not_a_crash(web):
    client, core, _ = web
    core.errors["mission.list"] = "internal"
    assert client.get("/missions").status_code == 503


def test_nav_links_follow_the_flag(web, monkeypatch):
    client, core, _ = web
    page = client.get("/missions").get_data(as_text=True)
    assert 'href="/missions"' in page and 'href="/memory"' in page
    monkeypatch.setattr(config, "MISSIONS_ENABLED", False)
    page = client.get("/missions").get_data(as_text=True)
    assert 'href="/memory"' not in page


# ── Memory ────────────────────────────────────────────────────────────────────
def test_memory_page_sections(web):
    client, core, _ = web
    page = client.get("/memory").get_data(as_text=True)
    assert core.methods() == ["memory.candidates", "memory.cards", "memory.review_due"]
    assert "CANDIDATES" in page.upper() and "REVIEW DUE" in page.upper() and "CONFIRMED" in page.upper()
    for cid in ("cand-one", "cand-two", "conf-one", "due-one"):
        assert cid in page
    assert 'action="/memory/cand-one/confirm"' in page and 'action="/memory/conf-one/deprecate"' in page
    assert "S &lt;i&gt;x&lt;/i&gt;" in page and "<i>x</i>" not in page
    assert page.count('name="csrf_token"') >= 5 and 'name="viewport"' in page
    assert 'name="operator"' not in page and 'name="actor"' not in page


def test_memory_confirm_and_deprecate_use_session_operator(web):
    client, core, _ = web
    t = token(client)
    r = client.post("/memory/cand-one/confirm", data={"csrf_token": t, "operator": "bob", "actor": "runtime:x"})
    assert r.status_code == 302 and "notice=confirmed" in r.headers["Location"]
    assert ("memory.confirm", {"card_id": "cand-one", "operator": "alice"}) in core.calls
    r = client.post("/memory/conf-one/deprecate", data={"csrf_token": t, "reason": "stale"})
    assert "notice=deprecated" in r.headers["Location"]
    assert ("memory.deprecate", {"card_id": "conf-one", "operator": "alice", "reason": "stale"}) in core.calls
    assert not any("actor" in p for _, p in core.calls)


@pytest.mark.parametrize("code", ["conflict", "forbidden", "not_found", "disabled", "unavailable"])
def test_memory_refusals_redirect_with_a_fixed_notice(web, code):
    client, core, _ = web
    core.errors["memory.confirm"] = code
    r = client.post("/memory/cand-one/confirm", data={"csrf_token": token(client)})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/memory?notice={code}")
    assert client.get(r.headers["Location"]).status_code == 200


def test_memory_notice_is_never_reflected(web):
    client, _, _ = web
    page = client.get("/memory?notice=<script>alert(1)</script>").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page


def test_memory_bad_card_id_never_reaches_core(web):
    client, core, _ = web
    r = client.post("/memory/Bad Id/confirm", data={"csrf_token": token(client)})
    assert r.status_code in (400, 404) and "memory.confirm" not in core.methods()


def test_memory_disabled_state(web):
    client, core, _ = web
    core.errors["memory.candidates"] = "disabled"
    r = client.get("/memory")
    assert r.status_code == 200 and "Missions are disabled" in r.get_data(as_text=True)


def test_memory_unavailable_state(web):
    client, core, _ = web
    core.errors["memory.candidates"] = "unavailable"
    r = client.get("/memory")
    assert r.status_code == 200 and "unavailable" in r.get_data(as_text=True).lower()


# ── incident -> mission ───────────────────────────────────────────────────────
def test_investigate_creates_a_mission_as_the_session_operator(web):
    client, core, _ = web
    r = client.post(f"/api/incidents/{INC}/investigate", data={"csrf_token": token(client), "operator": "bob"})
    assert r.status_code == 200 and r.get_json()["mission"]["id"] == "M-7"
    assert ("mission.from_incident", {"incident_id": INC, "operator": "alice"}) in core.calls


def test_investigate_validates_the_id_and_maps_errors(web):
    client, core, _ = web
    t = token(client)
    assert client.post("/api/incidents/short/investigate", data={"csrf_token": t}).status_code == 400
    assert "mission.from_incident" not in core.methods()
    for code, status in (("disabled", 404), ("not_found", 404), ("bad_request", 400), ("timeout", 503)):
        core.errors["mission.from_incident"] = code
        r = client.post(f"/api/incidents/{INC}/investigate", data={"csrf_token": t})
        assert r.status_code == status and set(r.get_json()) == {"error"}


def test_incidents_js_offers_investigate():
    js = (Path(__file__).resolve().parent.parent / "static" / "incidents.js").read_text()
    assert "/investigate" in js and "INVESTIGATE" in js


# ── round 2 ───────────────────────────────────────────────────────────────────
def test_board_shows_old_open_missions_beside_many_closed_ones(web):
    client, core, _ = web
    core.pool = [mission("M-900", "Ancient but actionable", status="awaiting_decision")] + [
        mission(f"M-{i}", f"done {i}", status="completed") for i in range(1, 300)]
    page = client.get("/missions").get_data(as_text=True)
    assert "Ancient but actionable" in page
    active = [p for m, p in core.calls if m == "mission.list" and p["scope"] == "active"][0]
    closed = [p for m, p in core.calls if m == "mission.list" and p["scope"] == "closed"][0]
    assert active["limit"] == 500 and closed["limit"] <= 100
    assert "most recently closed" in page            # the truncation note is about the closed part only


def test_no_truncation_note_when_everything_fits(web):
    client, _, _ = web
    assert "most recently closed" not in client.get("/missions").get_data(as_text=True)


def test_a_status_the_page_has_never_heard_of_still_gets_a_column(web):
    client, core, _ = web
    core.pool = [mission("M-5", "Brand new state", status="brand_new_status")]
    page = client.get("/missions").get_data(as_text=True)
    assert "Brand new state" in page and "BRAND NEW STATUS" in page


def test_board_columns_follow_the_model_statuses():
    from typing import get_args
    from planet_express.application import mission_service
    from planet_express.missions.models import Status
    assert set(mission_service.STATUS_ORDER) == set(get_args(Status))
    assert casa_scruffy.MISSION_ID_RE is mission_service.MISSION_ID_RE
    assert casa_scruffy.TERMINAL_STATUSES is mission_service.TERMINAL_STATUSES


def test_detail_latest_view_uses_tail(web):
    client, core, _ = web
    page = client.get("/missions/M-2?view=latest").get_data(as_text=True)
    assert ("mission.events", {"mission_id": "M-2", "tail": True, "limit": 100}) in core.calls
    assert "FROM THE START" in page and "LATEST ›" not in page
    assert client.get("/missions/M-2?view=bogus").status_code == 400
    assert client.get("/missions/M-2?view=latest&after=3").status_code == 400


def test_review_due_cards_offer_reverify(web):
    client, core, _ = web
    page = client.get("/memory").get_data(as_text=True)
    due = page.split('aria-label="Review due"')[1].split('aria-label="Confirmed"')[0]
    assert 'action="/memory/due-one/reverify"' in due and "RE-VERIFY" in due
    assert "/reverify" not in page.split('aria-label="Confirmed"')[1]
    assert "/reverify" not in page.split('aria-label="Review due"')[0]


def test_reverify_post_uses_session_operator_and_csrf(web):
    client, core, _ = web
    assert client.post("/memory/due-one/reverify").status_code == 400
    core.results["memory.reverify"] = card("due-one", "confirmed")
    r = client.post("/memory/due-one/reverify", data={"csrf_token": token(client), "operator": "bob"})
    assert r.status_code == 302 and "notice=reverified" in r.headers["Location"]
    assert ("memory.reverify", {"card_id": "due-one", "operator": "alice"}) in core.calls
    assert "re-verified" in client.get(r.headers["Location"]).get_data(as_text=True).lower()
    core.errors["memory.reverify"] = "conflict"
    r = client.post("/memory/due-one/reverify", data={"csrf_token": token(client)})
    assert r.headers["Location"].endswith("notice=conflict")
    assert client.post("/memory/Bad Id/reverify", data={"csrf_token": token(client)}).status_code in (400, 404)


def test_disabled_notice_says_core_needs_a_restart(web):
    client, core, _ = web
    core.errors["mission.list"] = "disabled"
    page = client.get("/missions").get_data(as_text=True)
    assert "restart" in page.lower() and "core" in page.lower()


def test_investigate_internal_failure_is_not_a_retry_prompt(web):
    client, core, _ = web
    core.errors["mission.from_incident"] = "internal"
    r = client.post(f"/api/incidents/{INC}/investigate", data={"csrf_token": token(client)})
    assert r.status_code == 500 and "retry" not in r.get_json()["error"].lower()
    core.errors["mission.from_incident"] = "timeout"
    r = client.post(f"/api/incidents/{INC}/investigate", data={"csrf_token": token(client)})
    assert r.status_code == 503
