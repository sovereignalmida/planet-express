"""S3c end to end: a mission created through the S4 service runs on the poll thread (FakeContainment plus a
fake or echo runtime), and the worker socket authenticates against the real session store."""
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from missions_helpers import make_store
from supervisor_fakes import FakeRuntime, tool_event, update

from config_schema import MissionsConfig
from planet_express.application.mission_service import MissionService
from planet_express.integrations.rpc import call
from planet_express.missions.adapters.command import EchoRuntime
from planet_express.missions.containment import FakeContainment
from planet_express.missions.runner import (
    MissionRunner,
    build_mission_runner,
    start_mission_runner,
)
from planet_express.missions.store import MissionStore
from planet_express.missions.supervisor import Supervisor
from planet_express.missions.worker_rpc import (
    WORKER_METHODS,
    WorkerAuthenticator,
    build_worker_server,
)


def wait_for(predicate, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def rig(tmp_path, factory, containment=None):
    store, clock = make_store(tmp_path)
    cont = containment or FakeContainment()
    sup = Supervisor(store, cont, factory, clock, enabled=lambda: True,
                     artifact_dir=tmp_path / "artifacts", workspace_dir=tmp_path / "ws")
    service = MissionService(store, enabled=True)
    return store, cont, sup, service


def create(service, **kw):
    return service.create_mission(operator="me", title="NAS down", goal="why is the NAS share stale",
                                  scope={"stacks": ["media"], "hosts": ["local"]}, **kw)["id"]


def statuses(store, mission_id):
    return MissionStore(store).get_mission(mission_id).status


def test_service_created_mission_runs_on_the_poll_thread_to_awaiting_decision(tmp_path):
    store, cont, sup, service = rig(tmp_path, lambda mission: EchoRuntime())
    mission_id = create(service)
    runner = MissionRunner(sup, None, interval_s=0.01)
    runner.start()
    try:
        assert wait_for(lambda: statuses(store, mission_id) == "awaiting_decision")
    finally:
        runner.stop()
    ms = MissionStore(store)
    assert ms.live_session(mission_id) is None
    names = {json.loads(e.body_json).get("name") for e in ms.events(mission_id) if e.kind == "artifact"}
    assert {"brief.md", "result.json"} <= names
    sessions = [r for r in cont._groups]                      # the session was cleaned up after it emptied
    assert sessions == []


def test_a_budget_breach_ends_in_needs_operator_with_the_session_provably_empty(tmp_path):
    runtimes = []

    def factory(mission):
        rt = FakeRuntime(cont, script=[update(events=[tool_event()] * 3)] * 5)
        runtimes.append(rt)
        return rt

    cont = FakeContainment()
    store, _, sup, service = rig(tmp_path, factory, containment=cont)
    mission_id = create(service)
    # the service creates default budgets (200 tool calls); tighten via the stored grant instead
    with store._write() as conn:
        row = conn.execute("SELECT grant_json FROM missions WHERE id=?", (mission_id,)).fetchone()
        grant = json.loads(row[0])
        grant.setdefault("budgets", {})["tool_calls"] = 2
        conn.execute("UPDATE missions SET grant_json=? WHERE id=?", (json.dumps(grant), mission_id))
    runner = MissionRunner(sup, None, interval_s=0.01)
    runner.start()
    try:
        assert wait_for(lambda: statuses(store, mission_id) == "needs_operator")
    finally:
        runner.stop()
    sid = runtimes[0].context.session.session_id
    assert cont.is_empty(runtimes[0].context.session) and not cont.exists(sid)
    assert cont.kill_calls(sid) >= 1
    assert MissionStore(store).live_session(mission_id) is None


def test_runner_stop_kills_running_sessions_and_leaves_the_mission_for_recovery(tmp_path):
    cont = FakeContainment()
    runtimes = []

    def factory(mission):
        rt = FakeRuntime(cont, script=[update()] * 1000)
        runtimes.append(rt)
        return rt

    store, _, sup, service = rig(tmp_path, factory, containment=cont)
    mission_id = create(service)
    runner = MissionRunner(sup, None, interval_s=0.01)
    runner.start()
    assert wait_for(lambda: len(runtimes) == 1 and statuses(store, mission_id) == "investigating")
    runner.stop()
    assert cont.is_empty(runtimes[0].context.session)
    assert statuses(store, mission_id) == "investigating"


def test_startup_recovery_restarts_an_interrupted_mission(tmp_path):
    cont = FakeContainment()
    runtimes = []

    def factory(mission):
        rt = FakeRuntime(cont, script=[update(), update("finished")])
        runtimes.append(rt)
        return rt

    store, _, sup, service = rig(tmp_path, factory, containment=cont)
    mission_id = create(service)
    sup.tick()                       # starts it
    assert statuses(store, mission_id) == "investigating"
    sup.shutdown()                   # the daemon stops
    sup2 = Supervisor(store, cont, factory, lambda: 1_000_000, enabled=lambda: True,
                      artifact_dir=tmp_path / "artifacts", workspace_dir=tmp_path / "ws")
    runner = MissionRunner(sup2, None, interval_s=0.01)
    runner.start()                   # recover() then the loop
    try:
        assert wait_for(lambda: statuses(store, mission_id) == "awaiting_decision")
    finally:
        runner.stop()
    assert len(runtimes) == 2


# ── the worker socket against real sessions ─────────────────────────────────

@pytest.fixture
def sockdir():
    directory = Path(tempfile.mkdtemp(prefix="pe3c", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def test_worker_socket_serves_only_the_live_session_of_the_right_cgroup(tmp_path, sockdir):
    cont = FakeContainment()
    runtimes = []

    def factory(mission):
        rt = FakeRuntime(cont, script=[update()] * 50)
        runtimes.append(rt)
        return rt

    store, _, sup, service = rig(tmp_path, factory, containment=cont)
    mission_id = create(service)
    sup.tick()
    rt = runtimes[0]
    cgroup = rt.context.session.cgroup_path
    server = build_worker_server(sockdir / "worker.sock", sup,
                                 WorkerAuthenticator(store, read_cgroup=lambda pid: cgroup),
                                 group=None, read_timeout=0.5)
    server.start()
    try:
        assert set(server.handlers) == set(WORKER_METHODS)
        ok = call(sockdir / "worker.sock", "mission.get_context", {}, session_token=rt.token)
        assert ok["ok"] and ok["result"]["mission_id"] == mission_id
        assert call(sockdir / "worker.sock", "mission.post_event", {"kind": "note", "data": {"a": 1}},
                    session_token=rt.token)["ok"]
        bad = call(sockdir / "worker.sock", "mission.get_context", {}, session_token="0" * 64)
        assert bad["ok"] is False and bad["error"]["code"] == "unauthorized"
        elsewhere = build_worker_server(sockdir / "w2.sock", sup,
                                        WorkerAuthenticator(store, read_cgroup=lambda pid: "/fake-cgroup/other"),
                                        group=None, read_timeout=0.5)
        elsewhere.start()
        try:
            other = call(sockdir / "w2.sock", "mission.get_context", {}, session_token=rt.token)
            assert other["ok"] is False and other["error"]["code"] == "unauthorized"
        finally:
            elsewhere.stop()
        # submitting over the socket ends the mission at the next poll; afterwards the token is dead
        done = call(sockdir / "worker.sock", "mission.submit_result",
                    {"result": {"diagnosis": "stale NFS handle", "confidence": "high"}}, session_token=rt.token)
        assert done["ok"] and done["result"] == {"accepted": True}
        sup.tick()
        assert statuses(store, mission_id) == "awaiting_decision"
        after = call(sockdir / "worker.sock", "mission.get_context", {}, session_token=rt.token)
        assert after["ok"] is False and after["error"]["code"] == "unauthorized"
    finally:
        server.stop()


# ── wiring from configuration ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def cgroup2_mount(tmp_path, monkeypatch):
    import config_schema
    path = tmp_path / "mountinfo"
    path.write_text("25 1 0:22 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n")
    monkeypatch.setattr(config_schema, "MOUNTINFO_PATH", str(path))


def cfg(**over):
    body = {"enabled": True, "runtime": {"argv": ["/usr/bin/agent", "--json"]}}
    body.update(over)
    return MissionsConfig.model_validate(body)


def account(uid=900, gid=900):
    return lambda name: SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_name=name)


def group(gid):
    return SimpleNamespace(gr_name="pe-worker", gr_gid=gid)


def test_disabled_builds_nothing(tmp_path):
    store, _ = make_store(tmp_path)
    assert build_mission_runner(store, MissionsConfig()) is None


def test_enabled_without_an_agent_command_builds_nothing(tmp_path):
    store, _ = make_store(tmp_path)
    assert build_mission_runner(store, MissionsConfig(enabled=True), getpwnam=account()) is None


def test_enabled_assembles_supervisor_runtime_and_a_worker_only_socket(tmp_path):
    store, _ = make_store(tmp_path)
    runner = build_mission_runner(store, cfg(worker_socket=str(tmp_path / "w.sock")), getpwnam=account(),
                                  getgrgid=group, containment=FakeContainment())
    assert runner is not None and runner.supervisor.enabled
    assert set(runner.worker_server.handlers) == set(WORKER_METHODS)
    runtime = runner.supervisor.runtime_factory(None)
    assert runtime.argv == ["/usr/bin/agent", "--json"] and runtime.worker_socket == str(tmp_path / "w.sock")
    assert (runtime.worker_uid, runtime.worker_gid) == (900, 900)


def test_a_root_worker_account_is_refused(tmp_path):
    store, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        build_mission_runner(store, cfg(), getpwnam=account(0, 0), containment=FakeContainment())


def test_start_mission_runner_never_raises(tmp_path):
    store, _ = make_store(tmp_path)

    def nobody(name):
        raise KeyError(name)
    assert start_mission_runner(store, cfg(), getpwnam=nobody) is None
    assert start_mission_runner(store, MissionsConfig()) is None


def test_the_core_user_cannot_be_the_worker(tmp_path):
    import os
    store, _ = make_store(tmp_path)
    with pytest.raises(ValueError):
        build_mission_runner(store, cfg(), getpwnam=account(os.geteuid(), 900), containment=FakeContainment())


def test_the_supervisor_reads_the_live_enabled_switch(tmp_path):
    store, _ = make_store(tmp_path)
    flag = {"on": True}
    runner = build_mission_runner(store, cfg(), getpwnam=account(), getgrgid=group, containment=FakeContainment(),
                                  enabled=lambda: flag["on"])
    assert runner.supervisor.enabled
    flag["on"] = False
    assert not runner.supervisor.enabled
    assert runner.supervisor.workspace_dir == tmp_path / "state" / "mission-workspaces"
