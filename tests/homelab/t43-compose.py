#!/usr/bin/env python3
"""T43 VM rehearsal: `compose.write` against real files and real Docker.

Run as casaroot inside the throwaway guest, with casa-planetexpress stopped (this process takes
core's place for the mutation lock):

    sudo systemctl stop casa-planetexpress
    CASA_CONFIG=/etc/planetexpress/config.yaml venv/bin/python tests/homelab/t43-compose.py

Cases: `install` (a brand-new LAN-only stack: one approval, write then up), `edit` (change an
existing stack's image and bring it up), `drift` (the file edited between approval and execution),
`undo` (roll back an edit, and roll back a created stack), `crash` (killed between the write and
the `stack.up`, reconciled by the next core start).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from notifier import FakeNotifier
from planet_express.application import compose_plans
from planet_express.application.command_service import CommandService
from planet_express.core.store import Store
from planet_express.execution import compose_files as cf, engine, runbook as runbooks

STACK = "rehearsal"
CONTENT = """services:
  app:
    image: busybox:1.36
    container_name: fixture-rehearsal
    restart: unless-stopped
    command: ["sh", "-c", "while true; do sleep 5; done"]
    labels:
      - traefik.http.routers.rehearsal-lan.rule=Host(`rehearsal.homelab.test`)
"""
CHANGED = CONTENT.replace("sleep 5", "sleep 7")


class State:
    busy_reason = "rehearsal"
    mutation_owner = None

    def try_begin_mutation(self, owner, **_):
        return True

    def end_mutation(self, owner, **_):
        pass


def service():
    store = Store(config.ACTIONS_DB)
    store.init()
    with store._write() as conn:
        # Rehearsal setup only: the cases run back to back, inside one T24 cooldown on `stack.up`.
        conn.execute("DELETE FROM attempts")
    return store, CommandService(store, FakeNotifier(), State())


def sh(*args) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()


def approve_and_run(store, svc, runbook, *, origin="install"):
    row, _created = store.propose_runbook(
        action="runbook", target_key=f"plan:{runbooks.plan_sha256(runbook)[:16]}",
        target={"title": runbook.title}, risk=runbooks.risk(runbook), requested_via=origin,
        requested_by="t43", plan_json=runbooks.canonical_json(runbook),
        plan_sha256=runbooks.plan_sha256(runbook), origin=origin,
    )
    execution = store.approve_and_create_execution(row["id"], decided_by="t43",
                                                   arrived_at=time.time())["id"]
    stored = svc._verified_plan(store.get_approval(row["id"]))
    result = engine.RunbookEngine(svc).run(execution, stored, origin=origin)
    store.set_execution_status(execution, result.status, reason=result.reason)
    print(f"  run: {result.status} — {result.reason}")
    print(f"  steps: {[(s['n'], s['type'], s['status']) for s in store.list_steps(execution)]}")
    return execution, result


def compose_path() -> Path:
    return cf.compose_path_for(config.STACKS_ROOT, STACK)


def cleanup():
    path = compose_path()
    if path.parent.exists():
        subprocess.run(["docker", "compose", "-f", str(path), "down"],
                       capture_output=True, check=False)
        for item in path.parent.iterdir():
            item.unlink()
        path.parent.rmdir()


def case_install():
    print("== install: a brand-new LAN-only stack, one approval")
    cleanup()
    store, svc = service()
    runbook = compose_plans.install_runbook(STACK, CONTENT, domain="rehearsal.homelab.test")
    print(f"  steps: {[s.type for s in runbook.steps]}")
    approve_and_run(store, svc, runbook)
    print(f"  file: {compose_path().exists()}, mode "
          f"{oct(compose_path().stat().st_mode & 0o777) if compose_path().exists() else '-'}")
    print(f"  container: {sh('docker', 'inspect', '--format', '{{.State.Status}}', 'fixture-rehearsal')}")


def case_edit():
    print("== edit: change the image and bring it up again")
    store, svc = service()
    current = compose_path().read_text()
    runbook = compose_plans.edit_runbook(STACK, CHANGED, current_content=current, restart=["app"])
    approve_and_run(store, svc, runbook, origin="amy")
    print(f"  file now matches the proposal: {compose_path().read_text() == CHANGED}")
    backups = sorted(p.name for p in compose_path().parent.glob("*.bak.*"))
    print(f"  backups kept: {backups}")


def case_drift():
    print("== drift: the file is edited between approval and execution")
    store, svc = service()
    current = compose_path().read_text()
    runbook = compose_plans.edit_runbook(STACK, CHANGED + "# drift case\n",
                                         current_content=current)
    compose_path().write_text(current + "# a human got here first\n")
    _execution, result = approve_and_run(store, svc, runbook, origin="amy")
    print(f"  refused without writing: {'changed since' in (result.reason or '')}")
    print(f"  the human's line is still there: "
          f"{'a human got here first' in compose_path().read_text()}")
    compose_path().write_text(current)


def case_undo():
    print("== undo: roll back an edit, then roll back a created stack")
    store, svc = service()
    current = compose_path().read_text()
    proposed = current + "# undo case\n"
    runbook = compose_plans.edit_runbook(STACK, proposed, current_content=current)
    execution, _result = approve_and_run(store, svc, runbook, origin="amy")
    plan = engine.plan_rollback(svc, "undo", store.list_steps(execution))
    print(f"  rollback steps: {[s.type for s in plan.runbook.steps]} "
          f"(not reversible: {plan.not_reversible})")
    _child, _result = approve_and_run(store, svc, plan.runbook, origin="telegram")
    print(f"  file restored: {compose_path().read_text() == current}")

    print("  -- and a created stack")
    cleanup()
    store, svc = service()
    runbook = compose_plans.install_runbook(STACK, CONTENT, domain="rehearsal.homelab.test")
    execution, _result = approve_and_run(store, svc, runbook)
    plan = engine.plan_rollback(svc, "undo", store.list_steps(execution))
    approve_and_run(store, svc, plan.runbook, origin="telegram")
    print(f"  file gone: {not compose_path().exists()}; "
          f"directory gone: {not compose_path().parent.exists()}")


def case_crash():
    print("== crash: killed between the write and the stack.up")
    cleanup()
    store, svc = service()
    runbook = compose_plans.install_runbook(STACK, CONTENT, domain="rehearsal.homelab.test")
    row, _created = store.propose_runbook(
        action="runbook", target_key="plan:t43-crash", target={"title": runbook.title},
        risk=runbooks.risk(runbook), requested_via="install", requested_by="t43",
        plan_json=runbooks.canonical_json(runbook), plan_sha256=runbooks.plan_sha256(runbook),
        origin="install")
    execution = store.approve_and_create_execution(row["id"], decided_by="t43",
                                                   arrived_at=time.time())["id"]
    if os.fork() == 0:
        original = svc._run_argv

        def die_before_up(argv, timeout=None):
            if "up" in argv:
                print("    (child exiting before the stack comes up)")
                os._exit(9)
            return original(argv, timeout=timeout)

        svc._run_argv = die_before_up
        engine.RunbookEngine(svc).run(execution, runbook, origin="install")
        os._exit(0)
    os.wait()
    print(f"  file written: {compose_path().exists()}")
    print(f"  steps before: {[(s['n'], s['type'], s['status']) for s in store.list_steps(execution)]}")
    engine.startup_reconcile(store, svc)
    rows = store.list_steps(execution)
    print(f"  steps after: {[(s['n'], s['type'], s['status'], s['effect']) for s in rows]}")
    preview = engine.rollback_preview(rows)
    print(f"  rollback would undo steps {preview['undo']} "
          f"(unknown {preview['unknown']}, not reversible {preview['not_reversible']})")
    store.set_execution_status(execution, "interrupted", reason="rehearsal")
    cleanup()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    for name, case in (("install", case_install), ("edit", case_edit), ("drift", case_drift),
                       ("undo", case_undo), ("crash", case_crash)):
        if which in (name, "all"):
            case()
