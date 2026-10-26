"""The Console's dashboard routes: auth, CSRF on every mutation, the operator is the signed-in one, strict ids, the core's refusals reach the
operator in words, and the page is the shell the JS fills (nothing from the agent is rendered server-side)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest
from test_scruffy_routes import ENV, csrf, login  # noqa: F401

import casa_scruffy
import config

CID = "0123456789abcdef"
STATUS = {"enabled": True, "open": 1, "max_open": 10, "token_budget": 150000, "min_turn_tokens": 20000, "proposal_only": False,
          "proposal_only_reason": None}
FEED = {"conversation": {"id": CID, "title": "t", "origin": "operator", "state": "open", "turns": 1,
                         "status": {"text": "Result ready", "level": "ok", "group": "Recent"}, "verdict": {"h": "Answer ready", "p": "p"}},
        "budget": {"used_pct": 1, "tokens_used": 1, "token_budget": 100, "tokens_left": 99, "state": "ok"}, "running": False,
        "composer": {"mode": "idle", "reason": None}, "messages": [], "last_message": 0, "events": [], "last_event": 0, "turn_starts": [],
        "plan": None, "status": STATUS}


class Core:
    def __init__(self):
        self.calls, self.errors = [], {}
        self.results = {
            "console.status": STATUS, "console.list": {"conversations": [], "status": STATUS}, "console.feed": FEED,
            "console.create": {"id": CID}, "console.post": {"seq": 3}, "console.interrupt": {"stopped": True},
            "console.archive": {"archiving": True}, "console.scrap_plan": {"scrapped": True},
            "canary.candidates": [], "proposal.list_pending": []}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method.startswith("auth."):
            return {"ok": True, "result": {"locked": False, "remaining_attempts": 2, "locked_until": None, "accepted": True, "epoch": 0, "sent": True}}
        if method in self.errors:
            code, message = self.errors[method]
            return {"ok": False, "error": {"code": code, "message": message}}
        return {"ok": True, "result": self.results[method]}

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
    client.get("/console")
    with client.session_transaction() as s:
        return s["csrf_token"]


@pytest.mark.parametrize("method, path", [("get", "/console"), ("get", f"/console/{CID}"), ("get", "/api/console/list"), ("get", f"/api/console/{CID}/feed"),
                                          ("post", "/api/console/new"), ("post", f"/api/console/{CID}/message"), ("post", f"/api/console/{CID}/interrupt"),
                                          ("post", f"/api/console/{CID}/archive"), ("post", f"/api/console/{CID}/plan/scrap")])
def test_every_console_route_needs_a_signed_in_operator(method, path):
    core = Core()
    app = casa_scruffy.create_app(ENV, rpc_call=core, clock=lambda: 1800000000.0)
    app.testing = True
    response = getattr(app.test_client(), method)(path)
    assert response.status_code in (302, 401, 400) and core.methods() == []
    if path.startswith("/api/"):
        assert response.status_code in (401, 400)


@pytest.mark.parametrize("path", ["/api/console/new", f"/api/console/{CID}/message", f"/api/console/{CID}/interrupt", f"/api/console/{CID}/archive",
                                  f"/api/console/{CID}/plan/scrap"])
def test_every_mutation_needs_the_csrf_token(web, path):
    client, core, _ = web
    assert client.post(path, data={"text": "x"}).status_code == 400
    assert client.post(path, data={"text": "x", "csrf_token": "wrong"}).status_code == 400
    assert core.methods() == []


def test_the_acting_operator_is_the_signed_in_one_and_a_form_field_cannot_change_it(web):
    client, core, _ = web
    t = token(client)
    assert client.post("/api/console/new", data={"csrf_token": t, "text": "why?", "operator": "mallory", "tier": "repair"}).get_json() == {"id": CID}
    method, params = core.calls[-1]
    assert method == "console.create" and params["operator"] == "alice" and set(params) == {"operator", "text"}
    client.post(f"/api/console/{CID}/message", data={"csrf_token": t, "text": "more", "operator": "mallory"})
    assert core.calls[-1] == ("console.post", {"conversation_id": CID, "operator": "alice", "text": "more"})
    for path, method in ((f"/api/console/{CID}/interrupt", "console.interrupt"), (f"/api/console/{CID}/archive", "console.archive"),
                         (f"/api/console/{CID}/plan/scrap", "console.scrap_plan")):
        assert client.post(path, data={"csrf_token": t, "operator": "mallory"}).status_code == 200
        assert core.calls[-1] == (method, {"conversation_id": CID, "operator": "alice"})


def test_an_incident_can_start_a_conversation_and_its_id_is_checked(web):
    client, core, _ = web
    t = token(client)
    client.post("/api/console/new", data={"csrf_token": t, "text": "look", "incident_id": "c" * 12})
    assert core.calls[-1][1]["incident_id"] == "c" * 12
    assert client.post("/api/console/new", data={"csrf_token": t, "text": "look", "incident_id": "nope"}).status_code == 400


@pytest.mark.parametrize("path", ["/api/console/new", f"/api/console/{CID}/message"])
def test_empty_and_oversize_text_never_reaches_core(web, path):
    client, core, _ = web
    t = token(client)
    n = len(core.calls)
    assert client.post(path, data={"csrf_token": t, "text": "   "}).status_code == 400
    assert client.post(path, data={"csrf_token": t, "text": "x" * 8001}).status_code == 400
    assert len(core.calls) == n


@pytest.mark.parametrize("bad", ["../etc", "0123", "0123456789ABCDEF", "g" * 16, "0123456789abcdef0"])
def test_a_conversation_id_is_exactly_sixteen_hex_digits(web, bad):
    client, core, _ = web
    t = token(client)
    assert client.get(f"/console/{bad}").status_code == 404
    assert client.get(f"/api/console/{bad}/feed").status_code == 404
    assert client.post(f"/api/console/{bad}/message", data={"csrf_token": t, "text": "x"}).status_code == 404
    assert "console.feed" not in core.methods() and "console.post" not in core.methods()


def test_the_feed_cursors_are_digits_only_and_passed_through(web):
    client, core, _ = web
    assert client.get(f"/api/console/{CID}/feed?after_message=7&after_event=12").status_code == 200
    assert core.calls[-1] == ("console.feed", {"conversation_id": CID, "after_message": 7, "after_event": 12})
    for bad in ("after_message=-1", "after_message=x", "after_event=1.5", "after_event=" + "9" * 16):
        assert client.get(f"/api/console/{CID}/feed?{bad}").status_code == 400


def test_the_cores_refusals_reach_the_operator_in_words_with_the_right_status(web):
    client, core, _ = web
    t = token(client)
    core.errors["console.create"] = ("conflict", "10 conversations are open: archive one first")
    r = client.post("/api/console/new", data={"csrf_token": t, "text": "x"})
    assert r.status_code == 409 and r.get_json()["error"] == "10 conversations are open: archive one first"
    core.errors["console.post"] = ("not_found", "internal detail that must not show")
    r = client.post(f"/api/console/{CID}/message", data={"csrf_token": t, "text": "x"})
    assert r.status_code == 404 and "internal" not in r.get_data(as_text=True)
    core.errors["console.post"] = ("bad_request", "x")
    assert client.post(f"/api/console/{CID}/message", data={"csrf_token": t, "text": "x"}).status_code == 400
    core.errors["console.list"] = ("unavailable", "The Console is not enabled on this host.")
    r = client.get("/api/console/list")
    assert r.status_code == 503 and "not enabled" in r.get_json()["error"]
    core.errors["console.list"] = ("internal", "boom")
    r = client.get("/api/console/list")
    assert r.status_code == 503 and "boom" not in r.get_data(as_text=True)


def test_the_page_is_a_shell_with_the_status_and_nothing_from_a_conversation(web):
    client, core, _ = web
    page = client.get(f"/console/{CID}").get_data(as_text=True)
    assert 'id="pe-con"' in page and f'data-selected="{CID}"' in page and "console.js" in page and "console.css" in page
    assert 'name="csrf-token"' in page and "Work log" not in page                      # the work log link is drawn by the JS with the thread
    boot = json.loads(page.split('id="con-boot">')[1].split("</script>")[0])
    assert boot["status"]["max_open"] == 10 and boot["selected"] == CID
    assert core.methods() == ["console.status"]                                         # only the status: the thread comes from the feed


def test_when_the_console_is_not_enabled_the_page_says_how_to_turn_it_on(web):
    client, core, _ = web
    core.errors["console.status"] = ("unavailable", "x")
    page = client.get("/console").get_data(as_text=True)
    assert "missions.console.enabled" in page and 'id="pe-con"' not in page
    core.errors["console.status"] = ("disabled", "x")
    assert "missions.enabled" in client.get("/console").get_data(as_text=True)


def test_the_console_tab_follows_the_config_flag(web, monkeypatch):
    client, _, _ = web
    monkeypatch.setattr(config.MISSIONS.console, "enabled", False, raising=False)
    assert 'href="/console"' not in client.get("/console").get_data(as_text=True)
    monkeypatch.setattr(config.MISSIONS.console, "enabled", True, raising=False)
    page = client.get("/console").get_data(as_text=True)
    assert 'href="/console"' in page and "tab active" in page
