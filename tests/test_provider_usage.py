"""rc14.7: credit and spend as the model provider reports them, shown and alerted on, never enforced."""
import json
import types

import pytest
from pydantic import ValidationError

from config_schema import ModelBrokerConfig, ProviderUsageConfig
from planet_express.missions import provider_usage as pu
from planet_express.missions.model_broker import _UsageCounter

KEY = "sk-or-v1-SECRETSECRETSECRET"
ADMIN = "sk-ant-admin01-SECRETADMIN"
NOW = 1_794_268_800.0          # 2026-11-10T00:00:00Z


def cfg(**kw):
    return ProviderUsageConfig(**kw)


class Provider:
    """A fake provider: answers by path, records each call's host, path and headers."""

    def __init__(self, credits=(100.0, 91.6), key=None, fail=None):
        self.calls = []
        self.credits = credits
        self.key = key if key is not None else {"usage": 7.62, "usage_daily": 1.48, "usage_weekly": 3.0, "usage_monthly": 4.74,
                                                "limit": None, "limit_remaining": None}
        self.fail = fail
        self.cost_pages = []

    def __call__(self, host, path, headers):
        self.calls.append((host, path, dict(headers)))
        if self.fail:
            raise self.fail
        if path.endswith("/v1/credits"):
            return {"data": {"total_credits": self.credits[0], "total_usage": self.credits[1]}}
        if path.endswith("/v1/key"):
            return {"data": self.key}
        if path.startswith("/v1/organizations/cost_report"):
            return self.cost_pages.pop(0)
        raise AssertionError(path)


def usage(tmp_path, *, host="openrouter.ai", fetch=None, notify=None, environ=None, clock=lambda: NOW, **kw):
    return pu.ProviderUsage(upstream_host=host, path_prefix="/api" if "openrouter" in host else "", key_env="OPENROUTER_KEY",
                            usage_cfg=cfg(**kw), state_path=tmp_path / "provider_usage.json",
                            environ={"OPENROUTER_KEY": KEY} if environ is None else environ, fetch=fetch or Provider(),
                            clock=clock, notify=notify)


# ── OpenRouter ────────────────────────────────────────────────────────────────────────────────
def test_openrouter_credit_left_and_this_keys_spend(tmp_path):
    provider = Provider()
    snap = usage(tmp_path, fetch=provider).refresh()
    assert snap["source"] == "openrouter" and snap["available"] and snap["state"] == "warn"
    assert snap["credit_left"] == pytest.approx(8.4) and snap["left"] == pytest.approx(8.4)
    assert snap["spend"] == {"day": 1.48, "week": 3.0, "month": 4.74, "all": 7.62} and snap["spend_scope"] == "this key"
    paths = [p for _, p, _ in provider.calls]
    assert paths == ["/api/v1/key", "/api/v1/credits"]
    assert all(h == "openrouter.ai" and hd["authorization"] == f"Bearer {KEY}" for h, _, hd in provider.calls)


def test_openrouter_state_follows_thresholds(tmp_path):
    assert usage(tmp_path, fetch=Provider(credits=(100, 91.6))).refresh()["state"] == "warn"       # $8.40 < $10
    assert usage(tmp_path, fetch=Provider(credits=(100, 98.5))).refresh()["state"] == "low"        # $1.50 < $2
    assert usage(tmp_path, fetch=Provider(credits=(100, 50))).refresh()["state"] == "ok"


def test_a_key_limit_lower_than_the_credit_is_what_is_left(tmp_path):
    provider = Provider(credits=(100, 10), key={"limit": 20, "limit_remaining": 5.0, "usage": 15})
    snap = usage(tmp_path, fetch=provider).refresh()
    assert snap["credit_left"] == 90 and snap["key_limit_left"] == 5.0 and snap["left"] == 5.0 and snap["state"] == "warn"


def test_unreadable_account_credit_falls_back_to_this_keys_own_limit(tmp_path):
    def fetch(host, path, headers):
        if path.endswith("/v1/credits"):
            raise pu.UsageError("the provider answered HTTP 403")
        return {"data": {"limit": 20, "limit_remaining": 1.5, "usage_daily": 0.4}}
    snap = usage(tmp_path, fetch=fetch).refresh()
    assert snap["available"] and snap["credit_left"] is None and snap["left"] == 1.5 and snap["state"] == "low"

    def no_limit(host, path, headers):
        if path.endswith("/v1/credits"):
            raise pu.UsageError("the provider answered HTTP 403")
        return {"data": {"limit": None}}
    snap = usage(tmp_path / "b", fetch=no_limit).refresh()
    assert not snap["available"] and "403" in snap["error"]


def test_no_key_is_an_error_and_nothing_is_fetched(tmp_path):
    provider = Provider()
    snap = usage(tmp_path, fetch=provider, environ={}).refresh()
    assert not snap["available"] and snap["state"] == "unknown" and "OPENROUTER_KEY" in snap["error"] and provider.calls == []


def test_a_failed_read_keeps_the_last_figures_and_says_so(tmp_path):
    now = [NOW]
    provider = Provider()
    u = usage(tmp_path, fetch=provider, clock=lambda: now[0])
    u.refresh()
    provider.fail = pu.UsageError("the provider answered HTTP 503")
    now[0] += 3600
    snap = u.refresh()
    assert snap["available"] and snap["credit_left"] == pytest.approx(8.4)
    assert snap["error"] == "the provider answered HTTP 503" and snap["stale"] and snap["age_s"] == 3600
    provider.fail = None
    assert u.refresh()["error"] is None


def test_a_malformed_reply_is_an_error_not_a_crash(tmp_path):
    snap = usage(tmp_path, fetch=lambda h, p, hd: {"data": {"total_credits": "lots"}}).refresh()
    assert not snap["available"] and "no figures" in snap["error"]
    snap = usage(tmp_path, fetch=lambda h, p, hd: ["not", "a", "dict"]).refresh()
    assert not snap["available"] and "AttributeError" in snap["error"]


def test_no_key_ever_reaches_the_view_or_the_state_file(tmp_path):
    u = usage(tmp_path, fetch=Provider(fail=pu.UsageError("the provider answered HTTP 401")))
    u.refresh()
    u._fetch = Provider()
    u.refresh()
    blob = json.dumps(u.snapshot()) + (tmp_path / "provider_usage.json").read_text()
    assert KEY not in blob and "SECRET" not in blob


# ── Anthropic ─────────────────────────────────────────────────────────────────────────────────
def test_anthropic_without_an_admin_key_says_how_to_get_spend(tmp_path):
    provider = Provider()
    snap = usage(tmp_path, host="api.anthropic.com", fetch=provider).refresh()
    assert snap["source"] == "anthropic" and not snap["available"] and "admin_key_env" in snap["error"] and provider.calls == []


def test_anthropic_cost_report_in_cents_across_pages(tmp_path):
    provider = Provider()
    provider.cost_pages = [
        {"data": [{"starting_at": "2026-11-01T00:00:00Z", "results": [{"amount": "250.5"}, {"amount": "49.5"}]},
                  {"starting_at": "2026-11-05T00:00:00Z", "results": [{"amount": "100"}]}],
         "has_more": True, "next_page": "page_2"},
        {"data": [{"starting_at": "2026-11-10T00:00:00Z", "results": [{"amount": "120"}, {"amount": None}]}], "has_more": False},
    ]
    snap = usage(tmp_path, host="api.anthropic.com", fetch=provider, admin_key_env="ANTHROPIC_ADMIN_KEY",
                 environ={"ANTHROPIC_ADMIN_KEY": ADMIN}).refresh()
    assert snap["available"] and snap["left"] is None and snap["state"] == "ok" and snap["spend_scope"] == "the organisation"
    assert snap["spend"] == {"day": 1.2, "week": 2.2, "month": 5.2, "all": None}
    (h1, p1, hd1), (_, p2, _) = provider.calls
    assert h1 == "api.anthropic.com" and hd1["x-api-key"] == ADMIN and hd1["anthropic-version"]
    assert "starting_at=2026-11-01T00%3A00%3A00Z" in p1 and "bucket_width=1d" in p1 and "limit=31" in p1 and "page=page_2" in p2


def test_anthropic_week_reaches_into_the_previous_month_early_in_a_month(tmp_path):
    provider = Provider()
    provider.cost_pages = [{"data": [{"starting_at": "2026-10-29T00:00:00Z", "results": [{"amount": "300"}]},
                                     {"starting_at": "2026-11-02T00:00:00Z", "results": [{"amount": "100"}]}], "has_more": False}]
    snap = usage(tmp_path, host="api.anthropic.com", fetch=provider, admin_key_env="ANTHROPIC_ADMIN_KEY",
                 environ={"ANTHROPIC_ADMIN_KEY": ADMIN}, clock=lambda: NOW - 8 * 86400).refresh()       # 2026-11-02
    assert "starting_at=2026-10-27T00%3A00%3A00Z" in provider.calls[0][1]
    assert snap["spend"]["week"] == 4.0 and snap["spend"]["month"] == 1.0 and snap["spend"]["day"] == 1.0


def test_anthropic_totals_past_the_page_limit_are_marked_partial(tmp_path):
    provider = Provider()
    provider.cost_pages = [{"data": [{"starting_at": "2026-11-10T00:00:00Z", "results": [{"amount": "100"}]}], "has_more": True,
                            "next_page": f"p{i}"} for i in range(pu.MAX_COST_PAGES)]
    snap = usage(tmp_path, host="api.anthropic.com", fetch=provider, admin_key_env="ANTHROPIC_ADMIN_KEY",
                 environ={"ANTHROPIC_ADMIN_KEY": ADMIN}).refresh()
    assert snap["partial"] is True and len(provider.calls) == pu.MAX_COST_PAGES


def test_the_admin_key_is_kept_out_of_sessions_like_the_model_key():
    from planet_express.missions.model_broker import BrokerLimits, ModelBroker
    from planet_express.missions.runner import without_broker_key
    broker = ModelBroker(upstream_host="api.anthropic.com", key_env="ANTHROPIC_API_KEY", models=["m"], limits=BrokerLimits(),
                         store=None, environ={"ANTHROPIC_API_KEY": "sk-ant-model", "ANTHROPIC_ADMIN": ADMIN},
                         extra_secret_envs=["ANTHROPIC_ADMIN", ""])
    assert broker.secret_values() == ["sk-ant-model", ADMIN]
    policy = without_broker_key(None, broker)
    out = policy(None, "M-1", "/w", {"ANTHROPIC_ADMIN": "x", "OTHER": "has " + ADMIN, "FINE": "ok"})
    assert out == {"FINE": "ok"}


def test_another_provider_has_no_source_and_no_poller(tmp_path):
    provider = Provider()
    u = usage(tmp_path, host="gateway.example", fetch=provider)
    snap = u.refresh()
    assert snap["source"] == "other" and "no usage source" in snap["note"].lower() and provider.calls == []
    u.start()
    assert u._thread is None


# ── alerts ────────────────────────────────────────────────────────────────────────────────────
def test_one_alert_per_level_and_it_rearms_above_warn(tmp_path):
    sent = []
    provider = Provider(credits=(100, 91.6))
    u = usage(tmp_path, fetch=provider, notify=sent.append)
    u.refresh(); u.refresh()
    assert len(sent) == 1 and "$8.40" in sent[0] and "Nothing has been stopped" in sent[0]
    provider.credits = (100, 98.5)
    u.refresh(); u.refresh()
    assert len(sent) == 2 and "$1.50" in sent[1] and "🔴" in sent[1]
    provider.credits = (100, 95)                       # a part top-up: still amber, no alert
    u.refresh()
    assert len(sent) == 2
    provider.credits = (100, 98.9)                     # back to red: alerts again
    u.refresh()
    assert len(sent) == 3
    provider.credits = (200, 98.9)                     # topped up above warn: re-armed
    u.refresh()
    provider.credits = (200, 191)
    u.refresh()
    assert len(sent) == 4 and "$9.00" in sent[3]


def test_a_restart_does_not_repeat_an_alert(tmp_path):
    sent = []
    usage(tmp_path, fetch=Provider(credits=(100, 91.6)), notify=sent.append).refresh()
    again = usage(tmp_path, fetch=Provider(credits=(100, 91.6)), notify=sent.append)
    assert again.snapshot()["available"] and again.snapshot()["credit_left"] == pytest.approx(8.4)     # last figures, before any read
    again.refresh()
    assert len(sent) == 1


def test_an_alert_that_could_not_be_sent_is_tried_again(tmp_path):
    attempts = []

    def notify(text):
        attempts.append(text)
        if len(attempts) == 1:
            raise OSError("telegram down")
    u = usage(tmp_path, fetch=Provider(credits=(100, 91.6)), notify=notify)
    u.refresh(); u.refresh(); u.refresh()
    assert len(attempts) == 2


def test_a_saved_file_from_another_source_is_ignored(tmp_path):
    (tmp_path / "provider_usage.json").write_text(json.dumps({"source": "anthropic", "figures": {"left": 1}, "fetched_at": NOW,
                                                              "alerted": "low"}))
    u = usage(tmp_path)
    assert not u.snapshot()["available"] and u._alerted is None
    (tmp_path / "provider_usage.json").write_text("not json")
    assert not usage(tmp_path).snapshot()["available"]


# ── the HTTP reader ───────────────────────────────────────────────────────────────────────────
class FakeConn:
    status, body, raises = 200, b'{"data": {}}', None

    def __init__(self, host, timeout):
        self.host = host

    def request(self, method, path, headers):
        if FakeConn.raises:
            raise FakeConn.raises

    def getresponse(self):
        return types.SimpleNamespace(status=FakeConn.status, read=lambda n: FakeConn.body)

    def close(self):
        pass


def test_https_json_errors_carry_no_body_or_key(monkeypatch):
    monkeypatch.setattr(pu.http.client, "HTTPSConnection", FakeConn)
    assert pu.https_json("openrouter.ai", "/api/v1/key", {"authorization": "Bearer " + KEY}) == {"data": {}}
    FakeConn.status, FakeConn.body = 401, b'{"error": "bad key ' + KEY.encode() + b'"}'
    with pytest.raises(pu.UsageError) as err:
        pu.https_json("openrouter.ai", "/api/v1/key", {})
    assert str(err.value) == "the provider answered HTTP 401"
    FakeConn.status, FakeConn.body = 200, b"x" * (pu.MAX_REPLY + 1)
    with pytest.raises(pu.UsageError, match="too large"):
        pu.https_json("openrouter.ai", "/", {})
    FakeConn.body, FakeConn.raises = b"{}", TimeoutError("slow")
    with pytest.raises(pu.UsageError, match="TimeoutError"):
        pu.https_json("openrouter.ai", "/", {})
    FakeConn.raises = None


# ── config, builder, broker cost ──────────────────────────────────────────────────────────────
def test_config_checks():
    with pytest.raises(ValidationError):
        ProviderUsageConfig(warn_below=1, low_below=5)
    with pytest.raises(ValidationError):
        ProviderUsageConfig(admin_key_env="PE_ADMIN")
    with pytest.raises(ValidationError):
        ProviderUsageConfig(poll_s=5)
    assert ModelBrokerConfig().usage.enabled and ModelBrokerConfig().usage.poll_s == 300


def test_build_only_with_the_broker_on(tmp_path):
    assert pu.build(ModelBrokerConfig(), state_dir=tmp_path) is None
    on = ModelBrokerConfig(enabled=True, models=["m"], upstream_host="openrouter.ai", upstream_path_prefix="/api")
    built = pu.build(on, state_dir=tmp_path)
    assert built.source == "openrouter" and built.prefix == "/api" and built.state_path == tmp_path / "provider_usage.json"
    off = ModelBrokerConfig(enabled=True, models=["m"], usage={"enabled": False})
    assert pu.build(off, state_dir=tmp_path) is None


def test_broker_records_the_vendors_own_cost():
    counter = _UsageCounter()
    counter.feed(b'{"usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0123}}')
    counter.finish()
    assert counter.cost == pytest.approx(0.0123)
    stream = _UsageCounter()
    stream.feed(b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}\n'
                b'data: {"type": "message_delta", "usage": {"output_tokens": 5, "cost": 0.02}}\n')
    assert stream.cost == pytest.approx(0.02)
    none = _UsageCounter()
    none.feed(b'{"usage": {"input_tokens": 1, "cost": true}}')
    none.finish()
    assert none.cost is None
