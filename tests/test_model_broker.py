"""planet_express/missions/model_broker.py: a real loopback listener in front of a fake upstream on another loopback port.
No network beyond 127.0.0.1, no real key."""
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from planet_express.missions.model_broker import (
    ALLOWED_PATHS,
    HARDENING_ENV,
    BrokerLimits,
    BrokerUnavailable,
    ModelBroker,
)
from planet_express.missions.runner import SessionSockets

REAL = "REAL-UPSTREAM-KEY-0123456789"
MODEL = "claude-test-1"


class Upstream:
    """A fake vendor API. `script` decides the answer; every request it saw is kept."""

    def __init__(self):
        self.seen = []
        self.mode = "json"
        self.hold = threading.Event()
        self.first_sent = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if owner.mode == "json":
                    payload = json.dumps({"id": "m", "usage": {"input_tokens": 11, "output_tokens": 4}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("x-should-retry", "false")
                    self.send_header("anthropic-ratelimit-unified-status", "allowed")
                    self.send_header("Set-Cookie", "must=not-pass")
                    self.end_headers()
                    self.wfile.write(payload)
                elif owner.mode == "cache":
                    payload = json.dumps({"id": "m", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                               "cache_read_input_tokens": 1000}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif owner.mode == "count":
                    payload = json.dumps({"input_tokens": 42}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif owner.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()

                    def send(event, data):
                        chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    send("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9, "output_tokens": 1}}})
                    owner.first_sent.set()
                    owner.hold.wait(10)                        # the rest is withheld until the test has read the first event
                    send("message_delta", {"type": "message_delta", "usage": {"output_tokens": 6}})
                    send("message_stop", {"type": "message_stop"})
                    self.wfile.write(b"0\r\n\r\n")
                elif owner.mode == "huge":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(200_000))
                    self.end_headers()
                    self.wfile.write(b"x" * 200_000)
                elif owner.mode == "error":
                    payload = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}).encode()
                    self.send_response(529)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()


class FakeStore:
    def __init__(self):
        self.events = []
        self.revoked = False
        self.status = "executing"
        self.now, self.expires_at = 1000, 5000

    def get_session(self, session_id):
        return {"id": session_id, "mission_id": "M-1", "revoked_at": 1 if self.revoked else None, "expires_at": self.expires_at}

    def _now(self):
        return self.now

    def get_mission(self, mission_id):
        class Mission:
            pass
        mission = Mission()
        mission.status = self.status
        return mission

    def append_event(self, mission_id, kind, actor, body):
        self.events.append((mission_id, kind, actor, body))


class CS:
    session_id = "S-1"


@pytest.fixture
def rig():
    upstream, store = Upstream(), FakeStore()
    made = []

    def build(limits=None, environ=None, models=(MODEL,)):
        broker = ModelBroker(
            upstream_host=f"127.0.0.1:{upstream.port}", key_env="UPSTREAM_KEY", models=list(models), limits=limits or BrokerLimits(),
            store=store, environ={"UPSTREAM_KEY": REAL} if environ is None else environ,
            connect=lambda host, timeout: http.client.HTTPConnection(host, timeout=timeout))
        made.append(broker)
        return broker
    yield upstream, store, build
    for broker in made:
        broker.shutdown()
    upstream.close()


def ask(broker, *, path="/v1/messages", body=None, headers=None, method="POST", raw=None, access=None):
    env = broker.env(CS())
    base = env["ANTHROPIC_BASE_URL"].removeprefix("http://")
    conn = http.client.HTTPConnection(base, timeout=10)
    payload = raw if raw is not None else json.dumps(body if body is not None else {"model": MODEL, "messages": []}).encode()
    sent = {"content-type": "application/json", "x-api-key": access or env["ANTHROPIC_API_KEY"], **(headers or {})}
    if method == "POST":
        conn.request("POST", path, body=payload, headers=sent)
    else:
        conn.request(method, path, headers=sent)
    response = conn.getresponse()
    data = response.read()
    return response, data


def started(rig_build, **kwargs):
    broker = rig_build(**kwargs)
    broker.start(CS())
    return broker


# ── the happy path ──────────────────────────────────────────────────────────────────────────────────

def test_a_request_is_relayed_with_the_real_key_and_the_session_never_holds_it(rig):
    upstream, store, build = rig
    broker = started(build)
    env = broker.env(CS())
    assert REAL not in json.dumps(env) and env["ANTHROPIC_API_KEY"].startswith("sk-ant-pe-")
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:") and set(HARDENING_ENV) <= set(env)
    response, data = ask(broker, headers={"Authorization": "Bearer client-supplied", "anthropic-version": "2023-06-01",
                                          "anthropic-beta": "a,b"})
    assert response.status == 200 and json.loads(data)["usage"]["input_tokens"] == 11
    (seen,) = upstream.seen
    assert seen["headers"]["x-api-key"] == REAL and "authorization" not in seen["headers"]
    assert seen["headers"]["anthropic-version"] == "2023-06-01" and seen["headers"]["anthropic-beta"] == "a,b"
    assert json.loads(seen["body"])["model"] == MODEL                      # the body is relayed as it came
    assert response.getheader("x-should-retry") == "false" and response.getheader("anthropic-ratelimit-unified-status") == "allowed"
    assert response.getheader("Set-Cookie") is None                         # headers outside the fixed set stop here
    assert broker.usage("S-1") == {"requests": 1, "tokens": 15, "refused": 0}


def test_the_beta_query_is_kept_and_any_other_query_is_dropped(rig):
    upstream, _store, build = rig
    broker = started(build)
    ask(broker, path="/v1/messages?beta=true")
    ask(broker, path="/v1/messages?beta=true&evil=1")
    ask(broker, path="/v1/messages?other=1")
    assert [s["path"] for s in upstream.seen] == ["/v1/messages?beta=true", "/v1/messages?beta=true", "/v1/messages"]


def test_count_tokens_is_relayed_and_counted(rig):
    upstream, _store, build = rig
    upstream.mode = "count"
    broker = started(build)
    response, _ = ask(broker, path="/v1/messages/count_tokens")
    assert response.status == 200 and broker.usage("S-1")["tokens"] == 42


def test_a_stream_is_relayed_as_it_arrives_and_its_tokens_are_counted(rig):
    upstream, _store, build = rig
    upstream.mode = "stream"
    broker = started(build)
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "stream": True}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    response = conn.getresponse()
    assert upstream.first_sent.wait(5)
    first = response.read1(4096)                      # the first event arrives while the upstream is still holding the rest
    assert b"message_start" in first and b"message_stop" not in first
    upstream.hold.set()
    rest = response.read()
    assert b"message_stop" in rest
    for _ in range(50):                               # the counter is read after the relay finishes
        if broker.usage("S-1")["tokens"]:
            break
        threading.Event().wait(0.05)
    assert broker.usage("S-1")["tokens"] == 9 + 6


def test_each_request_leaves_one_event_with_no_body_in_it(rig):
    upstream, store, build = rig
    broker = started(build)
    ask(broker, body={"model": MODEL, "messages": [{"role": "user", "content": "the secret question"}]})
    broker.shutdown()
    kinds = [(e[2], e[3]["event"]) for e in store.events]
    assert kinds == [("model-broker", "model.request"), ("model-broker", "model.summary")]
    text = json.dumps(store.events)
    assert "secret question" not in text and REAL not in text and "sk-ant-pe-" not in text
    assert store.events[0][3]["model"] == MODEL and store.events[0][3]["status"] == 200


# ── what it refuses ─────────────────────────────────────────────────────────────────────────────────

def test_a_wrong_or_missing_access_code_is_refused_and_nothing_goes_upstream(rig):
    upstream, _store, build = rig
    broker = started(build)
    assert ask(broker, access="sk-ant-pe-wrong")[0].status == 401
    assert ask(broker, headers={"x-api-key": ""})[0].status == 401
    assert upstream.seen == []


def test_the_access_code_is_accepted_as_a_bearer_token_too(rig):
    _upstream, _store, build = rig
    broker = started(build)
    code = broker.env(CS())["ANTHROPIC_API_KEY"]
    response, _ = ask(broker, headers={"x-api-key": "", "Authorization": f"Bearer {code}"})
    assert response.status == 200


@pytest.mark.parametrize(("method", "path"), [("POST", "/v1/complete"), ("POST", "/v1/files"), ("GET", "/v1/models"),
                                              ("POST", "/v1/messages/batches"), ("POST", "/anything"), ("DELETE", "/v1/messages"),
                                              ("HEAD", "/api/hello")])
def test_only_the_two_message_paths_are_served(rig, method, path):
    upstream, _store, build = rig
    broker = started(build)
    response, _ = ask(broker, method=method, path=path)
    assert response.status == 404 and upstream.seen == [] and ALLOWED_PATHS == {"/v1/messages", "/v1/messages/count_tokens"}


def test_a_model_off_the_list_a_non_json_body_and_an_oversize_body_are_refused(rig):
    upstream, _store, build = rig
    broker = started(build, limits=BrokerLimits(max_request_bytes=2000))
    assert ask(broker, body={"model": "some-other-model"})[0].status == 400
    assert ask(broker, body={"messages": []})[0].status == 400
    assert ask(broker, raw=b"not json")[0].status == 400
    assert ask(broker, raw=b"[1,2]")[0].status == 400
    assert ask(broker, body={"model": MODEL, "system": "x" * 5000})[0].status == 413
    assert upstream.seen == []


def test_the_request_budget_ends_in_a_refusal_that_is_not_retryable(rig):
    upstream, store, build = rig
    broker = started(build, limits=BrokerLimits(max_requests=2))
    assert [ask(broker)[0].status for _ in range(2)] == [200, 200]
    response, data = ask(broker)
    assert response.status == 403 and json.loads(data)["error"]["type"] == "permission_error"       # 403, never a 429 the CLI would retry
    assert len(upstream.seen) == 2 and any(e[3]["event"] == "model.refused" and "budget" in e[3]["reason"] for e in store.events)


def test_the_token_budget_ends_it_too(rig):
    upstream, _store, build = rig
    # each request is promised its body size (40 bytes here) before it runs, and costs 15 tokens as the vendor counts them
    broker = started(build, limits=BrokerLimits(max_tokens=90))
    assert [ask(broker)[0].status for _ in range(4)] == [200, 200, 200, 200]       # 0, 15, 30, 45 spent + 40 promised each
    assert ask(broker)[0].status == 403 and len(upstream.seen) == 4                # 60 spent + 40 promised would be 100 > 90


def test_a_session_cap_lowers_the_token_ceiling_and_can_never_raise_it(rig):
    upstream, _store, build = rig
    broker = started(build, limits=BrokerLimits(max_tokens=10_000))
    assert broker.set_session_cap("S-1", 90) is True                               # a conversation's remaining budget
    assert [ask(broker)[0].status for _ in range(4)] == [200, 200, 200, 200]       # the same arithmetic as the 90-token broker above
    assert ask(broker)[0].status == 403 and len(upstream.seen) == 4
    assert broker.set_session_cap("S-1", 10**9) is True                            # asking for more changes nothing
    assert ask(broker)[0].status == 403
    assert broker.set_session_cap("S-nope", 5) is False


def test_a_mission_cap_set_before_the_session_exists_applies_to_its_next_session_once(rig):
    """The Console asks for the turn's ceiling before the supervisor opens the turn's endpoint: the cap must wait for that session."""
    upstream, _store, build = rig
    broker = build(limits=BrokerLimits(max_tokens=10_000))
    broker.set_mission_cap("M-1", 90)                                             # no session yet
    broker.set_mission_cap("M-1", 10**9)                                          # asking for more changes nothing
    broker.start(CS())
    assert [ask(broker)[0].status for _ in range(4)] == [200, 200, 200, 200]      # the 90-token arithmetic again
    assert ask(broker)[0].status == 403 and len(upstream.seen) == 4
    assert broker._mission_caps == {}                                             # consumed: the mission's next session starts clean


def test_a_session_cap_applies_to_its_own_session_only(rig):
    _upstream, _store, build = rig
    broker = started(build, limits=BrokerLimits(max_tokens=10_000))
    broker.set_session_cap("S-1", 0)
    assert ask(broker)[0].status == 403                                            # a zero remainder refuses at once


def test_parallel_requests_cannot_each_pass_a_budget_that_only_one_fits(rig):
    upstream, store, build = rig
    upstream.mode = "stream"
    broker = started(build, limits=BrokerLimits(max_tokens=150))
    env = broker.env(CS())
    conns = []
    for _ in range(3):
        conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
        conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 100}).encode(),
                     headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
        conns.append(conn)
    statuses = sorted(conn.getresponse().status for conn in conns)
    upstream.hold.set()
    assert statuses == [200, 403, 403] and len(upstream.seen) == 1             # only one 100-token promise fits in 150


def test_a_session_cannot_hold_more_connections_than_the_cap(rig):
    import socket
    _upstream, _store, build = rig
    broker = started(build, limits=BrokerLimits(max_connections=2, client_timeout_s=30))
    port = int(broker.env(CS())["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1])
    held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]     # two stalled clients fill the slots
    threading.Event().wait(0.3)
    third = socket.create_connection(("127.0.0.1", port), timeout=5)
    third.sendall(b"POST /v1/messages HTTP/1.1\r\n\r\n")
    assert third.recv(100) == b""                      # closed at once: no handler thread was given to it
    for conn in held + [third]:
        conn.close()


def test_a_client_that_never_finishes_its_request_is_cut_off(rig):
    import socket
    _upstream, _store, build = rig
    broker = started(build, limits=BrokerLimits(max_connections=1, client_timeout_s=0.5))
    port = int(broker.env(CS())["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1])
    stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
    stalled.sendall(b"POST /v1/messages HTTP/1.1\r\nContent-Length: 100\r\n")        # headers never finish
    stalled.settimeout(5)
    assert stalled.recv(100) == b""                    # the server gave up and closed
    assert ask(broker)[0].status == 200                # and the slot is free again
    stalled.close()


def test_refusals_are_audited_without_bodies_and_only_up_to_a_cap(rig):
    _upstream, store, build = rig
    broker = started(build, limits=BrokerLimits(max_refusals_logged=3))
    for _ in range(6):
        ask(broker, path="/v1/files?token=abc", body={"model": MODEL, "messages": "secret text"})
    broker.shutdown()
    refusals = [e[3] for e in store.events if e[3]["event"] == "model.refused"]
    assert len(refusals) == 3 and refusals[0]["status"] == 404 and refusals[0]["path"] == "/v1/files"
    assert "abc" not in json.dumps(store.events) and "secret text" not in json.dumps(store.events)
    assert store.events[-1][3]["refused"] == 6        # the summary still counts them all


def test_stopping_the_session_cuts_a_relay_that_is_still_running(rig):
    upstream, _store, build = rig
    upstream.mode = "stream"
    broker = build()
    stop = broker.start(CS())
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 5}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    response = conn.getresponse()
    assert upstream.first_sent.wait(5) and b"message_start" in response.read1(4096)
    stop()                                              # the upstream is still holding the rest of the stream
    with pytest.raises((http.client.IncompleteRead, ConnectionError, OSError)):
        rest = response.read()
        assert b"message_stop" not in rest              # either an error, or at least not the finished stream
        raise OSError("ended early")
    upstream.hold.set()


def test_a_revoked_session_or_a_mission_that_is_not_running_gets_nothing(rig):
    upstream, store, build = rig
    broker = started(build)
    assert ask(broker)[0].status == 200
    store.status = "paused_tripwire"
    assert ask(broker)[0].status == 403
    store.status = "executing"
    assert ask(broker)[0].status == 200
    store.revoked = True
    assert ask(broker)[0].status == 403
    assert len(upstream.seen) == 2


def test_an_expired_session_gets_nothing_even_if_nobody_revoked_it(rig):
    upstream, store, build = rig
    broker = started(build)
    assert ask(broker)[0].status == 200
    store.now = 5000
    response, data = ask(broker)
    assert response.status == 403 and "expired" in json.loads(data)["error"]["message"]
    assert len(upstream.seen) == 1


def test_a_tight_loop_after_the_budget_is_gone_writes_a_bounded_audit(rig):
    _upstream, store, build = rig
    broker = started(build, limits=BrokerLimits(max_requests=1, max_refusals_logged=4))
    assert ask(broker)[0].status == 200
    for _ in range(30):
        assert ask(broker)[0].status == 403
    audited = [e for e in store.events if e[3]["event"] in ("model.refused", "model.limit")]
    assert len(audited) == 4 and broker.usage("S-1")["refused"] == 30


def test_stopping_the_session_also_cuts_the_request_to_the_vendor(rig):
    upstream, _store, build = rig
    upstream.mode = "stream"
    broker = build()
    stop = broker.start(CS())
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 5}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    conn.getresponse().read1(4096)
    with broker._lock:
        session = broker._sessions["S-1"]
    assert session.upstreams                           # a vendor connection is open
    stop()
    assert not session.upstreams                       # and it was closed with the session
    upstream.hold.set()


def test_a_missing_key_stops_the_session_from_starting_and_later_loss_is_a_refusal(rig):
    _upstream, _store, build = rig
    with pytest.raises(BrokerUnavailable, match="UPSTREAM_KEY"):
        build(environ={}).start(CS())
    environ = {"UPSTREAM_KEY": REAL}
    broker = build(environ=environ)
    broker.start(CS())
    del environ["UPSTREAM_KEY"]
    assert ask(broker)[0].status == 503


def test_an_upstream_that_is_down_is_a_502_and_an_upstream_error_passes_through_unmodified(rig):
    upstream, _store, build = rig
    upstream.mode = "error"
    broker = started(build)
    response, data = ask(broker)
    assert response.status == 529 and json.loads(data)["error"]["type"] == "overloaded_error"
    dead = ModelBroker(upstream_host="127.0.0.1:1", key_env="K", models=[MODEL], limits=BrokerLimits(), store=FakeStore(),
                       environ={"K": REAL}, connect=lambda host, timeout: http.client.HTTPConnection(host, timeout=timeout))
    dead.start(CS())
    try:
        assert ask(dead)[0].status == 502
    finally:
        dead.shutdown()


def test_a_response_over_the_cap_is_cut_off(rig):
    upstream, _store, build = rig
    upstream.mode = "huge"
    broker = started(build, limits=BrokerLimits(max_response_bytes=30_000))
    try:
        _response, data = ask(broker)
    except http.client.IncompleteRead as exc:
        data = exc.partial
    assert 0 < len(data) <= 30_000 + 8192


# ── lifecycle ───────────────────────────────────────────────────────────────────────────────────────

def test_stopping_closes_the_listener_and_records_a_summary(rig):
    _upstream, store, build = rig
    broker = build()
    stop = broker.start(CS())
    port = broker.env(CS())["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1]
    ask(broker)
    stop()
    with pytest.raises(OSError):
        http.client.HTTPConnection(f"127.0.0.1:{port}", timeout=2).request("POST", "/v1/messages", body=b"{}")
    assert store.events[-1][3]["event"] == "model.summary" and store.events[-1][3]["requests"] == 1
    assert broker.env(CS()) == {} and broker.usage("S-1") is None


def test_two_sessions_have_their_own_port_and_their_own_code(rig):
    _upstream, store, build = rig
    broker = build()
    other = type("Other", (), {"session_id": "S-2"})
    broker.start(CS())
    broker.start(other)
    one, two = broker.env(CS()), broker.env(other)
    assert one["ANTHROPIC_BASE_URL"] != two["ANTHROPIC_BASE_URL"] and one["ANTHROPIC_API_KEY"] != two["ANTHROPIC_API_KEY"]
    response, _ = ask(broker, access=two["ANTHROPIC_API_KEY"])                   # session 2's code at session 1's port
    assert response.status == 401


def test_the_real_key_is_offered_to_the_environment_builder_for_scanning(rig):
    _upstream, _store, build = rig
    assert build().secret_values() == [REAL] and build(environ={}).secret_values() == []


# ── plugged into the per-session sockets ────────────────────────────────────────────────────────────

def test_session_sockets_start_the_broker_and_stop_it_with_the_session(rig, tmp_path):
    _upstream, _store, build = rig
    broker = build()

    class Server:
        stopped = False

        def start(self):
            pass

        def stop(self):
            Server.stopped = True
    sockets = SessionSockets(str(tmp_path), None, None, build=lambda *a, **k: Server(), model_broker=broker)
    stop = sockets.start(CS())
    assert "ANTHROPIC_BASE_URL" in sockets.env(CS()) and broker.usage("S-1") is not None
    stop()
    assert Server.stopped and broker.usage("S-1") is None


def test_session_sockets_do_not_start_a_session_the_broker_cannot_serve(rig, tmp_path):
    _upstream, _store, build = rig

    class Server:
        stopped = False

        def start(self):
            pass

        def stop(self):
            Server.stopped = True
    sockets = SessionSockets(str(tmp_path), None, None, build=lambda *a, **k: Server(), model_broker=build(environ={}))
    with pytest.raises(BrokerUnavailable):
        sockets.start(CS())
    assert Server.stopped and not (tmp_path / "S-1").exists()


def test_a_pause_or_revoke_ends_a_stream_that_is_already_running(rig):
    upstream, store, build = rig
    upstream.mode = "stream"
    broker = started(build, limits=BrokerLimits(recheck_s=0))
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=15)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 5}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    response = conn.getresponse()
    assert upstream.first_sent.wait(5) and b"message_start" in response.read1(4096)
    store.status = "paused_tripwire"                   # the tripwire pauses the mission while the stream is open
    upstream.hold.set()                                # the upstream now sends the rest, a second later the broker rechecks
    rest = response.read()
    assert b"message_stop" not in rest                 # the relay stopped at the recheck, before the stream finished


def test_access_closing_during_the_vendor_connect_stops_the_request_before_it_is_sent(rig):
    upstream, _store, build = rig
    broker = started(build)
    original = broker._permitted
    calls = []

    def closes_after_the_first_check(session):
        calls.append(1)
        return None if len(calls) == 1 else "the mission is paused_tripwire"
    broker._permitted = closes_after_the_first_check
    response, _ = ask(broker)
    assert response.status == 502 and upstream.seen == [] and len(calls) == 2     # connected, looked again, did not send
    broker._permitted = original


def test_an_upstream_that_drops_after_the_headers_does_not_get_a_second_status_line(rig):
    import socket
    _upstream, _store, build = rig
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead.listen(1)
    port = dead.getsockname()[1]

    def upstream_that_hangs_up():
        conn, _ = dead.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        conn.close()
    threading.Thread(target=upstream_that_hangs_up, daemon=True).start()
    broker = ModelBroker(upstream_host=f"127.0.0.1:{port}", key_env="K", models=[MODEL], limits=BrokerLimits(), store=FakeStore(),
                         environ={"K": REAL}, connect=lambda h, t: http.client.HTTPConnection(h, timeout=t))
    broker.start(CS())
    try:
        env = broker.env(CS())
        raw = socket.create_connection((env["ANTHROPIC_BASE_URL"].removeprefix("http://").split(":")[0],
                                        int(env["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1])), timeout=10)
        body = json.dumps({"model": MODEL}).encode()
        raw.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: " + env["ANTHROPIC_API_KEY"].encode()
                    + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        data = b""
        while chunk := raw.recv(4096):
            data += chunk
        assert data.count(b"HTTP/1.") == 1                  # one status line only
        raw.close()
    finally:
        broker.shutdown()
        dead.close()


def test_a_pause_closes_model_access_until_the_operator_resumes(rig):
    upstream, _store, build = rig
    broker = started(build)
    assert ask(broker)[0].status == 200
    broker.pause("M-1")
    response, data = ask(broker)
    assert response.status == 403 and "paused" in json.loads(data)["error"]["message"] and len(upstream.seen) == 1
    broker.resume("M-1", "grant-hash")
    assert ask(broker)[0].status == 200 and len(upstream.seen) == 2


def test_a_pause_is_forgotten_when_the_session_ends(rig):
    _upstream, _store, build = rig
    broker = build()
    stop = broker.start(CS())
    broker.pause("M-1")
    stop()
    broker.start(CS())                                  # the next session of that mission starts open
    assert ask(broker)[0].status == 200


def test_an_upstream_timeout_is_recorded_as_the_504_the_client_got(rig):
    import socket
    _upstream, store, build = rig
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    port = silent.getsockname()[1]
    broker = ModelBroker(upstream_host=f"127.0.0.1:{port}", key_env="K", models=[MODEL], limits=BrokerLimits(timeout_s=0.5),
                         store=store, environ={"K": REAL}, connect=lambda h, t: http.client.HTTPConnection(h, timeout=t))
    broker.start(CS())
    try:
        assert ask(broker)[0].status == 504
    finally:
        broker.shutdown()
        silent.close()
    assert [e[3]["status"] for e in store.events if e[3]["event"] == "model.request"] == [504]


def test_a_response_cut_short_is_charged_what_it_was_promised(rig):
    upstream, _store, build = rig
    upstream.mode = "stream"
    broker = started(build, limits=BrokerLimits(max_tokens=10_000))
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 500}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    response = conn.getresponse()
    assert upstream.first_sent.wait(5) and b"message_start" in response.read1(4096)
    broker.pause("M-1")                                 # the vendor request is cut before the stream's end marker arrives
    for _ in range(100):
        if broker.usage("S-1")["tokens"]:
            break
        threading.Event().wait(0.05)
    upstream.hold.set()
    assert broker.usage("S-1")["tokens"] >= 500         # the promise, not the 10 tokens seen so far


def test_a_stream_that_finished_is_charged_only_what_the_vendor_counted(rig):
    upstream, _store, build = rig
    upstream.mode = "stream"
    upstream.hold.set()
    broker = started(build, limits=BrokerLimits(max_tokens=10_000))
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 500}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    conn.getresponse().read()
    for _ in range(100):
        if broker.usage("S-1")["tokens"]:
            break
        threading.Event().wait(0.05)
    assert broker.usage("S-1")["tokens"] == 9 + 6


def test_a_pause_cuts_a_request_that_is_already_with_the_vendor(rig):
    upstream, _store, build = rig
    upstream.mode = "stream"
    broker = started(build)
    env = broker.env(CS())
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 5}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    response = conn.getresponse()
    assert upstream.first_sent.wait(5) and b"message_start" in response.read1(4096)
    with broker._lock:
        session = broker._sessions["S-1"]
    assert session.upstreams
    broker.pause("M-1")
    for _ in range(100):
        if not session.upstreams:
            break
        threading.Event().wait(0.05)
    assert not session.upstreams                         # the vendor connection is gone without waiting for the stream
    upstream.hold.set()


def test_a_finished_request_frees_its_promise_in_the_same_step_it_is_charged(rig):
    _upstream, _store, build = rig
    broker = started(build, limits=BrokerLimits(max_tokens=100))
    assert ask(broker)[0].status == 200
    with broker._lock:
        session = broker._sessions["S-1"]
    assert session.reserved == 0 and session.tokens == 15


def test_the_broker_key_is_stripped_from_any_session_environment_by_name_and_by_value(rig):
    from planet_express.missions.runner import without_broker_key
    _upstream, _store, build = rig
    broker = build()
    strip = without_broker_key(None, broker)
    env = {"LANG": "C", "UPSTREAM_KEY": REAL, "INNOCENT": f"prefix-{REAL}-suffix", "FINE": "yes"}
    assert strip(None, "M-1", "/w", env) == {"LANG": "C", "FINE": "yes"}
    wrapped = without_broker_key(lambda grant, mission, workspace, base: {**base, "ADDED": REAL}, broker)
    assert "ADDED" not in wrapped(None, "M-1", "/w", {"LANG": "C"})


@pytest.mark.parametrize("extra", [
    {"mcp_servers": [{"type": "url", "url": "https://attacker.example/mcp", "name": "x"}]},
    {"container": "container_011"},
    {"tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}]},
    {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
    {"tools": [{"name": "ok", "input_schema": {}}, {"type": "code_execution_20250522", "name": "code_execution"}]},
])
def test_the_vendor_is_never_asked_to_reach_out_for_the_agent(rig, extra):
    upstream, _store, build = rig
    broker = started(build)
    response, data = ask(broker, body={"model": MODEL, "max_tokens": 5, "messages": [], **extra})
    assert response.status == 400 and "not allowed for missions" in json.loads(data)["error"]["message"] and upstream.seen == []


def test_the_agents_own_tools_are_relayed(rig):
    upstream, _store, build = rig
    broker = started(build)
    tools = [{"name": "Read", "description": "d", "input_schema": {"type": "object"}},
             {"type": "custom", "name": "Write", "input_schema": {"type": "object"}}]
    assert ask(broker, body={"model": MODEL, "max_tokens": 5, "messages": [], "tools": tools})[0].status == 200
    assert len(upstream.seen) == 1


def test_a_refusal_after_a_large_body_is_actually_delivered_not_lost_to_a_reset(rig):
    _upstream, _store, build = rig
    broker = started(build)
    big = {"model": MODEL, "system": "x" * 2_000_000}
    for _ in range(5):
        response, data = ask(broker, body=big, access="sk-ant-pe-wrong")
        assert response.status == 401 and json.loads(data)["error"]["type"] == "authentication_error"
    broker.pause("M-1")
    response, data = ask(broker, body={"model": MODEL, "system": "x" * 2_000_000})
    assert response.status == 403 and "paused" in json.loads(data)["error"]["message"]


def test_cache_reads_count_a_tenth_against_the_budget(rig):
    upstream, _store, build = rig
    upstream.mode = "cache"
    broker = started(build)
    assert ask(broker)[0].status == 200
    assert broker.usage("S-1")["tokens"] == 10 + 5 + 100


def test_one_session_stopping_does_not_reopen_a_mission_that_is_still_paused(rig):
    _upstream, _store, build = rig
    broker = build()
    broker.start(CS())
    other = type("Other", (), {"session_id": "S-2"})
    stop_other = broker.start(other)
    broker.pause("M-1")
    stop_other()                                        # a second session of the same mission goes away
    response, _ = ask(broker)
    assert response.status == 403                       # the mission is still paused for the one that remains


def test_a_request_that_reached_the_vendor_but_died_unexpectedly_is_charged_its_promise(rig):
    upstream, _store, build = rig
    broker = started(build)
    original = broker._record

    def explode(session, event, body):
        if event == "model.request":
            raise RuntimeError("the audit write failed")
        return original(session, event, body)
    broker._record = explode
    try:
        ask(broker, body={"model": MODEL, "max_tokens": 200, "messages": []})
    except Exception:                                   # the client may see the connection end
        pass
    broker._record = original
    for _ in range(50):
        if broker.usage("S-1")["tokens"]:
            break
        threading.Event().wait(0.05)
    with broker._lock:
        session = broker._sessions["S-1"]
    assert session.reserved == 0                         # nothing is left promised, and no request is un-accounted


def test_a_gateway_mounted_under_a_path_with_bearer_auth_is_reached_that_way(rig):
    upstream, _store, build = rig
    broker = ModelBroker(upstream_host=f"127.0.0.1:{upstream.port}", key_env="K", models=["~anthropic/claude-sonnet-latest"],
                         limits=BrokerLimits(), store=FakeStore(), environ={"K": REAL}, path_prefix="/api", auth_scheme="bearer",
                         connect=lambda h, t: http.client.HTTPConnection(h, timeout=t))
    broker.start(CS())
    try:
        response, _ = ask(broker, path="/v1/messages?beta=true", body={"model": "~anthropic/claude-sonnet-latest", "messages": []})
        assert response.status == 200
    finally:
        broker.shutdown()
    (seen,) = upstream.seen
    assert seen["path"] == "/api/v1/messages?beta=true"
    assert seen["headers"]["authorization"] == f"Bearer {REAL}" and "x-api-key" not in seen["headers"]


def test_session_env_adds_the_model_mapping_but_never_replaces_the_broker_settings(rig):
    _upstream, _store, build = rig
    broker = ModelBroker(upstream_host="127.0.0.1:1", key_env="K", models=[MODEL], limits=BrokerLimits(), store=FakeStore(),
                         environ={"K": REAL}, session_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "~anthropic/claude-haiku-latest",
                                                           "ANTHROPIC_BASE_URL": "https://evil.example"})
    broker.start(CS())
    try:
        env = broker.env(CS())
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "~anthropic/claude-haiku-latest"
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")          # the broker's own value always wins
    finally:
        broker.shutdown()


@pytest.mark.parametrize("field", ["plugins", "models", "provider", "route", "transforms", "web_search_options", "user", "stop", "anything_new"])
def test_a_field_outside_the_messages_api_is_refused_unless_the_operator_allowed_it(rig, field):
    upstream, _store, build = rig
    broker = started(build)
    response, data = ask(broker, body={"model": MODEL, "max_tokens": 5, "messages": [], field: {"x": 1}})
    assert response.status == 400 and field in json.loads(data)["error"]["message"] and upstream.seen == []
    allowing = build(models=(MODEL,))
    allowing.extra_body_keys = frozenset({field})                      # what `allow_body_keys` sets
    allowing.start(type("Other", (), {"session_id": "S-9"}))
    env = allowing.env(type("Other", (), {"session_id": "S-9"}))
    conn = http.client.HTTPConnection(env["ANTHROPIC_BASE_URL"].removeprefix("http://"), timeout=10)
    conn.request("POST", "/v1/messages", body=json.dumps({"model": MODEL, "max_tokens": 5, "messages": [], field: 1}).encode(),
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"], "content-type": "application/json"})
    assert conn.getresponse().status == 200


def test_every_field_the_real_cli_sends_is_allowed():
    from planet_express.missions.model_broker import ALLOWED_BODY_KEYS
    seen = {"context_management", "max_tokens", "messages", "metadata", "model", "output_config", "stream", "system", "thinking", "tools"}
    assert seen <= ALLOWED_BODY_KEYS


def test_the_prefix_is_applied_to_count_tokens_and_the_default_scheme_still_sends_x_api_key(rig):
    upstream, _store, build = rig
    broker = ModelBroker(upstream_host=f"127.0.0.1:{upstream.port}", key_env="K", models=[MODEL], limits=BrokerLimits(),
                         store=FakeStore(), environ={"K": REAL}, path_prefix="/api",
                         connect=lambda h, t: http.client.HTTPConnection(h, timeout=t))
    broker.start(CS())
    try:
        upstream.mode = "count"
        assert ask(broker, path="/v1/messages/count_tokens")[0].status == 200
    finally:
        broker.shutdown()
    (seen,) = upstream.seen
    assert seen["path"] == "/api/v1/messages/count_tokens" and seen["headers"]["x-api-key"] == REAL
    assert "authorization" not in seen["headers"]


def test_an_unknown_auth_scheme_is_refused_at_construction():
    with pytest.raises(ValueError, match="auth_scheme"):
        ModelBroker(upstream_host="h", key_env="K", models=[MODEL], limits=BrokerLimits(), store=FakeStore(), auth_scheme="Bearer")


def test_an_upstream_that_echoes_the_key_has_its_response_cut(rig):
    import socket
    _upstream, store, build = rig
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def echoing_upstream():
        conn, _ = server.accept()
        conn.recv(65536)
        payload = (b'{"error":"bad key ' + REAL.encode() + b'"}')
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        conn.close()
    threading.Thread(target=echoing_upstream, daemon=True).start()
    broker = ModelBroker(upstream_host=f"127.0.0.1:{server.getsockname()[1]}", key_env="K", models=[MODEL], limits=BrokerLimits(),
                         store=store, environ={"K": REAL}, connect=lambda h, t: http.client.HTTPConnection(h, timeout=t))
    broker.start(CS())
    try:
        try:
            _response, data = ask(broker)
        except (http.client.IncompleteRead, http.client.HTTPException, OSError):
            data = b""
        assert REAL.encode() not in data
    finally:
        broker.shutdown()
        server.close()
    assert any(e[3].get("reason") == "the upstream echoed the key" for e in store.events)
