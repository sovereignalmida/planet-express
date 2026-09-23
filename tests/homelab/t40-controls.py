#!/usr/bin/env python3
"""T40 VM rehearsal: abort and rollback against the fixture stacks, through CommandService.

Run as casaroot with casa-planetexpress stopped (this process takes core's place for the lock):

    CASA_CONFIG=/etc/planetexpress/config.yaml venv/bin/python tests/homelab/t40-controls.py

`rollback`: stop slow-start/app through the engine, then roll it back and check the container runs
again (and that a second rollback is refused). `abort`: run [wait 30, restart healthy/web] in a
thread and abort it through CommandService; the restart must never run.
"""

import subprocess
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


class State:
    busy_reason = "rehearsal"
    owner = None

    def try_begin_mutation(self, owner, **_):
        if self.owner is not None:
            return False
        self.owner = owner
        return True

    def end_mutation(self, owner, **_):
        self.owner = None


def status_of(container):
    return subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", container],
                          capture_output=True, text=True, check=False).stdout.strip()


def build(svc, title, *steps):
    return runbooks.Runbook.model_validate({"title": title, "steps": list(steps), "artifacts": {}})


def step(svc, kind, stack, service):
    target = actions.resolve_target(stack, service, for_mutation=True)
    return {"type": kind, "params": {"stack": stack, "service": service},
            "binding": svc._binder.service(target, timeout=10)}


def approve(svc, plan, key):
    row, _ = svc._store.propose_runbook(
        action="rehearsal.t40", target_key=key, target={"title": plan.title}, risk="R2",
        requested_via="telegram", requested_by="t40", plan_json=runbooks.canonical_json(plan),
        plan_sha256=runbooks.plan_sha256(plan), origin="telegram",
    )
    return svc._store.approve_and_create_execution(row["id"], decided_by="t40", arrived_at=time.time())["id"]


def case_rollback(svc):
    print("== rollback: stop slow-start/app, then undo it")
    plan = build(svc, "Stop slow-start/app", step(svc, "service.stop", "slow-start", "app"))
    ex = approve(svc, plan, "rehearsal:t40-rollback")
    result = engine.RunbookEngine(svc).run(ex, plan, origin="telegram")
    svc._store.set_execution_status(ex, result.status, reason=result.reason)
    print(f"  run: {result.status} — {result.reason}; container is {status_of('fixture-slow-start')}")
    status = svc.get_status(ex)
    print(f"  controls: rollbackable={status['capabilities']['rollbackable']} "
          f"abortable={status['capabilities']['abortable']} preview={status['rollback_preview']}")
    control = svc.rollback(ex, operator="t40")
    print(f"  rollback: {control.outcome} — {control.message}")
    for _ in range(180):  # slow-start's healthcheck needs ~60s; verification allows 90s
        child = svc._store.get_execution(control.execution_id) if control.execution_id else None
        if child and child["status"] not in ("running", "verifying"):
            break
        time.sleep(1)
    child = svc._store.get_execution(control.execution_id)
    print(f"  child {child['id']}: {child['status']} — {child['reason']}")
    print(f"  parent is now {svc._store.get_execution(ex)['status']}; "
          f"container is {status_of('fixture-slow-start')}")
    print(f"  second rollback: {svc.rollback(ex, operator='t40').outcome}")


def case_abort(svc):
    print("== abort: [wait 30, restart healthy/web] aborted while waiting")
    plan = build(svc, "Wait then restart healthy/web",
                 {"type": "wait", "params": {"seconds": 30}, "binding": {}},
                 step(svc, "service.restart", "healthy", "web"))
    ex = approve(svc, plan, "rehearsal:t40-abort")
    before = subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", "fixture-healthy"],
                            capture_output=True, text=True, check=False).stdout.strip()
    runner = engine.RunbookEngine(
        svc, is_abort_requested=lambda: bool((svc._store.get_execution(ex) or {}).get("abort_requested_at")))
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(result=runner.run(ex, plan, origin="telegram")))
    thread.start()
    time.sleep(3)
    print(f"  abort while running: {svc.abort(ex, operator='t40').outcome}")
    print(f"  abort again: {svc.abort(ex, operator='t40').outcome}")
    thread.join(60)
    result = outcome["result"]
    svc._store.set_execution_status(ex, result.status, reason=result.reason)
    after = subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", "fixture-healthy"],
                           capture_output=True, text=True, check=False).stdout.strip()
    print(f"  run: {result.status} — {result.reason}")
    print(f"  steps: {[(s['n'], s['type'], s['status']) for s in svc._store.list_steps(ex)]}")
    print(f"  healthy/web untouched: {before == after}")
    print(f"  abort after it finished: {svc.abort(ex, operator='t40').outcome}")


if __name__ == "__main__":
    store = Store(config.ACTIONS_DB)
    store.init()
    service = CommandService(store, FakeNotifier(), State())
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("rollback", "all"):
        case_rollback(service)
    if which in ("abort", "all"):
        case_abort(service)
