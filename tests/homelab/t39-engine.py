#!/usr/bin/env python3
"""T39 VM rehearsal: drive the real runbook engine with real Docker against the fixture stacks.

Run as casaroot inside the throwaway guest, **with casa-planetexpress stopped** (this process takes
the core's place for the mutation lock):

    sudo systemctl stop casa-planetexpress
    CASA_CONFIG=/etc/planetexpress/config.yaml venv/bin/python tests/homelab/t39-engine.py all
    CASA_CONFIG=... venv/bin/python tests/homelab/t39-engine.py kill   # exits mid-run on purpose
    sudo systemctl start casa-planetexpress                            # startup reconciliation

Cases: `multistep` (check → stop → wait → start → check on slow-start/app), `drift` (compose file
edited between approval and execution), `abort` (abort during a wait), `kill` (os._exit right after
step 1 is dispatched, leaving a running execution for the next core start to reconcile).
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from notifier import FakeNotifier
from planet_express.application.command_service import CommandService
from planet_express.core.store import Store
from planet_express.execution import actions, engine, runbook as runbooks


class State:  # stands in for PipelineState: this process is the only mutator while core is stopped
    busy_reason = "rehearsal"

    def try_begin_mutation(self, owner, **_):
        return True

    def end_mutation(self, owner, **_):
        pass


def service():
    store = Store(config.ACTIONS_DB)
    store.init()
    return CommandService(store, FakeNotifier(), State())


def approve(svc, plan, key):
    row, _ = svc._store.propose_runbook(
        action="rehearsal.t39", target_key=key, target={"title": plan.title}, risk="R2",
        requested_via="telegram", requested_by="t39", plan_json=runbooks.canonical_json(plan),
        plan_sha256=runbooks.plan_sha256(plan), origin="telegram",
    )
    return svc._store.approve_and_create_execution(row["id"], decided_by="t39", arrived_at=time.time())["id"]


def plan(svc, title, *steps):
    return runbooks.Runbook.model_validate({"title": title, "steps": list(steps), "artifacts": {}})


def svc_step(svc, kind, stack, name, params=None):
    target = actions.resolve_target(stack, name, for_mutation=kind != "check.container")
    return {"type": kind, "params": params if params is not None else {"stack": stack, "service": name},
            "binding": svc._binder.service(target, timeout=10)}


def report(svc, execution_id, result):
    print(f"  result: {result.status} — {result.reason}")
    for step in svc._store.list_steps(execution_id):
        print(f"   {step['n']}. {step['type']:<22} {step['status']:<10} effect={step['effect']} "
              f"pre={json.dumps(step['pre_state'])} :: {step['reason']}")


def started_at(container):
    import subprocess
    return subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
                          capture_output=True, text=True, check=False).stdout.strip()


def case_multistep(svc):
    print("== multistep: slow-start/app check → stop → wait 3 → start → check healthy")
    before = started_at("fixture-slow-start")
    p = plan(svc, "Bounce slow-start/app",
             svc_step(svc, "check.container", "slow-start", "app", {"expect": "running"}),
             svc_step(svc, "service.stop", "slow-start", "app"),
             {"type": "wait", "params": {"seconds": 3}, "binding": {}},
             svc_step(svc, "service.start", "slow-start", "app"),
             svc_step(svc, "check.container", "slow-start", "app", {"expect": "healthy"}))
    ex = approve(svc, p, "rehearsal:multistep")
    report(svc, ex, engine.RunbookEngine(svc).run(ex, p, origin="telegram"))
    print(f"  StartedAt {before} -> {started_at('fixture-slow-start')}")


def case_drift(svc):
    print("== drift: compose file edited between approval and execution")
    p = plan(svc, "Restart healthy/web", svc_step(svc, "service.restart", "healthy", "web"))
    ex = approve(svc, p, "rehearsal:drift")
    compose = actions.compose_file("healthy")
    original = compose.read_bytes()
    before = started_at("fixture-healthy")
    try:
        compose.write_bytes(original + b"\n# t39 drift\n")
        report(svc, ex, engine.RunbookEngine(svc).run(ex, p, origin="telegram"))
    finally:
        compose.write_bytes(original)
    print(f"  StartedAt unchanged: {before == started_at('fixture-healthy')}")


def case_abort(svc):
    print("== abort: abort requested during a 30s wait; the restart after it must not run")
    p = plan(svc, "Wait then restart healthy/web",
             {"type": "wait", "params": {"seconds": 30}, "binding": {}},
             svc_step(svc, "service.restart", "healthy", "web"))
    ex = approve(svc, p, "rehearsal:abort")
    flag = threading.Event()
    threading.Timer(3, flag.set).start()
    before = started_at("fixture-healthy")
    t0 = time.time()
    report(svc, ex, engine.RunbookEngine(svc, is_abort_requested=flag.is_set).run(ex, p, origin="telegram"))
    print(f"  returned after {time.time() - t0:.1f}s; StartedAt unchanged: {before == started_at('fixture-healthy')}")


def case_kill(svc):
    print("== kill: exit right after step 1 (stop slow-start/app) is dispatched")
    p = plan(svc, "Stop, wait, start slow-start/app",
             svc_step(svc, "service.stop", "slow-start", "app"),
             {"type": "wait", "params": {"seconds": 20}, "binding": {}},
             svc_step(svc, "service.start", "slow-start", "app"))
    ex = approve(svc, p, "rehearsal:kill")
    print(f"  execution {ex}; the next core start must reconcile it")
    real = svc._store.mark_step_dispatched

    def dispatched_then_die(execution_id, n):
        real(execution_id, n)
        if n == 1:
            time.sleep(0.5)
            os._exit(3)

    svc._store.mark_step_dispatched = dispatched_then_die
    engine.RunbookEngine(svc).run(ex, p, origin="telegram")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    svc = service()
    for name, fn in (("multistep", case_multistep), ("drift", case_drift), ("abort", case_abort)):
        if which in (name, "all"):
            fn(svc)
    if which == "kill":
        case_kill(svc)
