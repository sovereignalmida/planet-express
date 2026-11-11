"""rc14.8: the model broker's Core channel. v3's own model calls (Hermes, Farnsworth, Amy, Fry; LLM_PROVIDER=broker) go through the
broker's key and land in its record, next to the missions'. A real loopback listener in front of a fake vendor; no network beyond
127.0.0.1, no real key."""
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_model_broker import MODEL, REAL, FakeStore, Upstream

import casa_llm
import config
from planet_express.missions import model_broker as mb_mod
from planet_express.missions.model_broker import BrokerLimits, ModelBroker


class CoreLog:
    """Core's own event log (`Store.record_event`), as the Core channel writes to it."""
    def __init__(self):
        self.events = []

    def record_event(self, kind, **payload):
        self.events.append((kind, payload))


@pytest.fixture
def core(monkeypatch):
    upstream, store = Upstream(), FakeStore()
    store.store = CoreLog()
    made = []

    def build(limits=None, models=(MODEL,), upstream_port=None):
        broker = ModelBroker(
            upstream_host=f"127.0.0.1:{upstream_port or upstream.port}", key_env="UPSTREAM_KEY", models=list(models),
            limits=limits or BrokerLimits(), store=store, environ={"UPSTREAM_KEY": REAL},
            connect=lambda host, timeout: http.client.HTTPConnection(host, timeout=timeout))
        made.append(broker)
        return broker
    yield upstream, store, build
    for broker in made:
        broker.shutdown()
    upstream.close()


def post(endpoint, body, *, access=None, headers=None):
    base, code = endpoint
    conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps(body).encode(),
                 headers={"content-type": "application/json", "x-api-key": access or code, **(headers or {})})
    response = conn.getresponse()
    return response, response.read()


WEB = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}, {"type": "web_fetch_20260209", "name": "web_fetch"}]


def test_the_core_channel_relays_on_the_brokers_key_and_records_who_asked(core):
    upstream, store, build = core
    broker = build()
    endpoint = broker.start_core()
    assert mb_mod.core_endpoint() == endpoint and endpoint[0].startswith("http://127.0.0.1:")
    response, _ = post(endpoint, {"model": MODEL, "messages": []}, headers={"x-pe-purpose": "hermes"})
    assert response.status == 200
    (seen,) = upstream.seen
    assert seen["headers"]["x-api-key"] == REAL and "x-pe-purpose" not in seen["headers"]      # who asked stays with Core
    kinds = [k for k, _ in store.store.events]
    assert kinds == ["core.model.request"]
    payload = store.store.events[0][1]
    assert payload["purpose"] == "hermes" and payload["input_tokens"] == 11 and payload["model"] == MODEL
    assert store.events == []                                     # nothing in any mission's record


def test_the_core_channel_needs_its_access_code(core):
    _upstream, _store, build = core
    endpoint = build().start_core()
    response, _ = post(endpoint, {"model": MODEL, "messages": []}, access="sk-ant-pe-wrong")
    assert response.status == 401


def test_only_the_core_channel_may_use_the_vendors_web_tools(core):
    """A contained session must not reach the internet through the vendor on Core's key; Core's own research calls may, as they did
    calling the vendor directly. A gateway's own fields stay refused on both."""
    upstream, store, build = core
    broker = build()
    endpoint = broker.start_core()
    assert post(endpoint, {"model": MODEL, "messages": [], "tools": WEB})[0].status == 200
    assert post(endpoint, {"model": MODEL, "messages": [], "tools": [{"type": "code_execution_20260521", "name": "x"}]})[0].status == 400
    assert post(endpoint, {"model": MODEL, "messages": [], "plugins": [{"id": "web"}]})[0].status == 400
    assert mb_mod._vendor_side_reach({"model": MODEL, "tools": WEB}) is not None                 # a mission session: refused
    assert mb_mod._vendor_side_reach({"model": MODEL, "tools": WEB}, core=True) is None


def test_no_mission_state_gates_the_core_channel(core):
    _upstream, store, build = core
    store.status, store.revoked = "completed", True               # what would close a mission session
    endpoint = build().start_core()
    assert post(endpoint, {"model": MODEL, "messages": []})[0].status == 200


def test_the_core_channels_budget_starts_again_each_day(core, monkeypatch):
    _upstream, _store, build = core
    broker = build()
    broker.core_max_requests = 1                                  # Core's own daily budget, not a mission session's
    endpoint = broker.start_core()
    day = {"now": "2026-11-11"}
    monkeypatch.setattr(mb_mod.time, "strftime", lambda fmt, t=None: day["now"])
    assert post(endpoint, {"model": MODEL, "messages": []})[0].status == 200
    response, data = post(endpoint, {"model": MODEL, "messages": []})
    assert response.status == 403 and b"Core's model budget for today" in data
    day["now"] = "2026-11-12"
    assert post(endpoint, {"model": MODEL, "messages": []})[0].status == 200


def test_shutdown_closes_the_core_channel(core):
    _upstream, _store, build = core
    broker = build()
    broker.start_core()
    broker.shutdown()
    assert mb_mod.core_endpoint() is None


# ── v3 through the channel, with the official SDK ───────────────────────────────────────────────────────────────────────────────
class Vendor:
    """A fake vendor that answers real Messages responses: a thinking block first (as current models do), then the text; streamed
    when asked; refusing the vendor's server tools with a 400 when `refuse_tools` is set."""
    def __init__(self, text='{"ok": true}', refuse_tools=False):
        self.bodies, owner = [], self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                owner.bodies.append(body)
                if refuse_tools and body.get("tools"):
                    data = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "tools"}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn",
                           "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3},
                           "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}]}
                if not body.get("stream"):
                    data = json.dumps(message).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(event, data):
                    chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                send("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                                            "usage": {"input_tokens": 7, "output_tokens": 1}}})
                send("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                send("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                send("content_block_stop", {"type": "content_block_stop", "index": 0})
                send("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                       "usage": {"output_tokens": 3}})
                send("message_stop", {"type": "message_stop"})
                self.wfile.write(b"0\r\n\r\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def vendor_broker(core, monkeypatch):
    _upstream, store, build = core
    made = []

    def make(**kw):
        vendor = Vendor(**kw)
        made.append(vendor)
        broker = build(upstream_port=vendor.port)
        broker.start_core()
        monkeypatch.setattr(config, "LLM_PROVIDER", "broker")
        monkeypatch.setattr(config, "model_for", lambda tier, provider=None: MODEL)
        return vendor, store
    yield make
    for vendor in made:
        vendor.close()


def test_casa_llm_on_the_broker_reads_the_text_past_a_thinking_block(vendor_broker):
    vendor, store = vendor_broker()
    assert casa_llm.complete("system", "user", 512, purpose="farnsworth") == '{"ok": true}'
    (body,) = vendor.bodies
    assert body["model"] == MODEL and body["output_config"] == {"effort": "low"}
    assert store.store.events[-1][1]["purpose"] == "farnsworth"


def test_a_research_call_keeps_its_web_tools_through_the_broker(vendor_broker):
    vendor, _store = vendor_broker()
    client = casa_llm.broker_client("amy")
    message, researched = casa_llm.ask_with_web_tools(client, MODEL, "s", "u", 4000, max_search_uses=4, max_fetch_uses=4,
                                                      tools_optional=True)
    assert next(b.text for b in message.content if b.type == "text") == '{"ok": true}' and researched
    assert [t["type"] for t in vendor.bodies[0]["tools"]] == ["web_search_20260209", "web_fetch_20260209"]


def test_a_gateway_that_refuses_the_web_tools_gets_the_question_again_without_them(vendor_broker):
    vendor, _store = vendor_broker(refuse_tools=True)
    client = casa_llm.broker_client("fry")
    message, researched = casa_llm.ask_with_web_tools(client, MODEL, "s", "u", 4000, max_search_uses=4, max_fetch_uses=4,
                                                      tools_optional=True)
    assert next(b.text for b in message.content if b.type == "text") == '{"ok": true}' and researched is False
    assert "tools" in vendor.bodies[0] and "tools" not in vendor.bodies[-1]
    assert "not available" in vendor.bodies[-1]["messages"][0]["content"]          # the model is told, too


def test_without_the_core_channel_the_broker_provider_says_why():
    mb_mod._CORE = None
    with pytest.raises(RuntimeError, match="Core channel is not running"):
        casa_llm.broker_client("hermes")


def test_the_broker_provider_uses_the_brokers_own_small_and_large_models(monkeypatch):
    from types import SimpleNamespace
    fake = SimpleNamespace(model_broker=SimpleNamespace(
        models=["~anthropic/claude-sonnet-latest", "~anthropic/claude-haiku-latest"],
        session_env={"ANTHROPIC_DEFAULT_SONNET_MODEL": "~anthropic/claude-sonnet-latest",
                     "ANTHROPIC_SMALL_FAST_MODEL": "~anthropic/claude-haiku-latest"}))
    monkeypatch.setattr(config, "MISSIONS", fake)
    monkeypatch.setattr(config, "LLM_PROVIDER", "broker")
    assert config.model_for("small") == "~anthropic/claude-haiku-latest"
    assert config.model_for("large") == "~anthropic/claude-sonnet-latest"


def test_the_core_channels_code_is_a_secret_no_session_may_hold(core):
    _upstream, _store, build = core
    broker = build()
    _base, code = broker.start_core()
    assert code in broker.secret_values() and REAL in broker.secret_values()


def test_amy_and_fry_mark_an_unresearched_answer_whatever_the_model_claims(vendor_broker):
    """Review, rc14.8: the model's own say-so is not enough; the callers enforce it."""
    import casa_amy
    import casa_fry
    claims = '{"researched": true, "sources": ["https://x"], "sufficient_context": true, "category": "config_error"}'
    vendor_broker(text=claims, refuse_tools=True)
    diagnosis = casa_amy.diagnose("media", "web", "web", "crash loop", "logs")
    assert diagnosis["researched"] is False and diagnosis["sources"] == []
    requirements = casa_fry.onboard("https://example.org/project", "proj", "proj.example.org")
    assert requirements["sufficient_context"] is False and requirements["sources"] == []
    assert requirements["notes"].startswith("PE could not research this project")


def test_a_small_model_that_takes_no_effort_setting_is_asked_again_without_it(core, monkeypatch):
    """Review, rc14.8: the broker's small model is an alias; one that takes no effort (Haiku 4.5) answers 400 to it."""
    _upstream, _store, build = core

    class NoEffort(Vendor):
        pass
    vendor = NoEffort()
    original = vendor.server.RequestHandlerClass.do_POST

    def do_post(handler):
        length = int(handler.headers.get("Content-Length", "0"))
        raw = handler.rfile.read(length)
        body = json.loads(raw)
        if "output_config" in body:
            vendor.bodies.append(body)
            data = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "effort"}}).encode()
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        import io
        handler.rfile = io.BytesIO(raw)
        original(handler)
    vendor.server.RequestHandlerClass.do_POST = do_post
    try:
        broker = build(upstream_port=vendor.port)
        broker.start_core()
        monkeypatch.setattr(config, "LLM_PROVIDER", "broker")
        monkeypatch.setattr(config, "model_for", lambda tier, provider=None: MODEL)
        assert casa_llm.complete("s", "u", 256, purpose="hermes") == '{"ok": true}'
        assert "output_config" in vendor.bodies[0] and "output_config" not in vendor.bodies[-1]
    finally:
        vendor.close()


def test_v3_uses_the_broker_by_itself_whenever_it_runs_and_its_direct_provider_otherwise(core, monkeypatch):
    """No switch to set (rc14.8): the broker when its Core channel runs; LLM_PROVIDER is the fallback while it does not."""
    _upstream, _store, build = core
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    mb_mod._CORE = None
    assert casa_llm.provider() == "openai"
    broker = build()
    broker.start_core()
    assert casa_llm.provider() == "broker"
    broker.shutdown()
    assert casa_llm.provider() == "openai"


def test_with_no_tier_named_research_gets_a_sonnet_or_opus_never_the_smallest(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(config, "MISSIONS", SimpleNamespace(model_broker=SimpleNamespace(
        models=["claude-haiku-4-5", "claude-sonnet-4-6"], session_env={})))
    assert config.model_for("large", "broker") == "claude-sonnet-4-6"
    assert config.model_for("small", "broker") == "claude-haiku-4-5"


def test_a_refusal_that_is_not_about_the_tools_is_not_hidden_behind_a_retry(vendor_broker):
    """Review, rc14.8: a refused model or an oversized request must surface, not come back as "researched=False"."""
    import anthropic
    vendor, _store = vendor_broker()
    client = casa_llm.broker_client("amy")
    with pytest.raises(anthropic.BadRequestError):
        casa_llm.ask_with_web_tools(client, "not-allowed-model", "s", "u", 4000, max_search_uses=4, max_fetch_uses=4,
                                    tools_optional=True)
    assert vendor.bodies == []                                  # the broker refused it; nothing was asked again


def test_a_second_core_start_replaces_the_first_and_another_brokers_shutdown_leaves_it(core):
    _upstream, _store, build = core
    first, second = build(), build()
    old = first.start_core()
    new = first.start_core()
    assert old != new and mb_mod.core_endpoint() == new
    second.shutdown()                                           # a broker that never started it
    assert mb_mod.core_endpoint() == new
    first.shutdown()
    assert mb_mod.core_endpoint() is None


def test_farnsworths_diagnostic_pre_check_goes_through_the_broker_too(vendor_broker):
    """Codex, rc14.8: the pre-planning diagnostics and the chat investigation still called the vendor directly."""
    import casa_farnsworth
    vendor, store = vendor_broker(text="Done.")
    assert casa_farnsworth._gather_diagnostics('{"findings": []}') == []
    body = vendor.bodies[0]
    assert body["tools"][0]["name"] == casa_farnsworth.DIAGNOSTIC_TOOL_SCHEMA["name"]
    assert store.store.events[-1][1]["purpose"] == "farnsworth"


def test_a_research_model_that_takes_no_adaptive_thinking_is_asked_again_without_it(core, monkeypatch):
    """Codex, rc14.8: an existing broker config's Sonnet 4.5 refuses adaptive thinking and effort."""
    _upstream, _store, build = core
    vendor = Vendor()
    original = vendor.server.RequestHandlerClass.do_POST

    def do_post(handler):
        import io
        raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        if "thinking" in body:
            vendor.bodies.append(body)
            data = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                          "message": "adaptive thinking is not supported on this model"}}).encode()
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        handler.rfile = io.BytesIO(raw)
        original(handler)
    vendor.server.RequestHandlerClass.do_POST = do_post
    try:
        build(upstream_port=vendor.port).start_core()
        message, researched = casa_llm.ask_with_web_tools(casa_llm.broker_client("amy"), MODEL, "s", "u", 4000, max_search_uses=4,
                                                          max_fetch_uses=4, tools_optional=True)
        assert researched and next(b.text for b in message.content if b.type == "text") == '{"ok": true}'
        assert "thinking" in vendor.bodies[0] and "thinking" not in vendor.bodies[-1] and "tools" in vendor.bodies[-1]
    finally:
        vendor.close()


def test_a_runner_whose_recovery_fails_closes_the_core_channel():
    """Codex, rc14.8: start_mission_runner then returns None, and nothing would be left to stop the listener."""
    from planet_express.missions.runner import MissionRunner

    class Broker:
        stopped = started = False

        def _key(self):
            return "k"

        def start_core(self):
            Broker.started = True

        def shutdown(self):
            Broker.stopped = True

    class Supervisor:
        model_broker = Broker()

        def recover(self):
            raise RuntimeError("recovery broke")
    with pytest.raises(RuntimeError):
        MissionRunner(Supervisor()).start()
    assert Broker.started and Broker.stopped


def test_the_broker_client_ignores_an_outbound_proxy_for_its_loopback_hop(vendor_broker, monkeypatch):
    """Codex, rc14.8: with HTTP_PROXY set, the SDK sent the loopback call to the proxy, which cannot reach 127.0.0.1."""
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    vendor, _store = vendor_broker()
    assert casa_llm.complete("s", "u", 256, purpose="hermes") == '{"ok": true}'


def test_the_transport_comes_from_the_http_library_the_installed_sdk_uses(monkeypatch):
    """Codex, rc14.8: the host ran anthropic 0.94 on httpx, with no httpx2; importing httpx2 would have broken every v3 call."""
    import importlib
    from types import SimpleNamespace

    import anthropic
    monkeypatch.setattr(importlib, "import_module", lambda name: SimpleNamespace(__name__=name))
    for root in ("httpx", "httpx2"):                   # anthropic 0.x is built on httpx, 1.x and later on httpx2
        base = type("Client", (), {"__module__": f"{root}._client"})
        monkeypatch.setattr(anthropic, "DefaultHttpxClient", type("DefaultHttpxClient", (base,), {"__module__": "anthropic._base_client"}))
        assert casa_llm._sdk_http().__name__ == root
