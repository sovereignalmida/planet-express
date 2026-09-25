"""
Route-level tests for casa_scruffy.py using Flask's test_client() -- no real socket,
no real Docker/host dependency. Same config.STATE_* monkeypatch pattern as
test_dashboard_data.py.
"""
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest
from werkzeug.datastructures import MultiDict

import casa_scruffy
import config
import web_auth
from planet_express.integrations.rpc import RpcError

PASSPHRASE = "a long test passphrase"
SECRET = web_auth.new_totp_secret()
ENV = {
    "PE_DASHBOARD_SECRET_KEY": "s" * 48,
    "PE_OPERATORS": "alice,bob",
    "PE_OPERATOR_ALICE_PASSPHRASE_HASH": web_auth.hash_passphrase(PASSPHRASE),
    "PE_OPERATOR_ALICE_TOTP_SECRET": SECRET,
    "PE_OPERATOR_BOB_PASSPHRASE_HASH": web_auth.hash_passphrase("another long passphrase"),
    "PE_OPERATOR_BOB_TOTP_SECRET": web_auth.new_totp_secret(),
}


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.results = {}

    def __call__(self, method, params):
        self.calls.append((method, params))
        result = self.results.get((method, params.get("operator")), self.results.get(method))
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        if result is None:
            result = {"locked": False, "remaining_attempts": 2, "locked_until": None,
                      "accepted": True, "epoch": 0, "sent": True}
        return {"ok": True, "result": result}


def make_client(**env):
    rpc = FakeRpc()
    now = [1800000000.0]
    app = casa_scruffy.create_app(ENV | env, rpc_call=rpc, clock=lambda: now[0])
    app.testing = True
    return app.test_client(), rpc, now


def csrf(client):
    client.get("/login")
    with client.session_transaction() as session:
        return session["csrf_token"]


def login(client, now, **overrides):
    data = {"csrf_token": csrf(client), "passphrase": PASSPHRASE,
                "code": web_auth.totp_at(SECRET, int(now[0] // 30)), "trust": "on", "next": "/"}
    data.update(overrides)
    return client.post("/login", data=data)


def _client():
    client, _rpc, now = make_client()
    assert login(client, now).status_code == 302
    return client


def test_index_with_no_state_returns_200_not_500(tmp_path, monkeypatch):
    # The single most important case per the design principle: a fresh install with
    # no pipeline run yet must never 500 -- it should render a "waiting" placeholder.
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_STATUS", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"waiting for the first scheduled run" in resp.data
    assert b"cockpit.css" in resp.data
    assert b"dashboard.css" not in resp.data
    assert b"lamp-rail" not in resp.data
    assert b"osc-gauge" not in resp.data


def test_index_renders_real_findings(tmp_path, monkeypatch):
    findings_path = tmp_path / "latest_findings.json"
    findings_path.write_text(json.dumps({
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [{
            "id": "f1", "severity": "CRITICAL",
            "resource": "backups.daily", "description": "Backup timer has not run in 9 days",
        }],
        "has_critical": True,
        "has_high": False,
    }))
    monkeypatch.setattr(config, "STATE_FINDINGS", findings_path)
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_STATUS", tmp_path / "nope.json")
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", tmp_path / "nope.json")

    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"backups.daily" in resp.data
    assert b"Backup timer has not run in 9 days" in resp.data
    assert b"1 critical" in resp.data


def test_widget_with_no_state_returns_200_unknown(tmp_path, monkeypatch):
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_STATUS", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    resp = _client().get("/api/widget")
    assert resp.status_code == 200
    assert resp.content_type == "application/json"
    data = resp.get_json()
    assert data["status"] == "unknown"
    assert data["state_available"] is False


def test_widget_with_real_findings(tmp_path, monkeypatch):
    findings_path = tmp_path / "latest_findings.json"
    findings_path.write_text(json.dumps({
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [{
            "id": "f1", "severity": "CRITICAL",
            "resource": "backups.daily", "description": "Backup timer has not run in 9 days",
        }],
        "has_critical": True,
        "has_high": False,
    }))
    monkeypatch.setattr(config, "STATE_FINDINGS", findings_path)
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_STATUS", tmp_path / "nope.json")
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", tmp_path / "nope.json")

    resp = _client().get("/api/widget")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "critical"
    assert data["open_findings"] == 1
    assert data["state_available"] is True


# ── 1r.1: certificate vault attention count (Codex finding on 1r) ───────────────
def _render_with_certs(tmp_path, monkeypatch, certs):
    monitor = tmp_path / "latest_monitor.json"
    monitor.write_text(json.dumps({"timestamp": "2026-09-15T12:00:00+00:00", "mode": "full", "certs": certs}))
    monkeypatch.setattr(config, "STATE_MONITOR", monitor)
    for attr in ("STATE_FINDINGS", "STATE_STATUS", "UPDATE_HISTORY_FILE"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers", lambda: {"available": False, "routers": []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats", lambda: {"available": False})
    resp = _client().get("/")
    assert resp.status_code == 200
    return resp.data.decode()


def _render_with_service_snapshot(tmp_path, monkeypatch, mode="full", stacks=None):
    monitor = tmp_path / "latest_monitor.json"
    monitor.write_text(json.dumps({
        "timestamp": "2026-09-21T12:00:00+00:00", "mode": mode,
        "stack_completeness": stacks or [],
    }))
    monkeypatch.setattr(config, "STATE_MONITOR", monitor)
    for attr in ("STATE_FINDINGS", "STATE_STATUS", "UPDATE_HISTORY_FILE"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers",
                        lambda: {"available": False, "routers": []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats",
                        lambda: {"available": False})
    response = _client().get("/")
    assert response.status_code == 200
    return response.data.decode()


def test_services_render_stack_cards_worst_first_with_links_and_attention(tmp_path, monkeypatch):
    html = _render_with_service_snapshot(tmp_path, monkeypatch, stacks=[
        {"stack": "healthy", "services": {
            "web": {"status": "healthy", "state": "running(healthy)"},
        }},
        {"stack": "broken", "services": {
            "api": {"status": "failing", "state": "exited(1)"},
            "worker": {"status": "unknown", "state": "running(starting)"},
        }},
    ])
    assert "running(healthy)" not in html
    assert html.index('data-stack-name="broken"') < html.index('data-stack-name="healthy"')
    assert 'href="/containers/broken/api"' in html
    assert 'href="/containers/broken/worker"' in html
    assert 'href="/containers/healthy/web"' in html
    assert "ATTENTION 1" in html


def test_services_attention_rail_only_renders_for_non_ok_stack(tmp_path, monkeypatch):
    html = _render_with_service_snapshot(tmp_path, monkeypatch, stacks=[
        {"stack": "healthy", "services": {
            "web": {"status": "healthy", "state": "running"},
        }},
    ])
    assert "data-services-rail" not in html
    assert "ATTENTION 0" in html
    assert "1 stacks · 1 of 1 online · worst first" in html


def test_services_empty_and_unavailable_states_render_honest_copy(tmp_path, monkeypatch):
    empty = _render_with_service_snapshot(tmp_path, monkeypatch, stacks=[])
    assert "No compose stacks found." in empty

    unavailable = _render_with_service_snapshot(tmp_path, monkeypatch, mode="status", stacks=[])
    assert "Service details need a full scan" in unavailable
    assert "CAN&#39;T REACH DOCKER" not in unavailable


def test_cert_attention_count_includes_legacy_status_only_snapshots(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [
        {"domain": "a.example", "status": "expiring", "days_remaining": 3},
        {"domain": "b.example", "status": "renew_soon", "days_remaining": 20},
        {"domain": "c.example", "status": "valid", "days_remaining": 200},
    ])
    assert "3 collected" in html
    assert "2 need attention" in html


def test_cert_attention_count_uses_tier_when_present(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [
        {"domain": "a.example", "tier": "expired", "days_left": -2},
        {"domain": "b.example", "tier": "valid", "days_left": 90},
    ])
    assert "1 needs attention" in html


def test_cert_attention_chip_absent_when_all_valid(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [
        {"domain": "a.example", "tier": "valid", "days_left": 90},
        {"domain": "b.example", "status": "valid", "days_remaining": 120},
    ])
    assert "need attention" not in html and "needs attention" not in html


def test_public_and_default():
    client, _rpc, _now = make_client()
    response = client.get("/")
    assert response.status_code == 302
    assert response.location == "/login?next=/"
    assert client.get("/api/widget").status_code == 200
    assert client.get("/static/login.js").status_code == 200
    response = client.get("/login")
    assert b'data-state="default"' in response.data
    assert b'name="csrf_token"' in response.data
    assert b'name="username"' not in response.data
    assert b'checked' in response.data


@pytest.mark.parametrize("path", ["/login", "/login/notify", "/logout"])
@pytest.mark.parametrize("token", [None, "wrong", "é"])
def test_csrf_rejected_without_rpc(path, token):
    client, rpc, _now = make_client()
    csrf(client)
    rpc.calls.clear()
    assert client.post(path, data={} if token is None else {"csrf_token": token}).status_code == 400
    assert rpc.calls == []


@pytest.mark.parametrize("overrides,operator", [({"passphrase": "wrong"}, "?"), ({"code": "bad"}, "alice")])
def test_rejection(overrides, operator):
    client, rpc, now = make_client()
    response = login(client, now, **overrides)
    assert b"PASSPHRASE OR AUTH CODE REJECTED" in response.data
    assert b"2 attempts left before this device is locked for 15 minutes" in response.data
    assert rpc.calls[-1] == ("auth.record_failure", {"operator": operator, "client_ip": "127.0.0.1"})


@pytest.mark.parametrize("operator", ["?", "alice"])
def test_lock_order(operator, monkeypatch):
    client, rpc, now = make_client()
    token = csrf(client)
    rpc.calls.clear()
    rpc.results[("auth.status", operator)] = {"locked": True, "locked_until": now[0] + 900}
    checked = []
    original = web_auth.verify_passphrase

    def verify(hash_, phrase):
        checked.append(hash_)
        return original(hash_, phrase)

    monkeypatch.setattr(web_auth, "verify_passphrase", verify)
    response = client.post("/login", data={"csrf_token": token, "passphrase": PASSPHRASE})
    assert b"AIRLOCK SEALED" in response.data and b"15:00" in response.data
    assert len(checked) == (0 if operator == "?" else 2)
    assert all(method == "auth.status" for method, params in rpc.calls)


def test_failure_locks_and_replay_fails():
    client, rpc, now = make_client()
    rpc.results["auth.consume_totp_step"] = {"accepted": False}
    rpc.results["auth.record_failure"] = {"locked": True, "locked_until": now[0] + 900}
    response = login(client, now)
    assert b"AIRLOCK SEALED" in response.data
    assert rpc.calls[-1][0] == "auth.record_failure"
    assert not client.get_cookie("pe_auth")


@pytest.mark.parametrize("target,expected", [("/network?tab=x", "/network?tab=x"),
                                               ("//evil.example", "/"),
                                               ("https://evil.example", "/"),
                                               ("/\\evil", "/")])
def test_success_redirect_and_flags(target, expected):
    client, rpc, now = make_client()
    response = login(client, now, next=target)
    assert response.status_code == 302 and response.location == expected
    assert [method for method, params in rpc.calls][-3:] == [
        "auth.consume_totp_step", "auth.record_success", "auth.device_epoch"]
    cookie = client.get_cookie("pe_auth")
    assert cookie.http_only and cookie.same_site == "Strict" and not cookie.secure
    assert cookie.max_age == 30 * 86400
    assert b"LOG OUT" in client.get("/").data


@pytest.mark.parametrize("forced,base_url", [(True, "http://localhost"), (False, "https://localhost")])
def test_secure_cookies(forced, base_url):
    client, _rpc, now = make_client(CASA_DASHBOARD_HTTPS="1" if forced else "0")
    client.get("/login", base_url=base_url)
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post("/login", base_url=base_url, data={
        "csrf_token": token, "passphrase": PASSPHRASE,
        "code": web_auth.totp_at(SECRET, int(now[0] // 30)), "trust": "on"})
    assert response.status_code == 302
    assert all("Secure" in cookie for cookie in response.headers.getlist("Set-Cookie"))


def test_untrusted_expiry_and_marker_required():
    client, _rpc, now = make_client()
    login(client, now, trust="")
    assert client.get_cookie("pe_auth").max_age is None
    assert client.get("/").status_code == 200
    now[0] += 12 * 3600 + 1
    assert client.get("/").location == "/login?next=/"
    login(client, now)
    client.delete_cookie("pe_auth_trust")
    assert client.get("/").status_code == 302


def test_epoch_cache_tamper_and_removed_operator():
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["auth.device_epoch"] = {"epoch": 1}
    assert client.get("/").status_code == 200
    now[0] += 60
    assert client.get("/").status_code == 302
    assert client.get_cookie("pe_auth") is None
    login(client, now)
    cookie = client.get_cookie("pe_auth").value
    client.set_cookie("pe_auth", cookie + "tampered")
    assert client.get("/").status_code == 302
    login(client, now)
    other, _, _ = make_client(PE_OPERATORS="bob")
    for name in ("pe_auth", "pe_auth_trust"):
        other.set_cookie(name, client.get_cookie(name).value)
    assert other.get("/").status_code == 302


@pytest.mark.parametrize("method", ["status", "consume_totp_step", "record_success", "device_epoch"])
@pytest.mark.parametrize("failure", [RpcError("secret diagnostic"), {"ok": False}])
def test_rpc_failure_closed(method, failure):
    client, rpc, now = make_client()
    token = csrf(client)
    rpc.results["auth." + method] = failure
    response = client.post("/login", data={"csrf_token": token, "passphrase": PASSPHRASE,
                                          "code": web_auth.totp_at(SECRET, int(now[0] // 30))})
    assert response.status_code == 503
    assert b"SHIP COMPUTER UNREACHABLE" in response.data
    assert b"secret diagnostic" not in response.data
    assert client.get_cookie("pe_auth") is None


def test_notify_and_logout():
    client, rpc, now = make_client()
    token = csrf(client)
    rpc.results["auth.status"] = {"locked": True, "locked_until": now[0] + 900}
    response = client.post("/login/notify", data={"csrf_token": token})
    assert b"Notification sent" in response.data
    assert rpc.calls[-2] == ("auth.notify_locked", {"operator": "?", "client_ip": "127.0.0.1"})
    rpc.results.clear()
    login(client, now)
    response = client.post("/logout", data={"csrf_token": csrf(client)})
    assert response.location == "/login"
    assert client.get_cookie("pe_auth") is None


@pytest.mark.parametrize("env,variable", [({"PE_DASHBOARD_SECRET_KEY": ""}, "PE_DASHBOARD_SECRET_KEY"),
                                         ({"PE_DASHBOARD_SECRET_KEY": "short-secret"}, "PE_DASHBOARD_SECRET_KEY"),
                                         ({"PE_OPERATORS": ""}, "PE_OPERATORS")])
def test_creation_fails_closed(env, variable):
    with pytest.raises(SystemExit) as error:
        casa_scruffy.create_app(ENV | env)
    assert variable in str(error.value)
    assert "short-secret" not in str(error.value)
    assert ENV["PE_DASHBOARD_SECRET_KEY"] not in str(error.value)


def test_all_operator_hashes_checked_and_bob_identified(monkeypatch):
    client, rpc, now = make_client()
    original = web_auth.verify_passphrase
    checked = []

    def verify(hash_, phrase):
        checked.append(hash_)
        return original(hash_, phrase)

    monkeypatch.setattr(web_auth, "verify_passphrase", verify)
    login(client, now, passphrase="wrong")
    assert len(checked) == 2
    checked.clear()
    response = login(client, now, passphrase="another long passphrase",
                     code=web_auth.totp_at(ENV["PE_OPERATOR_BOB_TOTP_SECRET"], int(now[0] // 30)))
    assert len(checked) == 2 and response.status_code == 302
    assert ("auth.record_success", {"operator": "bob", "client_ip": "127.0.0.1"}) in rpc.calls


def test_trusted_expiry_and_outer_rpc_error():
    client, _rpc, now = make_client()
    login(client, now)
    now[0] += 30 * 86400 + 1
    assert client.get("/").status_code == 302
    app = casa_scruffy.create_app(ENV, rpc_call=lambda *args: {"ok": False})
    response = app.test_client().get("/login")
    assert response.status_code == 503


def test_missing_secret_and_invalid_operator_configuration():
    env = dict(ENV)
    del env["PE_DASHBOARD_SECRET_KEY"]
    with pytest.raises(SystemExit, match="PE_DASHBOARD_SECRET_KEY"):
        casa_scruffy.create_app(env)
    env = ENV | {"PE_OPERATOR_ALICE_TOTP_SECRET": "bad-secret-value"}
    with pytest.raises(SystemExit) as error:
        casa_scruffy.create_app(env)
    assert "PE_OPERATOR_" in str(error.value)
    assert "bad-secret-value" not in str(error.value)


def test_notify_already_sent():
    client, rpc, now = make_client()
    token = csrf(client)
    rpc.results["auth.status"] = {"locked": True, "locked_until": now[0] + 300}
    rpc.results["auth.notify_locked"] = {"sent": False, "reason": "already_notified"}
    response = client.post("/login/notify", data={"csrf_token": token})
    assert b"Notification already sent" in response.data
    assert b"05:00" in response.data


def test_core_outage_keeps_the_session():
    client, rpc, now = make_client()
    login(client, now)
    now[0] += 61                                     # past the epoch cache
    rpc.results["auth.device_epoch"] = RpcError("core restarting")
    response = client.get("/")
    assert response.status_code == 503 and b"SHIP COMPUTER UNREACHABLE" in response.data
    assert client.get_cookie("pe_auth") is not None and client.get_cookie("pe_auth_trust") is not None
    del rpc.results["auth.device_epoch"]
    assert client.get("/").status_code == 200


def test_notify_when_not_locked_returns_to_login():
    client, rpc, _now = make_client()
    rpc.results["auth.notify_locked"] = {"sent": False, "reason": "not_locked"}
    response = client.post("/login/notify", data={"csrf_token": csrf(client)})
    assert response.status_code == 302 and response.location == "/login"


def test_notify_uses_the_operator_whose_lock_was_shown():
    client, rpc, now = make_client()
    locked = {"locked": True, "locked_until": now[0] + 600, "remaining_attempts": 0}
    rpc.results[("auth.status", "alice")] = locked      # alice locked from other IPs; "?" + this IP not
    response = client.post("/login", data={"csrf_token": csrf(client), "passphrase": PASSPHRASE,
                                          "code": "000000", "trust": "on"})
    assert b"AIRLOCK SEALED" in response.data
    response = client.post("/login/notify", data={"csrf_token": csrf(client)})
    assert b"Notification sent" in response.data
    assert [c for c in rpc.calls if c[0] == "auth.notify_locked"][-1] == (
        "auth.notify_locked", {"operator": "alice", "client_ip": "127.0.0.1"})


def test_notify_after_a_failure_that_locks_only_the_operator():
    client, rpc, now = make_client()
    rpc.results[("auth.record_failure", "alice")] = {"locked": True, "locked_until": now[0] + 900,
                                                     "remaining_attempts": 0, "just_locked": True}
    response = client.post("/login", data={"csrf_token": csrf(client), "passphrase": PASSPHRASE,
                                          "code": "000000", "trust": "on"})
    assert b"AIRLOCK SEALED" in response.data
    rpc.results[("auth.status", "alice")] = {"locked": True, "locked_until": now[0] + 900, "remaining_attempts": 0}
    response = client.post("/login/notify", data={"csrf_token": csrf(client)})
    assert b"Notification sent" in response.data
    assert [c for c in rpc.calls if c[0] == "auth.notify_locked"][-1][1]["operator"] == "alice"


@pytest.fixture
def chat_client():
    client, rpc, now = make_client()
    login(client, now)
    token = csrf(client)
    rpc.calls.clear()
    return client, rpc, now, {"csrf_token": token, "question": " What failed? ",
                              "submission_id": "submission-123"}


def assert_chat_error(response, status):
    assert response.status_code == status
    assert set(response.get_json()) == {"error"}
    assert response.headers["Cache-Control"] == "no-store"
    assert b"secret diagnostic" not in response.data
    assert b"<html" not in response.data


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/012345abcdef", "/api/chat/quota"])
def test_chat_requires_auth_json(path):
    client, rpc, _now = make_client()
    response = client.post(path) if path == "/api/chat" else client.get(path)
    assert_chat_error(response, 401)
    assert not rpc.calls


def test_chat_csrf_and_operator(chat_client):
    client, rpc, _now, data = chat_client
    assert_chat_error(client.post("/api/chat", data={"question": "hello"}), 400)
    assert_chat_error(client.post("/api/chat", data=data | {"csrf_token": "wrong"}), 400)
    assert not rpc.calls
    ticket = {"ticket_id": "012345abcdef", "status": "queued"}
    rpc.results["chat.ask"] = ticket
    response = client.post("/api/chat", data=data | {"operator": "bob"})
    assert response.status_code == 200 and response.get_json() == ticket
    assert response.headers["Cache-Control"] == "no-store"
    assert rpc.calls == [("chat.ask", {"operator": "alice", "question": "What failed?",
                                       "submission_id": "submission-123"})]


@pytest.mark.parametrize("field,value", [("question", ""), ("question", "  "),
    ("question", "x" * 2001), ("submission_id", "short"), ("submission_id", "x" * 65),
    ("submission_id", "invalid!"), ("submission_id", "abcdefgh\n")])
def test_chat_validation_before_rpc(chat_client, field, value):
    client, rpc, _now, data = chat_client
    assert_chat_error(client.post("/api/chat", data=data | {field: value}), 400)
    assert not rpc.calls


@pytest.mark.parametrize("ticket_id", ["bad", "ABCDEF012345", "a" * 13, "a" * 11])
def test_chat_malformed_ticket(chat_client, ticket_id):
    client, rpc, _now, _data = chat_client
    assert_chat_error(client.get("/api/chat/" + ticket_id), 404)
    assert not rpc.calls


@pytest.mark.parametrize("code,status", [("not_found", 404), ("bad_request", 400), ("internal", 503)])
def test_chat_core_error_envelope(chat_client, code, status):
    client, rpc, _now, _data = chat_client
    # Exercise the actual wire envelope, rather than only raised transport errors.
    original = rpc.__class__.__call__
    with pytest.MonkeyPatch.context() as patch:
        def call(self, method, params):
            if method == "chat.get":
                self.calls.append((method, params))
                return {"ok": False, "error": {"code": code, "message": "secret diagnostic"}}
            return original(self, method, params)
        patch.setattr(FakeRpc, "__call__", call)
        assert_chat_error(client.get("/api/chat/012345abcdef"), status)
    assert rpc.calls == [("chat.get", {"operator": "alice", "ticket_id": "012345abcdef"})]


@pytest.mark.parametrize("method", ["chat.ask", "chat.get", "chat.quota", "auth.device_epoch"])
def test_chat_unreachable_is_json(chat_client, method):
    client, rpc, now, data = chat_client
    rpc.results[method] = RpcError("secret diagnostic")
    if method == "auth.device_epoch":
        now[0] += 61
    response = (client.post("/api/chat", data=data) if method == "chat.ask" else
                client.get("/api/chat/quota" if method == "chat.quota" else "/api/chat/012345abcdef"))
    assert_chat_error(response, 503)
    assert client.get_cookie("pe_auth") is not None


def test_chat_quota_and_ticket_passthrough(chat_client):
    client, rpc, _now, data = chat_client
    quota = {"used": 12, "limit": 100, "resets_at": 1800050000}
    ticket = {"ticket_id": "012345abcdef", "status": "failed", "error": "chat is busy, try again shortly"}
    rpc.results.update({"chat.quota": quota, "chat.ask": ticket, "chat.get": ticket})
    for response, expected in [(client.get("/api/chat/quota"), quota),
                               (client.post("/api/chat", data=data), ticket),
                               (client.get("/api/chat/012345abcdef"), ticket)]:
        assert response.status_code == 200 and response.get_json() == expected
        assert response.headers["Cache-Control"] == "no-store"
    assert rpc.calls[0] == ("chat.quota", {})


def test_chat_panel_outside_live_snapshot(tmp_path, monkeypatch):
    class DashboardParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.chat_ancestors = None
            self.chat_tab = False
            self.csrf_meta = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("data-tab-panel") == "chat":
                self.chat_ancestors = list(self.stack)
                assert "tab-panel" in attrs.get("class", "").split()
            if tag == "button" and attrs.get("data-tab") == "chat":
                self.chat_tab = True
            if tag == "meta" and attrs.get("name") == "csrf-token":
                self.csrf_meta = bool(attrs.get("content"))
            if tag not in {"meta", "link", "img", "input", "br", "hr"}:
                self.stack.append((tag, attrs.get("id")))

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1][0] == tag:
                self.stack.pop()

    parser = DashboardParser()
    parser.feed(_render_with_certs(tmp_path, monkeypatch, []))
    assert parser.chat_tab and parser.csrf_meta
    assert parser.chat_ancestors is not None
    assert ("div", "dashboard-live") not in parser.chat_ancestors
    assert parser.chat_ancestors == [("html", None), ("body", None)]



def test_chat_js_does_not_need_a_secure_context():
    """crypto.randomUUID() only exists over HTTPS or localhost; the dashboard is plain HTTP on the LAN."""
    js = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text()
    code = "\n".join(line for line in js.splitlines() if not line.strip().startswith("//"))
    assert "randomUUID" not in code
    assert "crypto.getRandomValues" in code


def test_container_shell_and_validation(chat_client):
    client, rpc, _, _ = chat_client
    response = client.get("/containers/media/search")
    assert response.status_code == 200
    assert b'LOGGED AS</dt><dd>alice' in response.data
    assert b'class="pe-sheet warn"' in response.data
    assert b'container.js' in response.data
    assert not rpc.calls
    for name in ("bad!", "a" * 65):
        assert client.get("/containers/media/" + name).status_code == 404
        assert_chat_error(client.get("/api/containers/media/" + name), 404)
    js = (Path(__file__).resolve().parent.parent / "static/container.js").read_text()
    assert "innerHTML" not in js
    assert client.get("/executions/test-id").status_code == 200


def test_container_authentication():
    client, rpc, _ = make_client()
    assert client.get("/containers/media/search").location == "/login?next=/containers/media/search"
    for suffix in ("", "/logs", "/restart"):
        path = "/api/containers/media/search" + suffix
        response = client.post(path) if suffix == "/restart" else client.get(path)
        assert_chat_error(response, 401)
    assert not rpc.calls


@pytest.mark.parametrize("method,suffix", [("query.container", ""), ("logs.tail", "/logs")])
def test_container_reads(chat_client, method, suffix):
    client, rpc, _, _ = chat_client
    payload = {"target": {"stack": "media", "service": "search"}, "facts": {}, "vitals": {}}
    rpc.results[method] = payload
    token = csrf(client)
    rpc.calls.clear()        # csrf() fetches /login, which itself calls auth.status
    response = (client.post("/api/containers/media/search" + suffix, data={"csrf_token": token})
                if suffix else client.get("/api/containers/media/search"))
    assert response.get_json() == payload
    assert response.headers["Cache-Control"] == "no-store"
    expected = {"stack": "media", "service": "search"}
    if suffix:
        expected["cursor_hashes"] = []
    assert rpc.calls == [(method, expected)]


@pytest.mark.parametrize("code,status", [("not_found", 404), ("bad_request", 400), ("internal", 503)])
def test_container_rpc_errors(chat_client, code, status):
    client, rpc, _, _ = chat_client
    rpc.results["query.container"] = RpcError("secret diagnostic", code)
    assert_chat_error(client.get("/api/containers/media/search"), status)


def test_container_auth_outage_json(chat_client):
    client, rpc, now, _ = chat_client
    now[0] += 61
    rpc.results["auth.device_epoch"] = RpcError("secret diagnostic")
    assert_chat_error(client.get("/api/containers/media/search"), 503)


def test_container_logs_cursor(chat_client):
    client, rpc, _, _ = chat_client
    path = "/api/containers/media/search/logs"
    token = csrf(client)
    rpc.calls.clear()        # csrf() fetches /login, which itself calls auth.status
    # POSTed, not in the query string: 1000 hashes exceed gunicorn's request-line limit (T30).
    assert_chat_error(client.post(path, data={"csrf_token": token, "cursor": "bad"}), 400)
    assert_chat_error(client.post(path, data={"csrf_token": token, "hash": "bad"}), 400)
    assert not rpc.calls
    no_token = client.post(path, data={"cursor": "bad"})                         # no CSRF token
    assert no_token.status_code == 400 and no_token.get_json()["error"]
    cursor = "2026-09-17T12:34:56.123456789Z"
    client.post(path, data=MultiDict([("csrf_token", token), ("cursor", cursor),
                                      ("hash", "a" * 16), ("hash", "b" * 16)]))
    assert rpc.calls[-1] == ("logs.tail", {"stack": "media", "service": "search",
        "cursor": cursor, "cursor_hashes": ["a" * 16, "b" * 16]})
    client.post(path, data=MultiDict([("csrf_token", token)] + [("hash", "a" * 16)] * 1001))
    assert len(rpc.calls[-1][1]["cursor_hashes"]) == 1000
    assert client.get(path).status_code == 405


@pytest.mark.parametrize("outcome", ["started", "busy", "refused", "timeout"])
def test_container_restart_identity_and_outcomes(chat_client, outcome):
    client, rpc, _, data = chat_client
    path = "/api/containers/media/search/restart"
    assert_chat_error(client.post(path), 400)
    assert not rpc.calls
    payload = {"outcome": outcome, "message": "result", "execution_id": "exec-123" if outcome == "started" else None}
    rpc.results["action.request"] = payload
    response = client.post(path, data={"csrf_token": data["csrf_token"], "operator": "bob"})
    assert response.status_code == 200 and response.get_json() == payload
    assert "Location" not in response.headers
    assert rpc.calls == [("action.request", {"stack": "media", "service": "search",
        "action": "docker.restart_service", "operator": "alice"})]


def test_approvals_list_returns_pending_and_recent(chat_client):
    client, rpc, _, _ = chat_client
    pending = {"id": "a" * 12, "status": "pending", "capabilities": {"rollbackable": False}}
    recent = {"id": "b" * 12, "status": "denied", "decided_by": "bob"}
    # FakeRpc uses a list as a sequence of replies; nest collection results once.
    rpc.results.update({"proposal.list_pending": [[{"id": "a" * 12}]],
                        "approval.get": pending, "approval.list_recent": [[recent]]})
    response = client.get("/api/approvals")
    assert response.status_code == 200
    assert response.get_json() == {
        "pending": [pending], "recent": [recent | {"denial_reason": "Denied by bob."}],
    }
    assert rpc.calls == [
        ("proposal.list_pending", {}), ("approval.get", {"approval_id": "a" * 12}),
        ("approval.list_recent", {"limit": 20}),
    ]


def test_approval_get_passthrough(chat_client):
    client, rpc, _, _ = chat_client
    payload = {"id": "a" * 12, "status": "pending", "executions": [],
               "capabilities": {"rollbackable": False}}
    rpc.results["approval.get"] = payload
    response = client.get("/api/approvals/aaaaaaaaaaaa")
    assert response.status_code == 200 and response.get_json() == payload
    assert rpc.calls == [("approval.get", {"approval_id": "a" * 12})]


def test_approvals_deduplicate_transition_and_keep_denial_reason(chat_client):
    client, rpc, _, _ = chat_client
    resolved = {"id": "a" * 12, "status": "denied", "decided_by": "alice"}
    rpc.results.update({"proposal.list_pending": [[{"id": "a" * 12}]],
                        "approval.get": resolved, "approval.list_recent": [[resolved]]})
    response = client.get("/api/approvals")
    assert response.status_code == 200
    assert response.get_json() == {
        "pending": [], "recent": [resolved | {"denial_reason": "Denied by alice."}],
    }

    rpc.calls.clear()
    rpc.results["approval.get"] = {"id": "b" * 12, "status": "denied", "decided_by": "policy"}
    response = client.get("/api/approvals/bbbbbbbbbbbb")
    assert response.get_json()["denial_reason"] == "Refused by current policy."


@pytest.mark.parametrize("method,path", [
    ("get", "/api/approvals"), ("get", "/api/approvals/aaaaaaaaaaaa"),
    ("post", "/api/approvals/aaaaaaaaaaaa/decide"), ("get", "/api/executions/aaaaaaaaaaaa"),
])
def test_action_apis_require_auth_json(method, path):
    client, rpc, _ = make_client()
    response = getattr(client, method)(path)
    assert response.status_code == 401 and set(response.get_json()) == {"error"}
    assert not rpc.calls


@pytest.mark.parametrize("method,path", [
    ("approval.get", "/api/approvals/aaaaaaaaaaaa"),
    ("execution.get_status", "/api/executions/aaaaaaaaaaaa"),
])
@pytest.mark.parametrize("error,status", [
    (RpcError("secret", "not_found"), 404), (RpcError("secret"), 503),
])
def test_action_read_errors_are_json(chat_client, method, path, error, status):
    client, rpc, _, _ = chat_client
    rpc.results[method] = error
    response = client.get(path)
    assert response.status_code == status
    assert set(response.get_json()) == {"error"}
    assert b"<html" not in response.data
    assert b"secret" not in response.data


@pytest.mark.parametrize("path", [
    "/api/approvals/not-an-id", "/api/approvals/not-an-id/decide", "/api/executions/not-an-id",
])
def test_action_ids_are_twelve_lowercase_hex(chat_client, path):
    client, rpc, _, data = chat_client
    response = (client.post(path, data={"csrf_token": data["csrf_token"], "approve": "1"})
                if path.endswith("decide") else client.get(path))
    assert response.status_code == 404
    assert set(response.get_json()) == {"error"}
    assert not rpc.calls


@pytest.mark.parametrize("outcome", [
    "started", "denied", "refused", "busy", "already_decided", "expired", "unknown",
])
def test_approval_decide_uses_device_identity_and_maps_outcome(chat_client, outcome):
    client, rpc, _, data = chat_client
    path = "/api/approvals/aaaaaaaaaaaa/decide"
    assert client.post(path, data={"approve": "1"}).status_code == 400
    assert not rpc.calls
    payload = {"outcome": outcome, "message": "decision result",
               "execution_id": "b" * 12 if outcome == "started" else None}
    rpc.results["approval.decide"] = payload
    response = client.post(path, data={"csrf_token": data["csrf_token"], "approve": "0", "decided_by": "bob"})
    assert response.status_code == 200 and response.get_json() == payload
    assert rpc.calls == [("approval.decide", {
        "approval_id": "a" * 12, "approve": False, "decided_by": "alice",
    })]


def test_approval_decide_rejects_invalid_boolean(chat_client):
    client, rpc, _, data = chat_client
    response = client.post("/api/approvals/aaaaaaaaaaaa/decide",
                           data={"csrf_token": data["csrf_token"], "approve": "true"})
    assert response.status_code == 400
    assert not rpc.calls


def test_execution_status_passthrough_and_restart_has_no_unsupported_controls(chat_client):
    client, rpc, _, _ = chat_client
    payload = {"id": "a" * 12, "status": "passed", "reason": "healthy for 15s",
               "capabilities": {"abortable": False, "rollbackable": False, "resumable": False}}
    rpc.results["execution.get_status"] = payload
    response = client.get("/api/executions/aaaaaaaaaaaa")
    assert response.status_code == 200 and response.get_json() == payload
    assert response.get_json()["capabilities"] == payload["capabilities"]
    assert rpc.calls == [("execution.get_status", {"execution_id": "a" * 12})]

    page = client.get("/executions/aaaaaaaaaaaa")
    assert page.status_code == 200
    assert b"ABORT" not in page.data
    assert b"ROLL BACK" not in page.data
    assert b"RESUME" not in page.data
    assert b"execution.js" in page.data


def test_incident_routes_validate_list_and_use_session_operator(chat_client):
    client, rpc, _, data = chat_client
    incident_id = "c" * 12
    rows = [{"id": incident_id, "hint": {"state": "proposal_available"}}]
    rpc.results["incident.list"] = [rows]
    response = client.get("/api/incidents?status=open&limit=20")
    assert response.status_code == 200 and response.get_json() == rows
    assert rpc.calls == [("incident.list", {"status": "open", "limit": 20})]

    rpc.calls.clear()
    payload = {"ok": True, "approval_id": "a" * 12, "created": True,
               "reason": "awaiting approval"}
    rpc.results["incident.propose"] = payload
    assert client.post(f"/api/incidents/{incident_id}/propose").status_code == 400
    assert not rpc.calls
    response = client.post(
        f"/api/incidents/{incident_id}/propose",
        data={"csrf_token": data["csrf_token"], "operator": "bob"},
    )
    assert response.status_code == 200 and response.get_json() == payload
    assert rpc.calls == [("incident.propose", {"incident_id": incident_id, "operator": "alice"})]


@pytest.mark.parametrize("query", ["status=bad&limit=20", "status=open&limit=0", "status=open&limit=x"])
def test_incident_list_rejects_invalid_query_without_rpc(chat_client, query):
    client, rpc, _, _ = chat_client
    response = client.get("/api/incidents?" + query)
    assert response.status_code == 400 and set(response.get_json()) == {"error"}
    assert not rpc.calls


@pytest.mark.parametrize("method,path", [
    ("get", "/api/incidents/BAD"), ("post", "/api/incidents/short/propose"),
])
def test_incident_item_rejects_malformed_id_as_bad_request(chat_client, method, path):
    client, rpc, _, data = chat_client
    response = getattr(client, method)(
        path, data={"csrf_token": data["csrf_token"]} if method == "post" else None,
    )
    assert response.status_code == 400 and set(response.get_json()) == {"error"}
    assert not rpc.calls


@pytest.mark.parametrize("method,path", [
    ("get", "/api/incidents"), ("get", "/api/incidents/aaaaaaaaaaaa"),
    ("post", "/api/incidents/aaaaaaaaaaaa/propose"),
])
def test_incident_apis_require_auth_json(method, path):
    client, rpc, _ = make_client()
    response = getattr(client, method)(path)
    assert response.status_code == 401 and set(response.get_json()) == {"error"}
    assert not rpc.calls


@pytest.mark.parametrize("method,path", [
    ("incident.get", "/api/incidents/aaaaaaaaaaaa"),
    ("incident.list", "/api/incidents"),
])
@pytest.mark.parametrize("error,status", [
    (RpcError("secret", "not_found"), 404), (RpcError("secret"), 503),
])
def test_incident_read_errors_are_json(chat_client, method, path, error, status):
    client, rpc, _, _ = chat_client
    rpc.results[method] = error
    response = client.get(path)
    assert response.status_code == status and set(response.get_json()) == {"error"}
    assert b"secret" not in response.data and b"<html" not in response.data


def test_config_routes_use_csrf_and_authenticated_operator(chat_client):
    client, rpc, _, data = chat_client
    snapshot = {"text": "stacks_root: /srv\n", "sha256": "a" * 64,
                "path": "/etc/planetexpress/config.yaml", "sensitive_edits_enabled": False,
                "editable_fields": ["backup_jobs"], "sensitive_fields": ["autonomy"]}
    validation = {"ok": True, "errors": [], "changed_fields": ["backup_jobs"],
                  "locked_fields": []}
    applying = {"status": "activating", "errors": [], "reason": "",
                "changed_fields": ["backup_jobs"], "locked_fields": []}
    rpc.results.update({"config.get": snapshot, "config.validate": validation,
                        "config.apply": applying})

    assert client.get("/api/config").get_json() == snapshot
    assert client.post("/api/config/validate", data={"text": "draft"}).status_code == 400
    response = client.post("/api/config/validate", data={
        "csrf_token": data["csrf_token"], "text": "draft",
    })
    assert response.status_code == 200 and response.get_json() == validation
    response = client.post("/api/config/apply", data={
        "csrf_token": data["csrf_token"], "text": "draft", "base_sha256": "a" * 64,
        "operator": "bob",
    })
    assert response.status_code == 200 and response.get_json() == applying
    assert rpc.calls == [
        ("config.get", {}),
        ("config.validate", {"text": "draft"}),
        ("config.apply", {"text": "draft", "base_sha256": "a" * 64,
                          "operator": "alice"}),
    ]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/config"), ("post", "/api/config/validate"),
    ("post", "/api/config/apply"),
])
def test_config_routes_require_auth_as_json(method, path):
    client, rpc, _ = make_client()
    response = getattr(client, method)(path)
    assert response.status_code == 401 and set(response.get_json()) == {"error"}
    assert not rpc.calls


@pytest.mark.parametrize("path", [
    "/api/config", "/api/config/validate", "/api/config/apply",
])
def test_config_rpc_errors_are_always_503_json(chat_client, path):
    client, rpc, _, data = chat_client
    method = {"/api/config": "config.get", "/api/config/validate": "config.validate",
              "/api/config/apply": "config.apply"}[path]
    rpc.results[method] = RpcError("secret diagnostic", "bad_request")
    response = (client.get(path) if path == "/api/config" else client.post(
        path, data={"csrf_token": data["csrf_token"], "text": "draft",
                    "base_sha256": "a" * 64}
    ))
    assert response.status_code == 503 and set(response.get_json()) == {"error"}
    assert b"secret" not in response.data and b"<html" not in response.data


def test_actions_splits_what_wants_you_from_what_is_wrong(tmp_path, monkeypatch):
    """Left column: things asking for a decision -- a plan to authorise, an open rollback
    window. Right column: things that are wrong -- incidents, then hull findings. The update
    history is not on this tab at all; it is the one thing here you could never act on."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    dock = html[html.index('data-tab-panel="actions"'):html.index('data-tab-panel="history"')]
    columns = re.findall(r'<div class="actions-col">(.*?)\n      </div>', dock, re.DOTALL)
    assert len(columns) == 2
    assert re.findall(r'id="([a-z-]+-panel|rollback-candidates-panel)"', columns[0]) == [
        "approval-panel", "rollback-candidates-panel"]
    assert re.findall(r'id="([a-z-]+-panel)"', columns[1]) == [
        "incident-panel", "hull-diagnostics-panel"]
    assert 'data-tab-panel="history" id="manifest-panel"' in html
    assert 'data-tab="history"' in html          # and it has a tab of its own to live on


def test_the_actions_panels_are_not_themselves_tab_panels(tmp_path, monkeypatch):
    """They are children of one tab-panel now. Leaving the class on them would have
    setActiveTab() toggling .active on the column contents too."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    for panel in ("approval-panel", "incident-panel", "hull-diagnostics-panel",
                  "rollback-candidates-panel"):
        opening = re.search(rf'<div[^>]*id="{panel}"', html).group(0)
        assert "tab-panel" not in opening, panel
        assert "data-tab-panel" not in opening, panel


def test_overview_replaces_full_width_hull_and_system_with_tiles(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [])
    overview = html[html.index('data-tab-panel="overview"'):html.index('data-tab-panel="backups"')]
    assert 'data-tile-tab="actions"' in overview
    assert 'class="overview-tile none"' in overview
    assert "panel-green" not in overview
    assert "stat-tiles" not in overview
    # It moved to Actions, which is where the HULL tile's "actions ›" leads.
    dock = html[html.index('data-tab-panel="actions"'):html.index('data-tab-panel="history"')]
    assert 'id="hull-diagnostics-panel"' in dock


def test_static_assets_are_versioned_so_a_cached_one_cannot_outlive_a_deploy(tmp_path, monkeypatch):
    """A browser holding the previous cockpit.css against freshly deployed markup looks
    exactly like a broken release. Every stylesheet and script carries its file mtime."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    for asset in ("cockpit.css", "dashboard.js", "incidents.js", "approvals.js",
                  "chat.js", "config.js"):
        assert re.search(rf"/static/{re.escape(asset)}\?v=\d+", html), asset


def test_the_backups_tab_never_calls_a_disabled_daily_a_fault(tmp_path, monkeypatch):
    """This host's daily borg timer is off on purpose. Without the header note a reader
    counts one pod where they expected two and reads the absence as a failure."""
    monkeypatch.setattr(config, "BACKUP_JOBS", ["weekly"])
    html = _render_with_certs(tmp_path, monkeypatch, [])
    assert "daily disabled on purpose" in html


def test_a_stack_with_no_readable_members_still_says_why(tmp_path, monkeypatch):
    """summarize_services() marks it warn with note="state unreadable". A dots-only card has
    no dots to draw for it and nothing to drill into, so without the note it is a bare 0/0."""
    html = _render_with_service_snapshot(tmp_path, monkeypatch, stacks=[
        {"stack": "mystery", "status": "unknown", "services": {}},
    ])
    assert "state unreadable" in html


def test_the_fleet_and_system_tiles_are_not_clickable_controls(tmp_path, monkeypatch):
    """Both summarise the tab you are already on. A button that re-selects it is a no-op
    control that keyboard users still have to tab through."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    overview = html[html.index('data-tab-panel="overview"'):html.index('data-tab-panel="backups"')]
    tiles = overview[overview.index('class="overview-tiles"'):overview.index("overview-control-grid")]
    assert 'data-tile-tab="overview"' not in tiles
    for target in ("actions", "backups", "network"):
        assert f'data-tile-tab="{target}"' in tiles


def test_the_actions_docks_survive_a_snapshot_refresh(tmp_path, monkeypatch):
    """The 60-second swap replaces #dashboard-live's contents; a decision in flight must not be
    inside it."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    # Everything below this marker is a sibling of the live region, never a child.
    detached = html[html.index("PANELS OUTSIDE #dashboard-live"):]
    for panel in ("approval-panel", "incident-panel", "hull-diagnostics-panel",
                  "rollback-candidates-panel", "manifest-panel", "chat-panel",
                  "config-panel", "crew-panel"):
        assert f'id="{panel}"' in detached, panel
    assert "incidents.js" in html
    assert "approvals.js" in html


def test_the_manifest_is_still_refreshed_even_though_it_left_the_live_region(tmp_path, monkeypatch):
    """It renders below the docks, so it cannot live inside #dashboard-live — the refresh has to
    swap it by id instead, or it would be the one panel that silently stopped updating."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    assert 'id="manifest-panel"' in html
    script = (Path(__file__).resolve().parent.parent / "static" / "dashboard.js").read_text()
    assert 'getElementById("manifest-panel")' in script


def test_history_renders_run_cards_beside_a_detail_pane(tmp_path, monkeypatch):
    history = tmp_path / "update_history.json"
    history.write_text(json.dumps({"entries": [
        {"ts": "2026-09-06T06:29:23+00:00", "stack": "tgbot", "service": "renderd",
         "status": "updated"},
    ]}))
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", history)
    monitor = tmp_path / "latest_monitor.json"
    monitor.write_text(json.dumps({"timestamp": "2026-09-15T12:00:00+00:00", "mode": "full"}))
    monkeypatch.setattr(config, "STATE_MONITOR", monitor)
    for attr in ("STATE_FINDINGS", "STATE_STATUS"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers",
                        lambda: {"available": False, "routers": []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats",
                        lambda: {"available": False})
    html = _client().get("/").data.decode()
    assert 'class="history-split"' in html
    assert 'id="deploy-manifest"' in html and 'id="run-detail"' in html


def test_the_selected_run_survives_a_manifest_rebuild(tmp_path, monkeypatch):
    """manifest-panel's innerHTML is replaced every 60 seconds. A selection held inside
    buildDeployManifest() would reset to the newest run under the operator every minute, and
    an index would point at a different run once a new one lands at the top."""
    script = (Path(__file__).resolve().parent.parent / "static" / "dashboard.js").read_text()
    declaration = re.search(r"^  var selectedRunKey = null;", script, re.MULTILINE)
    assert declaration, "selectedRunKey must live at module scope, not inside the builder"
    assert declaration.start() < script.index("function buildDeployManifest")
    # Identity, not position.
    assert 'function runKey(run) { return (run.stack || "unknown") + "@" + run.entries[0].ts; }' in script


def test_the_crew_log_is_still_refreshed_even_though_it_left_the_live_region(tmp_path, monkeypatch):
    """The crew cards are static, but the ship's computer log beside them is professor_lines,
    derived from the current scan. Outside #dashboard-live it would freeze at page-load state
    and start contradicting the rest of the dashboard, so the refresh swaps it by id."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    assert 'id="crew-panel"' in html
    script = (Path(__file__).resolve().parent.parent / "static" / "dashboard.js").read_text()
    assert 'getElementById("crew-panel")' in script


def test_the_header_still_wraps_on_a_narrow_screen(tmp_path, monkeypatch):
    """The 54px single row only fits on a wide screen. Collapsing the header without a wrapping
    fallback pushed the tabs and both buttons off the right edge on a phone."""
    css = (Path(__file__).resolve().parent.parent / "static" / "cockpit.css").read_text()
    narrow = css[css.index("@media (max-width: 1180px)"):]
    assert "flex-wrap: wrap" in narrow
    assert "overflow-x: auto" in narrow


def test_config_panel_is_persistent_and_loaded_by_javascript(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [])
    class ConfigParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.ancestors = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id") == "config-panel":
                self.ancestors = list(self.stack)
            if tag not in {"meta", "link", "img", "input", "br", "hr"}:
                self.stack.append((tag, attrs.get("id")))

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1][0] == tag:
                self.stack.pop()

    parser = ConfigParser()
    parser.feed(html)
    config = html.index('id="config-panel"')
    assert "outside #dashboard-live" in html[config - 300:config]
    assert parser.ancestors == [("html", None), ("body", None)]
    assert 'data-tab="config"' in html
    assert 'data-tab-panel="config"' in html
    assert "config.js" in html
    assert 'id="config-editor"' in html
    assert "stacks_root:" not in html


def test_config_reload_watch_signals_once_for_a_new_valid_file(tmp_path):
    # Codex review round 5, T35: dashboard workers must pick up an applied config.
    import hashlib
    import os

    from casa_scruffy import ConfigReloadWatch
    path = tmp_path / "config.yaml"
    path.write_text("stacks_root: /srv/stacks\n")
    loaded = hashlib.sha256(path.read_bytes()).hexdigest()
    reloads = []
    watch = ConfigReloadWatch(path, loaded, reload=lambda: reloads.append(1))

    watch.check()
    assert reloads == []                      # unchanged file: nothing to do

    path.write_text("stacks_root: /srv/stacks\npaused_containers: [x]\n")
    os.utime(path, ns=(1, 1))
    watch.check()
    watch.check()
    assert reloads == [1]                     # new valid file: exactly one reload

    path.write_text("stacks_root: relative/not/allowed\n")
    os.utime(path, ns=(2, 2))
    watch.check()
    assert reloads == [1]                     # invalid file: never reload into a crash loop


def test_config_reload_watch_only_runs_under_gunicorn(monkeypatch):
    calls = []
    monkeypatch.setattr(casa_scruffy.ConfigReloadWatch, "check", lambda self: calls.append(1))
    client, _rpc, _now = make_client()
    client.get("/login")
    assert calls == []                        # test client / dev server: never signal a parent
    client.get("/login", environ_base={"SERVER_SOFTWARE": "gunicorn/26.0"})
    assert calls == [1]


# ── T40: abort and rollback controls ───────────────────────────────────────────
@pytest.mark.parametrize("kind", ["abort", "rollback"])
def test_execution_controls_use_the_session_operator_and_require_csrf(chat_client, kind):
    client, rpc, _, _ = chat_client
    rpc.results[f"execution.{kind}"] = {"outcome": "requested", "message": "ok",
                                        "execution_id": "a" * 12, "report": None}
    token = csrf(client)
    rpc.calls.clear()

    no_token = client.post(f"/api/executions/aaaaaaaaaaaa/{kind}")
    assert no_token.status_code == 400 and not rpc.calls

    response = client.post(f"/api/executions/aaaaaaaaaaaa/{kind}",
                           data={"csrf_token": token, "operator": "someone-else"})
    assert response.status_code == 200 and response.get_json()["outcome"] == "requested"
    method, params = rpc.calls[-1]
    assert method == f"execution.{kind}"
    assert params["execution_id"] == "a" * 12
    assert params["operator"] == "alice"  # the session operator, never the one in the form


@pytest.mark.parametrize("kind", ["abort", "rollback"])
def test_execution_controls_reject_a_bad_id_and_surface_core_failures(chat_client, kind):
    client, rpc, _, _ = chat_client
    token = csrf(client)
    rpc.calls.clear()
    bad = client.post(f"/api/executions/not-an-id/{kind}", data={"csrf_token": token})
    assert bad.status_code == 400 and set(bad.get_json()) == {"error"} and not rpc.calls
    rpc.results[f"execution.{kind}"] = RpcError("core is down", "internal")
    down = client.post(f"/api/executions/aaaaaaaaaaaa/{kind}", data={"csrf_token": token})
    assert down.status_code == 503 and set(down.get_json()) == {"error"}


def test_index_shows_open_canary_windows_from_core(tmp_path, monkeypatch):
    """The dashboard cannot open the core database, so the panel is fed over RPC (slice 5b-3)."""
    client, rpc, now = make_client()
    assert login(client, now).status_code == 302
    # FakeRpc treats a list as a queue of responses, so the row list is queued as one response
    rpc.results["canary.candidates"] = [[
        {"stack": "media", "service": "sonarr", "old_image_id": "a" * 64,
         "image_reference": "nginx:1.27", "recorded_at": "2026-09-23T12:00:00+01:00",
         "expires_at": "when a human closes it"},
    ]]
    resp = client.get("/")
    assert resp.status_code == 200
    assert ("canary.candidates", {}) in rpc.calls
    assert b"OPEN ROLLBACK CANDIDATES" in resp.data and b"sonarr" in resp.data
    assert b"when a human closes it" in resp.data


def test_index_still_renders_when_core_cannot_answer(tmp_path, monkeypatch):
    client, rpc, now = make_client()
    assert login(client, now).status_code == 302
    rpc.results["canary.candidates"] = OSError("core is down")
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"OPEN ROLLBACK CANDIDATES" not in resp.data


def test_every_detached_tab_hides_the_empty_live_grid():
    """A tab whose panel is a sibling of #dashboard-live leaves the live region's main column with
    nothing in it. Without hiding that grid the tab opens with a screen of empty space above its
    content — which is exactly what happened when the manifest moved to History."""
    root = Path(__file__).resolve().parent.parent
    script = (root / "static" / "dashboard.js").read_text()
    css = (root / "static" / "cockpit.css").read_text()
    html = (root / "templates" / "dashboard.html").read_text()

    detached = set(re.search(r"var DETACHED_TABS = \[(.*?)\];", script).group(1).replace('"', "").replace(" ", "").split(","))
    tabs = set(re.search(r"var TAB_NAMES = \[(.*?)\];", script).group(1).replace('"', "").replace(" ", "").split(","))
    outside = set(re.findall(r'data-tab-panel="([a-z]+)"',
                             html.split("PANELS OUTSIDE #dashboard-live")[-1]))

    assert "crew" in tabs
    assert "crew" in outside
    assert "crew" in detached
    assert outside <= detached, f"these tabs render outside the live grid but are not detached: {outside - detached}"
    assert ".detached-tab #dashboard-live > .body-grid { display: none; }" in css
