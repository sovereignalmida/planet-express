"""
Route-level tests for casa_scruffy.py using Flask's test_client() -- no real socket,
no real Docker/host dependency. Same config.STATE_* monkeypatch pattern as
test_dashboard_data.py.
"""
import json
import os
import re
import sys
import threading
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest
from werkzeug.datastructures import MultiDict

import casa_scruffy
import config
import web_auth
from planet_express.application.multi_host import FleetView, HostCard
from planet_express.core.hosts import HostDetails, HostMetrics, Liveness
from planet_express.integrations.rpc import RpcError

PASSPHRASE = "a long test passphrase"
SECRET = web_auth.new_totp_secret()
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"a tiny but believable png"

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


def _hosts_page(cards, locality_reason=None):
    class Cache:
        def view(self):
            return FleetView(tuple(cards), "local", locality_reason)

    now = [1800000000.0]
    app = casa_scruffy.create_app(ENV, host_cache=Cache(), rpc_call=FakeRpc(), clock=lambda: now[0])
    app.testing = True
    client = app.test_client()
    assert login(client, now).status_code == 302
    return client.get("/hosts")


def test_hosts_route_escapes_unconfigured_remote_name_and_uses_cockpit():
    hostile = "Remote <script>alert(1)</script>"
    response = _hosts_page((
        HostCard("new", hostile, None, False, Liveness("current"), containers=(),
                 containers_liveness=Liveness("current")),
    ))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Remote &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page
    assert "shown as text only" in page
    assert "cockpit.css" in page


def test_hosts_all_unknown_has_one_nothing_can_be_read_verdict():
    cards = tuple(
        HostCard(str(i), f"Unknown {i}", None, True, Liveness("unknown", "collector unreachable"),
                 containers=None, containers_liveness=Liveness("unknown", "collector unreachable"))
        for i in range(3)
    )
    page = _hosts_page(cards).get_data(as_text=True)
    assert "NOTHING CAN BE READ" in page
    assert "SOME HOSTS ARE NOT READING" not in page
    assert page.count("NO CONTACT") == 3


def test_hosts_with_no_configured_entries_says_the_feature_is_not_set_up():
    page = _hosts_page(()).get_data(as_text=True)
    assert "OTHER HOSTS NOT CONFIGURED" in page
    assert "No other hosts are configured." in page
    assert "multi_host block in config.yaml" in page
    assert "OTHER HOSTS READING" not in page


def test_hosts_page_explains_when_unconfigured_rows_are_withheld():
    reason = "locality is unknown; unconfigured rows are withheld"
    page = _hosts_page((), locality_reason=reason).get_data(as_text=True)
    assert reason in page


def test_hosts_zero_containers_is_not_unreadable_containers():
    zero = HostCard("zero", "Zero containers", None, True, Liveness("current"),
                    details=HostDetails(os_name=None),
                    metrics=HostMetrics(cpu_pct=0, mem_pct=0, disk_pct=0, load=(0.1, 0.2, 0.3)),
                    containers=(), containers_liveness=Liveness("current"))
    unknown = HostCard("unknown", "Unreadable containers", None, True,
                       Liveness("unknown", "collector unreachable"), containers=None,
                       containers_liveness=Liveness("unknown", "collector unreachable"))
    stale = HostCard("stale", "Stale host", None, True, Liveness("stale", "old", 121),
                     metrics=HostMetrics(cpu_pct=21, mem_pct=22, disk_pct=23), containers=(),
                     containers_liveness=Liveness("current"))
    page = _hosts_page((zero, stale, unknown)).get_data(as_text=True)
    assert "No containers on this host. It runs none; that is normal here, not a failed read." in page
    assert "CONTAINERS</span><span class=\"pe-value none\">unreadable" in page
    assert "2m ago" in page
    assert "not reported" in page  # os_name stays absent, never rendered as zero


def test_hosts_unknown_reason_cards_have_distinct_explanations():
    cards = (
        HostCard("contact", "No contact", None, True, Liveness("unknown", "collector unreachable")),
        HostCard("permission", "No permission", None, True,
                 Liveness("unknown", "account not permitted; listed no systems")),
        HostCard("clock", "Bad clock", None, True,
                 Liveness("unknown", "timestamp is unusable")),
    )
    page = _hosts_page(cards).get_data(as_text=True)
    for text in ("NO CONTACT", "NOT PERMITTED", "CLOCK UNUSABLE", "⌁", "⛨", "◷",
                 "returned success with an empty list", "timestamp cannot be trusted"):
        assert text in page


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
        "action": "docker.restart_service", "operator": "alice", "elevated": False})]


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
        # Apply asks what the change touches before applying it, so whether an elevated
        # session is required is read off the change rather than guessed (T47).
        ("config.validate", {"text": "draft"}),
        ("config.apply", {"text": "draft", "base_sha256": "a" * 64,
                          "operator": "alice", "elevated": False}),
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
    if path == "/api/config/apply":
        # Apply now validates first; give that step a real answer so the failure under test is
        # config.apply's own and not the pre-check refusing for want of one.
        rpc.results["config.validate"] = {"ok": True, "errors": [],
                                          "changed_fields": ["backup_jobs"],
                                          "locked_fields": []}
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


def test_the_disabled_note_follows_the_config_not_a_stale_snapshot(tmp_path, monkeypatch):
    """A job disabled since the last full scan is still in that snapshot. Reading the note
    from the snapshot's keys would have dropped it for hours, while the pod it explains had
    already gone."""
    monkeypatch.setattr(config, "BACKUP_JOBS", ["weekly"])
    monitor = tmp_path / "latest_monitor.json"
    monitor.write_text(json.dumps({
        "timestamp": "2026-09-15T12:00:00+00:00", "mode": "full",
        "backups": {"daily": {"result": "success"}, "weekly": {"result": "success"}},
    }))
    monkeypatch.setattr(config, "STATE_MONITOR", monitor)
    for attr in ("STATE_FINDINGS", "STATE_STATUS", "UPDATE_HISTORY_FILE"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers",
                        lambda: {"available": False, "routers": []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats",
                        lambda: {"available": False})
    html = _client().get("/").data.decode()
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


def test_the_crew_tab_carries_the_eight_the_design_package_names(tmp_path, monkeypatch):
    """The v2.2 package revised this list: Zapp Brannigan is the dashboard now, replacing
    Scruffy, whose "observes and does nothing" stopped being true once the dashboard could
    authorise an action. Pinned because a crew card is easy to leave behind a rename."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    crew = html[html.index('id="crew-panel"'):html.index('id="computer-log-title"')]
    assert re.findall(r"<h3>([^<]+)</h3>", crew) == [
        "Prof. Farnsworth", "Leela", "Hermes", "Bender",
        "Dr. Zoidberg", "Amy", "Fry", "Zapp Brannigan",
    ]
    assert "scruffy.png" not in crew


def test_the_config_editor_layers_cannot_drift_apart(tmp_path, monkeypatch):
    """Gutter, locked-line tints and the textarea are three elements that must agree line for
    line. They only do so while the textarea does not soft-wrap: one wrapped line would put
    every tint below it on the wrong row, and a tint on the wrong row is worse than none."""
    html = _render_with_certs(tmp_path, monkeypatch, [])
    shell = html[html.index('class="config-editor-shell"'):html.index('class="config-actions"')]
    assert 'id="config-gutter"' in shell
    assert 'id="config-tints"' in shell
    assert 'wrap="off"' in shell
    script = (Path(__file__).resolve().parent.parent / "static" / "config.js").read_text()
    # Both decorations scroll with the textarea, or they only line up at the top.
    assert 'gutter.scrollTop = editor.scrollTop' in script
    assert 'tints.scrollTop = editor.scrollTop' in script


def test_locked_line_tints_come_from_the_servers_own_key_list(tmp_path, monkeypatch):
    """The tint is presentation over the core's refusal, never a second opinion on it: it
    reads sensitive_fields straight off the config payload."""
    script = (Path(__file__).resolve().parent.parent / "static" / "config.js").read_text()
    painter = script[script.index("function lockedLineFlags"):script.index("function paintEditor")]
    assert "state.loaded.sensitive_fields" in painter
    assert "sensitive_edits_enabled" in painter


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


def test_the_header_still_wraps_before_the_single_row_runs_out_of_space(tmp_path, monkeypatch):
    """The 54px single row only fits on a wide screen. Collapsing the header without a wrapping
    fallback pushed the tabs and both buttons off the right edge on a phone.

    The breakpoint is 1560 because that is what the row measures, not because it is a round
    number. Rendered with the nav in place, the single-row topbar needs 1536px for nine tabs
    and needed 1465px for eight -- so the old 1180 left every viewport from 1181 to 1465
    clipping the right-hand controls, with LOG going off the edge first, before the Hosts tab
    was added. A breakpoint below what the row needs is not a fallback, it is a gap.
    """
    css = (Path(__file__).resolve().parent.parent / "static" / "cockpit.css").read_text()
    narrow = css[css.index("@media (max-width: 1560px)"):]
    assert "flex-wrap: wrap" in narrow
    assert "overflow-x: auto" in narrow
    # Nine tabs today. A tenth needs the measurement redone, not the number nudged.
    # `class="tab active"` is one of them, so match the class rather than the literal string.
    dashboard = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()
    assert len(re.findall(r'class="tab(?: active)?"', dashboard)) == 9


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



# ── Launch links: which container declares a router comes from core (T46.1) ─────

def _launch_link_client(monkeypatch, *, traefik_up=True):
    client, rpc, now = make_client()
    assert login(client, now).status_code == 302
    routers = [{"name": "actual@docker", "rule": "Host(`actual.casalan.com`)", "service": "actual",
                "status": "enabled", "entry_points": ["websecure"]}]
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers",
                        lambda: {"available": traefik_up, "routers": routers if traefik_up else []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats",
                        lambda: {"available": False})
    seen = []
    real = casa_scruffy.casa_scruffy_net.container_urls

    def spy(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "container_urls", spy)
    return client, rpc, seen


def test_launch_links_join_through_core_s_router_owners(monkeypatch):
    client, rpc, seen = _launch_link_client(monkeypatch)
    rpc.results["containers.routers"] = {"routers": {"actual": "CASA_ACTUAL"},
                                         "services": {"actual": "CASA_ACTUAL"}}
    assert client.get("/").status_code == 200
    assert ("containers.routers", {}) in rpc.calls
    assert seen[-1] == {"CASA_ACTUAL": [{"href": "https://actual.casalan.com", "zone": "lan"}]}


@pytest.mark.parametrize("answer", [OSError("core is down"), RpcError("slow", "timeout"),
                                    ["not", "a", "dict"], {"actual": "CASA_ACTUAL"},
                                    {"routers": {"actual": "CASA_ACTUAL"}},
                                    {"routers": ["actual"], "services": {}}])
def test_no_answer_from_core_means_no_derived_links_never_guessed_ones(monkeypatch, answer):
    client, rpc, seen = _launch_link_client(monkeypatch)
    # FakeRpc pops a list as a queue, so a list answer is queued as one response.
    rpc.results["containers.routers"] = [answer] if isinstance(answer, list) else answer
    assert client.get("/").status_code == 200
    assert seen[-1] == {}


def test_core_is_not_asked_for_routers_when_traefik_is_down(monkeypatch):
    client, rpc, _ = _launch_link_client(monkeypatch, traefik_up=False)
    assert client.get("/").status_code == 200
    assert ("containers.routers", {}) not in rpc.calls



# ── enforced config comes from core, not this process's import (T46.1 P1 #2) ─────

def test_index_uses_core_s_enforced_links(monkeypatch):
    client, rpc, _seen = _launch_link_client(monkeypatch)
    monkeypatch.setattr(casa_scruffy.config, "LAUNCH_LINKS", [])     # stale import
    rpc.results["config.enforced"] = {
        "paused_containers": [], "backup_jobs": ["weekly"],
        "links": [{"name": "CASA_JELLYFIN", "href": "https://jellyfin.casalan.com"}]}
    rpc.results["containers.routers"] = {"routers": {}, "services": {}}
    captured = {}
    real = casa_scruffy.casa_scruffy_net.merge_declared_links

    def spy(derived, declared):
        captured["declared"] = declared
        return real(derived, declared)
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "merge_declared_links", spy)
    assert client.get("/").status_code == 200
    assert ("config.enforced", {}) in rpc.calls
    assert captured["declared"] == [{"name": "CASA_JELLYFIN", "href": "https://jellyfin.casalan.com",
                                     "zone": "lan"}]


def test_index_falls_back_to_its_own_import_when_core_cannot_say(monkeypatch):
    client, rpc, _ = _launch_link_client(monkeypatch)
    import_links = [{"name": "CASA_A", "href": "https://a.casalan.com", "zone": "lan"}]
    monkeypatch.setattr(casa_scruffy.config, "LAUNCH_LINKS", import_links)
    rpc.results["config.enforced"] = OSError("core is down")
    rpc.results["containers.routers"] = OSError("core is down")
    captured = {}
    real = casa_scruffy.casa_scruffy_net.merge_declared_links

    def spy(derived, declared):
        captured["declared"] = declared
        return real(derived, declared)
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "merge_declared_links", spy)
    assert client.get("/").status_code == 200
    assert captured["declared"] == import_links



# ── container widget route (T46.3) ────────────────────────────────────────────────

def test_the_widget_route_needs_a_login_unlike_the_public_health_widget():
    client, rpc, _now = make_client()
    assert client.get("/api/containers/media/sonarr/widget").status_code in (302, 401)
    assert all(method != "query.widget_target" for method, _ in rpc.calls)


def test_the_widget_route_fetches_what_core_resolved(monkeypatch):
    client, rpc, now = make_client()
    assert login(client, now).status_code == 302
    rpc.results["query.widget_target"] = {"container": "CASA_SONARR", "container_id": "a" * 64,
                                          "running": True, "widget": None, "addresses": []}
    response = client.get("/api/containers/media/sonarr/widget")
    assert response.status_code == 200 and response.get_json() == {"state": "none"}
    assert ("query.widget_target", {"stack": "media", "service": "sonarr"}) in rpc.calls


def test_the_widget_route_refuses_a_bad_target_before_asking_core():
    client, rpc, now = make_client()
    assert login(client, now).status_code == 302
    rpc.calls.clear()
    assert client.get("/api/containers/me dia/sonarr/widget").status_code == 404
    assert not rpc.calls


# ── T46.4: launch links in the UI ─────────────────────────────────────────────────

def _ui_client(tmp_path, monkeypatch, routers):
    monitor = tmp_path / "latest_monitor.json"
    monitor.write_text(json.dumps({
        "timestamp": "2026-09-26T12:00:00+00:00", "mode": "full",
        "containers": [{"name": "CASA_ACTUAL", "status": "Up", "image": "actualbudget/actual-server"}],
        "stack_completeness": [{"stack": "money", "status": "complete", "services": {
            "actual_server": {"status": "healthy", "state": "running", "container": "CASA_ACTUAL"},
            "db": {"status": "healthy", "state": "running", "container": "CASA_DB"}}}],
    }))
    monkeypatch.setattr(config, "STATE_MONITOR", monitor)
    for attr in ("STATE_FINDINGS", "STATE_STATUS", "UPDATE_HISTORY_FILE"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers",
                        lambda: {"available": True, "routers": routers})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats", lambda: {"available": False})
    client, rpc, now = make_client()
    assert login(client, now).status_code == 302
    rpc.results["containers.routers"] = {"routers": {"actual": "CASA_ACTUAL"},
                                         "services": {"actual": "CASA_ACTUAL"}}
    rpc.results["config.enforced"] = {"paused_containers": [], "backup_jobs": ["weekly"], "links": []}
    return client, rpc


_ACTUAL = {"name": "actual@docker", "service": "actual", "status": "enabled",
           "entry_points": ["websecure"],
           "rule": "Host(`actual.casalan.com`) || Host(`actual.casaalmida.com`)"}


def test_the_overview_tile_counts_launchable_containers_and_carries_the_drawer(tmp_path, monkeypatch):
    client, _ = _ui_client(tmp_path, monkeypatch, [_ACTUAL])
    html = client.get("/").data.decode()
    assert 'class="pe-tile-links"' in html and "↗ 1" in html
    assert 'data-drawer-for="money"' in html
    assert 'href="https://actual.casalan.com" target="_blank" rel="noopener">LAN ↗' in html
    assert 'href="https://actual.casaalmida.com" target="_blank" rel="noopener">WEB ↗' in html
    assert "— no route" in html                                   # db has none


def test_a_network_pill_is_a_split_link(tmp_path, monkeypatch):
    client, _ = _ui_client(tmp_path, monkeypatch, [_ACTUAL])
    html = client.get("/").data.decode()
    assert 'class="pe-pill-main" href="https://actual.casaalmida.com"' in html
    assert 'class="pe-pill-lan" href="https://actual.casalan.com"' in html


def test_a_down_pill_links_to_its_containers_detail(tmp_path, monkeypatch):
    client, _ = _ui_client(tmp_path, monkeypatch, [{**_ACTUAL, "status": "disabled"}])
    html = client.get("/").data.decode()
    assert 'href="/containers/money/actual_server"' in html
    assert 'href="https://actual.casaalmida.com"' not in html.split("ROUTING MATRIX")[1]


def test_container_detail_header_links_come_from_their_own_endpoint(tmp_path, monkeypatch):
    """Fetched after render: a slow Traefik or core delays two buttons, not the page."""
    client, _ = _ui_client(tmp_path, monkeypatch, [_ACTUAL])
    html = client.get("/containers/money/actual_server").data.decode()
    assert 'id="container-launch"' in html and "OPEN ↗</a>" not in html.split('id="container-launch"')[1][:200]
    assert client.get("/api/containers/money/actual_server/links").get_json() == {
        "lan": "https://actual.casalan.com", "web": "https://actual.casaalmida.com",
        "launchable": True, "host": "actual.casalan.com"}


def test_container_detail_without_a_route_offers_no_launch(tmp_path, monkeypatch):
    client, _ = _ui_client(tmp_path, monkeypatch, [_ACTUAL])
    assert client.get("/api/containers/money/db/links").get_json()["launchable"] is False


def test_the_links_endpoint_refuses_a_bad_target():
    client, _rpc, now = make_client()
    assert login(client, now).status_code == 302
    assert client.get("/api/containers/me dia/x/links").status_code == 404


def test_container_detail_carries_the_widget_slot_after_the_verdict(tmp_path, monkeypatch):
    """v2.2 C: header -> verdict -> widget -> vitals. The slot starts hidden; with no widget
    it stays out entirely rather than showing an empty box."""
    client, _ = _ui_client(tmp_path, monkeypatch, [_ACTUAL])
    html = client.get("/containers/money/actual_server").data.decode()
    assert html.index('id="verdict"') < html.index('id="widget"') < html.index('class="container-vitals"')
    assert 'data-api="/api/containers/money/actual_server/widget"' in html
    widget_tag = html[html.index('id="widget"') - 20:html.index('id="widget"') + 300]
    assert " hidden>" in widget_tag
    # Announced through a small status line on state change, not by re-reading the box every 30s.
    assert "aria-live" not in widget_tag
    assert 'id="widget-status"' in html
    assert 'id="log-toggle"' in html
# ── SCAN ────────────────────────────────────────────────────────────────────────

def test_scan_posts_the_operators_identity_and_returns_cores_answer(chat_client):
    """The button used to be <a href="/">: it said SCAN and reloaded the page."""
    client, rpc, _, data = chat_client
    rpc.results["scan.start"] = {"status": "started"}

    response = client.post("/api/scan", data={"csrf_token": data["csrf_token"]})

    assert response.status_code == 200 and response.get_json() == {"status": "started"}
    # The device identity, never anything the browser supplied.
    assert rpc.calls == [("scan.start", {"operator": "alice"})]


def test_a_refusal_reaches_the_browser_rather_than_only_telegram(chat_client):
    """Whoever pressed the button is looking at the button, not at Telegram."""
    client, rpc, _, data = chat_client
    rpc.results["scan.start"] = {"status": "busy", "reason": "host busy (act:abc123)"}

    response = client.post("/api/scan", data={"csrf_token": data["csrf_token"]})

    assert response.status_code == 200
    assert response.get_json() == {"status": "busy", "reason": "host busy (act:abc123)"}


@pytest.mark.parametrize("token", [None, "wrong", "é"])
def test_scan_is_refused_without_a_csrf_token(chat_client, token):
    """CSRF is enforced for exactly the paths is_json_request() matches, so a route left out
    of that list has no CSRF check at all. /api/scan starts a host-wide scan."""
    client, rpc, _, _ = chat_client
    rpc.calls.clear()
    rpc.results["scan.start"] = {"status": "started"}

    response = client.post("/api/scan", data={} if token is None else {"csrf_token": token})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid CSRF token"}
    assert rpc.calls == []


def test_scan_needs_a_session():
    client, rpc, _now = make_client()
    rpc.calls.clear()
    response = client.post("/api/scan", data={"csrf_token": "anything"})
    assert response.status_code == 401
    assert rpc.calls == []


@pytest.mark.parametrize(("code", "status", "message"), [
    ("unavailable", 503, "Scanning is unavailable"),
    ("bad_request", 400, "Invalid scan request"),
    ("internal", 503, "Core unavailable; try again shortly"),
])
def test_a_scan_rpc_failure_answers_json_not_an_html_airlock(chat_client, code, status, message):
    """The handler's fallback renders the airlock page. The button parses JSON, so an HTML
    body yields null and the real reason is lost -- it would say "could not start a scan"
    whatever actually went wrong."""
    client, rpc, _, data = chat_client
    rpc.results["scan.start"] = RpcError("secret diagnostic", code)

    response = client.post("/api/scan", data={"csrf_token": data["csrf_token"]})

    assert response.status_code == status
    assert response.get_json() == {"error": message}


def test_the_dashboard_never_starts_a_thread_that_writes_the_icon_cache():
    """The dashboard runs as planetexpress-web, which web_access.py grants rX on state/ and
    nothing more. A warmer here could not create state/icons, would swallow the OSError, and
    every container would render a monogram on the live host while passing every test on a
    developer machine. Core warms the cache when a scan writes the snapshot.
    """
    before = {t.name for t in threading.enumerate()}
    casa_scruffy.create_app(dict(ENV))
    started = {t.name for t in threading.enumerate()} - before
    assert not started, f"create_app started {started}"
    assert not hasattr(casa_scruffy, "_warm_icons_forever")


# ── /icons ──────────────────────────────────────────────────────────────────────

def _icon_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(casa_scruffy, "_ICON_CACHE", None)
    directory = tmp_path / "icons"
    directory.mkdir()
    return directory


def test_an_icon_requires_a_signed_in_operator(monkeypatch, tmp_path):
    """Which icons exist says which apps run here. No reason to answer that before login."""
    _icon_dir(monkeypatch, tmp_path).joinpath("sonarr.png").write_bytes(PNG_BYTES)
    client, _rpc, _now = make_client()
    response = client.get("/icons/sonarr")
    assert response.status_code in (302, 401)


def test_a_cached_icon_is_served_as_a_png(monkeypatch, tmp_path):
    _icon_dir(monkeypatch, tmp_path).joinpath("sonarr.png").write_bytes(PNG_BYTES)
    client, _rpc, now = make_client()
    login(client, now)
    response = client.get("/icons/sonarr")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == PNG_BYTES
    # Revalidated, never stored fresh: a cached icon must not outlive the session on a
    # shared machine, or it answers "which apps run here" after logout -- the question this
    # route sits behind the login gate to avoid answering.
    assert response.headers.get("Cache-Control") == "private, no-cache"
    assert "max-age" not in response.headers.get("Cache-Control", "")


def test_an_uncached_icon_is_a_404_and_never_a_fetch(monkeypatch, tmp_path):
    """A 404 here is the normal path for 26 of this host's containers, and the page renders a
    monogram. What it must never be is a request that goes out to a CDN."""
    _icon_dir(monkeypatch, tmp_path)
    client, _rpc, now = make_client()
    login(client, now)
    assert client.get("/icons/sonarr").status_code == 404


@pytest.mark.parametrize("slug", ["..%2f..%2fetc%2fpasswd", "..", "a.b", "X", "-x", "x" * 80])
def test_a_slug_that_is_not_a_slug_is_a_404(monkeypatch, tmp_path, slug):
    _icon_dir(monkeypatch, tmp_path).joinpath("sonarr.png").write_bytes(PNG_BYTES)
    client, _rpc, now = make_client()
    login(client, now)
    assert client.get(f"/icons/{slug}").status_code == 404


def test_a_merged_lan_pill_keeps_an_icon_known_only_for_its_twin():
    """A `-lan` router folds into its sibling, and ownership may be readable for only one of
    the two. Looking at the surviving pill's `raw` alone drops the icon from a pill that has
    one -- the same reason `detail` resolves across every merged router."""
    import casa_scruffy_net

    zones = {"zones": [{"items": [{"raw": "sonarr@docker", "name": "sonarr",
                                   "raws": ["sonarr@docker", "sonarr-lan@docker"]}]}]}
    casa_scruffy_net.link_pills(zones, [], lan_domain="casalan.com",
                                icon_for=lambda raw: "sonarr" if raw == "sonarr-lan@docker" else None)
    assert zones["zones"][0]["items"][0]["icon"] == "sonarr"


# ── elevated sessions (T47) ──────────────────────────────────────────────────────

def _elevate(client, passphrase=PASSPHRASE):
    return client.post("/api/elevate",
                       data={"csrf_token": csrf(client), "passphrase": passphrase})


def test_signing_in_does_not_elevate():
    """R0 and R1 are the session; R2 and R3 are a second, deliberate step."""
    client, _rpc, now = make_client()
    login(client, now)
    assert client.get_cookie("pe_elevated") is None


def test_the_right_passphrase_elevates():
    client, _rpc, now = make_client()
    login(client, now)
    response = _elevate(client)
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["expires_in"] == web_auth.ELEVATION_SECONDS
    assert client.get_cookie("pe_elevated") is not None


def test_the_wrong_passphrase_does_not():
    client, _rpc, now = make_client()
    login(client, now)
    response = _elevate(client, "not the passphrase")
    assert response.status_code == 403
    assert response.json["reason"] == "rejected"
    assert client.get_cookie("pe_elevated") is None


def test_a_wrong_passphrase_is_counted_by_the_lockout():
    """Otherwise this is an unlimited passphrase oracle for anyone holding a session -- the
    exact attacker elevation exists to stop."""
    client, rpc, now = make_client()
    login(client, now)
    rpc.calls.clear()
    _elevate(client, "not the passphrase")
    assert [c for c in rpc.calls if c[0] == "auth.record_failure"], rpc.calls


def test_a_right_passphrase_clears_the_count():
    client, rpc, now = make_client()
    login(client, now)
    rpc.calls.clear()
    _elevate(client)
    assert [c for c in rpc.calls if c[0] == "auth.record_success"], rpc.calls


def test_elevation_is_refused_while_locked():
    client, rpc, now = make_client()
    login(client, now)
    rpc.results[("auth.status", "alice")] = {"locked": True, "remaining_attempts": 0,
                                            "locked_until": now[0] + 900}
    response = _elevate(client)
    assert response.status_code == 429
    assert response.json["reason"] == "locked"
    assert client.get_cookie("pe_elevated") is None


def test_elevating_needs_a_csrf_token():
    """CSRF is enforced on both branches of the auth hook, so this holds either way -- what
    being in is_json_request() changes is that the refusal comes back as JSON rather than an
    HTML abort, which is what the fetch() calling it can actually read."""
    client, _rpc, now = make_client()
    login(client, now)
    response = client.post("/api/elevate", data={"passphrase": PASSPHRASE})
    assert response.status_code == 400
    assert client.get_cookie("pe_elevated") is None


def test_elevating_needs_a_session():
    """401 and JSON, not a redirect. This is what being in is_json_request() buys: a fetch()
    gets an answer it can read instead of the login page's HTML."""
    client, _rpc, _now = make_client()
    response = client.post("/api/elevate", data={"passphrase": PASSPHRASE})
    assert response.status_code == 401
    assert response.is_json
    assert client.get_cookie("pe_elevated") is None


def test_a_marker_carrying_a_stale_epoch_is_refused():
    """Belt and braces, and tested directly because the normal path cannot reach it: an epoch
    bump also invalidates the device token, so the session dies before the marker is read.
    This forges the state that would exist if it did not -- a current session, a marker minted
    under an older epoch -- because that is the state a passphrase change has to survive.
    """
    client, _rpc, now = make_client()
    login(client, now)
    app = client.application
    token = client.get_cookie("pe_auth").value
    stale = web_auth.make_elevation(
        ENV["PE_DASHBOARD_SECRET_KEY"], "alice", 7, token, now[0],
        credential=ENV["PE_OPERATOR_ALICE_PASSPHRASE_HASH"])
    client.set_cookie("pe_elevated", stale, domain="localhost")
    with _in_session(client):
        app.preprocess_request()
        assert app.elevated_operator() is None


def test_logging_out_clears_the_elevation():
    client, _rpc, now = make_client()
    login(client, now)
    _elevate(client)
    assert client.get_cookie("pe_elevated") is not None
    client.post("/logout", data={"csrf_token": csrf(client)})
    assert client.get_cookie("pe_elevated") is None


def _in_session(client, path="/api/containers/media/sonarr"):
    """A request context carrying this client's cookies, with the auth hook already run."""
    app = client.application
    jar = {c.key: c.value for c in client._cookies.values()} if hasattr(client, "_cookies") else {}
    return app.test_request_context(path, headers={
        "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())})


def test_require_elevation_refuses_an_unelevated_session():
    """403 with a typed reason, never a redirect: these arrive from fetch(), and a redirect
    would put the login page's HTML into a JSON handler."""
    client, _rpc, now = make_client()
    login(client, now)
    app = client.application
    with _in_session(client):
        app.preprocess_request()
        refusal = app.require_elevation()
    assert refusal is not None
    body, status = refusal
    assert status == 403
    assert body.json["reason"] == "elevation_required"


def test_require_elevation_allows_an_elevated_one():
    client, _rpc, now = make_client()
    login(client, now)
    _elevate(client)
    app = client.application
    with _in_session(client):
        app.preprocess_request()
        assert app.require_elevation() is None
        assert app.elevated_operator() == "alice"


def test_revoking_devices_ends_the_session_and_its_elevation():
    """What a revocation actually does, end to end: the device token carries the epoch it was
    minted under, so once core moves past it the whole session goes -- elevation with it.

    The previous version of this test checked elevated_operator() in a bare request context
    with no auth hook run, so g had no operator and it returned None whatever the epoch did.
    It passed with revocation completely broken. A request through the app cannot do that.
    """
    client, rpc, now = make_client()
    app = client.application

    @app.get("/api/elevated-probe-revoked")
    def probe_revoked():
        refusal = app.require_elevation()
        return refusal if refusal is not None else ("elevated", 200)

    login(client, now)
    _elevate(client)
    assert client.get("/api/elevated-probe-revoked").status_code == 200

    rpc.results[("auth.device_epoch", "alice")] = {"epoch": 9}
    rpc.results["auth.device_epoch"] = {"epoch": 9}
    assert client.get("/api/elevated-probe-revoked").status_code != 200
def test_an_elevation_does_not_survive_a_new_epoch():
    """A passphrase change bumps the epoch, which revokes every outstanding marker without
    having to find them."""
    client, rpc, now = make_client()
    login(client, now)
    _elevate(client)
    app = client.application
    with _in_session(client):
        app.preprocess_request()
        assert app.elevated_operator() == "alice"
    rpc.results[("auth.device_epoch", "alice")] = {"epoch": 9}
    rpc.results["auth.device_epoch"] = {"epoch": 9}
    with _in_session(client):
        # the device token carries the old epoch, so the session itself is gone too
        assert app.elevated_operator() is None


def test_working_keeps_the_elevation_alive():
    """The window slides on each elevated action. Without that it expires a flat ten minutes
    after the passphrase, and an operator part-way through editing a compose file gets asked
    again for no reason -- which is how people learn to keep a second tab elevated.
    """
    client, _rpc, now = make_client()
    app = client.application

    @app.get("/api/elevated-probe-alive")
    def probe_alive():
        refusal = app.require_elevation()
        if refusal is not None:
            return refusal
        app.elevation_acted()          # this probe stands in for a route that did the thing
        return ("elevated", 200)

    login(client, now)
    _elevate(client)
    for _ in range(3):                              # 3 x 500s = well past a 600s window
        now[0] += web_auth.ELEVATION_SECONDS - 100
        assert client.get("/api/elevated-probe-alive").status_code == 200
    assert now[0] - 1800000000 > web_auth.ELEVATION_SECONDS, "never left the original window"


def test_refreshing_cannot_outlive_the_cap():
    """Each elevated action slides the window; the cap is measured from the passphrase entry
    and carried through every refresh. Without that, clicking often enough would keep a
    session elevated forever -- which is the whole thing the cap exists to prevent.

    Exercised through a probe route because nothing calls require_elevation() yet: the seam
    has to be proven before the stack-control slice is built on it.
    """
    client, _rpc, now = make_client()
    app = client.application

    @app.get("/api/elevated-probe")
    def probe():                                    # registered before the first request
        refusal = app.require_elevation()
        return refusal if refusal is not None else ("elevated", 200)

    login(client, now)
    assert _elevate(client).status_code == 200
    first = now[0]

    elevated_for = 0
    for _ in range(30):                             # far more clicks than the cap allows
        now[0] += web_auth.ELEVATION_SECONDS - 1    # just inside the sliding window
        if client.get("/api/elevated-probe").status_code != 200:
            break
        elevated_for = now[0] - first
    else:
        raise AssertionError("refreshing never ran out; the cap is not being enforced")

    assert elevated_for <= web_auth.ELEVATION_CAP_SECONDS, (
        f"stayed elevated {elevated_for}s against a {web_auth.ELEVATION_CAP_SECONDS}s cap")
    assert client.get("/api/elevated-probe").status_code == 403


def test_revoking_an_operator_ends_an_elevation_at_once():
    """Not within a minute. The epoch cache is fine for deciding whether a page renders and
    not fine for authorising a stack going down: a minute is long enough to do it.
    """
    client, rpc, now = make_client()
    app = client.application

    @app.get("/api/elevated-probe-revoke")
    def probe_revoke():
        refusal = app.require_elevation()
        return refusal if refusal is not None else ("elevated", 200)

    login(client, now)
    _elevate(client)
    assert client.get("/api/elevated-probe-revoke").status_code == 200

    # Revoked this instant, with the cached epoch still well inside its 60 seconds.
    rpc.results[("auth.device_epoch", "alice")] = {"epoch": 11}
    rpc.results["auth.device_epoch"] = {"epoch": 11}
    assert client.get("/api/elevated-probe-revoke").status_code != 200


def test_elevating_answers_json_when_core_is_down():
    """The route is called by fetch(). An RpcError falling through to the HTML airlock gives
    it a login page to parse as JSON."""
    from planet_express.integrations.rpc import RpcError

    client, rpc, now = make_client()
    login(client, now)
    rpc.results["auth.status"] = RpcError("Authentication unavailable")
    response = _elevate(client)
    assert response.status_code == 503
    assert response.is_json
    assert response.json["reason"] == "unavailable"
    assert client.get_cookie("pe_elevated") is None


def test_the_attempt_that_locks_says_so():
    """Not a plain rejection with the fact buried in a flag: the countdown would then only
    appear when the operator tries again, which is the moment they are least inclined to."""
    client, rpc, now = make_client()
    login(client, now)
    rpc.results[("auth.record_failure", "alice")] = {
        "locked": True, "locked_until": now[0] + 900, "remaining_attempts": 0}
    response = _elevate(client, "not the passphrase")
    assert response.status_code == 429
    assert response.json["reason"] == "locked"
    assert response.json["locked_until"] == now[0] + 900
    assert client.get_cookie("pe_elevated") is None


def test_a_revoked_device_cannot_elevate_even_if_this_worker_had_not_noticed():
    """The multi-worker window. The auth hook accepts a device token against a cached epoch,
    so a worker up to a minute behind will authenticate a revoked device. Minting against a
    fresh epoch there would hand it a marker that every other worker then honours -- one
    elevated action bought by whichever worker had not caught up.
    """
    client, rpc, now = make_client()
    login(client, now)                       # token minted under epoch 0
    # Revoked at core. The worker's cached epoch is still 0 and still inside its 60 seconds,
    # so the session itself continues to authenticate.
    rpc.results[("auth.device_epoch", "alice")] = {"epoch": 3}
    rpc.results["auth.device_epoch"] = {"epoch": 3}
    response = _elevate(client)
    assert response.status_code == 401
    assert response.json["reason"] == "stale_session"
    assert client.get_cookie("pe_elevated") is None


def test_a_revoked_session_is_not_a_passphrase_oracle():
    """Checked before the submitted passphrase is looked at. Otherwise a revoked browser gets
    a minute of free guessing while spending the real operator's lockout budget.

    A side effect worth knowing: the fresh lookup also refreshes this worker's cache, so the
    very next request is refused by the auth hook instead. The revoked session gets one 401
    that tells it nothing about the passphrase, and then it is simply signed out.
    """
    client, rpc, now = make_client()
    login(client, now)
    rpc.results[("auth.device_epoch", "alice")] = {"epoch": 3}
    rpc.results["auth.device_epoch"] = {"epoch": 3}
    rpc.calls.clear()

    wrong = _elevate(client, "not the passphrase")
    assert wrong.status_code == 401
    assert wrong.json["reason"] == "stale_session"
    # Nothing about the passphrase was recorded, so it cannot burn the lockout budget...
    assert not [c for c in rpc.calls if c[0] == "auth.record_failure"], rpc.calls
    # ...and the answer says nothing about whether the guess was right.
    assert "rejected" not in wrong.get_data(as_text=True)


def test_the_correct_passphrase_tells_a_revoked_session_nothing_either():
    client, rpc, now = make_client()
    login(client, now)
    rpc.results[("auth.device_epoch", "alice")] = {"epoch": 3}
    rpc.results["auth.device_epoch"] = {"epoch": 3}
    rpc.calls.clear()

    right = _elevate(client)
    assert right.status_code == 401
    assert right.json["reason"] == "stale_session"
    assert not [c for c in rpc.calls if c[0] == "auth.record_success"], rpc.calls
    assert client.get_cookie("pe_elevated") is None


# ── raising your own ceiling (T47) ───────────────────────────────────────────────

def _config_client(changed_fields):
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": changed_fields, "locked_fields": []}
    rpc.results["config.apply"] = {"status": "activating", "errors": [], "reason": "",
                                   "changed_fields": changed_fields, "locked_fields": []}
    return client, rpc


def _apply(client, text="autonomy:\n  direct_request_risks: [R1, R2, R3]\n"):
    return client.post("/api/config/apply", data={
        "csrf_token": csrf(client), "text": text, "base_sha256": "a" * 64})


def test_an_unelevated_session_cannot_raise_its_own_ceiling():
    """The single edit that would undo the whole gate: autonomy holds the direct-request
    ceiling, so changing it unelevated would let an operator grant themselves R3 and then use
    it. Elevation is required to change the thing that decides what elevation is for."""
    client, rpc = _config_client(["autonomy"])
    response = _apply(client)
    assert response.status_code == 403
    assert response.json["reason"] == "elevation_required"
    assert not [c for c in rpc.calls if c[0] == "config.apply"], "it was applied anyway"


def test_an_elevated_session_can():
    client, rpc = _config_client(["autonomy"])
    _elevate(client)
    response = _apply(client)
    assert response.status_code == 200
    assert [c for c in rpc.calls if c[0] == "config.apply"]


@pytest.mark.parametrize("field", ["sudo_allowlist", "forbidden_stacks"])
def test_the_other_load_bearing_fields_need_it_too(field):
    """Same class of consequence as autonomy: what may run as root, and what may not be
    touched at all."""
    client, rpc = _config_client([field])
    assert _apply(client).status_code == 403
    assert not [c for c in rpc.calls if c[0] == "config.apply"]


def test_an_ordinary_config_edit_does_not_need_elevation():
    """Everything this gate is not for. Pausing a container is a normal day."""
    client, rpc = _config_client(["paused_containers", "links"])
    assert _apply(client).status_code == 200
    assert [c for c in rpc.calls if c[0] == "config.apply"]


def test_an_unreadable_validation_refuses_rather_than_guesses():
    """No answer about what a change touches is no basis for letting it through unelevated."""
    client, rpc = _config_client(["autonomy"])
    rpc.results["config.validate"] = {"ok": True, "errors": []}     # no changed_fields
    response = _apply(client)
    assert response.status_code == 503
    assert set(response.json) == {"error"}
    assert not [c for c in rpc.calls if c[0] == "config.apply"]


def test_an_elevated_request_that_did_nothing_does_not_slide_the_window():
    """The window slides when an elevated action happened, not when one was attempted.

    config.apply answers 200 for `conflict`, `locked` and `invalid` alike, so "the response
    was not an error" says nothing about whether anything was done. Without this, resubmitting
    a stale config would keep an otherwise idle session elevated up to the hour cap.
    """
    client, _rpc, now = make_client()
    app = client.application

    @app.get("/api/elevated-probe-idle")
    def probe_idle():
        refusal = app.require_elevation()
        return refusal if refusal is not None else ("checked, did nothing", 200)

    login(client, now)
    _elevate(client)
    for _ in range(3):
        now[0] += web_auth.ELEVATION_SECONDS - 100
        client.get("/api/elevated-probe-idle")
    # Past the original ten minutes, and nothing ever confirmed an action.
    assert client.get("/api/elevated-probe-idle").status_code == 403


def test_a_rejected_config_apply_does_not_slide_the_window():
    """Resubmitting a config that keeps being refused must not keep the session elevated.
    Checked by running the clock out, because the cookie is still present either way -- what
    differs is whether its window was pushed forward."""
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": ["autonomy"], "locked_fields": []}
    rpc.results["config.apply"] = {"status": "conflict", "errors": [],
                                   "reason": "someone else changed it",
                                   "changed_fields": ["autonomy"], "locked_fields": []}
    _elevate(client)

    now[0] += web_auth.ELEVATION_SECONDS - 100                # still inside the first window
    assert _apply(client).status_code == 200                  # refused by core, answered 200

    now[0] += web_auth.ELEVATION_SECONDS - 100                # past it, had it not slid
    assert _apply(client).json["reason"] == "elevation_required", (
        "a refused apply pushed the window forward")


# ── the elevation prompt (T47) ───────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent


def test_the_dashboard_carries_the_elevation_prompt():
    """A backend gate with no way to satisfy it is a feature removed, not secured: with
    sensitive config edits enabled, apply would 403 with nothing the operator could do."""
    page = (ROOT / "templates/dashboard.html").read_text()
    assert 'id="elevate-dialog"' in page
    assert 'id="elevate-passphrase"' in page and 'type="password"' in page
    # The script tags, not any mention: config.js is named in a comment much earlier in this
    # file, and comparing against that compares nothing.
    elevate = page.index("asset('elevate.js')")
    config = page.index("asset('config.js')")
    # Loaded before the tab that calls it, or window.peElevate is undefined when it is needed.
    assert elevate < config


def test_the_prompt_sends_csrf_and_keeps_no_passphrase():
    js = (ROOT / "static/elevate.js").read_text()
    assert "csrf_token" in js, "an unprotected POST of the passphrase"
    assert "/api/elevate" in js
    # Cleared on every exit, so a passphrase is never left sitting in the field behind a
    # closed dialog.
    assert js.count('input.value = ""') >= 2
    assert "innerHTML" not in js
    assert "console.log" not in js, "never log anything from the passphrase path"


def test_config_apply_retries_after_elevating():
    js = (ROOT / "static/config.js").read_text()
    assert "error.reason = data.reason" in js, "the refusal's reason is discarded"
    assert 'error.reason !== "elevation_required"' in js
    assert "window.peElevate" in js
    # Asked once and retried once; not a loop that re-prompts on every refusal.
    assert js.count("await send()") == 2


def test_cores_own_elevation_refusal_reaches_the_browser_as_a_403():
    """The race the pre-check cannot cover: core decides this needs elevation although the
    dashboard's earlier read did not. Answered as the same typed 403, so the browser prompts
    and retries instead of reporting an unknown outcome for something recoverable."""
    client, rpc = _config_client(["paused_containers"])     # pre-check sees nothing sensitive
    rpc.results["config.apply"] = {"status": "elevation_required", "errors": [],
                                   "reason": "needs the passphrase again",
                                   "changed_fields": ["autonomy"], "locked_fields": []}
    response = _apply(client)
    assert response.status_code == 403
    assert response.json["reason"] == "elevation_required"


def test_an_ordinary_config_edit_does_not_extend_an_elevated_window():
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": ["links"], "locked_fields": []}
    rpc.results["config.apply"] = {"status": "activating", "errors": [], "reason": "",
                                   "changed_fields": ["links"], "locked_fields": []}
    _elevate(client)

    now[0] += web_auth.ELEVATION_SECONDS - 100
    assert _apply(client).status_code == 200                # ordinary edit inside the window
    now[0] += web_auth.ELEVATION_SECONDS - 100              # past it, had the edit slid it

    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": ["autonomy"], "locked_fields": []}
    assert _apply(client).json["reason"] == "elevation_required", (
        "an ordinary edit kept the privileged window alive")


def test_a_locked_field_is_not_worth_a_passphrase_prompt():
    """With PE_ALLOW_SENSITIVE_CONFIG_EDITS off, a sensitive field appears in changed_fields
    AND locked_fields. Prompting there spends the operator's lockout attempts to arrive at
    `locked` regardless -- core checks locked first, and this mirrors it."""
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": ["autonomy"],
                                      "locked_fields": ["autonomy"]}
    rpc.results["config.apply"] = {"status": "locked", "errors": [],
                                   "reason": "Sensitive config edits require …",
                                   "changed_fields": ["autonomy"],
                                   "locked_fields": ["autonomy"]}
    response = _apply(client)
    assert response.status_code == 200
    assert response.json["status"] == "locked"
    assert [c for c in rpc.calls if c[0] == "config.apply"], "it never reached core"


def test_a_sensitive_field_that_is_not_locked_still_needs_the_passphrase():
    """The other side of it: with the switch on, the field is editable and elevation is the
    gate that remains."""
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": ["autonomy"], "locked_fields": []}
    response = _apply(client)
    assert response.status_code == 403
    assert response.json["reason"] == "elevation_required"
    assert not [c for c in rpc.calls if c[0] == "config.apply"]


# ── stack control (T47) ──────────────────────────────────────────────────────────

def _stack_client(outcome="started", message="ok"):
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["action.request"] = {"outcome": outcome, "message": message,
                                     "approval_id": None, "execution_id": "exec-1",
                                     "capabilities": {}}
    return client, rpc


def test_taking_a_stack_up_asks_core_for_the_right_action():
    client, rpc = _stack_client()
    response = client.post("/api/stacks/media/up", data={"csrf_token": csrf(client)})
    assert response.status_code == 200
    assert ("action.request", {"action": "compose.up_stack", "stack": "media",
                               "operator": "alice", "elevated": False}) in rpc.calls


def test_taking_a_stack_down_asks_for_the_down_action():
    client, rpc = _stack_client()
    client.post("/api/stacks/media/down", data={"csrf_token": csrf(client)})
    assert ("action.request", {"action": "compose.down_stack", "stack": "media",
                               "operator": "alice", "elevated": False}) in rpc.calls


@pytest.mark.parametrize("path", [
    "/api/stacks/media/sideways", "/api/stacks/media/restart",
    "/api/stacks/bad!name/up", "/api/stacks/" + "x" * 65 + "/up",
])
def test_only_up_and_down_are_stack_actions(path):
    """The verb is looked up in a fixed map, so a URL cannot name an action the registry
    happens to hold -- down_all and down_ingress are R3 and are not reachable from here."""
    client, rpc = _stack_client()
    response = client.post(path, data={"csrf_token": csrf(client)})
    assert response.status_code == 404
    assert not [c for c in rpc.calls if c[0] == "action.request"]


def test_a_stack_action_needing_elevation_comes_back_as_a_403():
    """Core decides, against the risk it computed. The route only translates, so the browser
    can prompt and retry."""
    client, _rpc = _stack_client(outcome="elevation_required",
                                 message="compose.down_stack needs your passphrase again.")
    response = client.post("/api/stacks/media/down", data={"csrf_token": csrf(client)})
    assert response.status_code == 403
    assert response.json["reason"] == "elevation_required"


def test_an_elevated_operator_says_so_to_core():
    client, rpc = _stack_client()
    _elevate(client)
    client.post("/api/stacks/media/down", data={"csrf_token": csrf(client)})
    assert ("action.request", {"action": "compose.down_stack", "stack": "media",
                               "operator": "alice", "elevated": True}) in rpc.calls


def test_stack_actions_need_csrf_and_a_session():
    client, rpc = _stack_client()
    assert client.post("/api/stacks/media/up").status_code == 400
    assert not [c for c in rpc.calls if c[0] == "action.request"]

    stranger, rpc2, _ = make_client()
    response = stranger.post("/api/stacks/media/up")
    assert response.status_code == 401 and response.is_json
    assert not rpc2.calls


def test_the_drawer_carries_stack_controls():
    page = (ROOT / "templates/dashboard.html").read_text()
    assert 'data-stack-act="up"' in page and 'data-stack-act="down"' in page
    assert "data-svc-restart" in page
    assert "stacks.js" in page
    # After elevate.js, which defines the prompt it calls.
    assert page.index("asset('elevate.js')") < page.index("asset('stacks.js')")


def test_stack_control_js_prompts_once_and_keeps_no_rule_of_its_own():
    js = (ROOT / "static/stacks.js").read_text()
    assert "window.peElevate" in js
    assert '"elevation_required"' in js
    # Delegated from document: #dashboard-live is replaced wholesale every 60 seconds.
    assert "document.addEventListener" in js
    # It must not decide which actions need a passphrase -- core does, against the risk it
    # computed. A list of action names or risk classes in here is the duplication that went
    # stale twice in this feature.
    #
    # Comments stripped first: the file explains the rule in prose, which is the point, and a
    # test that matches a comment is testing nothing. (Same trap as grepping for a name a file
    # only mentions in a note about it.)
    code = "\n".join(line for line in js.splitlines() if not line.strip().startswith("//"))
    for encoded in ("compose.down_stack", "compose.up_stack", "R1", "R2", "R3"):
        assert encoded not in code, f"stacks.js encodes {encoded}; that rule belongs to core"
    assert "innerHTML" not in js


def test_an_r1_action_does_not_renew_an_elevated_window():
    """Restarting containers all afternoon must not keep a privileged session alive. Core
    reports whether an action spent elevation; the route renews on that, not on success."""
    client, rpc, now = make_client()
    login(client, now)
    rpc.results["action.request"] = {"outcome": "started", "message": "ok", "approval_id": None,
                                     "execution_id": "e", "capabilities": {},
                                     "used_elevation": False}
    _elevate(client)

    now[0] += web_auth.ELEVATION_SECONDS - 100
    assert client.post("/api/stacks/media/up",
                       data={"csrf_token": csrf(client)}).status_code == 200
    now[0] += web_auth.ELEVATION_SECONDS - 100      # past the window, had the R1 renewed it

    rpc.results["config.validate"] = {"ok": True, "errors": [],
                                      "changed_fields": ["autonomy"], "locked_fields": []}
    assert _apply(client).json["reason"] == "elevation_required", (
        "an R1 action kept the privileged window alive")


def test_the_drawer_offers_no_down_for_an_ingress_stack():
    page = (ROOT / "templates/dashboard.html").read_text()
    assert "stack.ingress" in page, "the template does not consult core's ingress rule"
    # The template must not carry its own idea of which stacks are ingress.
    for guess in ('== "network"', "'network'", "traefik", "adguard"):
        assert guess not in page.split("data-stack-act")[1][:600], \
            f"the drawer restates the ingress rule ({guess}); that rule belongs to core"
