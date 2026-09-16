"""
Route-level tests for casa_scruffy.py using Flask's test_client() -- no real socket,
no real Docker/host dependency. Same config.STATE_* monkeypatch pattern as
test_dashboard_data.py.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest

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
        result = self.results.get((method, params["operator"]), self.results.get(method))
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
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS",
        "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"waiting for the first scheduled run" in resp.data
    assert b"cockpit.css" in resp.data
    assert b"dashboard.css" not in resp.data


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
    monkeypatch.setattr(config, "STATE_PLAN", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_STATUS", tmp_path / "nope.json")
    monkeypatch.setattr(config, "ROLLBACK_CANDIDATES_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", tmp_path / "nope.json")

    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"backups.daily" in resp.data
    assert b"Backup timer has not run in 9 days" in resp.data
    assert b"1 critical" in resp.data


def test_widget_with_no_state_returns_200_unknown(tmp_path, monkeypatch):
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS",
        "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE",
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
    monkeypatch.setattr(config, "STATE_PLAN", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_STATUS", tmp_path / "nope.json")
    monkeypatch.setattr(config, "ROLLBACK_CANDIDATES_FILE", tmp_path / "nope.json")
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
    for attr in ("STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS", "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers", lambda: {"available": False, "routers": []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats", lambda: {"available": False})
    resp = _client().get("/")
    assert resp.status_code == 200
    return resp.data.decode()


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
