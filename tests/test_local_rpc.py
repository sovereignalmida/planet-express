"""Real Unix socket protocol, admission control, and core adapter tests."""

import json
import os
import pwd
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as farnsworth
from planet_express.application.command_service import DecideResult, ProposeResult
from planet_express.core.store import Store
from planet_express.integrations.rpc import (
    MAX_FRAME,
    RpcError,
    RpcServer,
    build_core_handlers,
    call,
)


@pytest.fixture
def socket_path():
    directory = Path(tempfile.mkdtemp(prefix="pe", dir="/tmp"))
    yield directory / "core.sock"
    shutil.rmtree(directory)


@pytest.fixture
def server(socket_path):
    servers = []

    def start(handlers=None, **kwargs):
        options = {"allowed_uids": {os.getuid()}, "group": None, "read_timeout": 0.3}
        options.update(kwargs)
        instance = RpcServer(socket_path, handlers or {"echo": lambda params: params}, **options)
        servers.append(instance)
        instance.start()
        return instance

    yield start
    for instance in servers:
        instance.stop()


def connect(path):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(2)
    conn.connect(str(path))
    return conn


def frame(value):
    data = json.dumps(value).encode()
    return struct.pack("!I", len(data)) + data


def receive(conn):
    def exact(size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            assert chunk
            data += chunk
        return data

    return json.loads(exact(struct.unpack("!I", exact(4))[0]))


def assert_closed(conn):
    try:
        assert conn.recv(1) == b""
    except ConnectionResetError:
        pass


def test_round_trip_and_unknown(server, socket_path):
    server()
    assert call(socket_path, "echo", {"hello": "世界"}, request_id="r") == {
        "request_id": "r", "ok": True, "result": {"hello": "世界"}}
    assert call(socket_path, "missing")["error"]["code"] == "unknown_method"


@pytest.mark.parametrize("data", [b"{", b"\xff", b"[]", b'{}',
                                      b'{"request_id":"r","method":"echo","params":[]}',
                                      b'{"request_id":"r","method":"echo","params":{"x":NaN}}'])
def test_bad_json_or_shape(server, socket_path, data):
    server()
    with connect(socket_path) as conn:
        conn.sendall(struct.pack("!I", len(data)) + data)
        assert receive(conn)["error"]["code"] == "bad_request"
        assert_closed(conn)


@pytest.mark.parametrize("data", [struct.pack("!I", MAX_FRAME + 1), b"\x00\x00",
                                      struct.pack("!I", 10) + b"{", b""])
def test_oversize_partial_and_idle_close(server, socket_path, data):
    server(read_timeout=0.08)
    with connect(socket_path) as conn:
        conn.sendall(data)
        assert_closed(conn)


def test_wrong_peer_closed_without_sending(server, socket_path, caplog):
    server(allowed_uids={os.getuid() + 1}, read_timeout=5)
    with connect(socket_path) as conn:
        conn.settimeout(0.5)
        assert_closed(conn)
    assert "Rejected RPC peer uid" in caplog.text


def test_slow_handler_and_reserved_worker(server, socket_path):
    entered = threading.Event()
    release = threading.Event()

    def slow(params):
        entered.set()
        assert release.wait(3)
        return "done"

    server({"slow": slow, "echo": lambda p: p, "approval.decide": lambda p: "decided"}, workers=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, socket_path, "slow")
        try:
            assert entered.wait(1)
            assert call(socket_path, "echo")["error"]["code"] == "busy"
            assert call(socket_path, "approval.decide")["result"] == "decided"
        finally:
            release.set()
        assert future.result()["result"] == "done"


def test_slow_handler_does_not_block_second_general_worker(server, socket_path):
    entered = threading.Event()
    release = threading.Event()

    def slow(params):
        entered.set()
        assert release.wait(3)

    server({"slow": slow, "echo": lambda p: p}, workers=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, socket_path, "slow")
        try:
            assert entered.wait(1)
            assert call(socket_path, "echo", {"ready": True})["result"] == {"ready": True}
        finally:
            release.set()
        assert future.result()["ok"]


def test_both_pools_full_reply_without_read(server, socket_path):
    general_entered = threading.Event()
    reserved_entered = threading.Event()
    release = threading.Event()

    def hold(event):
        def handler(params):
            event.set()
            assert release.wait(3)
        return handler

    server({"slow": hold(general_entered), "approval.decide": hold(reserved_entered)}, workers=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, socket_path, "slow")
        try:
            assert general_entered.wait(1)
            second = pool.submit(call, socket_path, "approval.decide")
            assert reserved_entered.wait(1)
            with connect(socket_path) as conn:
                response = receive(conn)
                assert response["request_id"] is None
                assert response["error"]["code"] == "busy"
                assert_closed(conn)
        finally:
            release.set()
        assert first.result()["ok"]
        assert second.result()["ok"]


def test_mode_stale_socket_and_stop(server, socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(socket_path))
    instance = server(group=os.getgid())
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
    assert call(socket_path, "echo")["ok"]
    instance.stop()
    assert not socket_path.exists()


def test_non_socket_not_removed(server, socket_path):
    socket_path.write_text("keep")
    with pytest.raises(FileExistsError, match="not a socket"):
        server()
    assert socket_path.read_text() == "keep"


def test_missing_parent(server, socket_path):
    socket_path.parent.rmdir()
    with pytest.raises(FileNotFoundError, match="socket directory"):
        server()
    socket_path.parent.mkdir()


@pytest.mark.parametrize("operation", ["chown", "chmod"])
def test_permission_failure_cleans_up(server, socket_path, monkeypatch, operation):
    def fail(*args):
        raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(os, operation, fail)
        with pytest.raises(PermissionError, match="denied"):
            server(group=os.getgid())
    assert not socket_path.exists()


def test_handler_exception_is_internal_without_params(server, socket_path, caplog):
    def fail(params):
        raise RuntimeError(params["secret"])

    server({"fail": fail})
    response = call(socket_path, "fail", {"secret": "do-not-log"})
    assert response["error"]["code"] == "internal"
    assert "RPC handler failed" in caplog.text
    assert "do-not-log" not in caplog.text + json.dumps(response)


def test_transport_failures(server, socket_path):
    with pytest.raises(RpcError):
        call(socket_path, "echo")
    server(read_timeout=0.05)
    with pytest.raises(RpcError):
        call(socket_path, "echo", {"huge": "x" * MAX_FRAME})


def test_core_handlers():
    commands = Mock()
    store = Mock()
    commands.propose.return_value = ProposeResult(True, "a", True, "pending")
    commands.decide.return_value = DecideResult("started", "ok", "e")
    commands.get_status.return_value = {"id": "e", "status": "running"}
    store.list_pending.return_value = [{"id": "a", "target_json": '{"stack":"media"}', "message_id": 5}]
    handlers = build_core_handlers(commands, store)
    assert set(handlers) == {"proposal.create", "proposal.list_pending", "approval.decide", "execution.get_status"}
    params = {"action": "docker.restart_service", "stack": "media", "service": "sonarr", "requested_by": "Chris"}
    assert handlers["proposal.create"](params) == asdict(commands.propose.return_value)
    commands.propose.assert_called_once_with(**params, requested_via="dashboard")
    params = {"approval_id": "a", "approve": True, "decided_by": "Chris"}
    assert handlers["approval.decide"](params) == asdict(commands.decide.return_value)
    commands.decide.assert_called_once_with(**params, decision=None)
    assert handlers["proposal.list_pending"]({}) == [{"id": "a", "target": {"stack": "media"}}]
    assert handlers["execution.get_status"]({"execution_id": "e"}) == commands.get_status.return_value
    commands.get_status.assert_called_once_with("e")
    commands.get_status.return_value = None
    with pytest.raises(RpcError) as error:
        handlers["execution.get_status"]({"execution_id": "missing"})
    assert error.value.code == "not_found"


@pytest.mark.parametrize("method,params", [
    ("proposal.create", {}),
    ("proposal.create", {"action": "a", "stack": "", "service": "s", "requested_by": "c"}),
    ("proposal.create", {"action": "a", "stack": "s", "service": "s", "requested_by": "c" * 257}),
    ("proposal.list_pending", {"extra": True}),
    ("approval.decide", {"approval_id": "a", "approve": 1, "decided_by": "c"}),
    ("approval.decide", {"approval_id": "a", "approve": False, "decided_by": " "}),
    ("execution.get_status", {"execution_id": None}),
])
def test_bad_params(server, socket_path, method, params):
    commands, store = Mock(), Mock()
    server(build_core_handlers(commands, store))
    assert call(socket_path, method, params)["error"]["code"] == "bad_request"
    assert not commands.mock_calls
    assert not store.mock_calls


def test_list_pending_expires_and_excludes_decided(tmp_path):
    now = [100.0]
    store = Store(tmp_path / "data" / "core.db", clock=lambda: now[0])
    store.init()

    def propose(key, ttl=3600):
        return store.propose(action="restart", target_key=key, target={"service": key},
                             risk="R1", requested_via="dashboard", ttl_seconds=ttl)[0]

    pending = propose("pending")
    decided = propose("decided")
    expired = propose("expired", ttl=1)
    store.consume(decided["id"], decision="denied", decided_by="Chris", arrived_at=100)
    now[0] = 102
    assert store.list_pending() == [pending]
    assert store.get_approval(expired["id"])["status"] == "expired"
    assert len([e for e in store.list_events() if e["kind"] == "approval.expired"]) == 1


def test_startup_failure_logs_and_returns(monkeypatch, caplog):
    monkeypatch.setattr(farnsworth.config, "RPC_PEER_USERS", [pwd.getpwuid(os.getuid()).pw_name])
    server = Mock()
    server.start.side_effect = OSError("socket unavailable")
    factory = Mock(return_value=server)
    monkeypatch.setattr(farnsworth, "RpcServer", factory)
    assert farnsworth._start_dashboard_rpc(Mock(), Mock()) is None
    assert [r.message for r in caplog.records] == ["Dashboard RPC not started: socket unavailable"]


def test_startup_no_peer_users(monkeypatch, caplog):
    monkeypatch.setattr(farnsworth.config, "RPC_PEER_USERS", [])
    assert farnsworth._start_dashboard_rpc(Mock(), Mock()) is None
    assert "Dashboard RPC not started: no resolvable RPC peer users" in caplog.text


def test_stop_interrupts_idle_read(server, socket_path):
    instance = server(read_timeout=10)
    with connect(socket_path) as conn:
        # A completed request proves the accept loop has dispatched the earlier idle client.
        assert call(socket_path, "echo")["ok"]
        started = time.monotonic()
        instance.stop()
        assert time.monotonic() - started < 1
        assert_closed(conn)


def test_core_errors_over_socket(server, socket_path):
    commands = Mock()
    commands.get_status.return_value = None
    server(build_core_handlers(commands, Mock()))
    assert call(socket_path, "execution.get_status", {"execution_id": "missing"})["error"]["code"] == "not_found"
    for method in ("query.container", "logs.tail", "action.request"):
        assert call(socket_path, method)["error"]["code"] == "unknown_method"


def test_core_param_validation_without_transport():
    commands = Mock()
    handlers = build_core_handlers(commands, Mock())
    for method in handlers:
        with pytest.raises(RpcError) as error:
            handlers[method]({"unexpected": True})
        assert error.value.code == "bad_request"
    assert not commands.mock_calls


def test_one_request_per_connection(server, socket_path):
    server()
    with connect(socket_path) as conn:
        conn.sendall(frame({"request_id": "r", "method": "echo", "params": {}}))
        assert receive(conn)["ok"]
        assert_closed(conn)


def test_client_timeout(server, socket_path):
    release = threading.Event()

    def slow(params):
        release.wait(2)

    server({"slow": slow})
    try:
        with pytest.raises(RpcError):
            call(socket_path, "slow", timeout=0.03)
    finally:
        release.set()


def test_umask_restored_on_bind_failure(server, monkeypatch):
    calls = []

    def umask(value):
        calls.append(value)
        return 0o027

    def fail(*args):
        raise OSError("bind failed")

    monkeypatch.setattr(os, "umask", umask)
    monkeypatch.setattr(socket.socket, "bind", fail)
    with pytest.raises(OSError, match="bind failed"):
        server()
    assert calls == [0o007, 0o027]


def test_startup_missing_group(monkeypatch, caplog, socket_path):
    monkeypatch.setattr(farnsworth.config, "RPC_PEER_USERS", [pwd.getpwuid(os.getuid()).pw_name])
    monkeypatch.setattr(farnsworth.config, "RPC_SOCKET", socket_path)
    monkeypatch.setattr(farnsworth.config, "RPC_GROUP", "pe-t9-nonexistent-group")
    assert farnsworth._start_dashboard_rpc(Mock(), Mock()) is None
    assert len(caplog.records) == 1
    assert "Dashboard RPC not started:" in caplog.text


def test_startup_success_passes_configuration(monkeypatch, socket_path):
    monkeypatch.setattr(farnsworth.config, "RPC_PEER_USERS", [pwd.getpwuid(os.getuid()).pw_name])
    monkeypatch.setattr(farnsworth.config, "RPC_SOCKET", socket_path)
    monkeypatch.setattr(farnsworth.config, "RPC_GROUP", "rpc-group")
    factory = Mock()
    monkeypatch.setattr(farnsworth, "RpcServer", factory)
    assert farnsworth._start_dashboard_rpc(Mock(), Mock()) is factory.return_value
    args = factory.call_args.args
    assert args[0] == socket_path
    assert args[2:] == ({os.getuid()}, "rpc-group")
    factory.return_value.start.assert_called_once_with()


def test_live_socket_is_not_replaced(server, socket_path):
    first = server()
    second = RpcServer(socket_path, {"echo": lambda p: "second"}, allowed_uids={os.getuid()},
                       group=None, read_timeout=0.3)
    with pytest.raises(FileExistsError, match="another RPC server is listening"):
        second.start()
    assert call(socket_path, "echo", {"from": "first"})["result"] == {"from": "first"}
    first.stop()


def test_slow_proposals_cannot_take_the_reserved_decision_worker(server, socket_path):
    entered = threading.Event()
    release = threading.Event()

    def slow_proposal(params):
        entered.set()
        assert release.wait(3)
        return "proposed"

    server({"proposal.create": slow_proposal, "approval.decide": lambda p: "decided"}, workers=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(call, socket_path, "proposal.create")
        try:
            assert entered.wait(1)
            assert call(socket_path, "proposal.create")["error"]["code"] == "busy"
            assert call(socket_path, "approval.decide")["result"] == "decided"
        finally:
            release.set()
        assert first.result()["result"] == "proposed"


def test_idle_overflow_connection_releases_reserved_slot_quickly(server, socket_path):
    entered = threading.Event()
    release = threading.Event()

    def slow(params):
        entered.set()
        assert release.wait(5)

    server({"slow": slow, "approval.decide": lambda p: "decided"}, workers=1, read_timeout=5,
           reserved_read_timeout=0.2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(call, socket_path, "slow")
        try:
            assert entered.wait(1)
            with connect(socket_path) as idle:  # takes the reserved slot, sends nothing
                started = time.monotonic()
                assert_closed(idle)
                assert time.monotonic() - started < 1.5, "reserved slot must use the short deadline"
            deadline = time.monotonic() + 2
            result = None
            while time.monotonic() < deadline:
                result = call(socket_path, "approval.decide")
                if result.get("ok"):
                    break
                time.sleep(0.05)
            assert result["result"] == "decided"
        finally:
            release.set()
        assert first.result()["ok"]


def test_parent_chown_on_start(server, socket_path, monkeypatch):
    chown = Mock(wraps=os.chown)
    monkeypatch.setattr(os, 'chown', chown)
    server(group=os.getgid())
    assert chown.call_args_list == [
        ((socket_path, -1, os.getgid()),),
        ((socket_path.parent, -1, os.getgid()),),
    ]
    assert call(socket_path, 'echo')['ok']


def test_parent_chown_failure_closes_and_unlinks(socket_path, monkeypatch):
    real_chown = os.chown

    def chown(path, uid, gid):
        if path == socket_path.parent:
            raise PermissionError('parent denied')
        real_chown(path, uid, gid)

    monkeypatch.setattr(os, 'chown', chown)
    instance = RpcServer(socket_path, {}, allowed_uids={os.getuid()}, group=os.getgid())
    try:
        with pytest.raises(PermissionError, match='parent denied'):
            instance.start()
        assert instance._socket is None
        assert not socket_path.exists()
        # A real fresh bind proves cleanup leaves the endpoint reusable.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.bind(str(socket_path))
    finally:
        instance.stop()
