"""The widget fetcher: the one part of v2.2 that sends a credential somewhere.

Every test here is about where a request may go, what it may carry, what may come back to the
page, and what an app's answer can cost. The unit tests use a fake http.client connection that
records what was sent; the attacks a gate review reproduced -- a 3xx body read and gunzipped
before any check, unbounded chunk lines, a server trickling a byte at a time -- are run against
a real socket server, because a fake could only agree with the code.
"""
import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.widgets import fetcher, registry  # noqa: E402

SECRET = "s3cr3t-sonarr-key-value"
KEY = ("media", "sonarr")


def _summarise(responses):
    (queue,) = responses
    return {"stats": [{"k": "QUEUE", "v": queue["totalRecords"]}], "rows": [], "line": None}


def _widget(**over):
    widget = {"name": "sonarr", "match": ["linuxserver/sonarr"], "port": 8989,
              "auth": {"type": "header", "header": "X-Api-Key", "env": "SONARR_API_KEY"},
              "get": ["/api/v3/queue"], "summarise": _summarise}
    widget.update(over)
    return widget


def _target(**over):
    target = {"container": "CASA_SONARR", "container_id": "a" * 64, "running": True,
              "widget": "sonarr", "addresses": ["172.18.0.5"]}
    target.update(over)
    return target


# ── a fake http.client connection ────────────────────────────────────────────────

class _Reply:
    length = None           # http.client's bytes still owed by Content-Length; none here

    def __init__(self, status=200, body=b'{"totalRecords": 3}', headers=None, chunks=None):
        self.status = status
        self._chunks = list(chunks) if chunks is not None else [body]
        self._headers = headers or {}
        self.read_calls = 0

    def getheader(self, name):
        return self._headers.get(name)

    def read1(self, amt):
        self.read_calls += 1
        return self._chunks.pop(0) if self._chunks else b""


class _Sock:
    def __init__(self):
        self.timeouts = []
        self.shut = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shut = True


class _Wire:
    """Every connection made, keyed to the answers by (address, path)."""

    def __init__(self, answers):
        self.answers = answers
        self.sent = []          # (address, port, path, headers)
        self.connects = []

    def __call__(self, address, port, timeout):
        wire = self

        class Conn:
            sock = None

            def connect(self):
                wire.connects.append((address, port))
                if (address, None) in wire.answers and isinstance(wire.answers[(address, None)], OSError):
                    raise wire.answers[(address, None)]
                self.sock = _Sock()

            def request(self, method, path, headers):
                assert method == "GET"
                self.path = path
                wire.sent.append((address, port, path, dict(headers)))

            def getresponse(self):
                answer = wire.answers.get((address, self.path))
                if isinstance(answer, BaseException):
                    raise answer
                if answer is None:
                    raise ConnectionResetError("no answer")
                return answer

            def close(self):
                pass
        return Conn()


def _fetcher(answers, env=None, widgets=None, clock=None):
    wire = _Wire(answers)
    now = clock or [1000.0]
    f = fetcher.WidgetFetcher(
        env={"SONARR_API_KEY": SECRET} if env is None else env,
        widgets={"sonarr": _widget()} if widgets is None else widgets,
        connection_factory=wire, clock=lambda: now[0], wall=lambda: 1_800_000_000.0,
        start=lambda fn: fn())
    return f, wire, now


A = "172.18.0.5"
Q = "/api/v3/queue"


def test_a_good_fetch_returns_the_summary_and_never_the_key():
    f, wire, _ = _fetcher({(A, Q): _Reply()})
    answer = f.fetch(_target(), KEY)
    assert answer["state"] == "ok" and answer["stats"] == [{"k": "QUEUE", "v": 3}]
    assert SECRET not in json.dumps(answer)
    address, port, path, headers = wire.sent[0]
    assert (address, port, path) == (A, 8989, Q)
    assert headers["X-Api-Key"] == SECRET and headers["Accept-Encoding"] == "identity"


def test_basic_auth_is_one_authorization_header():
    widget = _widget(name="adguard", auth={"type": "basic", "username_env": "ADGUARD_USERNAME",
                                           "password_env": "ADGUARD_PASSWORD"},
                     port=80, get=["/control/stats"])
    f, wire, _ = _fetcher({(A, "/control/stats"): _Reply()},
                          env={"ADGUARD_USERNAME": "u", "ADGUARD_PASSWORD": "p"},
                          widgets={"adguard": widget})
    f.fetch(_target(widget="adguard"), KEY)
    assert wire.sent[0][3]["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()


@pytest.mark.parametrize("status", [301, 302, 307, 401, 500])
def test_any_answer_but_200_is_an_error_and_its_body_is_never_read(status):
    """A redirect is not followed, and not read: requests read and gunzipped a whole 3xx body
    inside session.get() -- the reason this fetcher speaks http.client."""
    reply = _Reply(status=status)
    f, wire, _ = _fetcher({(A, Q): reply})
    answer = f.fetch(_target(), KEY)
    assert answer["state"] == "error" and answer["status"] == status
    assert reply.read_calls == 0 and len(wire.sent) == 1


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "0.0.0.0",
                                     "100.64.0.1", "240.0.0.1", "fd00::5", "not-an-ip",
                                     "172.18.0.5:80", ""])
def test_only_an_rfc1918_ipv4_address_is_ever_called(address):
    f, wire, _ = _fetcher({})
    assert f.fetch(_target(addresses=[address]), KEY)["state"] == "error"
    assert wire.connects == []


def test_an_address_that_refuses_the_connection_falls_through_to_the_next():
    f, wire, _ = _fetcher({("192.168.1.53", None): ConnectionRefusedError(), (A, Q): _Reply()})
    assert f.fetch(_target(addresses=["192.168.1.53", A]), KEY)["state"] == "ok"
    assert [s[0] for s in wire.sent] == [A]


def test_an_answer_from_the_first_address_is_final_even_when_it_is_an_error():
    """A 401 is the app speaking; trying another address would only resend the key."""
    f, wire, _ = _fetcher({(A, Q): _Reply(status=401)})
    assert f.fetch(_target(addresses=[A, "172.19.0.5"]), KEY)["status"] == 401
    assert len(wire.connects) == 1


def test_a_dropped_connection_after_sending_is_not_retried_elsewhere():
    f, wire, _ = _fetcher({(A, Q): ConnectionResetError("reset")})
    answer = f.fetch(_target(addresses=[A, "172.19.0.5"]), KEY)
    assert answer["error"] == "connection dropped" and len(wire.sent) == 1


def test_a_refused_second_path_is_not_a_reason_to_try_another_address():
    """The first path already delivered the key to this address."""
    widget = _widget(get=[Q, "/api/v3/health"], summarise=lambda r: {"stats": [{"k": "X", "v": 1}]})
    wire_answers = {(A, Q): _Reply()}

    class Refusing(_Wire):
        def __call__(self, address, port, timeout):
            conn = super().__call__(address, port, timeout)
            original = conn.connect

            def connect():
                if len(self.connects) == 1:
                    self.connects.append((address, port))
                    raise ConnectionRefusedError()
                original()
            conn.connect = connect
            return conn
    wire = Refusing(wire_answers)
    f = fetcher.WidgetFetcher(env={"SONARR_API_KEY": SECRET}, widgets={"sonarr": widget},
                              connection_factory=wire, start=lambda fn: fn())
    answer = f.fetch(_target(addresses=[A, "172.19.0.5"]), KEY)
    assert answer["error"] == "connection dropped"
    assert {s[0] for s in wire.sent} == {A}


def test_a_missing_key_is_needs_key_and_calls_nothing():
    f, wire, _ = _fetcher({(A, Q): _Reply()}, env={})
    assert f.fetch(_target(), KEY) == {"widget": "sonarr", "via": Q, "state": "needs_key",
                                       "env": ["SONARR_API_KEY"]}
    assert wire.connects == []


@pytest.mark.parametrize("key", ["p€ss", "line\r\nX-Evil: 1", "tab\nnewline"])
def test_a_key_http_cannot_carry_is_a_fixed_reason_never_a_crash(key):
    f, wire, _ = _fetcher({(A, Q): _Reply()}, env={"SONARR_API_KEY": key})
    answer = f.fetch(_target(), KEY)
    assert answer["error"] == "the configured key cannot be sent over HTTP"
    assert wire.connects == [] and key not in json.dumps(answer)


def test_a_widget_can_never_read_an_env_var_not_named_after_it():
    widget = _widget(auth={"type": "header", "header": "X-Api-Key", "env": "PE_DASHBOARD_SECRET_KEY"})
    f, wire, _ = _fetcher({(A, Q): _Reply()}, env={"PE_DASHBOARD_SECRET_KEY": "x" * 40},
                          widgets={"sonarr": widget})
    assert f.fetch(_target(), KEY)["state"] == "error" and wire.connects == []


@pytest.mark.parametrize("target", [{"widget": None}, {"widget": "unknown"}, {}, None, "x", {"widget": 5}])
def test_no_widget_is_state_none(target):
    f, _, _ = _fetcher({})
    assert f.fetch(target, KEY) == {"state": "none"}


def test_a_stopped_container_is_not_called():
    f, wire, _ = _fetcher({(A, Q): _Reply()})
    assert f.fetch(_target(running=False), KEY)["state"] == "error" and wire.connects == []


def test_the_byte_budget_covers_the_whole_fetch_not_each_path():
    """Parsed JSON is ~25x its wire size; a per-path cap multiplied by paths is the bill."""
    big = [b" " * (1024 * 1024)]
    paths = ["/a", "/b", "/c"]
    answers = {(A, p): _Reply(chunks=big + [b"{}"]) for p in paths}
    widget = _widget(get=paths, max_bytes=int(2.5 * 1024 * 1024),
                     summarise=lambda r: {"stats": [{"k": "X", "v": 1}]})
    f, _, _ = _fetcher(answers, widgets={"sonarr": widget})
    assert f.fetch(_target(), KEY)["error"] == "answer too large"


def test_a_compressed_answer_is_refused_and_never_decoded():
    reply = _Reply(headers={"Content-Encoding": "gzip"}, body=b"\x1f\x8b")
    f, _, _ = _fetcher({(A, Q): reply})
    assert f.fetch(_target(), KEY)["error"] == "answer was compressed"
    assert reply.read_calls == 0


@pytest.mark.parametrize("body", [b"<html>login</html>", b"[" * 100000,
                                  b'{"totalRecords": NaN}', b'{"totalRecords": Infinity}'])
def test_an_answer_that_is_not_strict_json_is_an_error(body):
    f, _, _ = _fetcher({(A, Q): _Reply(body=body)})
    assert f.fetch(_target(), KEY)["error"] == "answer was not JSON"


def test_an_overflowing_number_never_reaches_the_page_as_infinity():
    f, _, _ = _fetcher({(A, Q): _Reply(body=b'{"totalRecords": 1e999}')})
    answer = f.fetch(_target(), KEY)
    assert answer["stats"][0]["v"] == "—"
    json.dumps(answer, allow_nan=False)


def test_a_summarise_bug_costs_the_widget_not_the_page():
    def broken(responses):
        raise KeyError("records")
    f, _, _ = _fetcher({(A, Q): _Reply()}, widgets={"sonarr": _widget(summarise=broken)})
    assert f.fetch(_target(), KEY)["error"] == "widget failed to read the answer"


def test_a_fresh_answer_is_served_from_the_cache_without_core_or_the_app():
    f, _, now = _fetcher({(A, Q): _Reply()})
    answer = f.fetch(_target(), KEY)
    assert f.cached(KEY) == answer and f.cached(("media", "radarr")) is None
    now[0] += fetcher.CACHE_SECONDS
    assert f.cached(KEY) is None


def test_no_widget_is_cached_too_so_core_is_not_asked_again():
    f, _, _ = _fetcher({})
    f.fetch(_target(widget=None), KEY)
    assert f.cached(KEY) == {"state": "none"}


def test_an_error_after_a_good_fetch_keeps_the_last_good_values_as_stale():
    answers = {(A, Q): _Reply()}
    f, _, now = _fetcher(answers)
    good = f.fetch(_target(), KEY)
    answers[(A, Q)] = _Reply(status=401)
    now[0] += fetcher.CACHE_SECONDS
    answer = f.fetch(_target(), KEY)
    assert answer["status"] == 401 and answer["stale"]["stats"] == good["stats"]
    assert answer["stale_at"] == good["fetched_at"]


def test_a_recreated_container_never_shows_its_predecessors_values():
    answers = {(A, Q): _Reply()}
    f, _, now = _fetcher(answers)
    f.fetch(_target(), KEY)
    answers[(A, Q)] = _Reply(status=500)
    now[0] += fetcher.CACHE_SECONDS
    assert "stale" not in f.fetch(_target(container_id="b" * 64), KEY)


def test_a_thread_that_cannot_start_releases_its_slot():
    def cannot(fn):
        raise RuntimeError("can't start new thread")
    f = fetcher.WidgetFetcher(env={"SONARR_API_KEY": SECRET}, widgets={"sonarr": _widget()},
                              connection_factory=_Wire({}), start=cannot)
    assert f.fetch(_target(), KEY)["error"] == "busy"
    assert f._inflight == {}


def test_a_stuck_fetch_holds_its_own_slot_and_the_request_thread_waits_only_its_budget(monkeypatch):
    monkeypatch.setattr(fetcher, "FETCH_BUDGET_SECONDS", 0.2)
    release = threading.Event()

    class Stuck(_Wire):
        def __call__(self, address, port, timeout):
            conn = super().__call__(address, port, timeout)
            conn.getresponse = lambda: (release.wait(5), _Reply())[1]
            return conn
    f = fetcher.WidgetFetcher(env={"SONARR_API_KEY": SECRET}, widgets={"sonarr": _widget()},
                              connection_factory=Stuck({}))
    began = time.monotonic()
    try:
        assert f.fetch(_target(), KEY)["error"] == "timeout"
        assert time.monotonic() - began < 2
        # Cached briefly, so re-opening the view does not ask core again at once.
        assert f.cached(KEY)["error"] == "timeout"
        for i in range(fetcher.MAX_INFLIGHT - 1):
            f.fetch(_target(), ("s", str(i)))
        assert f.fetch(_target(), ("s", "one-more"))["error"] == "busy"
    finally:
        release.set()


# ── real sockets: the attacks the gate reproduced ────────────────────────────────

def _serve(script):
    """A one-connection TCP server on loopback running `script(conn)`; returns its port."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def run():
        conn, _ = server.accept()
        try:
            conn.recv(65536)
            script(conn)
        except OSError:
            pass
        finally:
            conn.close()
            server.close()
    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def _get(port, timeout=2.0, max_bytes=2 * 1024 * 1024):
    return fetcher.http_get_json("127.0.0.1", port, "/x", headers={}, timeout=timeout,
                                 budget=fetcher._Budget(max_bytes))


def test_real_a_redirect_body_is_never_read():
    """requests read -- and gunzipped -- the whole body of a 3xx inside session.get()."""
    sent = []

    def script(conn):
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /y\r\nContent-Encoding: gzip\r\n"
                     b"Content-Length: 104857600\r\n\r\n")
        try:
            for _ in range(20):
                conn.sendall(b"\0" * 65536)
                sent.append(1)
        except OSError:
            pass
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script))
    assert raised.value.status == 302


def test_real_an_unbounded_chunk_size_line_is_refused_not_buffered():
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        for _ in range(64):                           # 4 MiB of chunk-size line
            conn.sendall(b"1" * 65536)
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script))
    assert raised.value.reason in ("connection dropped", "answer was not JSON")


def test_real_an_unbounded_header_line_is_refused():
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nX-Big: " + b"a" * (1024 * 1024) + b"\r\n\r\n{}")
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script))
    assert raised.value.reason == "connection dropped"


def test_real_a_trickling_server_is_cut_off_by_the_watchdog():
    """A socket timeout bounds each read; this bounds the call."""
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(100):
            conn.sendall(b" ")
            time.sleep(0.1)
    began = time.monotonic()
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script), timeout=0.5)
    assert time.monotonic() - began < 2
    assert raised.value.reason == "timeout"


def test_real_endless_100_continue_is_cut_off_by_the_watchdog():
    def script(conn):
        for _ in range(200):
            conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            time.sleep(0.02)
    began = time.monotonic()
    with pytest.raises(fetcher._Failure):
        _get(_serve(script), timeout=0.5)
    assert time.monotonic() - began < 2


def test_real_a_gzip_answer_is_refused():
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\n\x1f\x8b")
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script))
    assert raised.value.reason == "answer was compressed"


def test_real_a_good_chunked_answer_parses():
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                     b"5\r\n{\"a\":\r\n2\r\n1}\r\n0\r\n\r\n")
    assert _get(_serve(script)) == {"a": 1}


def test_real_an_oversized_body_stops_at_the_budget():
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10485760\r\n\r\n")
        for _ in range(160):
            conn.sendall(b" " * 65536)
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script), max_bytes=1024 * 1024)
    assert raised.value.reason == "answer too large"


def test_real_a_refused_connection_is_unreachable():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    with pytest.raises(fetcher._Failure) as raised:
        _get(port)
    assert raised.value.reason == "unreachable"


# ── normalise_summary: what may reach the page ─────────────────────────────────

def test_the_summary_is_clamped_to_the_contract():
    got = fetcher.normalise_summary({
        "stats": [{"k": "A" * 100, "v": "x" * 1000, "level": "warn"},
                  {"k": "B", "v": {"nested": True}, "level": "<script>"},
                  {"k": "C", "v": True}, {"k": "D", "v": None}, {"k": "E", "v": 1}],
        "rows": [{"title": "t", "pct": 250}, {"title": "u", "pct": -3}, "junk", {"title": "v"},
                 {"title": "w"}],
        "rows_label": "L" * 100, "line": "y" * 500, "extra": "dropped"})
    assert len(got["stats"]) == 4
    assert got["stats"][0] == {"k": "A" * 24, "v": "x" * 120, "level": "warn"}
    assert got["stats"][1] == {"k": "B", "v": "{'nested': True}"}
    assert got["stats"][2]["v"] == "True" and got["stats"][3]["v"] == "—"
    assert [r.get("pct") for r in got["rows"]] == [100, 0, None]
    assert len(got["rows_label"]) == 24 and len(got["line"]) == 120 and "extra" not in got


@pytest.mark.parametrize("summary", [None, [], {"stats": []}, {"stats": "x"}, {"rows": []}])
def test_a_summary_without_stats_is_refused(summary):
    with pytest.raises(fetcher._Failure):
        fetcher.normalise_summary(summary)


# ── registry: the env and path rules the fetcher relies on ─────────────────────

@pytest.mark.parametrize("name,env,ok", [
    ("sonarr", "SONARR_API_KEY", True), ("adguard", "ADGUARD_PASSWORD", True),
    ("sonarr", "RADARR_API_KEY", False), ("sonarr", "SONARR_", False),
    ("sonarr", "sonarr_api_key", False), ("pe", "PE_DASHBOARD_SECRET_KEY", False),
    ("tg", "TG_BOT_TOKEN", False), ("casa", "CASA_RPC_SOCKET", False),
    ("anthropic", "ANTHROPIC_API_KEY", False),
])
def test_a_widget_reads_only_env_vars_named_after_itself(name, env, ok):
    assert (registry.env_name_problem(name, env) is None) is ok


@pytest.mark.parametrize("path", ["/api/v3/queue", "/control/stats", "/api?x=1&y=2"])
def test_plain_paths_are_accepted(path):
    assert registry._validated("w", {**_widget(), "get": [path]}, _summarise) is not None


@pytest.mark.parametrize("path", ["//evil.example/x", "/a/../b", "/a#frag", "/a b", "/@evil",
                                  "http://x/", "/a\r\nHost: x", "relative"])
def test_a_path_that_could_change_where_a_request_goes_is_refused(path):
    assert registry._validated("w", {**_widget(), "get": [path]}, _summarise) is None


@pytest.mark.parametrize("header", ["X-Api-Key\r\nX: y", "", "Bad Header", "a" * 65])
def test_a_header_name_must_be_a_plain_header_name(header):
    auth = {"type": "header", "header": header, "env": "SONARR_API_KEY"}
    assert registry._validated("w", {**_widget(), "auth": auth}, _summarise) is None


@pytest.mark.parametrize("port", [0, 65536, -1, True, "8989"])
def test_a_port_must_be_a_real_port(port):
    assert registry._validated("w", {**_widget(), "port": port}, _summarise) is None


@pytest.mark.parametrize("max_bytes", [0, -1, True, "1", 17 * 1024 * 1024])
def test_a_byte_budget_must_be_sane(max_bytes):
    assert registry._validated("w", {**_widget(), "max_bytes": max_bytes}, _summarise) is None


def test_every_shipped_widget_passes_the_rules():
    widgets = registry.load_widgets()
    assert {"sonarr", "adguard"} <= set(widgets)
    for widget in widgets.values():
        for env in registry.auth_env_names(widget.get("auth")):
            assert registry.env_name_problem(widget["name"], env) is None


def test_an_edited_widget_file_is_reloaded_not_served_from_the_old_import(tmp_path):
    from planet_express import widgets as package
    path = Path(package.__path__[0]) / "zz_edit_fixture.py"
    declaration = ("WIDGET = {{'name': 'zz_edit_fixture', 'match': ['x/y'], 'port': {port}, "
                   "'get': ['/a']}}\ndef summarise(r): return {{}}\n")
    path.write_text(declaration.format(port=1000))
    try:
        assert registry.load_widgets()["zz_edit_fixture"]["port"] == 1000
        path.write_text(declaration.format(port=2000) + "\n")
        assert registry.load_widgets()["zz_edit_fixture"]["port"] == 2000
    finally:
        path.unlink()
        sys.modules.pop("planet_express.widgets.zz_edit_fixture", None)


def test_real_a_connection_close_trickle_is_still_cut_off():
    """http.client drops conn.sock on a response that will close the connection; the watchdog
    must hold its own reference or the bound does nothing."""
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
        for _ in range(100):
            conn.sendall(b" ")
            time.sleep(0.1)
    began = time.monotonic()
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script), timeout=0.5)
    assert time.monotonic() - began < 2 and raised.value.reason == "timeout"


def test_real_a_header_flood_counts_against_the_byte_cap():
    """~100 header lines of 64 KiB each pass http.client's per-line limits."""
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        for i in range(90):
            conn.sendall(b"X-H%d: " % i + b"a" * 60000 + b"\r\n")
        conn.sendall(b"Content-Length: 2\r\n\r\n{}")
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script), max_bytes=2)
    assert raised.value.reason == "answer too large"


def test_a_failure_never_overwrites_a_fresh_good_answer():
    f, _, _ = _fetcher({(A, Q): _Reply()})
    good = f.fetch(_target(), KEY)
    f._store(KEY, _target(), {"widget": "sonarr", "state": "error", "error": "timeout"},
             ttl=fetcher.FAILED_CACHE_SECONDS)
    assert f.cached(KEY) == good


@pytest.mark.parametrize("name", ["ſonarr", "Sonarr", "sonarr!", "1sonarr", ""])
def test_a_widget_name_must_be_plain_lower_case_ascii(name):
    """Upper-casing "ſonarr" gives "SONARR": it would be allowed Sonarr's key."""
    assert registry._validated("w", {**_widget(), "name": name}, _summarise) is None


@pytest.mark.parametrize("name,env,ok", [
    ("sonarr", "SONARR_4K_API_KEY", False), ("sonarr-4k", "SONARR_4K_API_KEY", True),
    ("sonarr", "SONARR_TOKEN", True), ("sonarr", "SONARR_SECRET", False)])
def test_a_widget_reads_only_its_own_exact_key_names(name, env, ok):
    """`sonarr` must never read SONARR_4K_API_KEY: that is a second instance's key."""
    assert (registry.env_name_problem(name, env) is None) is ok


@pytest.mark.parametrize("label_from_image", [False])
def test_a_container_label_cannot_route_a_key_to_an_image_that_does_not_match(monkeypatch, label_from_image):
    """Compose content (including /install drafts) must not decide where a credential goes."""
    from unittest.mock import Mock

    import planet_express.integrations.rpc as rpc_module
    from planet_express.execution.actions import Target
    monkeypatch.setattr(rpc_module.actions, "resolve_target",
                        lambda **kw: Target(stack="new", service="app", container="CASA_APP"))
    monkeypatch.setattr(rpc_module.actions, "read_widget_target", lambda c, *, timeout: {
        "ok": True, "id": "a" * 64, "image": "someone/else", "running": True,
        "label": "adguard", "label_from_image": label_from_image, "addresses": ["172.18.0.9"]})
    handler = rpc_module.build_core_handlers(Mock(), Mock())["query.widget_target"]
    got = handler({"stack": "new", "service": "app"})
    assert got["widget"] is None and got["addresses"] == []


def test_a_widget_file_whose_declaration_is_removed_stops_loading():
    """importlib.reload keeps names the edited file no longer defines; this must not."""
    from planet_express import widgets as package
    path = Path(package.__path__[0]) / "zz_removed_fixture.py"
    path.write_text("WIDGET = {'name': 'zz_removed_fixture', 'match': ['x/y'], 'port': 1, "
                    "'get': ['/a']}\ndef summarise(r): return {}\n")
    try:
        assert "zz_removed_fixture" in registry.load_widgets()
        path.write_text("# disabled\ndef summarise(r): return {}\n\n\n")
        assert "zz_removed_fixture" not in registry.load_widgets()
    finally:
        path.unlink()


def test_two_widget_names_that_share_a_key_prefix_are_both_refused():
    from planet_express import widgets as package
    base = Path(package.__path__[0])
    body = ("WIDGET = {{'name': '{name}', 'match': ['x/{name}'], 'port': 1, 'get': ['/a']}}\n"
            "def summarise(r): return {{}}\n")
    paths = [base / "zz_pair_a.py", base / "zz_pair_b.py"]
    paths[0].write_text(body.format(name="zz-pair"))
    paths[1].write_text(body.format(name="zz_pair"))
    try:
        loaded = registry.load_widgets()
        assert "zz-pair" not in loaded and "zz_pair" not in loaded
    finally:
        for p in paths:
            p.unlink()


def test_real_body_bytes_cannot_spend_the_header_allowance():
    def script(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 200000\r\n\r\n" + b" " * 200000)
    with pytest.raises(fetcher._Failure) as raised:
        _get(_serve(script), max_bytes=100000)
    assert raised.value.reason == "answer too large"


def test_two_files_declaring_one_widget_name_are_both_refused():
    from planet_express import widgets as package
    base = Path(package.__path__[0])
    body = ("WIDGET = {{'name': 'zz_dup', 'match': ['x/{n}'], 'port': {n}, 'get': ['/a']}}\n"
            "def summarise(r): return {{}}\n")
    paths = [base / "zz_dup_a.py", base / "zz_dup_b.py"]
    paths[0].write_text(body.format(n=1))
    paths[1].write_text(body.format(n=2))
    try:
        assert "zz_dup" not in registry.load_widgets()
    finally:
        for p in paths:
            p.unlink()


def test_a_widget_file_using_dataclasses_still_loads():
    from planet_express import widgets as package
    path = Path(package.__path__[0]) / "zz_dc_fixture.py"
    path.write_text("from __future__ import annotations\nfrom dataclasses import dataclass\n"
                    "@dataclass\nclass Row:\n    title: str\n"
                    "WIDGET = {'name': 'zz_dc_fixture', 'match': ['x/dc'], 'port': 1, 'get': ['/a']}\n"
                    "def summarise(r): return {}\n")
    try:
        assert "zz_dc_fixture" in registry.load_widgets()
    finally:
        path.unlink()
        sys.modules.pop("planet_express.widgets.zz_dc_fixture", None)


def test_a_job_for_a_predecessor_is_not_joined_by_its_successor(monkeypatch):
    monkeypatch.setattr(fetcher, "FETCH_BUDGET_SECONDS", 0.2)
    release = threading.Event()

    class Stuck(_Wire):
        def __call__(self, address, port, timeout):
            conn = super().__call__(address, port, timeout)
            conn.getresponse = lambda: (release.wait(5), _Reply())[1]
            return conn
    f = fetcher.WidgetFetcher(env={"SONARR_API_KEY": SECRET}, widgets={"sonarr": _widget()},
                              connection_factory=Stuck({}))
    try:
        f.fetch(_target(), KEY)
        f._cache.clear()
        f.fetch(_target(container_id="b" * 64), KEY)
        assert len(f._inflight) == 2
    finally:
        release.set()


def test_provenance_matches_registry_and_repo_exactly():
    widget = {"match": ["lscr.io/linuxserver/sonarr", "linuxserver/sonarr"]}
    assert registry.trusted_provenance(widget, ["lscr.io/linuxserver/sonarr"])
    assert registry.trusted_provenance(widget, ["docker.io/linuxserver/sonarr"])
    assert registry.trusted_provenance(widget, ["index.docker.io/linuxserver/sonarr"])
    assert not registry.trusted_provenance(widget, ["ghcr.io/linuxserver/sonarr"])
    assert not registry.trusted_provenance(widget, ["evil.example/linuxserver/sonarr"])
    assert not registry.trusted_provenance(widget, [])



def test_a_predecessors_late_job_does_not_overwrite_its_successors_answer():
    f, _, now = _fetcher({(A, Q): _Reply()})
    successor = f.fetch(_target(container_id="b" * 64), KEY)
    late = {"widget": "sonarr", "via": Q, "state": "ok", "fetched_at": "x", "stats": [], "rows": [],
            "rows_label": "", "line": None}
    f._store(KEY, _target(container_id="a" * 64), late, started=now[0] - 1)
    assert f.cached(KEY) == successor


def test_one_widgets_values_are_never_shown_for_another():
    """A match-list edit can move a running container to a different widget."""
    f, _, now = _fetcher({(A, Q): _Reply()})
    f.fetch(_target(), KEY)
    other = {"widget": "radarr", "via": "/x", "state": "error", "error": "timeout"}
    kept = f._store(KEY, _target(), other, ttl=fetcher.FAILED_CACHE_SECONDS)
    assert kept == other and "stale" not in kept


def test_an_unexpected_failure_is_cached_like_any_other():
    def boom(*a, **k):
        raise RuntimeError("bug")
    f, _, _ = _fetcher({(A, Q): _Reply()})
    f._fetch_now = boom
    answer = f.fetch(_target(), KEY)
    assert answer["error"] == "request failed" and f.cached(KEY)["error"] == "request failed"


def test_a_request_that_asked_core_before_the_successor_was_answered_cannot_displace_it():
    """Resolved the predecessor, fetched late: its failure is not news."""
    f, _, now = _fetcher({(A, Q): _Reply()})
    asked = now[0]
    now[0] += 1
    successor = f.fetch(_target(container_id="b" * 64), KEY)
    now[0] += 1
    f.fetch(_target(container_id="a" * 64, addresses=[]), KEY, asked)
    assert f.cached(KEY) == successor


def test_an_unreadable_file_skips_itself_not_the_cache(tmp_path, monkeypatch):
    from planet_express import widgets as package
    link = Path(package.__path__[0]) / "zz_dangling.py"
    link.symlink_to("/nonexistent/target.py")
    try:
        registry.load_widgets()
        key = registry._loaded["key"]
        registry.load_widgets()
        assert registry._loaded["key"] == key and "zz_dangling" not in dict(key)
    finally:
        link.unlink()


def test_a_removed_widget_file_is_forgotten_by_both_import_forms():
    from planet_express import widgets as package
    path = Path(package.__path__[0]) / "zz_gone_fixture.py"
    path.write_text("WIDGET = {'name': 'zz_gone_fixture', 'match': ['x/g'], 'port': 1, "
                    "'get': ['/a']}\ndef summarise(r): return {}\n")
    registry.load_widgets()
    assert hasattr(package, "zz_gone_fixture")
    path.unlink()
    registry.load_widgets()
    assert not hasattr(package, "zz_gone_fixture")
    assert "planet_express.widgets.zz_gone_fixture" not in sys.modules
