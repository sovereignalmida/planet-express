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
import planet_express.integrations.rpc as rpc_module
from planet_express.application.command_service import DecideResult, ProposeResult
from planet_express.application.config_service import ApplyResult, ValidationResult
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


def test_post_reply_hook_runs_only_after_reply_is_read(server, socket_path):
    activated = threading.Event()
    server({"config.apply": lambda params: rpc_module._PostReply(
        {"status": "activating"}, activated.set
    )})
    response = call(socket_path, "config.apply", {})
    assert response["result"] == {"status": "activating"}
    assert activated.wait(1)


_EXEC_CHILD = r"""
import os, sys, time
from planet_express.integrations.rpc import RpcServer
path, stage = sys.argv[1], sys.argv[2]
if stage == "first":
    server = RpcServer(path, {"echo": lambda p: p}, allowed_uids={os.getuid()}, group=None)
    server.start()
    assert server._socket.get_inheritable() is False
    # Config activation re-execs core in place, without stop(): the listening fd must die
    # with the old image and the replacement must treat the leftover path as stale.
    os.execv(sys.executable, [sys.executable, "-c", sys.argv[3], path, "second", sys.argv[3]])
server = RpcServer(path, {"echo": lambda p: {**p, "pid": os.getpid()}},
                   allowed_uids={os.getuid()}, group=None)
server.start()
print("READY", os.getpid(), flush=True)
time.sleep(30)
"""


def test_rpc_socket_rebinds_after_exec_in_place(socket_path):
    import subprocess
    repo = Path(__file__).resolve().parent.parent
    child = subprocess.Popen(
        [sys.executable, "-c", _EXEC_CHILD, str(socket_path), "first", _EXEC_CHILD],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    try:
        line = child.stdout.readline()
        assert line.startswith("READY"), child.stderr.read()
        assert int(line.split()[1]) == child.pid  # same PID: exec, not a new process
        assert call(socket_path, "echo", {"new": True})["result"] == {"new": True, "pid": child.pid}
    finally:
        child.kill()
        child.wait()


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
    commands.list_incidents.return_value = []
    store.list_pending.return_value = [{
        "id": "a", "action": "docker.restart_service",
        "target_json": '{"stack":"media","service":"sonarr"}', "message_id": 5,
    }]
    handlers = build_core_handlers(commands, store)
    assert set(handlers) == {"logs.tail", "approval.get", "approval.list_recent", "action.request", "query.container", "proposal.create", "proposal.list_pending", "approval.decide", "execution.get_status",
                             "auth.status", "auth.record_failure", "auth.record_success",
                             "auth.consume_totp_step", "auth.device_epoch", "auth.notify_locked",
                             "incident.list", "incident.get", "incident.propose"}
    params = {"action": "docker.restart_service", "stack": "media", "service": "sonarr", "requested_by": "Chris"}
    assert handlers["proposal.create"](params) == asdict(commands.propose.return_value)
    commands.propose.assert_called_once_with(**params, requested_via="dashboard", timeout=4)
    params = {"approval_id": "a", "approve": True, "decided_by": "Chris"}
    assert handlers["approval.decide"](params) == asdict(commands.decide.return_value)
    commands.decide.assert_called_once_with(**params, decision=None)
    assert handlers["proposal.list_pending"]({}) == [{
        "id": "a", "action": "docker.restart_service",
        "target": {"stack": "media", "service": "sonarr"},
        "summary": "Restart media/sonarr",
        "capabilities": {"abortable": False, "rollbackable": False, "resumable": False},
    }]
    assert handlers["execution.get_status"]({"execution_id": "e"}) == commands.get_status.return_value
    commands.get_status.assert_called_once_with("e")
    commands.get_status.return_value = None
    with pytest.raises(RpcError) as error:
        handlers["execution.get_status"]({"execution_id": "missing"})
    assert error.value.code == "not_found"


def test_config_handlers_and_exact_param_validation():
    service = Mock()
    service.get.return_value = {"text": "x", "sha256": "a" * 64, "path": "/config",
                                "sensitive_edits_enabled": False, "editable_fields": [],
                                "sensitive_fields": []}
    service.validate.return_value = ValidationResult(True, [], ["backup_jobs"], [])
    applied = ApplyResult("activating", [], "", ["backup_jobs"], [])
    callback = Mock()
    object.__setattr__(applied, "_post_reply", callback)
    service.apply.return_value = applied
    handlers = build_core_handlers(Mock(), Mock(), config_service=service)

    assert handlers["config.get"]({}) == service.get.return_value
    assert handlers["config.validate"]({"text": "draft"}) == {
        "ok": True, "errors": [], "changed_fields": ["backup_jobs"], "locked_fields": [],
    }
    reply = handlers["config.apply"]({
        "text": "draft", "base_sha256": "a" * 64, "operator": "alice",
    })
    assert reply.result == applied.as_dict() and reply.callback is callback
    service.apply.assert_called_once_with("draft", base_sha256="a" * 64, operator="alice")

    invalid = [
        ("config.get", {"extra": True}),
        ("config.validate", {}),
        ("config.validate", {"text": "x" * (256 * 1024 + 1)}),
        ("config.apply", {"text": "x", "base_sha256": "bad", "operator": "alice"}),
        ("config.apply", {"text": "x", "base_sha256": "a" * 64, "operator": "?"}),
        ("config.apply", {"text": "x", "base_sha256": "a" * 64,
                          "operator": "alice", "extra": True}),
    ]
    for method, params in invalid:
        with pytest.raises(RpcError) as error:
            handlers[method](params)
        assert error.value.code == "bad_request"


def test_incident_handlers_validate_and_route_operator():
    commands, store = Mock(), Mock()
    incident_id = "a" * 12
    commands.list_incidents.return_value = [{"id": incident_id}]
    commands.get_incident_context.return_value = {"id": incident_id, "events": []}
    commands.propose_incident.return_value = ProposeResult(True, "b" * 12, True, "awaiting approval")
    store.get_incident.return_value = {"id": incident_id}
    handlers = build_core_handlers(commands, store)

    assert handlers["incident.list"]({"status": "open", "limit": 20}) == [{"id": incident_id}]
    commands.list_incidents.assert_called_once_with(status="open", limit=20, timeout=4)
    assert handlers["incident.get"]({"incident_id": incident_id})["id"] == incident_id
    assert handlers["incident.propose"]({"incident_id": incident_id, "operator": "alice"})["ok"]
    call = commands.propose_incident.call_args
    assert call.args == (incident_id,)
    assert call.kwargs["operator"] == "alice"
    assert call.kwargs["deadline"] > 0


@pytest.mark.parametrize("method,params", [
    ("incident.list", {"status": "bad", "limit": 20}),
    ("incident.list", {"status": "open", "limit": 101}),
    ("incident.get", {"incident_id": "BAD"}),
    ("incident.propose", {"incident_id": "a" * 12, "operator": "?"}),
])
def test_incident_handlers_reject_bad_params(method, params):
    commands, store = Mock(), Mock()
    handler = build_core_handlers(commands, store)[method]
    with pytest.raises(RpcError) as error:
        handler(params)
    assert error.value.code == "bad_request"


@pytest.mark.parametrize("method,params", [
    ("proposal.create", {}),
    ("proposal.create", {"action": "a", "stack": "", "service": "s", "requested_by": "c"}),
    ("proposal.create", {"action": "a", "stack": "s", "service": "s", "requested_by": "c" * 257}),
    ("proposal.list_pending", {"extra": True}),
    ("approval.decide", {"approval_id": "a", "approve": 1, "decided_by": "c"}),
    ("approval.decide", {"approval_id": "a", "approve": False, "decided_by": " "}),
    ("execution.get_status", {"execution_id": None}),
    ("incident.get", {"incident_id": "A" * 12}),
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
    assert farnsworth._start_dashboard_rpc(Mock(), Mock(), Mock()) is None
    assert [r.message for r in caplog.records] == ["Dashboard RPC not started: socket unavailable"]


def test_startup_no_peer_users(monkeypatch, caplog):
    monkeypatch.setattr(farnsworth.config, "RPC_PEER_USERS", [])
    assert farnsworth._start_dashboard_rpc(Mock(), Mock(), Mock()) is None
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
    # logs.tail was the unimplemented example until T29 built it; keep proving unknown methods fail.
    for method in ("logs.follow", "container.exec"):
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
    assert farnsworth._start_dashboard_rpc(Mock(), Mock(), Mock()) is None
    assert len(caplog.records) == 1
    assert "Dashboard RPC not started:" in caplog.text


def test_startup_success_passes_configuration(monkeypatch, socket_path):
    monkeypatch.setattr(farnsworth.config, "RPC_PEER_USERS", [pwd.getpwuid(os.getuid()).pw_name])
    monkeypatch.setattr(farnsworth.config, "RPC_SOCKET", socket_path)
    monkeypatch.setattr(farnsworth.config, "RPC_GROUP", "rpc-group")
    factory = Mock()
    monkeypatch.setattr(farnsworth, "RpcServer", factory)
    assert farnsworth._start_dashboard_rpc(Mock(), Mock(), Mock()) is factory.return_value
    args = factory.call_args.args
    assert args[0] == socket_path
    assert args[2:] == ({os.getuid()}, "rpc-group")
    factory.return_value.start.assert_called_once_with()


@pytest.mark.parametrize(("value", "enabled"), [
    ("1", True), ("0", False), ("true", False), (" 1", False), (None, False),
])
def test_sensitive_config_switch_only_accepts_exact_one(value, enabled):
    environ = {} if value is None else {"PE_ALLOW_SENSITIVE_CONFIG_EDITS": value}
    assert farnsworth._sensitive_config_edits_enabled(environ) is enabled


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


def test_auth_handler_mapping():
    store = Mock()
    handlers = build_core_handlers(Mock(), store)
    params = {'operator': '?', 'client_ip': '127.0.0.1'}
    store.auth_status.return_value = {'locked': False}
    assert handlers['auth.status'](params) == {'locked': False}
    store.auth_status.assert_called_once_with(**params)
    store.record_auth_failure.return_value = {'just_locked': False}
    assert handlers['auth.record_failure'](params) == {'just_locked': False}
    store.record_auth_failure.assert_called_once_with(**params)
    assert handlers['auth.record_success'](params) == {}
    store.record_auth_success.assert_called_once_with(**params)
    store.consume_totp_step.return_value = True
    assert handlers['auth.consume_totp_step']({'operator': 'alice', 'step': 0}) == {'accepted': True}
    store.consume_totp_step.assert_called_once_with(operator='alice', step=0)
    store.device_epoch.return_value = 3
    assert handlers['auth.device_epoch']({'operator': 'alice'}) == {'epoch': 3}
    store.device_epoch.assert_called_once_with(operator='alice')


@pytest.mark.parametrize('params', [
    {'operator': 'Alice', 'client_ip': 'ip'}, {'operator': '', 'client_ip': 'ip'},
    {'operator': 'a/b', 'client_ip': 'ip'}, {'operator': 'a' * 33, 'client_ip': 'ip'},
    {'operator': 'alice', 'client_ip': ''}, {'operator': 'alice', 'client_ip': 'x' * 65},
    {'operator': 'alice', 'client_ip': 'ip\n'}, {'operator': None, 'client_ip': 'ip'},
])
def test_auth_invalid_params(params):
    store = Mock()
    handlers = build_core_handlers(Mock(), store)
    for method in ('auth.status', 'auth.record_failure', 'auth.record_success', 'auth.notify_locked'):
        with pytest.raises(RpcError) as error:
            handlers[method](params)
        assert error.value.code == 'bad_request'
    assert not store.mock_calls


@pytest.mark.parametrize('step', [True, False, -1, 1.0, '1', None, 2**63])
def test_auth_invalid_step(step):
    with pytest.raises(RpcError) as error:
        build_core_handlers(Mock(), Mock())['auth.consume_totp_step']({'operator': '?', 'step': step})
    assert error.value.code == 'bad_request'


def test_auth_notifications(tmp_path, caplog):
    store = Store(tmp_path / 'data' / 'auth.db', clock=lambda: 1000)
    store.init()
    notifier = Mock()
    handlers = build_core_handlers(Mock(), store, notifier)
    params = {'operator': 'alice', 'client_ip': '<ip>&'}
    assert handlers['auth.notify_locked'](params) == {'sent': False, 'reason': 'not_locked'}
    for _ in range(4):
        result = handlers['auth.record_failure'](params)
    assert result['locked']
    notifier.notify.assert_called_once()
    assert '&lt;ip&gt;&amp;' in notifier.notify.call_args.args[0]
    assert handlers['auth.notify_locked'](params) == {'sent': True}
    assert handlers['auth.notify_locked'](params) == {'sent': False, 'reason': 'already_notified'}
    assert notifier.notify.call_count == 2
    handlers['auth.record_success'](params)
    notifier.notify.side_effect = RuntimeError('SECRET URL')
    for _ in range(3):
        result = handlers['auth.record_failure'](params)
    assert result['just_locked']
    assert 'Failed to send dashboard lock notification' in caplog.text
    assert 'SECRET URL' not in caplog.text


def test_manual_notification_failure():
    store, notifier = Mock(), Mock()
    store.auth_status.return_value = {'locked': True, 'locked_until': 1900}
    store.auth_lock_notified.return_value = False
    notifier.notify.side_effect = RuntimeError('private')
    handlers = build_core_handlers(Mock(), store, notifier)
    assert handlers['auth.notify_locked']({'operator': '?', 'client_ip': 'ip'}) == {'sent': True}


@pytest.mark.parametrize('operator', ['?', 'Chris', 'bad/name', '', ' ', 'a' * 33, None, 'chris\n'])
def test_action_request_rejects_bad_operator(operator):
    commands, store = Mock(), Mock()
    handler = build_core_handlers(commands, store)['action.request']
    with pytest.raises(RpcError) as error:
        handler({'action': 'docker.restart_service', 'stack': 'healthy', 'service': 'web', 'operator': operator})
    assert error.value.code == 'bad_request'
    assert not commands.mock_calls and not store.mock_calls


def test_action_request_exact_params_and_timeout_result():
    from planet_express.application.command_service import RequestResult

    commands = Mock()
    commands.request_action.return_value = RequestResult('timeout', 'host slow, retry', None, None, {})
    handler = build_core_handlers(commands, Mock())['action.request']
    params = {'action': 'docker.restart_service', 'stack': 'healthy', 'service': 'web', 'operator': 'chris'}
    for invalid in ({k: v for k, v in params.items() if k != 'operator'}, params | {'extra': True}):
        with pytest.raises(RpcError) as error:
            handler(invalid)
        assert error.value.code == 'bad_request'
    assert not commands.mock_calls
    assert handler(params) == asdict(commands.request_action.return_value)
    commands.request_action.assert_called_once_with(**params, timeout=4)


def test_stack_action_request_exact_params():
    from planet_express.application.command_service import RequestResult

    commands = Mock()
    commands.request_action.return_value = RequestResult("started", "ok", "a", "e", {})
    handler = build_core_handlers(commands, Mock())["action.request"]
    params = {"action": "compose.up_stack", "stack": "media", "operator": "chris"}
    assert handler(params) == asdict(commands.request_action.return_value)
    commands.request_action.assert_called_once_with(**params, timeout=4)
    for invalid in (params | {"service": "web"}, params | {"extra": True}):
        with pytest.raises(RpcError) as error:
            handler(invalid)
        assert error.value.code == "bad_request"


def test_container_handler_shape_and_timeouts(monkeypatch):
    from planet_express.execution import actions

    target = actions.Target('healthy', 'web', 'fixture')
    resolve = Mock(return_value=target)
    stats = Mock(return_value={'ok': True, 'stats': {'cpu_percent': 1.0}})
    from types import SimpleNamespace

    import planet_express.integrations.rpc as rpc_module
    monkeypatch.setattr(rpc_module, 'time', SimpleNamespace(monotonic=lambda: 50.0))
    monkeypatch.setattr(actions, 'resolve_target', resolve)
    monkeypatch.setattr(actions, 'read_stats', stats)
    monkeypatch.setattr(actions, 'read_facts', Mock(return_value={'ok': True, 'facts': {}}))
    handler = build_core_handlers(Mock(), Mock())['query.container']
    params = {'stack': 'healthy', 'service': 'web'}
    result = handler(params)
    assert result == {'target': target.as_dict(), 'vitals': stats.return_value, 'paused': False,
                      'facts': {'ok': True, 'facts': {}},
                      'actions': {actions.RESTART_SERVICE: actions.REGISTRY[actions.RESTART_SERVICE].capabilities()}}
    # An operator-paused container is usually `exited`, not docker-paused, so the state alone can't
    # say it was intentional: the read reports it (Codex review, T30).
    resolve.assert_called_once_with(**params, for_mutation=False, timeout=4)
    stats.assert_called_once_with('fixture', timeout=4)
    monkeypatch.setattr(rpc_module.config, 'PAUSED_CONTAINERS', ['fixture'])
    assert handler(params)['paused'] is True
    monkeypatch.setattr(rpc_module.config, 'PAUSED_CONTAINERS', [])
    resolve.reset_mock(); stats.reset_mock()
    handler(params)
    resolve.assert_called_once_with(**params, for_mutation=False, timeout=4)
    stats.assert_called_once_with('fixture', timeout=4)
    stats.return_value = {'ok': False, 'error': 'timeout'}
    assert handler(params)['vitals'] == stats.return_value
    for exception, code in ((actions.TargetTimeout('slow'), 'timeout'),
                            (actions.TargetError('healthy/web has no container'), 'not_found')):
        resolve.side_effect = exception
        with pytest.raises(RpcError) as error:
            handler(params)
        assert error.value.code == code
        if code == 'not_found':
            assert str(error.value) == str(exception)
    for invalid in ({'stack': 'healthy'}, params | {'extra': True}):
        with pytest.raises(RpcError) as error:
            handler(invalid)
        assert error.value.code == 'bad_request'


def test_status_handler_returns_action_capabilities(tmp_path):
    from planet_express.application.command_service import CommandService
    from planet_express.execution import actions

    store = Store(tmp_path / 'data' / 'core.db')
    store.init()
    result = store.create_direct_execution(action=actions.RESTART_SERVICE, target_key='healthy/web',
                                           target={'stack': 'healthy', 'service': 'web', 'container': 'fixture'},
                                           risk='R1', operator='chris', arrived_at=time.time())
    commands = CommandService(store, Mock(), Mock())
    response = build_core_handlers(commands, store)['execution.get_status']({'execution_id': result['execution']['id']})
    assert response['capabilities'] == {'abortable': False, 'rollbackable': False, 'resumable': False}


def test_container_stats_get_only_the_remaining_budget(monkeypatch):
    from types import SimpleNamespace

    import planet_express.integrations.rpc as rpc_module
    from planet_express.execution import actions

    now = [10.0]
    target = actions.Target('healthy', 'web', 'fixture')

    def resolve(**kwargs):
        now[0] += spent[0]
        return target

    stats = Mock(return_value={'ok': True, 'stats': {}})
    monkeypatch.setattr(rpc_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(actions, 'resolve_target', resolve)
    monkeypatch.setattr(actions, 'read_stats', stats)
    monkeypatch.setattr(actions, 'read_facts', Mock(return_value={'ok': True, 'facts': {}}))
    handler = build_core_handlers(Mock(), Mock())['query.container']

    spent = [1.5]
    handler({'stack': 'healthy', 'service': 'web'})
    stats.assert_called_once_with('fixture', timeout=2.5)

    stats.reset_mock()
    spent = [4.2]
    result = handler({'stack': 'healthy', 'service': 'web'})
    assert result['vitals'] == {'ok': False, 'error': 'timeout'}
    stats.assert_not_called()


def _chat_handlers():
    chat = Mock()
    chat.ask.return_value = {'ticket_id': 'ticket', 'status': 'queued'}
    chat.get.side_effect = lambda ticket_id, operator: (
        {'id': ticket_id, 'status': 'done'} if operator == 'chris' else None)
    chat.quota.return_value = {'used': 1, 'limit': 100, 'resets_at': 200}
    return build_core_handlers(Mock(), Mock(), chat=chat), chat


def test_chat_rpc_round_trip(server, socket_path):
    handlers, chat = _chat_handlers()
    server(handlers)
    params = {'operator': 'chris', 'question': 'What failed?', 'submission_id': 'abcdefgh'}
    assert call(socket_path, 'chat.ask', params)['result'] == chat.ask.return_value
    chat.ask.assert_called_once_with(**params)
    assert call(socket_path, 'chat.get', {'operator': 'chris', 'ticket_id': 'ticket'})['result']['status'] == 'done'
    assert call(socket_path, 'chat.get', {'operator': 'other', 'ticket_id': 'ticket'})['error']['code'] == 'not_found'
    assert call(socket_path, 'chat.quota')['result'] == chat.quota.return_value


@pytest.mark.parametrize('method,params', [
    ('chat.ask', {'operator': '?', 'question': '?', 'submission_id': 'abcdefgh'}),
    ('chat.ask', {'operator': 'Invalid', 'question': '?', 'submission_id': 'abcdefgh'}),
    ('chat.ask', {'operator': 'chris', 'question': 'x' * 2001, 'submission_id': 'abcdefgh'}),
    ('chat.ask', {'operator': 'chris', 'question': ' ', 'submission_id': 'abcdefgh'}),
    ('chat.ask', {'operator': 'chris', 'question': '?', 'submission_id': 'short'}),
    ('chat.ask', {'operator': 'chris', 'question': '?', 'submission_id': 'x' * 65}),
    ('chat.ask', {'operator': 'chris', 'question': '?', 'submission_id': 'invalid!'}),
    ('chat.ask', {}), ('chat.quota', {'operator': 'chris'}),
    ('chat.get', {'operator': '?', 'ticket_id': 'ticket'}),
    ('chat.get', {'operator': 'chris', 'ticket_id': ''}),
    ('chat.get', {'operator': 'chris', 'ticket_id': 'x' * 65}),
])
def test_chat_rpc_validation(method, params):
    handlers, chat = _chat_handlers()
    with pytest.raises(RpcError) as exc:
        handlers[method](params)
    assert exc.value.code == 'bad_request'
    assert chat.mock_calls == []


def test_chat_rpc_ownership_and_optional_handlers():
    handlers, _ = _chat_handlers()
    with pytest.raises(RpcError) as exc:
        handlers['chat.get']({'operator': 'other', 'ticket_id': 'ticket'})
    assert exc.value.code == 'not_found'
    assert 'chat.ask' not in build_core_handlers(Mock(), Mock())


def test_chat_ask_whitespace_question_is_bad_request_not_internal():
    class Chat:
        def ask(self, **params):
            raise ValueError("Invalid question")

    import planet_express.integrations.rpc as rpc_module

    handlers = rpc_module.build_core_handlers(None, None, chat=Chat())
    with pytest.raises(rpc_module.RpcError) as exc:
        handlers["chat.ask"]({"operator": "chris", "question": "   ", "submission_id": "abcdefgh1"})
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize('method,params', [
    ('logs.tail', {'stack': 's', 'service': 's', 'cursor': 'yesterday'}),
    ('logs.tail', {'stack': 's', 'service': 's', 'cursor': None}),
    ('logs.tail', {'stack': 's', 'service': 's', 'cursor_hashes': ['0' * 16] * 1001}),
    ('logs.tail', {'stack': 's', 'service': 's', 'cursor_hashes': 'bad'}),
    ('logs.tail', {'stack': 's', 'service': 's', 'extra': 1}),
    ('approval.get', {'approval_id': ''}),
    ('approval.get', {'approval_id': 'a', 'extra': 1}),
    *[('approval.list_recent', {'limit': limit}) for limit in (0, 21, True, '1', 1.5)],
    ('approval.list_recent', {'limit': 1, 'extra': 1}),
])
def test_action_reads_validate(method, params):
    with pytest.raises(RpcError) as error:
        build_core_handlers(Mock(), Mock())[method](params)
    assert error.value.code == 'bad_request'


def test_action_reads_round_trip(server, socket_path, tmp_path, monkeypatch):
    from planet_express.application.command_service import CommandService
    from planet_express.execution import actions

    store = Store(tmp_path / 'core.db')
    store.init()
    target = actions.Target('s', 'web', 'c')
    monkeypatch.setattr(actions, 'resolve_target', lambda *a, **k: target)
    ts = '2026-09-17T12:00:00Z'
    monkeypatch.setattr(actions.bender, 'run_argv', lambda argv, **k: (0, ts, ''))
    monkeypatch.setattr(actions.bender, 'run_argv_bounded', lambda argv, **k:
                        (0, f'{ts} API_KEY=secret', '', False))
    created = store.create_direct_execution(action=actions.RESTART_SERVICE, target_key=target.key,
                                             target=target.as_dict(), risk='R1', operator='chris',
                                             arrived_at=time.time())
    server(build_core_handlers(CommandService(store, Mock(), Mock()), store))
    logs = call(socket_path, 'logs.tail', {'stack': 's', 'service': 'web'})['result']
    assert logs['lines'][0]['text'] == 'API_KEY=[REDACTED]' and logs['started_at'] == ts
    approval = call(socket_path, 'approval.get', {'approval_id': created['approval']['id']})['result']
    assert 'message_id' not in approval and 'target_json' not in approval
    assert approval['target'] == target.as_dict()
    assert approval['executions'][0]['id'] == created['execution']['id']
    recent = call(socket_path, 'approval.list_recent', {'limit': 20})['result']
    assert recent[0]['execution'] == approval['executions'][0]
    status = call(socket_path, 'execution.get_status', {'execution_id': created['execution']['id']})['result']
    assert status['approval'] == {k: approval[k] for k in (
        'id', 'action', 'target', 'risk', 'requested_via', 'requested_by', 'decided_by', 'decided_at')}
    assert call(socket_path, 'approval.get', {'approval_id': 'missing'})['error']['code'] == 'not_found'


def test_logs_resolution_errors_and_budget(monkeypatch):
    from planet_express.execution import actions
    handler = build_core_handlers(Mock(), Mock())['logs.tail']
    for exception, code in [(actions.TargetTimeout('secret'), 'timeout'), (actions.TargetError('secret'), 'not_found')]:
        monkeypatch.setattr(actions, 'resolve_target', Mock(side_effect=exception))
        with pytest.raises(RpcError) as error:
            handler({'stack': 's', 'service': 's'})
        assert error.value.code == code and 'secret' not in str(error.value)


def test_container_facts_timeout_spends_shared_budget(monkeypatch):
    from types import SimpleNamespace

    import planet_express.integrations.rpc as rpc_module
    from planet_express.execution import actions

    now = [0]
    monkeypatch.setattr(rpc_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(actions, 'resolve_target', lambda **k: actions.Target('s', 's', 'c'))

    def facts(container, timeout):
        assert timeout == 4
        now[0] += timeout
        return {'ok': False, 'error': 'timeout'}

    monkeypatch.setattr(actions, 'read_facts', facts)
    stats = Mock()
    monkeypatch.setattr(actions, 'read_stats', stats)
    result = build_core_handlers(Mock(), Mock())['query.container']({'stack': 's', 'service': 's'})
    assert result['facts'] == result['vitals'] == {'ok': False, 'error': 'timeout'}
    stats.assert_not_called()


def test_approval_reads_direct_expiry_history_and_status(tmp_path):
    from planet_express.application.command_service import CommandService
    from planet_express.execution import actions

    now = [100]
    store = Store(tmp_path / 'core.db', clock=lambda: now[0])
    store.init()
    target = {'stack': 's', 'service': 'web', 'container': 'c'}
    row, _ = store.propose(action=actions.RESTART_SERVICE, target_key='s/web', target=target,
                           risk='R1', requested_via='telegram', requested_by='chris', ttl_seconds=1)
    store.set_message_id(row['id'], 42)
    handlers = build_core_handlers(CommandService(store, Mock(), Mock()), store)
    now[0] = 102
    approval = handlers['approval.get']({'approval_id': row['id']})
    assert approval['status'] == 'expired' and approval['executions'] == []
    assert approval['target'] == target and 'target_json' not in approval and 'message_id' not in approval
    first = store.create_execution(row['id'])
    now[0] += 1
    latest = store.create_execution(row['id'])
    approval = handlers['approval.get']({'approval_id': row['id']})
    assert [e['id'] for e in approval['executions']] == [latest['id'], first['id']]
    recent = handlers['approval.list_recent']({'limit': 1})
    assert recent[0]['execution'] == approval['executions'][0]
    assert recent[0]['capabilities'] == approval['capabilities']
    status = handlers['execution.get_status']({'execution_id': latest['id']})
    assert status['approval'] == {k: approval[k] for k in (
        'id', 'action', 'target', 'risk', 'requested_via', 'requested_by', 'decided_by', 'decided_at')}
    with pytest.raises(RpcError) as error:
        handlers['approval.get']({'approval_id': 'missing'})
    assert error.value.code == 'not_found'


def test_stack_approval_reads_carry_summary(tmp_path):
    from planet_express.execution import actions

    store = Store(tmp_path / "core.db", clock=lambda: 100)
    store.init()
    row, _ = store.propose(
        action=actions.UP_ALL, target_key="all",
        target={"scope": "all", "stacks": ["network", "media"]}, risk="R2",
        requested_via="telegram", requested_by="chris",
    )
    handlers = build_core_handlers(Mock(), store)
    approval = handlers["approval.get"]({"approval_id": row["id"]})
    assert approval["action"] == actions.UP_ALL
    assert approval["summary"] == "Bring every stack up"
    assert approval["target"] == {"scope": "all", "stacks": ["network", "media"]}


def test_approval_reads_preserve_denial_reason_from_audit_events(tmp_path):
    from planet_express.execution import actions

    store = Store(tmp_path / 'core.db', clock=lambda: 100)
    store.init()
    target = {'stack': 's', 'service': 'web', 'container': 'c'}
    row, _ = store.propose(action=actions.RESTART_SERVICE, target_key='s/web', target=target,
                           risk='R1', requested_via='chat', requested_by='chris', ttl_seconds=60)
    assert store.consume(row['id'], decision='denied', decided_by='policy', arrived_at=100)
    store.record_event('approval.refused_by_policy', approval_id=row['id'],
                       reason='R1 actions are disabled', attempted_by='chris')
    handlers = build_core_handlers(Mock(), store)
    approval = handlers['approval.get']({'approval_id': row['id']})
    assert approval['denial_reason'] == 'refused by current policy: R1 actions are disabled'
    assert handlers['approval.list_recent']({'limit': 1})[0]['denial_reason'] == approval['denial_reason']
    store.record_event('approval.refused_stale_incident', approval_id=row['id'],
                       reason='incident source is no longer current', attempted_by='chris')
    approval = handlers['approval.get']({'approval_id': row['id']})
    assert approval['denial_reason'] == (
        'Incident evidence refused: incident source is no longer current'
    )


def test_approval_reads_database_timeout():
    import sqlite3

    store = Mock()
    store.get_approval.side_effect = sqlite3.OperationalError('secret')
    store.list_recent_approvals.side_effect = sqlite3.OperationalError('secret')
    handlers = build_core_handlers(Mock(), store)
    for method, params in [('approval.get', {'approval_id': 'a'}), ('approval.list_recent', {'limit': 1})]:
        with pytest.raises(RpcError) as error:
            handlers[method](params)
        assert error.value.code == 'timeout' and 'secret' not in str(error.value)


def test_logs_remaining_budget_and_success(monkeypatch):
    from types import SimpleNamespace

    import planet_express.integrations.rpc as rpc_module
    from planet_express.execution import actions

    now = [0]
    spent = [1.5]
    monkeypatch.setattr(rpc_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))

    def resolve(*args, **kwargs):
        assert kwargs == {'for_mutation': False, 'timeout': 4}
        now[0] += spent[0]
        return actions.Target('s', 's', 'c')

    monkeypatch.setattr(actions, 'resolve_target', resolve)
    logs = Mock(return_value={'ok': True, 'lines': []})
    monkeypatch.setattr(actions, 'read_logs', logs)
    handler = build_core_handlers(Mock(), Mock())['logs.tail']
    assert handler({'stack': 's', 'service': 's'}) == logs.return_value
    logs.assert_called_once_with('c', cursor=None, cursor_hashes=[], timeout=2.5)
    logs.reset_mock()
    spent[0] = 4
    assert handler({'stack': 's', 'service': 's'}) == {'ok': False, 'error': 'timeout'}
    logs.assert_not_called()
