"""A whole Repair stack over fakes (slice 7a): the tripwire fake host, a real checkpoint service and Verifier, a Supervisor with
the controls wired, and the approval flow. Nothing here touches the host."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor_fakes import GOOD_RESULT, FakeRuntime, update
from tripwire_fakes import make_rig
from verify_fakes import FakeRun, check_env, snapshot

from planet_express.missions.approval_flow import ApprovalFlow
from planet_express.missions.launcher.preflight import Check, PreflightReport
from planet_express.missions.supervisor import Supervisor
from planet_express.missions.verify.baseline import normalize
from planet_express.missions.verify.runner import Verifier

CHECKS = [{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}]
SCOPE = {"stacks": ["media"], "hosts": ["local"]}
__all__ = ["GOOD_RESULT", "build", "gate_report", "update"]


def gate_report(ok: bool = True) -> PreflightReport:
    report = PreflightReport("side-doors")
    report.checks.append(Check("docker-tcp-exposure", ok, "" if ok else "unauthenticated Docker TCP: tcp://0.0.0.0:2375"))
    return report


class FakeProxy:
    """Stands in for slice 7b's per-session Docker proxy: closed by a tripwire pause, reopened by a resume."""

    def __init__(self):
        self.open: dict[str, str | None] = {}

    def pause(self, mission_id):
        self.open[mission_id] = None

    def resume(self, mission_id, grant_hash):
        self.open[mission_id] = grant_hash

    def usable(self, mission_id, grant_hash):
        return self.open.get(mission_id) == grant_hash


class Stack:
    pass


def build(tmp_path, *, behaviours=None, gate=True, start_monitor=True, with_proxy=False, covered=True, grant_over=None):
    """Returns a Stack. `stack.propose()` makes an approved-able Repair mission through the real flow; `stack.run()` drives the
    supervisor until nothing more happens. Stop the monitor with `stack.close()`."""
    rig = make_rig(tmp_path, monitor_kw={"interval_s": 3600, "investigating_interval_s": 3600}, grant_over=grant_over)
    env = rig.env
    st = Stack()
    st.rig, st.env, st.ms, st.store, st.clock, st.cont, st.monitor = rig, env, env.ms, env.store, env.clock, rig.cont, rig.monitor
    st.base = snapshot()
    st.fresh = snapshot()
    env.svc.baseline_provider = lambda: normalize(st.base)
    st.run_log = FakeRun()
    st.verifier = Verifier(missions=env.ms, checkpoint=env.svc, scan=lambda: st.fresh, env=check_env(st.run_log),
                           stacks_root=str(env.stacks), enabled=True, scan_timeout_s=5)
    st.behaviours = list(behaviours or [{"script": [update("finished")]}])
    st.runtimes = []
    st.proxy = FakeProxy() if with_proxy else None
    if st.proxy is not None:
        original = rig.monitor.revoke_endpoints
        rig.monitor.revoke_endpoints = lambda mid: (original(mid), st.proxy.pause(mid))

    def factory(mission):
        runtime = FakeRuntime(st.cont, **(st.behaviours.pop(0) if st.behaviours else {}))
        st.runtimes.append(runtime)
        return runtime

    st.sup = Supervisor(env.store, st.cont, factory, env.clock, enabled=True, artifact_dir=tmp_path / "artifacts",
                        workspace_dir=tmp_path / "ws", repair_gate=(lambda: gate_report(gate)), verifier=st.verifier,
                        checkpoint=env.svc, tripwire=rig.monitor, docker_proxy=st.proxy)
    st.flow = ApprovalFlow(env.store, supervisor=st.sup, validator=lambda grant, plan: [],
                           backup_status=lambda grant: {"covered": covered, "state": "ok" if covered else "stale"},
                           protected=lambda: rig.protected, runtime="fake")
    if start_monitor:
        rig.monitor.start()

    def propose(**kw):
        spec = {"title": "restart media", "goal": "bring media back", "scope": SCOPE, "checks": CHECKS}
        spec.update(kw)
        return st.flow.propose_repair("operator:me", **spec)

    def approve_all(mission_id, elevated=False):
        card = st.flow.card(mission_id)
        for item in card["required"]:
            if item["state"] == "pending":
                st.flow.approve("operator:me", mission_id, item["phase"], item["bound"], elevated=elevated)

    def status(mission_id):
        return env.ms.get_mission(mission_id).status

    def drive(mission_id, ticks=6):
        for _ in range(ticks):
            st.sup.tick()
            future = st.sup._verifying.get(mission_id)
            if future is not None:
                future.result(10)
        return status(mission_id)

    def close():
        rig.monitor.stop()
        st.sup._shutting_down = True
        if st.sup._verify_pool is not None:
            st.sup._verify_pool.shutdown(wait=True)

    st.propose, st.approve_all, st.status, st.drive, st.close = propose, approve_all, status, drive, close
    return st
