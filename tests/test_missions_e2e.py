"""S3c end to end: a mission created through the S4 service runs on the poll thread (FakeContainment plus a
fake or echo runtime), and the worker socket authenticates against the real session store."""
import json
import os
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
    # The test process stands in for the session: its uid must differ from "Core"'s, and the row's uid is its own.
    with store._write() as conn:
        conn.execute("UPDATE mission_sessions SET session_uid=?", (os.getuid(),))
    auth = WorkerAuthenticator(store, read_cgroup=lambda pid: cgroup, core_uid=os.getuid() + 1)
    server = build_worker_server(sockdir / "worker.sock", sup, auth, group=None, read_timeout=0.5)
    server.start()
    try:
        assert set(server.handlers) == set(WORKER_METHODS)
        ok = call(sockdir / "worker.sock", "mission.get_context", {}, session_token=rt.token)
        assert ok["ok"] and ok["result"]["mission_id"] == mission_id
        assert call(sockdir / "worker.sock", "mission.post_event", {"kind": "note", "data": {"a": 1}},
                    session_token=rt.token)["ok"]
        bad = call(sockdir / "worker.sock", "mission.get_context", {}, session_token="0" * 64)
        assert bad["ok"] is False and bad["error"]["code"] == "unauthorized"
        elsewhere = build_worker_server(
            sockdir / "w2.sock", sup,
            WorkerAuthenticator(store, read_cgroup=lambda pid: "/fake-cgroup/other", core_uid=os.getuid() + 1),
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


def test_disabled_builds_nothing(tmp_path):
    store, _ = make_store(tmp_path)
    assert build_mission_runner(store, MissionsConfig()) is None


def test_enabled_without_an_agent_command_builds_nothing(tmp_path):
    store, _ = make_store(tmp_path)
    assert build_mission_runner(store, MissionsConfig(enabled=True)) is None


def test_enabled_assembles_supervisor_runtime_and_no_global_worker_socket(tmp_path):
    store, _ = make_store(tmp_path)
    runner = build_mission_runner(store, cfg(worker_socket=str(tmp_path / "w.sock")), containment=FakeContainment())
    assert runner is not None and runner.supervisor.enabled
    assert runner.worker_server is None                  # sessions get their own socket in their own directory
    runtime = runner.supervisor.runtime_factory(None)
    assert runtime.argv == ["/usr/bin/agent", "--json"] and runtime.worker_socket == str(tmp_path / "w.sock")
    assert not hasattr(runtime, "worker_uid")            # there is no shared worker account any more


def test_an_injected_containment_is_trusted_and_gets_no_gate_unless_one_is_passed(tmp_path):
    store, _ = make_store(tmp_path)
    runner = build_mission_runner(store, cfg(), containment=FakeContainment())
    assert runner.supervisor.preflight_fn is None and runner.supervisor.socket_provider is None
    gate = lambda: None                                   # noqa: E731 -- stands in for the preflight
    assert build_mission_runner(store, cfg(), containment=FakeContainment(),
                                preflight=gate).supervisor.preflight_fn is gate


def test_the_real_assembly_is_gated_by_the_preflight_and_serves_per_session_sockets(tmp_path, monkeypatch):
    from planet_express.missions import runner as runner_module
    store, _ = make_store(tmp_path)
    monkeypatch.setattr(runner_module, "build_containment", lambda config: FakeContainment())
    runner = build_mission_runner(store, cfg(sock_root=str(tmp_path / "socks")))
    sup = runner.supervisor
    assert callable(sup.preflight_fn) and isinstance(sup.socket_provider, runner_module.SessionSockets)
    assert sup.socket_provider.path("S-1") == str(tmp_path / "socks" / "S-1" / "worker.sock")
    assert runner.worker_server is None


def test_a_containment_that_cannot_be_built_stops_mission_control_without_raising(tmp_path, monkeypatch):
    from planet_express.missions import runner as runner_module
    store, _ = make_store(tmp_path)

    def boom(config):
        raise RuntimeError("no launcher")
    monkeypatch.setattr(runner_module, "build_containment", boom)
    assert start_mission_runner(store, cfg()) is None
    assert start_mission_runner(store, MissionsConfig()) is None


def test_the_strategy_is_chosen_by_who_core_is_and_an_explicit_choice_is_never_downgraded():
    from planet_express.missions.runner import choose_strategy
    assert choose_strategy("auto", 0) == "direct" and choose_strategy("auto", 1000) == "sudo"
    assert choose_strategy("sudo", 0) == "sudo" and choose_strategy("direct", 1000) == "direct"


def test_an_explicit_direct_launcher_for_a_non_root_core_is_refused_not_downgraded():
    from planet_express.missions.containment import ContainmentError
    from planet_express.missions.runner import build_containment
    with pytest.raises(ContainmentError):
        build_containment(cfg(launcher="direct"), euid=1000)


def test_the_supervisor_reads_the_live_enabled_switch(tmp_path):
    store, _ = make_store(tmp_path)
    flag = {"on": True}
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), enabled=lambda: flag["on"])
    assert runner.supervisor.enabled
    flag["on"] = False
    assert not runner.supervisor.enabled
    assert runner.supervisor.workspace_dir == tmp_path / "state" / "mission-workspaces"
