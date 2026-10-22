"""The Docker proxy end to end over real Unix sockets against a fake daemon: framing, authentication, pause, recording before
forwarding, redaction, bounded streams, hijacked exec and the read-only variant."""
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)
from dockerproxy_fakes import (
    ID_A,
    FakeDaemon,
    FakeGate,
    create_body,
    make_scope,
    parse,
    request,
    talk,
)

from planet_express.missions.dockerproxy.policy import Policy
from planet_express.missions.dockerproxy.server import (
    ExecTable,
    Limits,
    ProxyServer,
)
from planet_express.missions.dockerproxy.service import build_read_only
from planet_express.missions.dockerproxy.upstream import (
    DaemonInspector,
    Upstream,
)


class Env:
    pass


@pytest.fixture
def env():
    e = Env()
    e.dir = tempfile.mkdtemp(prefix="dp")
    root = Path(e.dir)
    for d in ("stacks/media/config", "data"):
        (root / d).mkdir(parents=True)
    e.root = e.dir
    e.daemon = FakeDaemon(os.path.join(e.dir, "d.sock"))
    e.upstream = Upstream(e.daemon.path, timeout=5)
    e.gate = FakeGate()
    e.path = os.path.join(e.dir, "proxy.sock")
    e.servers = []

    def make(limits=Limits(head_s=2, body_s=3, stream_s=3, hijack_s=5), **kw):
        execs = ExecTable()
        policy = Policy(make_scope(e.dir), DaemonInspector(e.upstream), exec_owner=execs.owner, **kw)
        server = ProxyServer(e.path, policy=policy, upstream=e.upstream, gate=e.gate, execs=execs, limits=limits)
        server.start()
        e.servers.append(server)
        e.server = server
        return server

    e.make = make
    e.req = lambda method, target, **kw: request(e.path, method, target, **kw)
    yield e
    for s in e.servers:
        s.stop()
    e.daemon.close()
    shutil.rmtree(e.dir, ignore_errors=True)


@pytest.fixture
def proxy(env):
    env.make()
    return env


def raw(env, text: bytes, **kw):
    return talk(env.path, text, **kw)


def good_head(extra="", target="/v1.43/containers/json", method="GET", token="good"):
    return (f"{method} {target} HTTP/1.1\r\nHost: docker\r\nX-PE-Session: {token}\r\n{extra}\r\n").encode()


# ── the socket itself ─────────────────────────────────────────────────────────────────────────────────

def test_the_socket_is_bound_0600_by_core_and_never_widened(proxy):
    info = os.lstat(proxy.path)
    assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()


def test_stop_removes_the_socket_and_only_its_own_inode(proxy):
    proxy.server.stop()
    assert not os.path.exists(proxy.path)


# ── authentication and pause ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token", [None, "", "bad", "goo", "GOOD"])
def test_every_connection_needs_the_session_token_and_a_refused_one_reaches_nothing(proxy, token):
    status, _, body = proxy.req("GET", "/containers/json", token=token)
    assert status == 401 and b"unauthorized" in body
    assert proxy.daemon.requests == []                         # not even an inspect was asked of the daemon


def test_a_paused_session_gets_503_on_every_request_and_nothing_is_forwarded(proxy):
    proxy.gate.paused = True
    for method, target, body in (("GET", "/containers/json", None), ("POST", "/containers/qbittorrent/restart", None),
                                 ("POST", "/containers/create", create_body()), ("GET", "/_ping", None), ("GET", "/nonsense", None)):
        status, _, _ = proxy.req(method, target, body=body)
        assert status == 503, (method, target)
    assert proxy.daemon.requests == [] and proxy.gate.events == []
    proxy.gate.paused = False
    assert proxy.req("GET", "/containers/json")[0] == 200       # resume re-authorises: the same socket serves again


def test_a_write_the_gate_refuses_is_not_forwarded_and_the_refusal_is_recorded(proxy):
    for why in ("the session is revoked", "the approval no longer covers this grant", "a lease fence is stale"):
        proxy.gate.write_refusal = why
        status, _, body = proxy.req("POST", "/containers/qbittorrent/restart")
        assert status == 403 and why.encode() in body
    assert proxy.daemon.writes() == [] and proxy.gate.writes() == []
    assert [e["reason"] for e in proxy.gate.events if e["event"] == "docker.refused"][0] == "the session is revoked"
    assert proxy.req("GET", "/containers/json")[0] == 200       # reads do not need write authority


# ── recording before forwarding ─────────────────────────────────────────────────────────────────────────────

def test_an_accepted_write_is_recorded_before_it_is_forwarded(proxy):
    seen = []
    original = proxy.gate.record

    def spy(session, body):
        seen.append((body.get("event"), len(proxy.daemon.writes())))
        original(session, body)
    proxy.gate.record = spy
    status, _, _ = proxy.req("POST", "/containers/qbittorrent/restart?t=5")
    assert status == 204
    assert seen[0] == ("docker.write", 0)                       # the event existed while the daemon had seen no write
    write = proxy.daemon.writes()[0]
    assert write["target"] == f"/v1.43/containers/{ID_A}/restart?t=5"
    assert proxy.gate.writes()[0]["route"] == "container.restart" and proxy.gate.writes()[0]["targets"] == ["host:local/service:media/qbittorrent"]
    assert proxy.gate.events[-1] == {"event": "docker.result", "route": "container.restart", "status": 204}


def test_if_the_event_cannot_be_written_nothing_is_forwarded(proxy):
    proxy.gate.fail_record = True
    status, _, body = proxy.req("POST", "/containers/qbittorrent/restart")
    assert status == 500 and b"could not be recorded" in body and proxy.daemon.writes() == []


def test_the_event_exists_even_if_forwarding_fails(proxy):
    from planet_express.missions.dockerproxy.policy import ContainerInfo

    class Resolves:
        def container(self, ref):
            return ContainerInfo(ID_A, "qbittorrent", {"com.docker.compose.project": "media", "com.docker.compose.service": "qbittorrent"})

    proxy.server.policy.inspector = Resolves()
    proxy.upstream.socket_path = "/nonexistent/docker.sock"          # the daemon is gone by the time the request is forwarded
    status, _, _ = proxy.req("POST", "/containers/qbittorrent/restart")
    assert status == 502 and proxy.gate.writes()[-1]["route"] == "container.restart"     # recorded, then forwarding failed


def test_events_carry_no_secrets_no_env_values_no_command_text(proxy):
    body = create_body(Env=["API_KEY=topsecretvalue99"], Cmd=["run", "--password=topsecretvalue99"])
    assert proxy.req("POST", "/containers/create?name=x1", body=body)[0] == 201
    assert "topsecretvalue99" not in json.dumps(proxy.gate.events)
    event = proxy.gate.writes()[0]
    assert event["env_names"] == ["API_KEY"] and event["cmd_argc"] == 2 and event["image"].startswith("lscr.io")


# ── forwarding is a rebuild, not a pass-through ─────────────────────────────────────────────────────────────

def test_the_daemon_receives_the_validated_body_re_serialised_not_the_clients_bytes(proxy):
    messy = (b'{ "Image":"alpine:3",\n  "Labels": {"com.docker.compose.project":"media","com.docker.compose.service":"qbittorrent"},'
             b'   "Cmd":["a"] }')
    status, _, _ = proxy.req("POST", "/containers/create", body=messy)
    assert status == 201
    got = proxy.daemon.writes()[0]["body"]
    assert got == json.dumps(json.loads(got), separators=(",", ":"), sort_keys=True).encode() and b"\n" not in got
    assert json.loads(got)["Image"] == "alpine:3" and proxy.daemon.writes()[0]["headers"]["content-length"] == str(len(got))


def test_headers_the_client_sent_are_not_forwarded(proxy):
    proxy.req("POST", "/containers/qbittorrent/restart", headers={"X-Registry-Auth": "secret", "X-Evil": "1", "User-Agent": "x"})
    headers = proxy.daemon.writes()[0]["headers"]
    assert "x-pe-session" not in headers and "x-registry-auth" not in headers and "x-evil" not in headers


# ── refusals at the HTTP layer ───────────────────────────────────────────────────────────────────────────

BAD = [
    ("duplicate Content-Length", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 2\r\n"
                                 b"Content-Length: 2\r\n\r\n{}"),
    ("two different lengths", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 2\r\n"
                              b"Content-Length: 50\r\n\r\n{}"),
    ("chunked", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"2\r\n{}\r\n0\r\n\r\n"),
    ("CL and TE", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 2\r\n"
                  b"Transfer-Encoding: chunked\r\n\r\n{}"),
    ("obfuscated TE", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nTransfer-Encoding : chunked\r\n\r\n"),
    ("pipelined second request", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"
                                 b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"),
    ("body after a bodiless request", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 4\r\n\r\nabcd"),
    ("bytes after the body", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 2\r\n\r\n{}EXTRA"),
    ("bare LF", b"GET /v1.43/containers/json HTTP/1.1\nHost: d\nX-PE-Session: good\n\n"),
    ("bare CR in a header", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\rX-A: b\r\n\r\n"),
    ("folded header", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n X-Evil: 1\r\n\r\n"),
    ("NUL", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\x00\r\n\r\n"),
    ("HTTP/1.0", b"GET /v1.43/containers/json HTTP/1.0\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"),
    ("HTTP/2 preface", b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"),
    ("absolute-form target", b"GET http://evil/v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"),
    ("TRACE", b"TRACE /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"),
    ("CONNECT", b"CONNECT /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"),
    ("Expect", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nExpect: 100-continue\r\nContent-Length: 2\r\n\r\n{}"),
    ("signed Content-Length", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: +2\r\n\r\n{}"),
    ("hex Content-Length", b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 0x2\r\n\r\n{}"),
    ("bad header name", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nBad Name: 1\r\n\r\n"),
    ("two session headers", b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nX-PE-Session: good\r\n\r\n"),
    ("space in target", b"GET /v1.43/containers/json x HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n\r\n"),
]


@pytest.mark.parametrize("name, payload", BAD, ids=[b[0] for b in BAD])
def test_ambiguous_or_malformed_framing_is_refused_and_nothing_reaches_the_daemon(proxy, name, payload):
    status, _, _ = raw(proxy, payload)
    assert status in (400, 401, 403, 413), name
    assert proxy.daemon.requests == [] and proxy.daemon.writes() == []


@pytest.mark.parametrize("target", [
    "/v1.43//containers/json", "/v1.43/containers//json", "/v1.43/./containers/json", "/v1.43/containers/../containers/json",
    "/v1.43/containers/%2e%2e/json", "/v1.43/containers%2Fjson", "/v1.43/containers/%2e/json", "/v1.43/containers/plex%2fjson",
    "/v1.43\\containers\\json", "/v1.43/containers/json;x=1", "/v1.43/containers/json#frag", "/v1.43/containers/json/",
    "/v1.43/containers/json?all=1&all=0", "/v1.43/containers/json?all", "/v1.43/containers/json?=1", "/v1.43/containers/plex/json%00",
    "/v1.43/containers/péx/json", "/%76%31.43/containers/json", "/v1.43/containers/json?all=%0d%0a", "//v1.43/containers/json",
    "/v1.43/containers/%252e%252e/json",
])
def test_path_traversal_and_encoded_variants_are_refused(proxy, target):
    status, _, _ = proxy.req("GET", target, raw_target=True)
    assert status in (400, 403) or target.endswith("?all"), target
    assert proxy.daemon.requests == []


def test_an_oversized_head_or_body_is_refused(proxy):
    status, _, _ = raw(proxy, good_head("X-Pad: " + "a" * 20000 + "\r\n"))
    assert status == 400
    big = b'{"Image": "' + b"a" * (300 * 1024) + b'"}'
    status, _, _ = request(proxy.path, "POST", "/containers/create", body=big)
    assert status == 413 and proxy.daemon.requests == []
    status, _, _ = raw(proxy, b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Length: 99999999999\r\n\r\n")
    assert status in (400, 413)


def test_a_slow_head_is_cut_off(proxy):
    proxy.server.stop()
    proxy.make(Limits(head_s=0.5))
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(proxy.path)
    sock.sendall(b"GET /v1.43/containers/json HTTP/1.1\r\nHost: d\r\n")        # never finishes
    sock.settimeout(3)
    data = sock.recv(4096)
    assert b"400" in data.split(b"\r\n")[0]
    sock.close()


def test_a_connection_that_never_sends_is_cut_off_and_frees_its_slot(proxy):
    proxy.server.stop()
    proxy.make(Limits(head_s=0.5, connections=2))
    idle = []
    for _ in range(2):
        s = socket.socket(socket.AF_UNIX)
        s.connect(proxy.path)
        idle.append(s)
    time.sleep(0.2)
    status, _, _ = proxy.req("GET", "/_ping")
    assert status == 503                                       # both slots are held by silent connections
    time.sleep(0.8)                                            # ...which the head timeout then frees
    assert proxy.req("GET", "/_ping")[0] == 200
    for s in idle:
        s.close()


@pytest.mark.parametrize("target, status", [("/v1.99/containers/json", 403), ("/v1.40/containers/json", 403), ("/v2/containers/json", 403),
                                            ("/v1.43/info", 403), ("/v1.43/build", 403), ("/v1.43/images/create?fromImage=x", 403),
                                            ("/v1.43/containers/json?remote=http://x", 403), ("/containers/json", 403),
                                            ("/v1.43/volumes/prune", 403), ("/v1.43/swarm", 403)])
def test_unknown_versions_endpoints_and_parameters_get_403_and_are_logged(proxy, target, status):
    method = "POST" if target.endswith(("prune", "create?fromImage=x", "build")) else "GET"
    got, _, _ = proxy.req(method, target, raw_target=True)
    assert got == status
    assert proxy.daemon.requests == []
    assert any(e["event"] == "docker.refused" for e in proxy.gate.events)


def test_refusal_logging_is_bounded(proxy):
    proxy.server.stop()
    proxy.make(Limits(head_s=2, refusal_events=3))
    for _ in range(8):
        proxy.req("GET", "/v1.43/info", raw_target=True)
    refusals = [e for e in proxy.gate.events if e["event"] == "docker.refused"]
    assert len(refusals) == 4 and refusals[-1].get("further_refusals_not_recorded") is True


def test_malformed_json_fails_closed(proxy):
    for body in (b"{", b"", b"[]", b'"x"', b"null", b'{"Image": 1', b'{"a": NaN}', b'{"a": 1, "a": 2}', b"\xff\xfe", b"{" * 5000,
                 b'{"a":' * 20 + b"1" + b"}" * 20):
        status, _, _ = request(proxy.path, "POST", "/containers/create", body=body)
        assert status in (400, 403, 413), body[:20]
    assert proxy.daemon.writes() == []
    status, _, _ = request(proxy.path, "POST", "/containers/create", body=b"{}", headers={"Content-Type": "text/plain"})
    assert status == 400


def test_a_body_shorter_than_it_claims_times_out_cleanly(proxy):
    proxy.server.stop()
    proxy.make(Limits(head_s=1, body_s=0.5))
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(proxy.path)
    sock.sendall(b"POST /v1.43/containers/create HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\nContent-Type: application/json\r\n"
                 b"Content-Length: 100\r\n\r\n{}")
    sock.settimeout(3)
    assert b"400" in sock.recv(4096).split(b"\r\n")[0]
    assert proxy.daemon.writes() == []


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────

def test_a_container_list_drops_protected_containers_and_redacts(proxy):
    status, headers, body = proxy.req("GET", "/containers/json?all=1")
    names = [c["Names"][0] for c in json.loads(body)]
    assert status == 200 and "/vault" not in names and "/qbittorrent" in names and "/traefik" in names
    assert headers.get("api-version") == "1.43"


def test_inspect_output_is_redacted_on_the_wire(proxy):
    status, _, body = proxy.req("GET", "/containers/qbittorrent/json")
    text = body.decode()
    assert status == 200 and "hunter2" not in text and "API_KEY=" in text and "PUID=" in text and "1000" not in text
    status, _, body = proxy.req("GET", "/images/alpine:3/json")
    assert status == 200 and b"abcdef123456" not in body
    assert proxy.req("GET", "/containers/vault/json")[0] == 403                 # protected: not even inspect


def test_version_is_served_with_the_api_version_clamped_to_what_the_proxy_serves(env):
    env.daemon.api_version = "1.52"
    path = os.path.join(env.dir, "ro2.sock")
    ro = build_read_only(path, upstream=env.upstream)
    ro.start()
    try:
        for target in ("/version", "/v1.43/version"):
            status, _, body = request(path, "GET", target, token=None, version=None, raw_target=True)
            doc = json.loads(body)
            assert status == 200 and doc["ApiVersion"] == "1.48" and doc["Os"] == "linux" and doc["KernelVersion"]
        assert request(path, "POST", "/v1.43/version", token=None, version=None, raw_target=True)[0] in (403, 405)
        assert request(path, "GET", "/v1.43/version?x=1", token=None, version=None, raw_target=True)[0] == 403
    finally:
        ro.stop()


def test_a_read_of_a_missing_object_is_404_and_ping_works(proxy):
    assert proxy.req("GET", "/containers/nonesuch/json")[0] == 404
    status, _, body = proxy.req("GET", "/_ping", version=None)
    assert status == 200 and body == b"OK"
    assert proxy.req("HEAD", "/_ping", version=None)[0] == 200


def test_an_unreadable_answer_from_a_redacted_route_is_never_passed_through(proxy):
    class Raw(FakeDaemon):
        pass
    # point the inspect route at garbage by pointing the upstream at a daemon that returns non-JSON for /containers/json
    proxy.daemon._route = lambda conn, rec: proxy.daemon._send(conn, 200, raw=b"not json at all")
    status, _, body = proxy.req("GET", "/containers/json")
    assert status == 502 and b"not json" not in body


def test_streams_are_bounded_in_bytes(env):
    env.daemon.stream_bytes = 2 * 1024 * 1024
    env.make(Limits(head_s=2, stream_bytes=64 * 1024, stream_s=5))
    status, _, body = env.req("GET", "/containers/qbittorrent/logs?follow=1&stdout=1")
    assert status == 200 and 0 < len(body) <= 64 * 1024 + 8192


def test_streams_are_bounded_in_time_and_stop_when_the_session_is_paused(env):
    env.daemon.stream_bytes = 0                                 # sends its head and then goes quiet
    env.make(Limits(head_s=2, stream_bytes=1 << 20, stream_s=1.0))
    start = time.monotonic()
    status, _, _ = env.req("GET", "/containers/qbittorrent/logs?follow=1")
    assert status == 200 and time.monotonic() - start < 4
    env.server.stop()
    env.make(Limits(head_s=2, stream_bytes=1 << 20, stream_s=30))
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(env.path)
    sock.sendall(good_head(target="/v1.43/containers/qbittorrent/logs?follow=1"))
    sock.settimeout(1)
    sock.recv(4096)
    env.gate.paused = True
    sock.settimeout(4)
    began = time.monotonic()
    while sock.recv(4096):
        pass
    assert time.monotonic() - began < 4                         # closed by the proxy, not by the 30 s bound
    sock.close()


def test_logs_stats_and_archive_need_an_in_scope_container(proxy):
    for target in ("/containers/traefik/logs", "/containers/stray/stats", "/containers/traefik/archive?path=/etc",
                   "/containers/vault/logs"):
        assert proxy.req("GET", target)[0] == 403
    assert proxy.req("GET", "/containers/qbittorrent/archive?path=/config")[0] == 200
    assert proxy.req("HEAD", "/containers/qbittorrent/archive?path=/config")[0] == 200
    assert proxy.req("GET", "/containers/qbittorrent/archive")[0] == 400


# ── writes ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target, method", [("/containers/traefik/start", "POST"), ("/containers/vault/restart", "POST"),
                                            ("/containers/stray/kill?signal=KILL", "POST"), ("/containers/traefik", "DELETE"),
                                            ("/containers/traefik/rename?name=x", "POST"), ("/networks/other_default/connect", "POST")])
def test_a_write_to_an_out_of_scope_object_is_refused_and_never_reaches_the_daemon(proxy, target, method):
    body = {"Container": "qbittorrent"} if "networks" in target else None
    assert proxy.req(method, target, body=body)[0] == 403
    assert proxy.daemon.writes() == [] and proxy.gate.writes() == []


def test_create_remove_and_the_container_lifecycle_in_scope(proxy):
    assert proxy.req("POST", "/containers/qbittorrent/stop?t=10&signal=SIGTERM")[0] == 204
    assert proxy.req("POST", "/containers/qbittorrent/start")[0] == 204
    assert proxy.req("POST", "/containers/qbittorrent/rename?name=qb2")[0] == 204
    assert proxy.req("DELETE", "/containers/qbittorrent?force=1")[0] == 204
    assert proxy.req("DELETE", "/containers/qbittorrent?v=1")[0] == 403
    assert proxy.req("POST", "/containers/qbittorrent/update", body={"Memory": 1 << 28})[0] == 204
    assert proxy.req("POST", "/networks/media_default/connect", body={"Container": "qbittorrent"})[0] == 204
    assert [w["method"] for w in proxy.daemon.writes()][:2] == ["POST", "POST"]
    assert {e["route"] for e in proxy.gate.writes()} >= {"container.stop", "container.start", "container.remove", "network.connect"}


def test_archive_put_streams_a_bounded_body(proxy):
    status, _, _ = request(proxy.path, "PUT", "/containers/qbittorrent/archive?path=/config", body=b"T" * 5000,
                           headers={"Content-Type": "application/x-tar"})
    assert status == 204 or status == 200
    assert proxy.daemon.writes()[-1]["body"] == b"T" * 5000
    proxy.server.stop()
    proxy.make(Limits(head_s=2))
    proxy.server.policy.archive_cap = 1000
    status, _, _ = request(proxy.path, "PUT", "/containers/qbittorrent/archive?path=/config", body=b"T" * 5000)
    assert status == 413 and len(proxy.daemon.writes()) == 1


# ── exec and hijack ──────────────────────────────────────────────────────────────────────────────────

def hijack(path, target, body, *, upgrade=True, token="good", stdin=b"ping", wait=1.0):
    payload = json.dumps(body).encode() if body is not None else b""
    extra = "Connection: Upgrade\r\nUpgrade: tcp\r\n" if upgrade else ""
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(path)
    sock.sendall((f"POST {target} HTTP/1.1\r\nHost: d\r\nX-PE-Session: {token}\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(payload)}\r\n{extra}\r\n").encode() + payload)
    sock.settimeout(3)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    got = rest
    if stdin and head.startswith((b"HTTP/1.1 101", b"HTTP/1.1 200")):
        deadline = time.monotonic() + wait
        while b"HELLO" not in got and time.monotonic() < deadline:
            got += sock.recv(4096)
        sock.sendall(stdin)
        while b"echo:" not in got and time.monotonic() < deadline + 2:
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
    return sock, head, got


def test_exec_create_then_a_hijacked_start_for_an_in_scope_container(proxy):
    status, _, body = proxy.req("POST", "/containers/qbittorrent/exec", body={"Cmd": ["id"], "AttachStdout": True})
    assert status == 201
    exec_id = json.loads(body)["Id"]
    sock, head, got = hijack(proxy.path, f"/v1.43/exec/{exec_id}/start", {"Detach": False}, upgrade=True)
    assert head.startswith(b"HTTP/1.1 101") and b"Upgrade: tcp" in head and b"HELLO" in got and b"echo:ping" in got
    sock.close()
    start = proxy.daemon.writes()[-1]
    assert start["target"] == f"/v1.43/exec/{exec_id}/start" and start["headers"].get("upgrade") == "tcp"
    assert any(e["route"] == "exec.start" for e in proxy.gate.writes())
    sock2, head2, got2 = hijack(proxy.path, f"/v1.43/exec/{exec_id}/start", {"Detach": False}, upgrade=False)
    assert head2.startswith(b"HTTP/1.1 200") and b"HELLO" in got2
    sock2.close()


def test_a_hijacked_exec_to_an_out_of_scope_container_is_refused_at_create_and_at_start(proxy):
    assert proxy.req("POST", "/containers/traefik/exec", body={"Cmd": ["id"]})[0] == 403
    assert proxy.req("POST", "/containers/vault/exec", body={"Cmd": ["id"]})[0] == 403
    assert proxy.daemon.exec_ids == []
    foreign = "9" * 64                                             # an exec the daemon knows but this session never created
    sock, head, _ = hijack(proxy.path, f"/v1.43/exec/{foreign}/start", {}, upgrade=True, stdin=b"")
    assert head.startswith(b"HTTP/1.1 403")
    sock.close()
    sock, head, _ = hijack(proxy.path, "/v1.43/containers/traefik/attach?stream=1&stdin=1", None, upgrade=True, stdin=b"")
    assert head.startswith(b"HTTP/1.1 403")
    sock.close()
    assert [w for w in proxy.daemon.writes() if "/exec/" in w["target"] or "attach" in w["target"]] == []


def test_attach_in_scope_is_hijacked_and_an_upgrade_elsewhere_is_refused(proxy):
    sock, head, got = hijack(proxy.path, "/v1.43/containers/qbittorrent/attach?stream=1&stdin=1&stdout=1", None, upgrade=True)
    assert head.startswith(b"HTTP/1.1 101") and b"echo:ping" in got
    sock.close()
    sock, head, _ = hijack(proxy.path, "/v1.43/containers/qbittorrent/restart", None, upgrade=True, stdin=b"")
    assert head.startswith(b"HTTP/1.1 403")
    sock.close()


def test_a_hijacked_connection_is_closed_when_the_session_is_paused_or_over_budget(env):
    env.make(Limits(head_s=2, hijack_s=30))
    eid = json.loads(env.req("POST", "/containers/qbittorrent/exec", body={"Cmd": ["sh"]})[2])["Id"]
    sock, head, got = hijack(env.path, f"/v1.43/exec/{eid}/start", {}, upgrade=True)
    assert b"echo:ping" in got
    env.gate.paused = True
    sock.settimeout(4)
    began = time.monotonic()
    try:
        while sock.recv(4096):
            pass
    except OSError:
        pass
    assert time.monotonic() - began < 4
    sock.close()
    env.gate.paused = False
    env.server.stop()
    env.make(Limits(head_s=2, hijack_bytes=64, hijack_s=30))
    eid = json.loads(env.req("POST", "/containers/qbittorrent/exec", body={"Cmd": ["sh"]})[2])["Id"]
    sock, head, got = hijack(env.path, f"/v1.43/exec/{eid}/start", {}, upgrade=True, stdin=b"x" * 4096)
    sock.settimeout(3)
    total = len(got)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            total += len(chunk)
    except OSError:
        pass
    assert total <= 64 + 4096                                   # the byte budget ended it; the 4 KiB echo did not all come back
    sock.close()


# ── the read-only variant ────────────────────────────────────────────────────────────────────────────

def test_the_read_only_variant_serves_reads_to_its_allowed_uid_only(env):
    path = os.path.join(env.dir, "ro.sock")
    ro = build_read_only(path, allowed_uids={os.geteuid()}, upstream=env.upstream)
    ro.start()
    try:
        assert request(path, "GET", "/containers/json", token=None)[0] == 200
        assert request(path, "GET", "/containers/qbittorrent/json", token=None)[0] == 200
        for method, target in (("POST", "/containers/qbittorrent/restart"), ("POST", "/containers/create"),
                               ("GET", "/containers/qbittorrent/logs"), ("DELETE", "/containers/plex")):
            assert request(path, method, target, token=None, body={} if method == "POST" and "create" in target else None)[0] == 403
        assert env.daemon.writes() == []
    finally:
        ro.stop()
    other = build_read_only(path, allowed_uids={os.geteuid() + 1}, upstream=env.upstream)
    other.start()
    try:
        assert request(path, "GET", "/containers/json", token=None)[0] == 401
    finally:
        other.stop()


def test_the_read_only_variant_protects_what_the_site_protects(env):
    path = os.path.join(env.dir, "ro.sock")
    ro = build_read_only(path, upstream=env.upstream, protected=["host:local/service:media/vault"])
    ro.start()
    try:
        names = [c["Names"][0] for c in json.loads(request(path, "GET", "/containers/json", token=None)[2])]
        assert "/vault" not in names
        assert request(path, "GET", "/containers/vault/json", token=None)[0] == 403
    finally:
        ro.stop()


def test_parse_helper_handles_an_empty_response():
    assert parse(b"") == (0, {}, b"")


def test_the_events_stream_is_redacted_and_drops_protected_containers(env):
    lines = [
        {"Type": "container", "Action": "start", "Actor": {"ID": "c" * 64, "Attributes": {"name": "plex", "image": "x", "api_token": "abcdef123456",
                                                                                          "com.docker.compose.project": "media",
                                                                                          "com.docker.compose.service": "plex"}}},
        {"Type": "container", "Action": "die", "Actor": {"ID": "d" * 64, "Attributes": {"name": "vault", "com.docker.compose.project": "media",
                                                                                      "com.docker.compose.service": "vault"}}},
        {"Type": "network", "Action": "connect", "Actor": {"ID": "n", "Attributes": {"name": "media_default"}}},
    ]
    payload = b"".join(json.dumps(item).encode() + b"\n" for item in lines) + b"garbage not json\n"

    def route(conn, rec):
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + payload)
    env.daemon._route = route
    env.make()
    status, _, body = env.req("GET", "/events?since=1")
    got = [json.loads(line) for line in body.splitlines() if line]
    assert status == 200 and [g["Type"] for g in got] == ["container", "network"] and b"vault" not in body
    assert b"abcdef123456" not in body and b"garbage" not in body


def test_a_container_this_session_created_is_checked_again_at_start_against_a_swapped_bind_source(proxy):
    """Review finding: a worker-writable bind path can be swapped for a symlink to the Docker socket after create."""
    source = Path(proxy.root) / "stacks" / "media" / "config"
    body = create_body(HostConfig={"Binds": [f"{source}:/config"]})
    status, _, answer = proxy.req("POST", "/containers/create?name=qb2", body=body)
    assert status == 201
    made = json.loads(answer)["Id"]
    assert proxy.req("POST", f"/containers/{made}/start")[0] == 204                  # unchanged: starts
    writes = len(proxy.daemon.writes())
    source.rmdir()
    source.symlink_to("/var/run/docker.sock")                                         # swapped after the create-time check
    for op in ("start", "restart"):
        status, _, text = proxy.req("POST", f"/containers/{made}/{op}")
        assert status == 403 and (b"symlink" in text or b"socket" in text)
    assert len(proxy.daemon.writes()) == writes
    source.unlink()
    source.mkdir()
    assert proxy.req("POST", f"/containers/{made}/restart")[0] == 204


def test_the_proxy_marks_every_container_it_creates_and_the_worker_cannot_supply_or_remove_the_mark(proxy):
    from planet_express.missions.dockerproxy.policy import CREATED_LABEL
    body = create_body()
    body["Labels"][CREATED_LABEL] = "0"
    assert proxy.req("POST", "/containers/create?name=qb3", body=body)[0] == 201
    sent = json.loads(proxy.daemon.writes()[-1]["body"])
    assert sent["Labels"][CREATED_LABEL] == "1"


def test_the_recheck_survives_many_creates_and_a_proxy_restart_because_it_rides_on_the_label_not_on_memory(proxy):
    """Review finding: bounded in-memory tracking could be evicted; the mark lives on the container itself."""
    source = Path(proxy.root) / "stacks" / "media" / "config"
    status, _, answer = proxy.req("POST", "/containers/create?name=first", body=create_body(HostConfig={"Binds": [f"{source}:/c"]}))
    first = json.loads(answer)["Id"]
    for n in range(40):
        assert proxy.req("POST", f"/containers/create?name=more{n}", body=create_body())[0] == 201
    proxy.server.stop()
    proxy.make()                                                                     # a new proxy, no memory of anything
    source.rmdir()
    source.symlink_to("/var/run/docker.sock")
    assert proxy.req("POST", f"/containers/{first}/start")[0] == 403


def test_a_container_the_operator_created_is_not_held_to_the_grants_paths_when_restarted(proxy, monkeypatch):
    from dockerproxy_fakes import CONTAINERS
    monkeypatch.setitem(CONTAINERS["qbittorrent"], "host_config", {"Binds": ["/mnt/data:/data"]})
    assert proxy.req("POST", "/containers/qbittorrent/restart")[0] == 204


# ── API version negotiation ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("daemon, top, refused", [("1.52", 48, "v1.49"), ("1.45", 45, "v1.46"), ("1.48", 48, "v1.49")])
def test_the_proxy_serves_up_to_the_lower_of_the_daemons_version_and_its_own_and_says_so_in_ping(env, daemon, top, refused):
    env.daemon.api_version = f"{daemon}"
    path = os.path.join(env.dir, "ro.sock")
    ro = build_read_only(path, upstream=env.upstream)
    ro.start()
    try:
        status, headers, _ = request(path, "GET", "/_ping", token=None, version=None)
        assert status == 200 and headers["api-version"] == f"1.{top}"                 # clients negotiate down to this
        assert request(path, "GET", f"/v1.{top}/containers/json", token=None, version=None, raw_target=True)[0] == 200
        assert request(path, "GET", f"/{refused}/containers/json", token=None, version=None, raw_target=True)[0] == 403
        assert request(path, "GET", "/v1.41/containers/json", token=None, version=None, raw_target=True)[0] == 200
    finally:
        ro.stop()


def test_the_daemons_api_version_is_read_from_its_ping_and_an_unreadable_one_is_none(env):
    env.daemon.api_version = "1.52"
    assert env.upstream.api_version() == 52
    env.daemon.api_version = "garbage"
    assert env.upstream.api_version() is None
    assert Upstream("/nonexistent/docker.sock").api_version() is None


def test_the_proxys_own_questions_to_the_daemon_are_unversioned(proxy):
    assert proxy.req("POST", "/containers/qbittorrent/restart")[0] == 204
    asked = [r["target"] for r in proxy.daemon.requests if r["method"] == "GET"]
    assert asked and all(not t.startswith("/v1.") for t in asked)                   # no pinned version in inspection


# ── lists are scoped like inspect ─────────────────────────────────────────────────────────────────────

def test_the_volume_list_drops_out_of_scope_volumes_and_strips_host_paths_and_options(proxy):
    status, _, body = proxy.req("GET", "/volumes")
    doc = json.loads(body)
    names = sorted(v["Name"] for v in doc["Volumes"])
    assert status == 200 and names == ["bindvol", "fancy", "media_cfg"]                # other_vol (another stack) is gone
    assert all("Mountpoint" not in v and "Options" not in v for v in doc["Volumes"])
    status, _, body = proxy.req("GET", "/volumes/media_cfg")
    assert status == 200 and b"Mountpoint" in body                                     # (inspect of an in-scope volume is unchanged)
    assert proxy.req("GET", "/volumes/other_vol")[0] == 403


def test_the_network_list_strips_options_and_drops_protected_networks(env):
    from planet_express.missions.dockerproxy import policy as policy_mod
    env.make()
    status, _, body = env.req("GET", "/networks")
    assert status == 200 and all("Options" not in n for n in json.loads(body)) and len(json.loads(body)) == 4
    env.server.stop()
    original = env.server.policy
    scope = make_scope(env.dir, protected=("host:local/stack:other",))
    env.make()
    env.server.policy.scope = scope
    names = [n["Name"] for n in json.loads(env.req("GET", "/networks")[2])]
    assert "other_default" not in names and "media_default" in names and original is not None and policy_mod


def test_the_read_only_variant_lists_volumes_without_host_paths(env):
    path = os.path.join(env.dir, "ro.sock")
    ro = build_read_only(path, upstream=env.upstream)
    ro.start()
    try:
        doc = json.loads(request(path, "GET", "/volumes", token=None)[2])
        assert len(doc["Volumes"]) == 4 and all("Mountpoint" not in v for v in doc["Volumes"])
    finally:
        ro.stop()


# ── archive: the path stat, chunked uploads ─────────────────────────────────────────────────────────

def test_an_archive_download_forwards_the_path_stat_header_docker_cp_needs(proxy):
    status, headers, body = proxy.req("GET", "/containers/qbittorrent/archive?path=/config")
    assert status == 200 and body == b"TARDATA"
    assert headers.get("x-docker-container-path-stat") == "eyJuYW1lIjoiY29uZmlnIn0="


def chunked(path, target, pieces, *, extra="", tail=b"0\r\n\r\n", token="good", method="PUT"):
    body = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in pieces) + tail
    head = (f"{method} {target} HTTP/1.1\r\nHost: d\r\nX-PE-Session: {token}\r\nTransfer-Encoding: chunked\r\n"
            f"Content-Type: application/x-tar\r\n{extra}\r\n").encode()
    return talk(path, head + body)


def test_a_chunked_archive_upload_is_decoded_bounded_and_forwarded(proxy):
    pieces = [b"A" * 3000, b"B" * 10, b"C" * 70000]
    status, _, _ = chunked(proxy.path, "/v1.43/containers/qbittorrent/archive?path=/config", pieces)
    assert status in (200, 204)
    sent = proxy.daemon.writes()[-1]
    assert sent["body"] == b"".join(pieces) and sent["target"].startswith(f"/v1.43/containers/{ID_A}/archive?")
    assert any(e["route"] == "container.archive.put" and e["bytes"] == "chunked" for e in proxy.gate.writes())


def test_a_chunked_upload_over_the_cap_is_refused(env):
    env.make()
    env.server.policy.archive_cap = 1000
    status, _, _ = chunked(env.path, "/v1.43/containers/qbittorrent/archive?path=/config", [b"A" * 600, b"B" * 600])
    assert status in (413, 502, 0)
    assert env.daemon.writes() == []


@pytest.mark.parametrize("name, kwargs", [
    ("bad size", {"pieces": [b"x"], "tail": b"0\r\n\r\n", "mangle": (b"1\r\n", b"zz\r\n")}),
    ("extension", {"pieces": [b"x"], "tail": b"0\r\n\r\n", "mangle": (b"1\r\n", b"1;ext=1\r\n")}),
    ("trailers", {"pieces": [b"x"], "tail": b"0\r\nX-Evil: 1\r\n\r\n"}),
    ("bytes after last chunk", {"pieces": [b"x"], "tail": b"0\r\n\r\nGET /v1.43/containers/json HTTP/1.1\r\n\r\n"}),
    ("missing chunk CRLF", {"pieces": [b"x"], "tail": b"0\r\n\r\n", "mangle": (b"x\r\n", b"xYY")}),
    ("size too long", {"pieces": [b"x"], "tail": b"0\r\n\r\n", "mangle": (b"1\r\n", b"0000000000000001\r\n")}),
    ("negative size", {"pieces": [b"x"], "tail": b"0\r\n\r\n", "mangle": (b"1\r\n", b"-1\r\n")}),
])
def test_bad_chunk_framing_never_yields_a_forwarded_write(proxy, name, kwargs):
    pieces, tail, mangle = kwargs["pieces"], kwargs["tail"], kwargs.get("mangle")
    body = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in pieces) + tail
    if mangle:
        body = body.replace(*mangle, 1)
    head = (b"PUT /v1.43/containers/qbittorrent/archive?path=/config HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n"
            b"Transfer-Encoding: chunked\r\nContent-Type: application/x-tar\r\n\r\n")
    status, _, _ = talk(proxy.path, head + body)
    assert status != 204 and status != 200, name
    assert [w for w in proxy.daemon.writes() if "archive" in w["target"]] == [], name


@pytest.mark.parametrize("extra", ["Content-Length: 5\r\n", "Content-Length: 0\r\n"])
def test_content_length_together_with_transfer_encoding_is_refused(proxy, extra):
    head = (f"PUT /v1.43/containers/qbittorrent/archive?path=/config HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n"
            f"Transfer-Encoding: chunked\r\n{extra}\r\n5\r\nhello\r\n0\r\n\r\n").encode()
    assert talk(proxy.path, head)[0] == 400 and proxy.daemon.writes() == []


@pytest.mark.parametrize("value", ["gzip, chunked", "chunked, gzip", "identity", "Chunked2", "chunked\t;x"])
def test_only_a_plain_chunked_transfer_encoding_is_accepted(proxy, value):
    head = (f"PUT /v1.43/containers/qbittorrent/archive?path=/config HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n"
            f"Transfer-Encoding: {value}\r\n\r\n1\r\nx\r\n0\r\n\r\n").encode()
    assert talk(proxy.path, head)[0] == 400 and proxy.daemon.writes() == []


def test_chunked_bodies_are_refused_on_every_endpoint_but_the_archive_upload(proxy):
    for method, target in (("POST", "/v1.43/containers/create"), ("POST", "/v1.43/containers/qbittorrent/restart"),
                           ("POST", "/v1.43/containers/qbittorrent/exec"), ("GET", "/v1.43/containers/json")):
        status, _, _ = chunked(proxy.path, target, [b"{}"], method=method)
        assert status == 400, target
    assert proxy.daemon.writes() == []


def test_a_chunked_upload_whose_last_chunk_is_malformed_reaches_the_daemon_not_at_all(proxy):
    """Review finding: validate the whole body before the first byte is forwarded."""
    body = b"5\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n"
    head = (b"PUT /v1.43/containers/qbittorrent/archive?path=/config HTTP/1.1\r\nHost: d\r\nX-PE-Session: good\r\n"
            b"Transfer-Encoding: chunked\r\nContent-Type: application/x-tar\r\n\r\n")
    status, _, _ = talk(proxy.path, head + body)
    assert status == 400
    assert [r for r in proxy.daemon.requests if "archive" in r["target"] and r["method"] == "PUT"] == []


def test_a_daemon_older_than_the_floor_gets_a_controlled_refusal_not_a_500(env):
    env.daemon.api_version = "1.30"
    path = os.path.join(env.dir, "ro.sock")
    ro = build_read_only(path, upstream=env.upstream)
    ro.start()
    try:
        for target in ("/_ping", "/v1.41/containers/json"):
            status, _, body = request(path, "GET", target, token=None, version=None, raw_target=True)
            assert status == 403 and b"older" in body
    finally:
        ro.stop()


def test_a_created_container_id_is_recorded_before_the_answer_so_the_tripwire_can_trust_it(proxy):
    body = (b'{"Image":"alpine:3","Labels":{"com.docker.compose.project":"media",'
            b'"com.docker.compose.service":"qbittorrent"},"Cmd":["a"]}')
    status, _, data = proxy.req("POST", "/containers/create", body=body)
    assert status == 201
    made = list(proxy.daemon.made)
    created = [e for e in proxy.gate.events if e["event"] == "docker.created"]
    assert [e["created_id"] for e in created] == made
    assert proxy.gate.events.index(created[0]) < proxy.gate.events.index(
        next(e for e in proxy.gate.events if e["event"] == "docker.result"))


_CREATE = (b'{"Image":"alpine:3","Labels":{"com.docker.compose.project":"media",'
           b'"com.docker.compose.service":"qbittorrent"},"Cmd":["a"]}')


def test_a_create_is_announced_before_the_daemon_sees_it(proxy):
    order = []
    original = proxy.gate.record
    proxy.gate.record = lambda s, b: (order.append(("record", b["event"])), original(s, b))[1]
    proxy.daemon.requests.clear()
    status, _, _ = proxy.req("POST", "/containers/create", body=_CREATE)
    assert status == 201
    begin = next(e for e in proxy.gate.events if e["event"] == "docker.create_begin")
    created = next(e for e in proxy.gate.events if e["event"] == "docker.created")
    assert begin["nonce"] == created["nonce"] and order[0] == ("record", "docker.write")
    assert ("record", "docker.create_begin") in order


def test_if_the_intent_cannot_be_recorded_nothing_is_created(proxy):
    original = proxy.gate.record

    def selective(session, body):
        if body["event"] == "docker.create_begin":
            raise RuntimeError("disk full")
        return original(session, body)
    proxy.gate.record = selective
    status, _, _ = proxy.req("POST", "/containers/create", body=_CREATE)
    assert status == 500 and proxy.daemon.made == {}


def test_a_created_id_that_cannot_be_recorded_fails_the_create_closed_and_removes_the_container(proxy):
    original = proxy.gate.record

    def selective(session, body):
        if body["event"] == "docker.created":
            raise RuntimeError("disk full")
        return original(session, body)
    proxy.gate.record = selective
    status, _, _ = proxy.req("POST", "/containers/create", body=_CREATE)
    assert status == 500
    assert any(r["method"] == "DELETE" for r in proxy.daemon.requests)
    assert any(e["event"] == "docker.create_end" for e in proxy.gate.events)
