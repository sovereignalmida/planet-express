#!/usr/bin/env python3
"""T42 VM rehearsal: `update.canary` against a real registry, real images and real Docker.

Run as casaroot inside the throwaway guest, with casa-planetexpress stopped (this process takes
core's place for the mutation lock), after `bash tests/homelab/t42-canary.sh setup`:

    sudo systemctl stop casa-planetexpress
    CASA_CONFIG=/etc/planetexpress/config.yaml venv/bin/python tests/homelab/t42-canary.py

Cases: `nochange` (already on the tag's image), `update` (:latest moved to a second good image),
`rollback` (:latest moved to a crashing image — the watch fails and the inverse restores the old
one), `prune` (refused while a rollback window is open), `crash` (killed mid-pull, reconciled by
the next core start).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import casa_zoidberg as zoidberg
import config
from notifier import FakeNotifier
from planet_express.application.command_service import CommandService
from planet_express.core.store import Store
from planet_express.execution import engine, runbook as runbooks

STACK, SERVICE, CONTAINER = "canary", "app", "fixture-canary"
SETUP = Path(__file__).resolve().parent / "t42-canary.sh"


class State:  # stands in for PipelineState while core is stopped
    busy_reason = "rehearsal"
    mutation_owner = None

    def try_begin_mutation(self, owner, **_):
        return True

    def end_mutation(self, owner, **_):
        pass


def sh(*args) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()


def point(tag: str) -> None:
    print(f"  {sh('bash', str(SETUP), 'point', tag)}")


def image_of(container: str) -> str:
    return sh("docker", "inspect", "--format", "{{.Image}}", container).removeprefix("sha256:")[:12]


def service():
    store = Store(config.ACTIONS_DB)
    store.init()
    return store, CommandService(store, FakeNotifier(), State())


def open_windows(store) -> list:
    return store.open_rollback_candidates(time.time())


def run_canary(store, svc, label: str) -> dict:
    result = zoidberg.canary_update_service(
        Path(config.STACKS_ROOT) / STACK, SERVICE, tg=None, commands=svc)
    print(f"  {label}: status={result['status']} old={str(result.get('old_id'))[:12]} "
          f"new={str(result.get('new_id'))[:12]}")
    print(f"    reason: {result.get('reason')}")
    print(f"    container now runs {image_of(CONTAINER)}, "
          f"state {sh('docker', 'inspect', '--format', '{{.State.Status}}', CONTAINER)}")
    print(f"    open rollback windows: {len(open_windows(store))}")
    return result


def clear_attempts(store) -> None:
    """Rehearsal setup only: the cases run back to back, inside one T24 cooldown."""
    with store._write() as conn:
        conn.execute("DELETE FROM attempts")


def case_nochange():
    print("== nochange: the tag already points at the running image")
    store, svc = service()
    clear_attempts(store)
    run_canary(store, svc, "canary")


def case_update():
    print("== update: :latest moved to a second good image")
    store, svc = service()
    clear_attempts(store)
    point("good")                      # start from a known image, whatever earlier cases left
    subprocess.run(["docker", "compose", "-f",
                    f"{config.STACKS_ROOT}/{STACK}/docker-compose.yml", "up", "-d",
                    "--pull", "never", SERVICE], capture_output=True, check=False)
    point("good2")
    before = image_of(CONTAINER)
    result = run_canary(store, svc, "canary")
    print(f"    image changed: {before != image_of(CONTAINER)} (was {before})")
    print(f"    history: {result['status']}")


def case_rollback():
    print("== rollback: :latest moved to a crashing image")
    store, svc = service()
    clear_attempts(store)
    point("bad")
    before = image_of(CONTAINER)
    run_canary(store, svc, "canary")
    print(f"    back on the old image: {before == image_of(CONTAINER)}")


def case_ineligible():
    print("== ineligible: a digest-pinned service never becomes a runbook")
    store, svc = service()
    clear_attempts(store)
    compose = Path(config.STACKS_ROOT) / STACK / "docker-compose.yml"
    original = compose.read_text()
    digest = sh("docker", "image", "inspect", "--format", "{{.Id}}",
                "localhost:5000/canary:good").removeprefix("sha256:")
    compose.write_text(original.replace("image: localhost:5000/canary:latest",
                                        f"image: localhost:5000/canary@sha256:{digest}"))
    try:
        before_events = len(store.list_events())
        mark = time.time()
        result = run_canary(store, svc, "canary")
        events = [e for e in store.list_events()[before_events:] if e["kind"] == "canary.ineligible"]
        print(f"    canary.ineligible events: {len(events)} (want 1)")
        with store._connect() as conn:
            recent = conn.execute("SELECT COUNT(*) FROM executions WHERE started_at >= ?",
                                  (mark,)).fetchone()[0]
        print(f"    executions created by this case: {recent} (want 0)")
        assert result["status"] == "skipped"
    finally:
        compose.write_text(original)


def case_holdopen():
    """A failed automatic inverse must leave its window open past the ordinary grace period, or the
    next prune removes the only image that can restore the service (Codex, T42)."""
    print("== holdopen: the inverse fails, so the window is pinned open")
    store, svc = service()
    clear_attempts(store)
    point("good")
    subprocess.run(["docker", "compose", "-f",
                    f"{config.STACKS_ROOT}/{STACK}/docker-compose.yml", "up", "-d",
                    "--pull", "never", SERVICE], capture_output=True, check=False)
    point("bad")
    original = svc._run_argv
    tags = []

    def fail_the_undo(argv, timeout=None):
        if argv[:2] == ["docker", "tag"]:
            tags.append(argv)
            if len(tags) > 1:            # the first tag is the deploy; the second is the undo
                return 1, "", "simulated: the old image is gone"
        return original(argv, timeout=timeout)

    svc._run_argv = fail_the_undo
    result = run_canary(store, svc, "canary")
    windows = open_windows(store)
    print(f"    status: {result['status']} (want rollback_failed)")
    print(f"    open windows: {len(windows)}")
    if windows:
        from planet_express.core.store import INDEFINITE_EXPIRY
        print(f"    held open past the grace period: "
              f"{windows[0]['expires_at'] >= INDEFINITE_EXPIRY}")
        print(f"    prune gate sees it: "
              f"{store.any_open_rollback_candidate(time.time() + 86400)}")
        store.close_rollback_candidate(windows[0]["execution_id"], windows[0]["step_n"])
    point("good")
    subprocess.run(["docker", "compose", "-f",
                    f"{config.STACKS_ROOT}/{STACK}/docker-compose.yml", "up", "-d",
                    "--pull", "never", SERVICE], capture_output=True, check=False)


def case_prune():
    print("== prune: refused while a rollback window is open")
    import casa_farnsworth as fw
    store, svc = service()
    approval, _ = store.propose(action="docker.restart_service", target_key=f"{STACK}/{SERVICE}",
                                target={"stack": STACK, "service": SERVICE, "container": CONTAINER},
                                risk="R1", requested_via="telegram", requested_by="t42")
    execution = store.approve_and_create_execution(approval["id"], decided_by="t42",
                                                   arrived_at=time.time())
    store.create_steps(execution["id"], [{"type": "update.canary",
                                          "params": {"stack": STACK, "service": SERVICE},
                                          "binding": {}}])
    store.open_rollback_candidate(execution["id"], 1, stack=STACK, service=SERVICE,
                                  image_reference="x:latest", old_image_id="a" * 64,
                                  expires_at=time.time() + 900)
    print(f"  gate says an update is in flight: {fw._has_active_rollback_candidates(svc)}")
    ran = []
    svc.run_automatic = lambda *a, **k: ran.append(a)      # must never be reached
    state = fw.PipelineState()
    state.transition(fw.PipelineState.RUNNING)
    fw.maybe_run_safe_prune({"disk": [{"mount": "/", "used_pct": 99, "alert": "critical"}]},
                            FakeNotifier(), state, svc)
    print(f"  prune ran: {bool(ran)} (want False)")
    store.close_rollback_candidate(execution["id"], 1)
    store.set_execution_status(execution["id"], "failed", reason="rehearsal")


def case_crash():
    print("== crash: killed mid-pull, then reconciled")
    store, svc = service()
    clear_attempts(store)
    point("good2")
    approval, _ = store.propose(action="docker.restart_service", target_key=f"{STACK}/{SERVICE}",
                                target={"stack": STACK, "service": SERVICE, "container": CONTAINER},
                                risk="R1", requested_via="telegram", requested_by="t42")
    execution = store.approve_and_create_execution(approval["id"], decided_by="t42",
                                                   arrived_at=time.time())["id"]
    target = svc._resolve(STACK, SERVICE, for_mutation=True)
    plan = runbooks.Runbook.model_validate({
        "title": "Canary update", "artifacts": {},
        "steps": [{"type": "update.canary", "params": {"stack": STACK, "service": SERVICE},
                   "binding": svc._binder.service(target, timeout=30)}]})
    if os.fork() == 0:                      # child: dies right after the pull is dispatched
        runner = engine.RunbookEngine(svc)
        original = svc._run_argv

        def die_after_pull(argv, timeout=None):
            result = original(argv, timeout=timeout)
            if " pull " in " " + " ".join(argv) + " ":
                print("    (child exiting mid-update)")
                os._exit(9)
            return result

        svc._run_argv = die_after_pull
        runner.run(execution, plan, origin="zoidberg")
        os._exit(0)
    os.wait()
    step = store.list_steps(execution)[0]
    print(f"  before reconciliation: {step['status']}, pre_state phase "
          f"{(step['pre_state'] or {}).get('phase')}")
    print(f"  open windows: {len(open_windows(store))}")
    engine.startup_reconcile(store, svc)
    step = store.list_steps(execution)[0]
    print(f"  after reconciliation: {step['status']}/{step['effect']} — {step['reason']}")
    print(f"  open windows: {len(open_windows(store))}")
    store.set_execution_status(execution, "failed", reason="interrupted")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    for name, case in (("nochange", case_nochange), ("update", case_update),
                       ("rollback", case_rollback), ("holdopen", case_holdopen),
                       ("ineligible", case_ineligible),
                       ("prune", case_prune), ("crash", case_crash)):
        if which in (name, "all"):
            case()
