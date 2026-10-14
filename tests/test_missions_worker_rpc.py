"""Worker socket: per-call peer + session authentication and the five-method allowlist."""

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)
from planet_express.integrations.rpc import RpcError, RpcServer, call
from planet_express.missions.worker_rpc import (
    WORKER_METHODS,
    WorkerAuthenticator,
    build_worker_handlers,
    build_worker_server,
    parse_proc_cgroup,
)

TOKEN = "t" * 43
CG = "/sys/fs/cgroup/pe/s1"


@dataclass
class Row:
    id: str = "s1"
    mission_id: str = "m1"
    token_hash: str = hashlib.sha256(TOKEN.encode()).hexdigest()
    kind: str = "local_cgroup"
    cgroup_path: str | None = CG
    session_uid: int | None = os.getuid()      # sessions are bound to a unique uid too (cgroup AND uid AND token)
    revoked_at: int | None = None
    expires_at: int = 10**12


class Sessions:
    """Stands in for S3a's resolve_session: hash lookup only; S3b re-checks identity itself."""

    def __init__(self, row=None):
        self.row = row or Row()
        self.calls = []

    def resolve(self, token_hash, peer_cgroup=None, peer_uid=None):
        self.calls.append((token_hash, peer_cgroup, peer_uid))
        return self.row if token_hash == self.row.token_hash else None


class Backend:
    def __init__(self):
        self.seen = []

    def get_context(self, session):
        self.seen.append(("ctx", session.mission_id))
        return {"mission_id": session.mission_id}

    def post_event(self, session, kind, data):
        self.seen.append(("event", kind, data))
        return {"ok": True}

    def heartbeat(self, session):
        self.seen.append(("hb", session.id))
        return {"ok": True}

    def submit_result(self, session, result):
        self.seen.append(("result", result.diagnosis))
        return {"accepted": True}

    def memory_search(self, session, query, limit):
        self.seen.append(("mem", query, limit))
        return {"cards": []}


@pytest.fixture
def env():
    directory = Path(tempfile.mkdtemp(prefix="pw", dir="/tmp"))
    started = []

    def start(sessions=None, cgroup=CG, backend=None, uid_group=None, core_uid=os.getuid() + 1):
        sessions = sessions or Sessions()
        backend = backend or Backend()
        # The test process is the "worker" here, so Core must be some other uid for the identity to be acceptable.
        auth = WorkerAuthenticator(None, resolve=sessions.resolve, read_cgroup=lambda pid: cgroup,
                                   core_uid=core_uid)
        server = build_worker_server(directory / "worker.sock", backend, auth, group=uid_group,
                                     read_timeout=0.5)
        server.start()
        started.append(server)
        return directory / "worker.sock", sessions, backend, server

    yield start
    for s in started:
        s.stop()
    shutil.rmtree(directory)


def ask(sock, method="mission.heartbeat", params=None, token=TOKEN):
    return call(sock, method, params, session_token=token)


def refused(resp):
    return resp["ok"] is False and resp["error"] == {"code": "unauthorized", "message": "Refused"}


def test_handler_table_is_exactly_the_five_worker_methods():
    assert set(build_worker_handlers(Backend())) == set(WORKER_METHODS) == {
        "mission.get_context", "mission.post_event", "mission.heartbeat",
        "mission.submit_result", "memory.search"}


def test_server_cannot_be_given_the_operator_handler_table(env):
    sock, _, _, server = env()
    assert set(server.handlers) == set(WORKER_METHODS)
    with pytest.raises(ValueError):
        build_worker_server(sock.parent / "x.sock", Backend(),
                            WorkerAuthenticator(None, resolve=Sessions().resolve),
                            group=None, handlers={"approval.decide": lambda p, s: {}})


def test_right_cgroup_and_token_is_accepted_and_scoped_to_the_mission(env):
    sock, sessions, backend, _ = env()
    resp = ask(sock, "mission.get_context")
    assert resp["ok"] and resp["result"] == {"mission_id": "m1"}
    assert sessions.calls[0][0] == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert sessions.calls[0][1] == CG and sessions.calls[0][2] == os.getuid()


def test_wrong_cgroup_is_refused_even_with_the_right_token(env):
    sock, *_ = env(cgroup="/sys/fs/cgroup/pe/other")
    assert refused(ask(sock))


def test_process_outside_any_cgroup_is_refused(env):
    sock, *_ = env(cgroup=None)
    assert refused(ask(sock))


def test_right_cgroup_wrong_or_missing_token_is_refused(env):
    sock, *_ = env()
    assert refused(ask(sock, token="x" * 43))
    assert refused(ask(sock, token=None))
    assert refused(ask(sock, token=12345))
    assert refused(ask(sock, token="x" * 5000))


def test_revoked_session_is_refused(env):
    sock, *_ = env(sessions=Sessions(Row(revoked_at=5)))
    assert refused(ask(sock))


def test_uid_kind_requires_matching_uid_and_refuses_root_and_core_uid(env):
    sock, *_ = env(sessions=Sessions(Row(kind="local_uid", cgroup_path=None, session_uid=os.getuid())),
                   core_uid=os.getuid())
    # session uid equals the Core uid: that is not a worker identity, refuse
    assert refused(ask(sock))


def test_a_cgroup_session_needs_its_uid_as_well_as_its_cgroup_and_token(env):
    from planet_express.integrations.rpc import PeerCred
    for row, uid, ok in ((Row(), os.getuid(), True), (Row(), os.getuid() + 5, False),
                         (Row(session_uid=None), os.getuid(), False),      # a cgroup session with no uid: refused
                         (Row(session_uid=0), 0, False),
                         (Row(session_uid=True), 1, False)):
        sessions = Sessions(row)
        auth = WorkerAuthenticator(None, resolve=sessions.resolve, read_cgroup=lambda pid: CG, core_uid=os.getuid() + 1)
        got = auth(PeerCred(pid=1, uid=uid, gid=1), {"session_token": TOKEN})
        assert (got is row) is ok, (row, uid)


def test_the_core_uid_is_never_a_session_even_with_the_right_cgroup_and_token():
    from planet_express.integrations.rpc import PeerCred
    sessions = Sessions(Row(session_uid=1000))
    auth = WorkerAuthenticator(None, resolve=sessions.resolve, read_cgroup=lambda pid: CG, core_uid=1000)
    assert auth(PeerCred(pid=1, uid=1000, gid=1000), {"session_token": TOKEN}) is None


def test_uid_kind_accepts_the_session_uid(env, monkeypatch):
    sock, *_ = env(sessions=Sessions(Row(kind="local_uid", cgroup_path=None, session_uid=4242)),
                   cgroup=None)
    # peer uid here is ours (not 4242): refused
    assert refused(ask(sock))


def test_uid_kind_positive_path_via_authenticator_directly():
    sessions = Sessions(Row(kind="local_uid", cgroup_path=None, session_uid=4242))
    auth = WorkerAuthenticator(None, resolve=sessions.resolve, read_cgroup=lambda pid: None)
    from planet_express.integrations.rpc import PeerCred
    assert auth(PeerCred(pid=1, uid=4242, gid=1), {"session_token": TOKEN}) is sessions.row
    assert auth(PeerCred(pid=1, uid=4243, gid=1), {"session_token": TOKEN}) is None
    assert auth(PeerCred(pid=1, uid=0, gid=0), {"session_token": TOKEN}) is None


def test_fake_kind_is_never_accepted_on_a_real_socket():
    sessions = Sessions(Row(kind="fake"))
    auth = WorkerAuthenticator(None, resolve=sessions.resolve, read_cgroup=lambda pid: CG)
    from planet_express.integrations.rpc import PeerCred
    assert auth(PeerCred(pid=1, uid=5, gid=5), {"session_token": TOKEN}) is None


@pytest.mark.parametrize("method", ["approval.decide", "execution.abort", "config.get",
                                    "mission.request_decision", "tool.invoke", "nope"])
def test_methods_outside_the_allowlist_are_refused_for_everyone(env, method):
    sock, *_ = env()
    # authenticated peer: the method simply does not exist on this socket
    resp = ask(sock, method)
    assert resp["ok"] is False and resp["error"]["code"] == "unknown_method"
    # unauthenticated peer: indistinguishable generic refusal, nothing about the method table leaks
    assert refused(ask(sock, method, token="x" * 43))


def test_params_are_validated_and_bounded(env):
    sock, _, backend, _ = env()
    bad = [
        ("mission.post_event", {"kind": "", "data": {}}),
        ("mission.post_event", {"kind": "note", "data": {"x": "y" * 40000}}),
        ("mission.post_event", {"kind": "note", "data": {}, "extra": 1}),
        ("memory.search", {"query": "", "limit": 3}),
        ("memory.search", {"query": "q", "limit": 500}),
        ("mission.submit_result", {"result": {"diagnosis": "x"}}),           # confidence missing
        ("mission.submit_result", {"result": {"diagnosis": "x", "confidence": 2}}),
        ("mission.heartbeat", {"surprise": 1}),
    ]
    for method, params in bad:
        resp = ask(sock, method, params)
        assert resp["ok"] is False and resp["error"]["code"] == "bad_params", (method, params)
    assert backend.seen == []


def test_valid_calls_reach_the_backend_with_the_session(env):
    sock, _, backend, _ = env()
    assert ask(sock, "mission.post_event", {"kind": "note", "data": {"a": 1}})["ok"]
    assert ask(sock, "mission.heartbeat")["ok"]
    assert ask(sock, "memory.search", {"query": "nfs", "limit": 3})["ok"]
    r = ask(sock, "mission.submit_result", {"result": {"diagnosis": "d", "confidence": "medium"}})
    assert r["ok"] and r["result"] == {"accepted": True}
    assert backend.seen == [("event", "note", {"a": 1}), ("hb", "s1"), ("mem", "nfs", 3), ("result", "d")]


def test_backend_errors_do_not_leak(env):
    class Boom(Backend):
        def heartbeat(self, session):
            raise RuntimeError("secret-path /etc/shadow")

    sock, *_ = env(backend=Boom())
    resp = ask(sock)
    assert resp["ok"] is False and "shadow" not in str(resp)


def test_params_cannot_carry_the_token_through_to_handlers(env):
    sock, _, backend, _ = env()
    resp = ask(sock, "mission.heartbeat", {"session_token": TOKEN})
    assert resp["ok"] is False and resp["error"]["code"] == "bad_params"


# --- /proc cgroup parsing ------------------------------------------------------------------

def test_parse_proc_cgroup_v2_line():
    assert parse_proc_cgroup("0::/pe/s1\n", "/sys/fs/cgroup") == "/sys/fs/cgroup/pe/s1"
    assert parse_proc_cgroup("12:cpu:/x\n0::/pe/s1\n", "/sys/fs/cgroup") == "/sys/fs/cgroup/pe/s1"
    assert parse_proc_cgroup("0::/\n", "/sys/fs/cgroup") == "/sys/fs/cgroup"


@pytest.mark.parametrize("text", ["", "12:cpu:/x\n", "0::/a/../b\n", "0::/a (deleted)\n", "0::relative\n"])
def test_parse_proc_cgroup_rejects_anything_unexpected(text):
    assert parse_proc_cgroup(text, "/sys/fs/cgroup") is None


# --- RpcServer extension stays backwards compatible ----------------------------------------

def test_plain_rpcserver_still_requires_a_uid_allowlist(tmp_path):
    with pytest.raises(ValueError):
        RpcServer(tmp_path / "s.sock", {"x": lambda p: p}, allowed_uids=None, group=None)


def test_handlers_without_authenticator_keep_the_one_argument_shape():
    d = Path(tempfile.mkdtemp(prefix="pw", dir="/tmp"))
    server = RpcServer(d / "s.sock", {"echo": lambda params: params}, allowed_uids={os.getuid()}, group=None)
    server.start()
    try:
        assert call(d / "s.sock", "echo", {"a": 1})["result"] == {"a": 1}
    finally:
        server.stop()
        shutil.rmtree(d)


def test_rpcerror_is_still_the_transport_error():
    with pytest.raises(RpcError):
        call("/nonexistent/sock", "x")
