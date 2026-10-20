"""Slice 7b checks that need a real host: real Unix-socket ownership and permissions, real SO_PEERCRED and /proc cgroups, and
(where there is one) a real Docker daemon behind the proxy.

They refuse to run unless PE_PRIVILEGED_TESTS_OK=1, which scripts/run_privileged_tests.sh sets only inside a disposable
environment (docker or the MOS VM). Never run them on a developer machine: they chown sockets and run as other uids.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENABLED = os.geteuid() == 0 and os.environ.get("PE_PRIVILEGED_TESTS_OK") == "1"
pytestmark = [pytest.mark.skipif(
    not ENABLED, reason="root-only: run scripts/run_privileged_tests.sh (disposable container or the MOS VM)")]

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)
from dockerproxy_fakes import FakeDaemon, FakeGate, make_scope

from planet_express.missions.dockerproxy.policy import Policy
from planet_express.missions.dockerproxy.server import Limits, ProxyServer
from planet_express.missions.dockerproxy.service import build_read_only
from planet_express.missions.dockerproxy.upstream import (
    DaemonInspector,
    Upstream,
)
from planet_express.missions.worker_rpc import _proc_cgroup

SESSION_UID, OTHER_UID = 61500, 61501
DOCKER_SOCKET = "/var/run/docker.sock"

CLIENT = """
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(5)
try:
    s.connect(sys.argv[1])
except OSError as e:
    print("CONNECT-FAILED", e.errno)
    sys.exit(0)
s.sendall(b"GET /v1.43/_ping HTTP/1.1\\r\\nHost: d\\r\\nX-PE-Session: good\\r\\n\\r\\n")
data = b""
while True:
    chunk = s.recv(4096)
    if not chunk:
        break
    data += chunk
print(data.split(b"\\r\\n")[0].decode())
"""


def as_uid(uid, *args):
    done = subprocess.run([sys.executable, "-I", "-c", CLIENT, *args], user=uid, group=uid, capture_output=True, text=True, check=False,
                          timeout=20)
    return done.stdout.strip() or done.stderr.strip()


class PeerGate(FakeGate):
    def __init__(self):
        super().__init__()
        self.seen = []

    def authenticate(self, peer, token):
        self.seen.append((peer, _proc_cgroup(peer.pid)))
        return super().authenticate(peer, token)


@pytest.fixture
def host():
    base = tempfile.mkdtemp(prefix="dpp")
    os.chmod(base, 0o755)
    session = os.path.join(base, "S-1")
    os.mkdir(session, 0o700)
    daemon = FakeDaemon(os.path.join(base, "d.sock"))
    upstream = Upstream(daemon.path, timeout=5)
    gate = PeerGate()
    server = ProxyServer(os.path.join(session, "proxy.sock"), policy=Policy(make_scope(base), DaemonInspector(upstream)),
                         upstream=upstream, gate=gate, limits=Limits(head_s=3))
    server.start()
    yield base, session, server, gate
    server.stop()
    daemon.close()
    shutil.rmtree(base, ignore_errors=True)


def hand_over(session, uid):
    """What the root launcher helper does after Core bound the socket: the socket to the session, the directory to Core with
    the session's private group, 0750."""
    os.chown(os.path.join(session, "proxy.sock"), uid, uid)
    os.chmod(os.path.join(session, "proxy.sock"), 0o600)
    os.chown(session, 0, uid)
    os.chmod(session, 0o750)


def test_the_socket_core_binds_is_0600_and_only_the_handed_over_uid_can_connect(host):
    base, session, server, gate = host
    path = os.path.join(session, "proxy.sock")
    info = os.stat(path)
    assert oct(info.st_mode & 0o7777) == "0o600" and info.st_uid == 0                 # Core's own: no chown, no 0660 by Core
    hand_over(session, SESSION_UID)
    assert os.stat(path).st_uid == SESSION_UID and oct(os.stat(path).st_mode & 0o7777) == "0o600"
    assert as_uid(SESSION_UID, path).startswith("HTTP/1.1 200")
    assert as_uid(OTHER_UID, path) == "CONNECT-FAILED 13"                              # EACCES: the directory and the socket refuse
    assert as_uid(0, path).startswith("HTTP/1.1")                                      # root is root; the gate is what protects then


def test_the_server_sees_the_real_peer_credentials_and_cgroup(host):
    base, session, server, gate = host
    path = os.path.join(session, "proxy.sock")
    hand_over(session, SESSION_UID)
    as_uid(SESSION_UID, path)
    peer, cgroup = gate.seen[0]
    assert (peer.uid, peer.gid) == (SESSION_UID, SESSION_UID) and peer.pid > 0
    assert cgroup is None or cgroup.startswith("/")                                    # readable where the host exposes cgroup v2


def test_a_connection_without_the_session_token_is_refused_whoever_connects(host):
    from dockerproxy_fakes import request
    base, session, server, gate = host
    path = os.path.join(session, "proxy.sock")
    hand_over(session, SESSION_UID)
    assert request(path, "GET", "/containers/json", token=None)[0] == 401
    assert gate.seen and gate.seen[-1][0].uid == 0


def real_daemon_up():
    if not os.path.exists(DOCKER_SOCKET):
        return False
    try:
        status, _, _ = Upstream(DOCKER_SOCKET, timeout=3).call("GET", "/_ping")
        return status == 200
    except Exception:                                                                   # noqa: BLE001
        return False


@pytest.mark.skipif(not real_daemon_up(), reason="no Docker daemon here")
def test_the_read_only_proxy_in_front_of_a_real_daemon_lists_and_refuses_writes():
    base = tempfile.mkdtemp(prefix="dpr")
    try:
        path = os.path.join(base, "ro.sock")
        ro = build_read_only(path, docker_socket=DOCKER_SOCKET, allowed_uids={0})
        ro.start()
        try:
            from dockerproxy_fakes import request
            status, _, body = request(path, "GET", "/containers/json?all=1", token=None)
            assert status == 200 and isinstance(json.loads(body), list)
            assert request(path, "GET", "/_ping", token=None, version=None)[0] == 200
            assert request(path, "POST", "/containers/create", token=None, body={"Image": "x"})[0] == 403
            assert request(path, "GET", "/info", token=None)[0] == 403
        finally:
            ro.stop()
    finally:
        shutil.rmtree(base, ignore_errors=True)
