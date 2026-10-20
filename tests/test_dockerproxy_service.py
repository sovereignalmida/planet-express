"""The proxy's lifecycle over the real store: a socket per Repair session, the session gate, pause and the resume acceptance
test, and the daemon wiring. A fake Docker daemon stands behind it; nothing here needs root."""
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)
from dockerproxy_fakes import FakeDaemon, request
from launcher_support import good_conf, load_helper
from repair_fakes import build, update
from supervisor_fakes import Rig
from tripwire_fakes import touch

from planet_express.integrations.rpc import PeerCred
from planet_express.missions import leases
from planet_express.missions.dockerproxy.server import Limits
from planet_express.missions.dockerproxy.service import (
    DockerProxyService,
    ResumeRefused,
)
from planet_express.missions.dockerproxy.upstream import Upstream
from planet_express.missions.runner import SessionSockets
from planet_express.missions.supervisor import (
    ResumeRefused as SupervisorResumeRefused,
)
from planet_express.missions.worker_rpc import WorkerAuthenticator


class StubWorker:
    """Stands in for the worker RpcServer: binds a real socket named worker.sock, as Core does."""

    def __init__(self, path):
        self.path, self.sock = path, None

    def start(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)

    def stop(self):
        if self.sock is not None:
            self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Claimed:
    """An authenticator whose peer says whatever the test needs it to: the cgroup and uid the kernel would have reported.
    Everything else is the real WorkerAuthenticator over the real store."""

    def __init__(self, store):
        self.cgroup, self.uid = None, None
        self.inner = WorkerAuthenticator(store, read_cgroup=lambda pid: self.cgroup, core_uid=-1)

    def __call__(self, peer, req):
        return self.inner(PeerCred(peer.pid, self.uid if self.uid is not None else peer.uid, peer.gid), req)


class Stack:
    pass


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, behaviours=[{"script": [update()] * 12}], with_proxy=True, **kw)
        st.dir = tempfile.mkdtemp(prefix="dps")
        st.daemon = FakeDaemon(os.path.join(st.dir, "d.sock"))
        st.auth = Claimed(st.store)
        st.svc = DockerProxyService(st.store, authenticator=st.auth, docker_socket=st.daemon.path, sock_root=st.dir,
                                    stacks_root=str(st.env.stacks), protected=lambda: st.rig.protected.resources,
                                    protected_gids=(998,), limits=Limits(head_s=2, body_s=3, stream_s=3, hijack_s=5),
                                    upstream=Upstream(st.daemon.path, timeout=5))
        st.proxy = st.svc
        st.sup.docker_proxy = st.svc
        st.sup.socket_provider = SessionSockets(st.dir, st.sup, st.auth, build=lambda path, *a, **k: StubWorker(path), proxy=st.svc)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()
        st.svc.shutdown()
        st.daemon.close()
        shutil.rmtree(st.dir, ignore_errors=True)


def sqlite_rows(st, mid):
    with sqlite3.connect(st.store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM mission_sessions WHERE mission_id=?", (mid,))]


def go(st):
    """Propose, approve, start: a live Repair session with a proxy socket. Returns (mission id, session id, token, socket path)."""
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    sid = st.runtimes[0].context.session.session_id
    spawn = st.cont.spawns(sid)[0]
    st.auth.cgroup, st.auth.uid = st.cont.cgroup_path(sid), spawn["uid"]
    return mid, sid, spawn["token"], os.path.join(st.dir, sid, "proxy.sock"), spawn


def restart(path, token, target="/containers/qbittorrent/restart"):
    return request(path, "POST", target, token=token)


# ── the socket per session ──────────────────────────────────────────────────────────────────────────

def test_a_repair_session_gets_its_own_proxy_socket_and_its_environment_names_only_that_one(stack):
    st = stack()
    mid, sid, token, path, spawn = go(st)
    assert os.path.exists(path) and os.path.exists(os.path.join(st.dir, sid, "worker.sock"))
    assert spawn["env"]["PE_DOCKER_SOCKET"] == path and "DOCKER_HOST" not in spawn["env"]      # stock clients cannot send the token
    assert spawn["sock_dir"] is True and token not in json.dumps(spawn["env"])
    assert restart(path, token)[0] == 204


def test_an_observe_session_gets_no_proxy_and_no_docker_environment(tmp_path):
    rig = Rig(tmp_path, tier="observe", behaviours=[{"script": [update()] * 3}])
    d = tempfile.mkdtemp(prefix="dps")
    try:
        svc = DockerProxyService(rig.store, authenticator=Claimed(rig.store), sock_root=d)
        rig.sup.socket_provider = SessionSockets(d, rig.sup, None, build=lambda path, *a, **k: StubWorker(path), proxy=svc)
        rig.sup.start(rig.mid)
        sid = rig.session_ids()[0]
        assert not svc.wants(sid) and not os.path.exists(os.path.join(d, sid, "proxy.sock"))
        assert "DOCKER_HOST" not in rig.cont.spawns(sid)[0]["env"] and os.path.exists(os.path.join(d, sid, "worker.sock"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_socket_is_removed_when_the_session_ends(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    assert os.path.exists(path)
    st.sup.shutdown()
    assert not os.path.exists(path) and not os.path.exists(os.path.join(st.dir, sid, "worker.sock"))


def test_a_proxy_that_cannot_bind_fails_the_start_and_leaves_no_worker_socket_or_directory(stack, monkeypatch):
    st = stack()

    def boom(self, cs):
        raise OSError("cannot bind")
    monkeypatch.setattr(DockerProxyService, "start", boom)
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    sid = [r["id"] for r in sqlite_rows(st, mid)][0]
    assert st.status(mid) == "needs_operator" and st.cont.spawns(sid) == []          # the launcher was never asked
    assert [p for p in os.listdir(st.dir) if p != "d.sock"] == []


def test_the_helper_adopts_exactly_this_pair_of_sockets_and_hands_them_over_0600(stack):
    helper = load_helper()
    st = stack()
    mid, sid, token, path, _ = go(st)
    conf = good_conf(Path(st.dir), sock_root=st.dir)
    out = helper.adopt_sock_dir(conf, sid, os.geteuid(), os.getegid())
    assert out == os.path.join(st.dir, sid)
    for name in ("worker.sock", "proxy.sock"):
        assert oct(os.stat(os.path.join(out, name)).st_mode & 0o777) == "0o600"
    with open(os.path.join(out, "evil.sock"), "w"):
        pass
    with pytest.raises(helper.Refused, match="socket dir contents"):
        helper.adopt_sock_dir(conf, sid, os.geteuid(), os.getegid())          # any other name is still refused


# ── who may use it ───────────────────────────────────────────────────────────────────────────────

def test_a_same_uid_process_in_another_session_cannot_use_this_socket(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    st.auth.cgroup = "/fake-cgroup/another-session"                        # same uid, same token, a different cgroup
    assert restart(path, token)[0] == 401
    st.auth.cgroup = st.cont.cgroup_path(sid)
    st.auth.uid = st.auth.uid + 1                                          # same cgroup and token, another uid
    assert restart(path, token)[0] == 401
    st.auth.uid = st.cont.spawns(sid)[0]["uid"]
    assert restart(path, token)[0] == 204 and len(st.daemon.writes()) == 1


def test_another_sessions_token_is_refused_on_this_sessions_socket(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    from planet_express.missions.sessions import new_token
    st.ms.revoke_session(sid)
    assert restart(path, token)[0] == 401                                   # revoked: refused at authentication
    assert restart(path, new_token())[0] == 401                             # never issued
    assert st.daemon.writes() == []


def test_the_token_is_required_even_with_the_right_peer(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    assert request(path, "POST", "/containers/qbittorrent/restart", token=None)[0] == 401
    assert request(path, "GET", "/containers/json", token="x" * 64)[0] == 401
    assert st.daemon.requests == []


# ── what a write must prove ─────────────────────────────────────────────────────────────────────────

def events_of(st, mid):
    return [json.loads(e.body_json) for e in st.ms.events(mid) if e.actor == "docker-proxy"]


def test_a_write_is_recorded_in_the_mission_log_before_it_reaches_the_daemon(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    seen = []
    original = st.daemon._route

    def spy(conn, rec):
        if rec["method"] == "POST":
            seen.append([b["event"] for b in events_of(st, mid)])
        return original(conn, rec)
    st.daemon._route = spy
    assert restart(path, token)[0] == 204
    assert seen == [["docker.write"]]
    body = events_of(st, mid)[0]
    assert body["session"] == sid and body["route"] == "container.restart" and body["targets"] == ["host:local/service:media/qbittorrent"]


def test_a_stale_grant_a_missing_approval_and_a_stale_fence_each_refuse_the_write(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    assert restart(path, token)[0] == 204
    with sqlite3.connect(st.store.path) as conn:
        saved = conn.execute("SELECT * FROM mission_approvals WHERE mission_id=?", (mid,)).fetchall()
        conn.execute("DELETE FROM mission_approvals WHERE mission_id=?", (mid,))
    status, _, body = restart(path, token)
    assert status == 403 and b"approval" in body
    with sqlite3.connect(st.store.path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mission_approvals)")]
        for row in saved:
            conn.execute(f"INSERT INTO mission_approvals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(row))
    assert restart(path, token)[0] == 204
    leases.release(st.store, mid)                                          # the lease is gone: the session's fences are stale
    status, _, body = restart(path, token)
    assert status == 403 and b"fence" in body
    assert len(st.daemon.writes()) == 2
    assert request(path, "GET", "/containers/json", token=token)[0] == 200  # reads need no write authority


def test_a_grant_that_changed_since_the_session_started_refuses_writes(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    with sqlite3.connect(st.store.path) as conn:
        conn.execute("UPDATE missions SET grant_hash=? WHERE id=?", ("0" * 64, mid))
    status, _, body = restart(path, token)
    assert status == 403 and st.daemon.writes() == [] and (b"grant" in body or b"approval" in body)


def test_scope_comes_from_the_approved_grant_not_from_the_request(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    for target in ("/containers/traefik/restart", "/containers/stray/restart", "/containers/plex/restart"):
        status = restart(path, token, target)[0]
        assert status == (204 if "plex" in target else 403), target
    assert [w["target"].split("/")[3] for w in st.daemon.writes()] == ["b" * 64]


# ── pause and resume ────────────────────────────────────────────────────────────────────────────────

def test_resume_acceptance_a_tripwire_pause_closes_the_proxy_and_a_resume_reopens_it_before_thawing(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    assert restart(path, token)[0] == 204
    touch(st.rig.fs / "etc" / "hosts", "10.0.0.1 changed\n")
    st.monitor.scan_once()
    assert st.status(mid) == "paused_tripwire" and st.svc.is_paused(mid)
    for method, target in (("GET", "/containers/json"), ("POST", "/containers/qbittorrent/restart"), ("GET", "/_ping")):
        assert request(path, method, target, token=token)[0] == 503             # the socket is kept; every request is refused
    assert os.path.exists(path) and len(st.daemon.writes()) == 1
    st.flow.resume("operator:alice", mid)
    assert st.status(mid) == "executing" and not st.svc.is_paused(mid)
    assert not st.cont._groups[sid].frozen
    assert restart(path, token)[0] == 204 and len(st.daemon.writes()) == 2      # the same socket, re-authorised


def test_a_resume_the_approval_no_longer_covers_leaves_the_proxy_closed(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    with sqlite3.connect(st.store.path) as conn:
        conn.execute("DELETE FROM mission_approvals WHERE mission_id=?", (mid,))
    with pytest.raises(SupervisorResumeRefused):
        st.sup.resume(mid, "operator:alice")
    assert st.svc.is_paused(mid) and request(path, "GET", "/containers/json", token=token)[0] == 503


def test_resume_itself_refuses_a_changed_grant_and_a_missing_session(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    st.svc.pause(mid)
    with pytest.raises(ResumeRefused, match="grant changed"):
        st.svc.resume(mid, "1" * 64)
    st.ms.revoke_session(sid)
    with pytest.raises(ResumeRefused, match="no live session"):
        st.svc.resume(mid, st.ms.get_mission(mid).grant_hash)
    assert st.svc.is_paused(mid)


def test_a_paused_session_stays_paused_across_a_failed_resume_and_a_fresh_session_starts_open(stack):
    st = stack()
    mid, sid, token, path, _ = go(st)
    st.svc.pause(mid)
    assert request(path, "GET", "/containers/json", token=token)[0] == 503
    st.svc.pause("other-mission")
    assert st.svc.is_paused("other-mission") and request(path, "GET", "/containers/json", token=token)[0] == 503


# ── missions off, wiring ───────────────────────────────────────────────────────────────────────────

def test_with_missions_disabled_nothing_starts_not_even_the_proxy_service(tmp_path):
    from config_schema import MissionsConfig
    from planet_express.core.store import Store
    from planet_express.missions.runner import (
        build_mission_runner,
        start_mission_runner,
    )
    store = Store(str(tmp_path / "s.db"))
    cfg = MissionsConfig(enabled=False)
    assert build_mission_runner(store, cfg) is None and start_mission_runner(store, cfg) is None


def test_the_config_names_the_docker_socket_and_the_worker_sudo_ceiling_and_refuses_wild_rules():
    from pydantic import ValidationError

    from config_schema import MissionsConfig
    cfg = MissionsConfig(worker_sudo_rules=[{"unit": "plex.service", "actions": ["restart"]}])
    assert cfg.docker_socket == "/var/run/docker.sock" and cfg.worker_sudo_rules[0].unit == "plex.service"
    for bad in ({"unit": "*.service", "actions": ["restart"]}, {"unit": "a.service", "actions": ["reload"]},
                {"unit": "a.service", "actions": []}, {"unit": "a.service", "actions": ["restart"], "x": 1}):
        with pytest.raises(ValidationError):
            MissionsConfig(worker_sudo_rules=[bad])


def test_the_assembly_marks_exactly_the_generators_output_as_managed_and_a_grant_must_sit_inside_the_ceiling():
    from config_schema import MissionsConfig
    from planet_express.missions.assembly import managed_sudoers
    from planet_express.missions.sudo_gen import render
    cfg = MissionsConfig(worker_sudo_rules=[{"unit": "plex.service", "actions": ["restart", "start"]}])
    managed = managed_sudoers(cfg, "planetexpress")
    assert managed["/etc/sudoers.d/planetexpress-worker"] == render([("plex.service", ("restart", "start"))], uid_min=61000, uid_max=61999)
    assert b"planetexpress ALL=(root) NOPASSWD:" in managed["/etc/sudoers.d/planetexpress-launch"]
    assert "/etc/sudoers.d/planetexpress-worker" not in managed_sudoers(MissionsConfig(), "planetexpress")


def test_a_valid_session_that_is_not_this_sockets_session_is_refused_by_the_gate(stack):
    from types import SimpleNamespace

    from planet_express.missions.dockerproxy.service import StoreGate
    st = stack()
    theirs = SimpleNamespace(id="S-someone-else", fences={})
    gate = StoreGate(st.svc, "S-mine", "M-1", "0" * 64, lambda peer, req: theirs)       # authenticates, but as another session
    assert gate.authenticate(PeerCred(1, 2, 3), "t" * 40) is None
    mine = SimpleNamespace(id="S-mine", fences={})
    assert StoreGate(st.svc, "S-mine", "M-1", "0" * 64, lambda peer, req: mine).authenticate(PeerCred(1, 2, 3), "t" * 40) is mine
    assert gate.authenticate(PeerCred(1, 2, 3), None) is None and gate.authenticate(PeerCred(1, 2, 3), 5) is None


def test_the_daemon_wiring_builds_the_proxy_before_the_controls_so_a_tripwire_pause_reaches_it(tmp_path, monkeypatch):
    """Review finding: the service must exist when `build_repair_controls` captures the revoke callback."""
    from config_schema import MissionsConfig

    from planet_express.core.store import Store
    from planet_express.missions import assembly
    from planet_express.missions.runner import build_mission_runner
    seen = {}

    def spy(store, cfg, wiring, **kw):
        seen["proxy"] = kw.get("proxy")
        raise RuntimeError("stop here")
    monkeypatch.setattr(assembly, "build_repair_controls", spy)
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    store = Store(str(tmp_path / "s.db"))
    cfg = MissionsConfig(enabled=True, runtime={"argv": ["/bin/true"]}, sock_root=str(tmp_path / "socks"),
                         workspace_root=str(tmp_path / "ws"))
    runner = build_mission_runner(store, cfg, stacks_root=str(stacks), repair_wiring=object())
    assert isinstance(seen["proxy"], DockerProxyService)
    assert runner.supervisor.docker_proxy is seen["proxy"] and runner.supervisor.socket_provider.proxy is seen["proxy"]
