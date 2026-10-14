"""Per-session worker sockets (contract 22.8.1), Core's half of the cgroup tree, and the runner's prepare hook."""
import os
from pathlib import Path

import pytest
from supervisor_fakes import Rig, update

from planet_express.missions.containment import ContainedSession
from planet_express.missions.launcher.cgroup_tree import ensure_tree
from planet_express.missions.runner import MissionRunner, SessionSockets
from planet_express.missions.supervisor import SocketProvider


class Provider(SocketProvider):
    def __init__(self, fail=False):
        self.started, self.stopped, self.fail = [], [], fail

    def path(self, session_id):
        return f"/run/socks/{session_id}/worker.sock"

    def start(self, cs):
        if self.fail:
            raise OSError("cannot bind")
        self.started.append(cs)
        return lambda: self.stopped.append(cs.session_id)


def rig_with(tmp_path, provider, **kw):
    rig = Rig(tmp_path, **kw)
    rig.sup.socket_provider = provider
    return rig


def test_the_session_is_told_its_own_socket_and_the_launcher_is_asked_for_the_directory(tmp_path):
    provider = Provider()
    rig = rig_with(tmp_path, provider, behaviours=[{"script": [update()] * 3}])
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    spawn = rig.cont.spawns(sid)[0]
    assert spawn["env"]["PE_WORKER_SOCKET"] == f"/run/socks/{sid}/worker.sock" and spawn["sock_dir"] is True
    assert [cs.session_id for cs in provider.started] == [sid] and provider.started[0].session_uid is not None


def test_without_a_provider_no_socket_directory_is_requested(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update()]}])
    rig.sup.start(rig.mid)
    assert rig.cont.spawns(rig.session_ids()[0])[0]["sock_dir"] is False


def test_the_socket_is_stopped_when_the_session_is_reaped_not_before(tmp_path):
    provider = Provider()
    rig = rig_with(tmp_path, provider, behaviours=[{"script": [update(), update("finished")]}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert provider.stopped == []                           # still running
    rig.sup.poll(rig.mid)
    assert provider.stopped == provider.stopped[:1] and len(provider.stopped) == 1
    assert rig.status() == "awaiting_decision"


def test_the_socket_is_stopped_at_shutdown(tmp_path):
    provider = Provider()
    rig = rig_with(tmp_path, provider, behaviours=[{"script": [update()] * 10}])
    rig.sup.start(rig.mid)
    rig.sup.shutdown()
    assert len(provider.stopped) == 1


def test_a_socket_that_cannot_bind_fails_the_start_before_anything_is_launched(tmp_path):
    rig = rig_with(tmp_path, Provider(fail=True), behaviours=[{"script": [update()]}])
    rig.sup.start(rig.mid)
    sid = rig.session_ids()[0]
    assert rig.status() == "needs_operator" and rig.ms.live_session(rig.mid) is None
    assert not rig.cont.exists(sid) and rig.cont.spawns(sid) == []          # the launcher was never asked
    assert rig.ms.get_session(sid)["uid_released_at"] is not None           # and the uid went back


def test_a_socket_that_will_not_stop_never_blocks_the_reap(tmp_path):
    provider = Provider()

    def bad_stop_start(cs):
        return lambda: (_ for _ in ()).throw(OSError("stuck"))
    provider.start = bad_stop_start
    rig = rig_with(tmp_path, provider, behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    rig.sup.poll(rig.mid)
    assert rig.status() == "awaiting_decision"


# ── SessionSockets: one RpcServer per session, in the session's own directory ──────────────────

def test_session_sockets_make_the_directory_bind_a_server_with_no_group_and_return_its_stop(tmp_path):
    built = []

    class Server:
        def __init__(self, path, backend, auth, group):
            built.append((path, backend, auth, group))
            self.started = self.stopped = False
            self.dir_existed = os.path.isdir(os.path.dirname(path))

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    servers = []

    def build(*a, **kw):
        servers.append(Server(*a, **kw))
        return servers[-1]
    sockets = SessionSockets(str(tmp_path), "backend", "auth", build=build)
    assert sockets.path("S-1") == f"{tmp_path}/S-1/worker.sock"
    stop = sockets.start(ContainedSession("S-1", "local_cgroup", "/cg/S-1", 61005))
    # Core cannot chgrp to a uid it is not, so no group: the LAUNCHER chowns the socket to the session afterwards
    assert built == [(f"{tmp_path}/S-1/worker.sock", "backend", "auth", None)]
    assert servers[0].dir_existed and servers[0].started and oct((tmp_path / "S-1").stat().st_mode & 0o777) == "0o700"
    stop()
    assert servers[0].stopped


def test_a_server_that_cannot_start_leaves_no_directory_behind(tmp_path):
    class Broken:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise OSError("address in use")
    sockets = SessionSockets(str(tmp_path), "b", "a", build=lambda *a, **kw: Broken())
    with pytest.raises(OSError):
        sockets.start(ContainedSession("S-1", "local_cgroup", "/cg/S-1", 61005))
    assert not (tmp_path / "S-1").exists()


def test_the_socket_is_bound_before_the_launch_so_the_agents_first_connect_cannot_miss_it(tmp_path):
    order = []

    class Ordered(Provider):
        def start(self, cs):
            order.append("socket")
            return super().start(cs)
    rig = rig_with(tmp_path, Ordered(), behaviours=[{"script": [update()]}])
    real_launch = rig.cont.launch

    def launch(session_id, spec):
        order.append("launch")
        return real_launch(session_id, spec)
    rig.cont.launch = launch
    rig.sup.start(rig.mid)
    assert order == ["socket", "launch"]


def test_a_launch_that_fails_still_stops_the_socket_that_was_already_bound(tmp_path):
    provider = Provider()
    rig = rig_with(tmp_path, provider, behaviours=[{"script": [update()]}])
    rig.cont.fail_launch = OSError("the helper refused")
    rig.sup.start(rig.mid)
    assert len(provider.started) == 1 and len(provider.stopped) == 1
    assert rig.status() == "needs_operator"


# ── Core's half of the cgroup tree ─────────────────────────────────────────────

def tree(tmp_path, procs=""):
    delegated = tmp_path / "svc"
    (delegated / "sessions").mkdir(parents=True)
    (delegated / "cgroup.subtree_control").write_text("")
    return delegated


def test_ensure_tree_makes_core_and_sessions_moves_core_then_enables_pids(tmp_path):
    delegated = tmp_path / "svc"
    delegated.mkdir()
    (delegated / "cgroup.subtree_control").write_text("")
    (delegated / "core").mkdir()                     # a real cgroupfs makes cgroup.procs itself on mkdir
    (delegated / "core" / "cgroup.procs").write_text("")
    done = ensure_tree(str(delegated / "sessions"), pid=4242)
    assert done[0] == f"created {delegated / 'sessions'}"
    # the kernel refuses `+pids` in a cgroup that still holds processes, so the move must come first
    assert done.index(f"moved 4242 into {delegated / 'core'}") < done.index("enabled pids on the delegated cgroup")
    assert (delegated / "core" / "cgroup.procs").read_text() == "4242"
    assert (delegated / "cgroup.subtree_control").read_text() == "+pids"


def test_ensure_tree_is_idempotent(tmp_path):
    delegated = tree(tmp_path)
    (delegated / "core").mkdir()
    (delegated / "core" / "cgroup.procs").write_text("77\n")
    (delegated / "cgroup.subtree_control").write_text("pids\n")
    assert ensure_tree(str(delegated / "sessions"), pid=77) == []


def test_ensure_tree_reports_a_cgroup_it_cannot_write(tmp_path):
    with pytest.raises(OSError):
        ensure_tree(str(tmp_path / "missing" / "sessions"))


# ── the runner prepares the tree, then lets the preflight judge it ───────────────────

def test_the_runner_prepares_the_host_before_recovering_and_survives_a_failed_preparation(tmp_path):
    calls = []
    rig = Rig(tmp_path)
    runner = MissionRunner(rig.sup, None, interval_s=0.01, prepare=lambda: calls.append("prepare") or "did things")
    runner.start()
    runner.stop()
    assert calls == ["prepare"]

    def boom():
        raise OSError("read-only cgroupfs")
    runner = MissionRunner(Rig(tmp_path / "again").sup, None, interval_s=0.01, prepare=boom)
    runner.start()                                           # logged, not raised: the preflight will fail the host
    runner.stop()


def test_nothing_in_the_launcher_package_reads_the_environment_or_a_shell():
    root = Path(__file__).resolve().parents[1] / "planet_express/missions/launcher"
    for name in ("spec.py", "strategies.py", "preflight.py", "cgroup_tree.py", "config.py"):
        text = (root / name).read_text()
        for forbidden in ("shell", "os.system", "os.popen", "preexec_fn"):
            assert forbidden + "=" not in text and forbidden + "(" not in text, (name, forbidden)
    assert "PE_LAUNCH_CONF" not in (root / "pe_launch.py").read_text()      # the config path is a constant, not an input
