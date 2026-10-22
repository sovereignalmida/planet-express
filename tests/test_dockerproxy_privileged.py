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


def test_a_connection_the_gate_does_not_recognise_is_refused_whoever_connects(host):
    request = versioned_request()
    base, session, server, gate = host
    path = os.path.join(session, "proxy.sock")
    hand_over(session, SESSION_UID)
    assert request(path, "GET", "/containers/json", token="not-the-token")[0] == 401
    assert gate.seen and gate.seen[-1][0].uid == 0


def real_daemon_up():
    if not os.path.exists(DOCKER_SOCKET):
        return False
    try:
        status, _, _ = Upstream(DOCKER_SOCKET, timeout=3).call("GET", "/_ping")
        return status == 200
    except Exception:                                                                   # noqa: BLE001
        return False


def versioned_request():
    """`dockerproxy_fakes.request` speaking an API version this daemon serves (newer engines refuse v1.41/v1.43)."""
    import http.client
    import socket

    from dockerproxy_fakes import request as raw

    class _Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(DOCKER_SOCKET)

    try:
        conn = _Conn("docker", timeout=5)
        conn.request("GET", "/_ping")
        daemon = conn.getresponse().getheader("Api-Version", "1.43")
        conn.close()
        major, minor = (int(x) for x in daemon.split("."))
    except (OSError, ValueError):
        major, minor = 1, 43
    version = "v1.%d" % min(minor, 48)
    return lambda *a, **k: raw(*a, **{"version": version, **k}) if "version" not in k else raw(*a, **k)


@pytest.mark.skipif(not real_daemon_up(), reason="no Docker daemon here")
def test_the_read_only_proxy_in_front_of_a_real_daemon_lists_and_refuses_writes():
    base = tempfile.mkdtemp(prefix="dpr")
    try:
        path = os.path.join(base, "ro.sock")
        ro = build_read_only(path, docker_socket=DOCKER_SOCKET, allowed_uids={0})
        ro.start()
        try:
            request = versioned_request()
            status, _, body = request(path, "GET", "/containers/json?all=1", token=None)
            assert status == 200 and isinstance(json.loads(body), list)
            assert request(path, "GET", "/_ping", token=None, version=None)[0] == 200
            assert request(path, "POST", "/containers/create", token=None, body={"Image": "x"})[0] == 403
            assert request(path, "GET", "/info", token=None)[0] == 403
        finally:
            ro.stop()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def local_image():
    try:
        status, _, body = Upstream(DOCKER_SOCKET, timeout=5).call("GET", "/images/json")
        tags = [t for img in json.loads(body) for t in (img.get("RepoTags") or []) if not t.startswith("<")]
        return tags[0] if status == 200 and tags else None
    except Exception:                                                                   # noqa: BLE001
        return None


@pytest.mark.skipif(not real_daemon_up() or local_image() is None, reason="no Docker daemon with a local image here")
def test_a_real_create_start_stop_and_remove_through_the_proxy_and_the_refusals_in_front_of_a_real_daemon():
    from dockerproxy_fakes import FakeGate
    request = versioned_request()
    base = tempfile.mkdtemp(prefix="dpc")
    os.chmod(base, 0o755)
    up = Upstream(DOCKER_SOCKET, timeout=30)
    gate = FakeGate()
    path = os.path.join(base, "p.sock")
    scope = make_scope(base, resources=("host:local/stack:pe7btest",), paths=(), protected=(), protected_gids=frozenset({0}))
    server = ProxyServer(path, policy=Policy(scope, DaemonInspector(up)), upstream=up, gate=gate, limits=Limits(head_s=5, body_s=30))
    server.start()
    made = None
    labels = {"com.docker.compose.project": "pe7btest", "com.docker.compose.service": "probe"}
    try:
        image = local_image()
        body = {"Image": image, "Cmd": ["sleep", "30"], "Labels": labels, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        status, _, answer = request(path, "POST", "/containers/create?name=pe7b-probe", body=body)
        assert status == 201, answer
        made = json.loads(answer)["Id"]
        assert request(path, "POST", f"/containers/{made}/start")[0] == 204
        status, _, text = request(path, "GET", f"/containers/{made}/json")
        assert status == 200 and json.loads(text)["State"]["Running"] is True
        assert request(path, "POST", f"/containers/{made}/exec", body={"Cmd": ["id"]})[0] == 201          # an ordinary one is fine
        assert request(path, "POST", f"/containers/{made}/stop?t=1")[0] == 204
        bad = {**body, "HostConfig": {"Privileged": True}}
        assert request(path, "POST", "/containers/create?name=pe7b-priv", body=bad)[0] == 403
        assert request(path, "DELETE", f"/containers/{made}?force=1")[0] == 204
        made = None
        events = [e for e in gate.events if e.get("event") == "docker.write"]
        routes = [e["route"] for e in events]
        assert [r for r in routes if r in ("container.create", "container.start", "container.stop")][:3] == ["container.create", "container.start", "container.stop"]
    finally:
        if made:
            up.call("DELETE", f"/containers/{made}?force=1")
        server.stop()
        shutil.rmtree(base, ignore_errors=True)
