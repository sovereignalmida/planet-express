"""planet_express/execution/engine.py (slice 5b-1, T39): the runbook engine's step state machine,
drift checks, references, abort, failure handling and startup reconciliation. Real Store; every
host call is faked and recorded."""

import os
import sys
from itertools import pairwise
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_bender as bender
from planet_express.application.command_service import CommandService
from planet_express.core.store import Store
from planet_express.execution import actions, engine, runbook as runbooks
from tests.binding_fakes import FakeBinder


class Reading:
    def __init__(self, status="running", health="healthy", error=None, restart_count=0):
        self.status, self.health, self.error, self.restart_count = status, health, error, restart_count


class Svc:
    _stack_report = staticmethod(CommandService._stack_report)

    def __init__(self, tmp_path):
        self._store = Store(tmp_path / "db.sqlite", clock=lambda: 1_000_000.0)
        self._store.init()
        self._binder = FakeBinder()
        self.argv = []
        self.argv_result = (0, "", "")
        self.running = {"media-sonarr-1": True}
        self.verify_result = (True, "healthy for 15s")
        self._clock = lambda: 1_000_000.0

    def _run_argv(self, argv, timeout):
        self.argv.append(argv)
        if argv[-2:-1] == ["stop"]:
            self.running[f"media-{argv[-1]}-1"] = False
        if argv[-2:-1] == ["start"]:
            self.running[f"media-{argv[-1]}-1"] = True
        return self.argv_result

    def _resolve(self, stack, service, for_mutation=True, timeout=None):
        return actions.Target(stack, service, f"{stack}-{service}-1")

    def _read_health(self, container):
        return Reading("running" if self.running.get(container, True) else "exited")

    def _verify(self, container, baseline):
        return self.verify_result

    def _restart_count(self, container):
        return 0

    def _resolve_for_action(self, action, stack, service, *, timeout, approved_target=None):
        if action in actions.ALL_STACK_ACTIONS:
            return actions.AllStacksTarget(tuple(approved_target["stacks"]))
        return actions.StackTarget(stack)

    def _verify_stack_up(self, stack):
        return (True, "up")

    def _verify_stack_down(self, stack):
        return (True, "down")


def service_step(kind="service.restart", service="sonarr", binder=None):
    target = actions.Target("media", service, f"media-{service}-1")
    return {"type": kind, "params": {"stack": "media", "service": service},
            "binding": (binder or FakeBinder()).service(target, timeout=4)}


def container_step(kind, params, service="sonarr"):
    return {"type": kind, "params": params, "binding": service_step(service=service)["binding"]}


def runbook(*steps):
    return runbooks.Runbook.model_validate({"title": "t", "steps": list(steps), "artifacts": {}})


def execution(svc):
    approval, _ = svc._store.propose(
        action="docker.restart_service", target_key="media/sonarr",
        target={"stack": "media", "service": "sonarr", "container": "media-sonarr-1"},
        risk="R1", requested_via="telegram", requested_by="t",
    )
    return svc._store.approve_and_create_execution(approval["id"], decided_by="t", arrived_at=1)["id"]


def steps(svc, execution_id):
    return [(s["type"], s["status"], s["effect"]) for s in svc._store.list_steps(execution_id)]


def attempts(svc, execution_id):
    with svc._store._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT step_n, state FROM attempts WHERE execution_id=? ORDER BY step_n", (execution_id,)
        )]


@pytest.fixture
def svc(tmp_path):
    return Svc(tmp_path)


def test_multi_step_happy_path_records_pre_state_effects_and_attempts(svc):
    ex = execution(svc)
    rb = runbook(
        container_step("check.container", {"expect": "running"}),
        service_step("service.stop"),
        {"type": "wait", "params": {"seconds": 1}, "binding": {}},
        service_step("service.start"),
    )
    result = engine.RunbookEngine(svc, sleep=lambda s: None, monotonic=iter(range(100)).__next__).run(
        ex, rb, origin="telegram")
    assert result.status == "passed"
    assert steps(svc, ex) == [
        ("check.container", "passed", "not_applied"),
        ("service.stop", "passed", "applied"),
        ("wait", "passed", "not_applied"),
        ("service.start", "passed", "applied"),
    ]
    stored = svc._store.list_steps(ex)
    assert stored[1]["pre_state"] == {"running": True}
    assert stored[3]["pre_state"] == {"running": False}
    assert attempts(svc, ex) == [(2, "consumed"), (4, "consumed")]
    assert all(argv[:2] == ["docker", "compose"] for argv in svc.argv)  # argv, never a shell string


def test_start_of_an_already_running_service_is_not_applied(svc):
    ex = execution(svc)
    result = engine.RunbookEngine(svc).run(ex, runbook(service_step("service.start")), origin="telegram")
    assert result.status == "passed"
    assert steps(svc, ex) == [("service.start", "passed", "not_applied")]


@pytest.mark.parametrize("drift, reason", [
    (lambda b: setattr(b, "compose_sha", "b" * 64), "compose file changed since approval"),
    (lambda b: b.container_ids.update({"media-sonarr-1": "fedcba987654"}), "was recreated since approval"),
])
def test_drift_refuses_the_step_before_any_argv_and_skips_the_rest(svc, drift, reason):
    ex = execution(svc)
    rb = runbook(service_step("service.restart"), service_step("service.restart", service="radarr"))
    drift(svc._binder)
    failures = []
    result = engine.RunbookEngine(svc, on_failure=failures.append).run(ex, rb, origin="incident")
    assert result.status == "failed" and reason in result.reason
    assert svc.argv == []
    assert steps(svc, ex) == [("service.restart", "failed", "not_applied"),
                              ("service.restart", "skipped", None)]
    assert attempts(svc, ex) == [(1, "released"), (2, "released")]
    assert len(failures) == 1 and failures[0]["failed_step"] == 1


def test_failed_command_stops_run_with_unknown_effect_and_redacted_reason(svc):
    ex = execution(svc)
    svc.argv_result = (1, "", "boom API_KEY=hunter2secret")
    rb = runbook(service_step("service.restart"), {"type": "wait", "params": {"seconds": 1}, "binding": {}})
    result = engine.RunbookEngine(svc).run(ex, rb, origin="telegram")
    assert result.status == "failed"
    stored = svc._store.list_steps(ex)
    assert (stored[0]["status"], stored[0]["effect"]) == ("failed", "unknown")
    assert "hunter2secret" not in stored[0]["reason"]
    assert stored[1]["status"] == "skipped"
    assert attempts(svc, ex) == [(1, "consumed")]


def test_on_failure_hook_errors_never_change_the_outcome(svc):
    ex = execution(svc)
    svc.verify_result = (False, "healthcheck failing")

    def broken(context):
        raise RuntimeError("amy is down")

    result = engine.RunbookEngine(svc, on_failure=broken).run(ex, runbook(service_step()), origin="telegram")
    assert result.status == "failed" and result.reason == "healthcheck failing"


def test_abort_between_steps_and_during_wait(svc):
    ex = execution(svc)
    flag = {"abort": False}
    rb = runbook(service_step("service.restart"), service_step("service.restart", service="radarr"))

    def restart_then_abort(argv, timeout):
        flag["abort"] = True
        return (0, "", "")

    svc._run_argv = restart_then_abort
    result = engine.RunbookEngine(svc, is_abort_requested=lambda: flag["abort"]).run(ex, rb, origin="telegram")
    assert result.status == "aborted"
    assert steps(svc, ex) == [("service.restart", "passed", "applied"), ("service.restart", "aborted", None)]
    assert attempts(svc, ex) == [(1, "consumed"), (2, "released")]

    ex2 = execution_for(svc, "media/radarr")
    ticks = iter([0, 0, 0.5, 1.0, 1.5])
    calls = {"n": 0}

    def abort_on_second_check():
        calls["n"] += 1
        return calls["n"] >= 3

    rb2 = runbook({"type": "wait", "params": {"seconds": 60}, "binding": {}}, service_step())
    result = engine.RunbookEngine(svc, is_abort_requested=abort_on_second_check,
                                  sleep=lambda s: None, monotonic=lambda: next(ticks)).run(ex2, rb2, origin="telegram")
    assert result.status == "aborted"
    assert steps(svc, ex2) == [("wait", "failed", "not_applied"), ("service.restart", "aborted", None)]


def execution_for(svc, key):
    approval, _ = svc._store.propose(
        action="docker.restart_service", target_key=key,
        target={"stack": "media", "service": key.split("/")[1], "container": key.replace("/", "-") + "-1"},
        risk="R1", requested_via="telegram", requested_by="t",
    )
    return svc._store.approve_and_create_execution(approval["id"], decided_by="t", arrived_at=1)["id"]


def test_reference_substitution_uses_the_persisted_output(svc, monkeypatch):
    calls = []

    def fake_run_argv(argv, timeout):
        calls.append(argv)
        if argv[:2] == ["docker", "exec"]:
            return (0, "Session\\Port=51413\n", "")
        if argv[:3] == ["docker", "inspect", "--format"]:
            return (0, "2026-09-22T10:00:00.123456789Z\n", "")
        return (1, "", "unexpected")

    monkeypatch.setattr(bender, "run_argv", fake_run_argv)
    monkeypatch.setattr(bender, "run_argv_bounded",
                        lambda argv, timeout, max_bytes: (0, "x New : 51413 y\n", "", False))
    ex = execution(svc)
    rb = runbook(
        container_step("read.qbittorrent_session_port", {}, service="qbit"),
        container_step("check.log_since_start",
                       {"match": "New : ", "port": {"from_step": 1, "output": "port"}}, service="gsp"),
    )
    result = engine.RunbookEngine(svc).run(ex, rb, origin="planner")
    assert result.status == "passed", result.reason
    assert svc._store.list_steps(ex)[0]["output"] == {"port": 51413}
    exec_argv = next(a for a in calls if a[:2] == ["docker", "exec"])
    assert exec_argv[-3:] == ["1", "^Session\\\\Port=", engine.QBIT_CONFIG]  # one line, never the file


def test_missing_reference_output_fails_the_consumer(svc, monkeypatch):
    monkeypatch.setattr(bender, "run_argv", lambda argv, timeout: (1, "", "no such file"))
    ex = execution(svc)
    rb = runbook(
        container_step("read.qbittorrent_session_port", {}, service="qbit"),
        container_step("check.log_since_start",
                       {"match": "New : ", "port": {"from_step": 1, "output": "port"}}, service="gsp"),
    )
    result = engine.RunbookEngine(svc).run(ex, rb, origin="planner")
    assert result.status == "failed"
    assert steps(svc, ex)[0][:2] == ("read.qbittorrent_session_port", "failed")
    assert steps(svc, ex)[1][:2] == ("check.log_since_start", "skipped")


def test_unit_action_outside_the_sudo_allowlist_never_runs(svc, monkeypatch):
    def refuse(command):
        raise bender.SudoScopeError("sudo systemctl restart evil.service", command, "restart", "evil.service")

    monkeypatch.setattr(bender, "_check_sudo_allowlist", refuse)
    ex = execution(svc)
    rb = runbook({"type": "unit.action", "params": {"action": "restart", "unit": "evil.service"},
                  "binding": {"unit": "evil.service"}})
    result = engine.RunbookEngine(svc).run(ex, rb, origin="planner")
    assert result.status == "failed" and "sudo allowlist" in result.reason
    assert svc.argv == []


def test_prune_runs_the_fixed_list_as_argv(svc, monkeypatch):
    seen = []
    monkeypatch.setattr(bender, "run_argv_bounded",
                        lambda argv, timeout, max_bytes: seen.append(argv) or (0, "", "", False))
    ex = execution(svc)
    result = engine.RunbookEngine(svc).run(ex, runbook({"type": "prune.safe", "params": {}, "binding": {}}),
                                           origin="system")
    assert result.status == "passed"
    assert seen == [["docker", "image", "prune", "-a", "-f"], ["docker", "network", "prune", "-f"]]


def test_stack_up_outputs_container_identities(svc):
    ex = execution(svc)
    step = {"type": "stack.up", "params": {"stack": "media"}, "binding": FakeBinder().stack("media", timeout=4)}
    result = engine.RunbookEngine(svc).run(ex, runbook(step), origin="telegram-direct")
    assert result.status == "passed"
    assert svc._store.list_steps(ex)[0]["output"] == {"services": [
        {"project": "media", "service": "web", "container_name": "media-web-1",
         "container_id": "0123456789ab"}]}


def test_startup_reconcile_settles_dispatched_pending_and_reserved(svc):
    ex = execution(svc)
    rb = runbook(service_step("service.restart"), service_step("service.restart", service="radarr"),
                 {"type": "wait", "params": {"seconds": 1}, "binding": {}})
    svc._store.create_steps(ex, rb.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "service.restart", "media/sonarr"),
                                             (2, "service.restart", "media/radarr")],
                                        window_start=0, cooldown_start=2e6, max_per_day=99, now=1e6)
    svc._store.consume_attempt(ex, 1)
    svc._store.mark_step_dispatched(ex, 1)
    settled = engine.startup_reconcile(svc._store)
    assert steps(svc, ex) == [("service.restart", "failed", "unknown"),
                              ("service.restart", "skipped", None), ("wait", "skipped", None)]
    assert attempts(svc, ex) == [(1, "consumed"), (2, "released")]
    assert settled["unknown"] == 1 and settled["skipped"] == 2


def test_verified_up_whose_outputs_cannot_be_recorded_is_not_a_crash(svc):
    # Own review, T39: the stack is up; only its container identities are missing.
    def duplicate(stack, services, *, timeout):
        raise actions.TargetError(f"{stack}/web has 2 containers after up")

    svc._binder.stack_containers = duplicate
    ex = execution(svc)
    step = {"type": "stack.up", "params": {"stack": "media"}, "binding": FakeBinder().stack("media", timeout=4)}
    result = engine.RunbookEngine(svc).run(ex, runbook(step), origin="telegram-direct")
    assert result.status == "failed" and "is up and verified" in result.reason
    assert "crashed" not in result.reason
    assert steps(svc, ex) == [("stack.up", "failed", "applied")]


def test_settle_crashed_execution_closes_open_steps_and_attempts(svc):
    ex = execution(svc)
    rb = runbook(service_step("service.restart"), service_step("service.restart", service="radarr"))
    svc._store.create_steps(ex, rb.steps)
    svc._store.reserve_runbook_attempts(ex, [(1, "service.restart", "media/sonarr"),
                                             (2, "service.restart", "media/radarr")],
                                        window_start=0, cooldown_start=2e6, max_per_day=99, now=1e6)
    svc._store.consume_attempt(ex, 1)
    svc._store.mark_step_dispatched(ex, 1)
    engine.settle_crashed_execution(svc._store, ex)
    assert steps(svc, ex) == [("service.restart", "failed", "unknown"), ("service.restart", "skipped", None)]
    assert attempts(svc, ex) == [(1, "consumed"), (2, "released")]


def test_default_runner_is_bounded(monkeypatch):
    from planet_express.application import command_service as cs
    seen = {}

    def bounded(argv, timeout, max_bytes):
        seen["max_bytes"] = max_bytes
        return (0, " out \n", " err \n", True)

    monkeypatch.setattr(bender, "run_argv_bounded", bounded)
    assert cs._bounded_run_argv(["docker", "ps"], 5) == (0, "out", "err")
    assert seen["max_bytes"] == cs.STEP_OUTPUT_MAX_BYTES


# ── rollback planning (T40) ────────────────────────────────────────────────────
def _rows(svc, ex, rb):
    svc._store.create_steps(ex, rb.steps)
    return svc._store.list_steps(ex)


def test_rollback_plans_inverses_newest_first_and_lists_the_rest(svc):
    ex = execution(svc)
    rb = runbook(service_step("service.stop"), service_step("service.restart", service="radarr"),
                 service_step("service.start", service="web"))
    _rows(svc, ex, rb)
    svc._store.set_step_pre_state(ex, 1, {"running": True})
    svc._store.mark_step_dispatched(ex, 1)
    svc._store.finish_step(ex, 1, status="passed", effect="applied", reason="stopped")
    svc._store.mark_step_dispatched(ex, 2)
    svc._store.finish_step(ex, 2, status="passed", effect="applied", reason="restarted")
    svc._store.set_step_pre_state(ex, 3, {"running": False})
    svc._store.mark_step_dispatched(ex, 3)
    svc._store.finish_step(ex, 3, status="passed", effect="applied", reason="started")

    plan = engine.plan_rollback(svc, "Bounce media", svc._store.list_steps(ex))
    assert [step.type for step in plan.runbook.steps] == ["service.stop", "service.start"]
    assert [step.params["service"] for step in plan.runbook.steps] == ["web", "sonarr"]
    assert plan.undo == ["3. service.start", "1. service.stop"]
    assert plan.not_reversible == ["2. service.restart"]
    assert plan.unknown == []
    assert engine.rollback_preview(svc._store.list_steps(ex)) == {
        "undo": [3, 1], "unknown": [], "not_reversible": [2]}


def test_rollback_rechecks_unknown_steps_against_the_host(svc):
    ex = execution(svc)
    rb = runbook(service_step("service.stop"), service_step("service.stop", service="radarr"))
    _rows(svc, ex, rb)
    for n, service in ((1, "sonarr"), (2, "radarr")):
        svc._store.set_step_pre_state(ex, n, {"running": True})
        svc._store.mark_step_dispatched(ex, n)
        svc._store.finish_step(ex, n, status="failed", effect="unknown", reason="core died")
    svc.running = {"media-sonarr-1": False, "media-radarr-1": True}  # one really stopped, one not

    plan = engine.plan_rollback(svc, "Stop two", svc._store.list_steps(ex))
    # sonarr is stopped, so its stop was applied and is undone; radarr still runs: not applied.
    assert [step.params["service"] for step in plan.runbook.steps] == ["sonarr"]
    assert plan.unknown == []


def test_rollback_leaves_a_still_unknown_step_alone(svc):
    ex = execution(svc)
    rb = runbook(service_step("service.stop"))
    _rows(svc, ex, rb)
    svc._store.set_step_pre_state(ex, 1, {"running": True})
    svc._store.mark_step_dispatched(ex, 1)
    svc._store.finish_step(ex, 1, status="failed", effect="unknown", reason="core died")

    def broken(container):
        return Reading(status="", error="inspect failed")

    svc._read_health = broken
    plan = engine.plan_rollback(svc, "Stop one", svc._store.list_steps(ex))
    assert plan.runbook is None and plan.unknown == ["1. service.stop"] and plan.undo == []


# ── Codex review of 5b-1 ───────────────────────────────────────────────────────
def test_stack_service_set_change_refuses_before_any_argv(svc):
    # An include:/profile change can move the service set without touching the top-level file.
    ex = execution(svc)
    step = {"type": "stack.up", "params": {"stack": "media"}, "binding": FakeBinder().stack("media", timeout=4)}
    svc._binder.services_by_stack["media"] = ["web", "worker"]  # a service appeared since approval
    result = engine.RunbookEngine(svc).run(ex, runbook(step), origin="telegram-direct")
    assert result.status == "failed" and "services of media changed since approval" in result.reason
    assert svc.argv == []
    assert steps(svc, ex) == [("stack.up", "failed", "not_applied")]


def test_container_only_steps_also_refuse_a_changed_compose_file(svc):
    ex = execution(svc)
    step = container_step("check.container", {"expect": "running"})
    svc._binder.compose_sha = "b" * 64
    result = engine.RunbookEngine(svc).run(ex, runbook(step), origin="planner")
    assert result.status == "failed" and "compose file changed since approval" in result.reason


def test_unreadable_unit_state_is_never_read_as_stopped(svc, monkeypatch):
    calls = []

    def unreadable(argv, timeout):
        calls.append(argv)
        return (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out")

    monkeypatch.setattr(bender, "run_argv", unreadable)
    monkeypatch.setattr(bender, "_check_sudo_allowlist", lambda command: None)
    ex = execution(svc)
    rb = runbook({"type": "unit.action", "params": {"action": "stop", "unit": "casa-stacks.service"},
                  "binding": {"unit": "casa-stacks.service"}})
    result = engine.RunbookEngine(svc).run(ex, rb, origin="planner")
    # pre-state unreadable: the step never runs
    assert result.status == "failed" and "could not read the state" in result.reason
    assert steps(svc, ex) == [("unit.action", "failed", "not_applied")]
    assert svc.argv == []

    # and when only the post-check is unreadable, the outcome is unknown, never "stopped"
    ex2 = execution_for(svc, "media/radarr")
    states = iter([(0, "active\n", ""), (bender.RUN_ARGV_TIMEOUT_EXIT, "", "timed out")])
    monkeypatch.setattr(bender, "run_argv", lambda argv, timeout: next(states))
    result = engine.RunbookEngine(svc).run(ex2, rb, origin="planner")
    assert result.status == "failed" and "could not read the state" in result.reason
    assert steps(svc, ex2) == [("unit.action", "failed", "unknown")]


def test_all_stack_bindings_share_one_deadline(monkeypatch):
    from planet_express.execution.binding import Binder
    seen = []
    now = [0.0]

    def clock():
        now[0] += 2.0  # every binding step "takes" 2s
        return now[0]

    monkeypatch.setattr("planet_express.execution.binding.time.monotonic", clock)
    monkeypatch.setattr("planet_express.execution.binding._file_sha256", lambda path: "a" * 64)

    def slow(argv, timeout):
        seen.append(timeout)
        return (0, "web\n", "")

    binder = Binder(run_argv=slow)
    with pytest.raises(actions.TargetTimeout):
        binder.stack_set(["a", "b", "c", "d"], timeout=5)
    # each stack gets what is LEFT of the one budget, and the set is abandoned when it runs out
    assert len(seen) < 4 and all(later < earlier for earlier, later in pairwise(seen))


def test_container_only_steps_inspect_the_container_once(svc):
    # Codex review round 2, 5b-1: the binding check already fetched the id.
    calls = []
    real = svc._binder.container_id
    svc._binder.container_id = lambda container, *, timeout: calls.append(container) or real(container, timeout=timeout)
    ex = execution(svc)
    result = engine.RunbookEngine(svc).run(
        ex, runbook(container_step("check.container", {"expect": "running"})), origin="planner")
    assert result.status == "passed"
    assert len(calls) == 1


def test_a_non_operator_run_is_refused_at_reservation_when_the_target_is_cooling_down(svc, monkeypatch):
    """Two different plans can each hold a card for the same target, so the limits are re-checked
    atomically where the attempts are reserved (Codex, T41)."""
    import config as config_module
    from config_schema import AutonomyConfig
    monkeypatch.setattr(config_module, "AUTONOMY",
                        AutonomyConfig(cooldown_seconds=1800, max_attempts_per_day=3))
    first = execution(svc)
    assert engine.RunbookEngine(svc).run(first, runbook(service_step("service.restart")),
                                         origin="planner").status == "passed"
    second = execution(svc)
    result = engine.RunbookEngine(svc).run(second, runbook(service_step("service.restart")),
                                           origin="planner")
    assert result.status == "failed" and "cooling down" in result.reason
    assert steps(svc, second) == [("service.restart", "failed", "not_applied")]
    assert svc.argv == [svc.argv[0]]  # the second run never reached a command


def test_an_operator_run_still_reserves_without_a_cap(svc, monkeypatch):
    import config as config_module
    from config_schema import AutonomyConfig
    monkeypatch.setattr(config_module, "AUTONOMY",
                        AutonomyConfig(cooldown_seconds=1800, max_attempts_per_day=1))
    for _ in range(3):
        ex = execution(svc)
        assert engine.RunbookEngine(svc).run(ex, runbook(service_step("service.restart")),
                                             origin="telegram").status == "passed"


def test_a_reservation_refusal_blames_the_step_whose_target_is_at_its_limit(svc, monkeypatch):
    import config as config_module
    from config_schema import AutonomyConfig
    monkeypatch.setattr(config_module, "AUTONOMY",
                        AutonomyConfig(cooldown_seconds=1800, max_attempts_per_day=3))
    first = execution(svc)
    engine.RunbookEngine(svc).run(first, runbook(service_step("service.restart")), origin="planner")
    second = execution(svc)
    rb = runbook({"type": "wait", "params": {"seconds": 1}, "binding": {}},
                 service_step("service.restart"))
    result = engine.RunbookEngine(svc).run(second, rb, origin="planner")
    assert result.status == "failed"
    assert steps(svc, second) == [("wait", "skipped", None),
                                  ("service.restart", "failed", "not_applied")]
