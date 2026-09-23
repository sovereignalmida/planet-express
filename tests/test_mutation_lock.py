"""
Mutation lock (landing 1a, T4). CRITICAL: several tests here pin a change to EXISTING
legacy behavior, the order in which handle_callback checks, resolves, and executes.

Contract (design doc, "Lock rule: a mutation lock, not PipelineState"):
  - PipelineState.try_begin_mutation(owner) -> bool / end_mutation(owner): a flag checked
    and set under the existing _lock, independent of _state and the pending plan.
  - Every host-mutating path takes it: legacy plan execution, diff apply, rollback,
    /patchnow, the weekly update pass, safe-prune, /up and /down.
  - Legacy plan/diff approvals take the lock BEFORE notifier.resolve and before
    transition(EXECUTING). On busy they only acknowledge the tap ("busy"), leaving the
    card, the pending plan/diff and AWAITING_APPROVAL untouched.
  - The weekly scheduler retries every 5 minutes for up to 1 hour, then skips and logs.
"""

import logging
import os
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import Decision, FakeNotifier

AWAITING = fw.PipelineState.AWAITING_APPROVAL


COMMANDS = object()   # the canary pass is stubbed out; only the lock is under test


def _state():
    return fw.PipelineState()


class _ForbiddenThread:
    def __init__(self, *args, **kwargs):
        raise AssertionError("no execution thread may start while the host is busy")


class _RecordingThread:
    spawned: ClassVar[list] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        _RecordingThread.spawned.append((self.target, self.args))


# ── PipelineState mutation flag ─────────────────────────────────────────────────
def test_try_begin_mutation_is_exclusive():
    s = _state()
    assert s.try_begin_mutation("plan:p1") is True
    assert s.try_begin_mutation("zoidberg-weekly") is False
    assert s.mutation_owner == "plan:p1"
    s.end_mutation("plan:p1")
    assert s.mutation_owner is None
    assert s.try_begin_mutation("zoidberg-weekly") is True


def test_end_mutation_by_a_non_owner_does_not_release():
    s = _state()
    assert s.try_begin_mutation("plan:p1")
    s.end_mutation("someone-else")
    assert s.mutation_owner == "plan:p1"


def test_mutation_flag_leaves_pipeline_state_and_pending_plan_alone():
    s = _state()
    s.transition(AWAITING, plan_id="p1", msg_id=7)
    assert s.try_begin_mutation("stack:up:media")
    s.end_mutation("stack:up:media")
    assert s.state == AWAITING
    assert s.get_pending() == ("p1", 7)


def test_try_begin_mutation_is_atomic_under_racing_threads():
    s = _state()
    n = 24
    barrier = threading.Barrier(n)
    winners = []
    guard = threading.Lock()

    def worker(i):
        barrier.wait()
        if s.try_begin_mutation(f"w{i}"):
            with guard:
                winners.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


# ── Legacy plan/diff callbacks: lock BEFORE resolve (CRITICAL ordering) ─────────
def test_plan_approve_while_busy_leaves_card_plan_and_state_untouched(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: pytest.fail("plan must not be loaded while busy"))
    monkeypatch.setattr(fw.threading, "Thread", _ForbiddenThread)
    s = _state()
    s.transition(AWAITING, plan_id="p1", msg_id=5)
    assert s.try_begin_mutation("zoidberg-weekly")
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="p1", kind="plan", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert n.resolutions == [], "card must not be edited to 'approved' while busy"
    assert len(n.acknowledgements) == 1
    assert "busy" in n.acknowledgements[0][1].lower()
    assert s.state == AWAITING
    assert s.get_pending() == ("p1", 5)
    assert s.mutation_owner == "zoidberg-weekly"


def test_diff_approve_while_busy_does_not_apply(monkeypatch):
    monkeypatch.setattr(fw.bender, "apply_pending_diff", lambda _id: pytest.fail("diff applied while busy"))
    s = _state()
    assert s.try_begin_mutation("plan:p1")
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="d1", kind="diff", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert n.resolutions == []
    assert "busy" in n.acknowledgements[0][1].lower()
    assert s.mutation_owner == "plan:p1"


def test_plan_approve_takes_lock_before_resolving_and_executing(monkeypatch):
    order = []
    s = _state()
    real_begin = s.try_begin_mutation

    def spy_begin(owner):
        order.append(("lock", owner))
        return real_begin(owner)

    monkeypatch.setattr(s, "try_begin_mutation", spy_begin)

    class OrderNotifier(FakeNotifier):
        def resolve(self, decision, ack_text, resolution_text):
            order.append(("resolve", decision.request_id))
            super().resolve(decision, ack_text, resolution_text)

    plan_data = {"id": "p2", "steps": []}
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: plan_data)
    _RecordingThread.spawned = []
    monkeypatch.setattr(fw.threading, "Thread", _RecordingThread)
    n = OrderNotifier()
    n.queue_decision(Decision(request_id="p2", kind="plan", approved=True))

    fw.handle_callback({}, tg="tg", notifier=n, state=s)

    assert order[0] == ("lock", "plan:p2")
    assert order.index(("lock", "plan:p2")) < order.index(("resolve", "p2"))
    assert s.state == fw.PipelineState.EXECUTING
    assert s.mutation_owner == "plan:p2", "lock is held for the execution thread, released by _execute_plan"
    assert _RecordingThread.spawned == [(fw._execute_plan, ("tg", n, s, plan_data))]


def test_plan_approve_not_found_releases_lock(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: None)
    s = _state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="p3", kind="plan", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.IDLE


def test_plan_cancel_does_not_need_the_lock():
    s = _state()
    s.transition(AWAITING, plan_id="p4")
    assert s.try_begin_mutation("zoidberg-weekly")
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="p4", kind="plan", approved=False))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert n.resolutions[0][2] == "❌ Plan #p4 *cancelled*."
    assert s.state == fw.PipelineState.IDLE
    assert s.mutation_owner == "zoidberg-weekly"


def test_execute_plan_releases_lock_even_when_bender_raises(monkeypatch):
    def boom(plan, tg):
        raise RuntimeError("bender fell over")

    monkeypatch.setattr(fw.bender, "execute", boom)
    s = _state()
    assert s.try_begin_mutation("plan:p9")
    s.transition(fw.PipelineState.EXECUTING, plan_id="p9")

    fw._execute_plan(None, FakeNotifier(), s, {"id": "p9"})

    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.IDLE


def test_diff_approve_releases_lock_after_apply(monkeypatch):
    monkeypatch.setattr(fw.bender, "apply_pending_diff", lambda _id: {"backup_path": None})
    s = _state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="d2", kind="diff", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert n.resolutions[0][1] == "Applying diff..."
    assert s.mutation_owner is None


def test_diff_apply_safety_error_releases_lock(monkeypatch):
    def refuse(_id):
        raise fw.bender.SafetyError("path escapes stacks root")

    monkeypatch.setattr(fw.bender, "apply_pending_diff", refuse)
    s = _state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="d3", kind="diff", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert any("Could not apply diff" in m for m in n.notifications)
    assert s.mutation_owner is None


# ── Other mutating paths refuse while busy ──────────────────────────────────────
def test_patchnow_update_pass_refuses_while_busy(monkeypatch):
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: pytest.fail("update pass ran while busy"))
    s = _state()
    assert s.try_begin_mutation("plan:p1")
    n = FakeNotifier()

    fw._run_update_pass(None, n, s, COMMANDS)

    assert any("busy" in m.lower() for m in n.notifications)
    assert s.mutation_owner == "plan:p1"


def test_patchnow_update_pass_takes_and_releases_lock(monkeypatch):
    s = _state()
    seen = {}
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: seen.setdefault("owner", s.mutation_owner))

    fw._run_update_pass(None, FakeNotifier(), s, COMMANDS)

    assert seen["owner"] == "zoidberg-patchnow"
    assert s.mutation_owner is None


def test_safe_prune_skips_while_busy(monkeypatch):
    monkeypatch.setattr(fw, "_root_disk_alert", lambda snap: {"used_pct": 91, "alert": "high"})
    monkeypatch.setattr(fw, "_has_incomplete_stacks", lambda snap: False)
    monkeypatch.setattr(fw, "_safe_to_prune", lambda snap: True)
    monkeypatch.setattr(fw, "_has_active_rollback_candidates", lambda: False)
    monkeypatch.setattr(fw.bender, "run_safe_prune", lambda: pytest.fail("pruned while busy"))
    s = _state()
    assert s.try_begin_mutation("zoidberg-weekly")
    n = FakeNotifier()

    fw.maybe_run_safe_prune({}, n, s)

    assert n.notifications == []


def test_rollback_refuses_while_busy(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: {"id": pid, "rollback": [{"n": 1}]})
    monkeypatch.setattr(fw.bender, "execute_rollback", lambda p, tg: pytest.fail("rolled back while busy"))
    s = _state()
    assert s.try_begin_mutation("plan:p1")
    n = FakeNotifier()

    fw._do_rollback(None, n, s, "p0")

    assert any("busy" in m.lower() for m in n.notifications)
    assert s.mutation_owner == "plan:p1"


# ── Weekly update scheduler: retry every 5 min for up to 1 hour ─────────────────
def test_scheduled_update_runs_immediately_when_free(monkeypatch):
    s = _state()
    ran = []
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: ran.append(s.mutation_owner))

    result = fw._run_scheduled_update_pass(
        "tg", s, sleep=lambda _s: pytest.fail("should not wait"), commands=COMMANDS)

    assert result == "ran"
    assert ran == ["zoidberg-weekly"]
    assert s.mutation_owner is None


def test_scheduled_update_retries_every_5_minutes_then_skips(monkeypatch, caplog):
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: pytest.fail("ran while busy"))
    s = _state()
    assert s.try_begin_mutation("plan:p1")
    sleeps = []

    with caplog.at_level(logging.WARNING, logger="planetexpress.farnsworth"):
        result = fw._run_scheduled_update_pass(None, s, sleep=sleeps.append, commands=COMMANDS)

    assert result == "skipped"
    assert sleeps == [300] * 12, "attempts at 0, 5, ... 60 minutes"
    assert any("skip" in r.getMessage().lower() for r in caplog.records)
    assert s.mutation_owner == "plan:p1"


def test_scheduled_update_runs_once_the_lock_frees(monkeypatch):
    s = _state()
    assert s.try_begin_mutation("plan:p1")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            s.end_mutation("plan:p1")

    ran = []
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: ran.append(True))

    assert fw._run_scheduled_update_pass(None, s, sleep=sleep, commands=COMMANDS) == "ran"
    assert sleeps == [300, 300]
    assert ran == [True]


def test_scheduled_update_releases_lock_when_the_pass_raises(monkeypatch):
    def boom(**kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr(fw.zoidberg, "run_update_pass", boom)
    s = _state()

    assert fw._run_scheduled_update_pass(None, s, sleep=lambda _s: None, commands=COMMANDS) == "failed"
    assert s.mutation_owner is None


# ── Codex review (landing 1a): no lock leak if plan startup fails before hand-off ──
def test_plan_approve_releases_lock_when_resolve_raises(monkeypatch):
    class UnreachableTelegram(FakeNotifier):
        def resolve(self, decision, ack_text, resolution_text):
            raise ConnectionError("telegram unreachable")

    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: pytest.fail("must not get past resolve"))
    s = _state()
    s.transition(AWAITING, plan_id="p5", msg_id=9)
    n = UnreachableTelegram()
    n.queue_decision(Decision(request_id="p5", kind="plan", approved=True))

    with pytest.raises(ConnectionError):
        fw.handle_callback({}, None, n, s)

    assert s.mutation_owner is None
    assert s.state == AWAITING, "nothing executed, so the plan stays approvable"
    assert s.get_pending() == ("p5", 9)


def test_plan_approve_releases_lock_and_resets_state_when_thread_start_fails(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: {"id": pid, "steps": []})

    class NoThreads:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(fw.threading, "Thread", NoThreads)
    s = _state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="p6", kind="plan", approved=True))

    with pytest.raises(RuntimeError):
        fw.handle_callback({}, "tg", n, s)

    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.IDLE


def test_plan_not_found_releases_lock_exactly_once(monkeypatch, caplog):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: None)
    s = _state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="p7", kind="plan", approved=True))

    with caplog.at_level(logging.WARNING, logger="planetexpress.farnsworth"):
        fw.handle_callback({}, None, n, s)

    assert s.mutation_owner is None
    assert not any("ignored" in r.getMessage() for r in caplog.records), "lock released twice"


# ── Codex re-review (landing 1a): idle check and lock grab are one atomic step ──
@pytest.mark.parametrize("busy_state", [fw.PipelineState.RUNNING, AWAITING, fw.PipelineState.EXECUTING])
def test_require_idle_refuses_without_taking_the_lock(busy_state):
    s = _state()
    s.transition(busy_state, plan_id="p1")
    assert s.try_begin_mutation("zoidberg-weekly", require_idle=True) is False
    assert s.mutation_owner is None


def test_require_idle_takes_the_lock_when_idle():
    s = _state()
    assert s.try_begin_mutation("zoidberg-weekly", require_idle=True) is True
    assert s.mutation_owner == "zoidberg-weekly"


def test_require_idle_reads_state_under_the_lock_transitions_use():
    # Deterministic: while the test holds _lock (the lock transition() takes), the grab
    # must wait; a scan starting during that wait must make the grab fail, lock untaken.
    s = _state()
    result = {}

    def grab():
        result["got"] = s.try_begin_mutation("zoidberg-weekly", require_idle=True)

    with s._lock:
        t = threading.Thread(target=grab)
        t.start()
        t.join(timeout=0.2)
        assert t.is_alive(), "try_begin_mutation must wait on the same lock transition() holds"
        s._state = fw.PipelineState.RUNNING  # what transition(RUNNING) does under this lock
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["got"] is False
    assert s.mutation_owner is None

def test_scheduled_update_waits_while_a_scan_is_running(monkeypatch):
    s = _state()
    s.transition(fw.PipelineState.RUNNING)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        s.transition(fw.PipelineState.IDLE)  # scan finishes during the first wait

    ran = []
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: ran.append(True))

    assert fw._run_scheduled_update_pass(None, s, sleep=sleep, commands=COMMANDS) == "ran"
    assert sleeps == [300]
    assert ran == [True]
    assert s.mutation_owner is None


# ── Codex review (landing 1a): no scan may start while a host mutation holds the lock ──
def test_scan_refuses_to_start_while_a_mutation_holds_the_lock(monkeypatch):
    monkeypatch.setattr(fw.leela, "run_status", lambda: pytest.fail("scanned a host mid-change"))
    monkeypatch.setattr(fw.leela, "run_full", lambda: pytest.fail("scanned a host mid-change"))
    s = _state()
    assert s.try_begin_mutation("zoidberg-weekly", require_idle=True)
    n = FakeNotifier()

    fw.run_pipeline(n, s, mode="full")

    assert s.state == fw.PipelineState.IDLE
    assert any("busy" in m.lower() and "zoidberg-weekly" in m for m in n.notifications)
    assert s.mutation_owner == "zoidberg-weekly"


def test_scan_starts_when_host_is_free(monkeypatch):
    scanned = []

    def run_status():
        scanned.append(True)
        raise RuntimeError("stop after the scan starts")

    monkeypatch.setattr(fw.leela, "run_status", run_status)
    s = _state()
    n = FakeNotifier()

    fw.run_pipeline(n, s, mode="status")

    assert scanned == [True]
    assert s.state == fw.PipelineState.IDLE  # error path returns to idle
    assert s.mutation_owner is None


def test_scan_still_refuses_while_awaiting_approval(monkeypatch):
    monkeypatch.setattr(fw.leela, "run_full", lambda: pytest.fail("scanned while awaiting approval"))
    s = _state()
    s.transition(AWAITING, plan_id="p1")
    n = FakeNotifier()

    fw.run_pipeline(n, s, mode="full")

    assert s.state == AWAITING
    assert any("already running or awaiting approval" in m for m in n.notifications)


def test_try_start_run_checks_the_lock_in_the_same_acquisition():
    # While the test holds _lock, try_start_run must wait; a mutation taking the lock
    # during that wait must make the scan start fail and leave the pipeline IDLE.
    s = _state()
    result = {}

    def start():
        result["started"] = s.try_start_run()

    with s._lock:
        t = threading.Thread(target=start)
        t.start()
        t.join(timeout=0.2)
        assert t.is_alive(), "try_start_run must wait on the same lock try_begin_mutation uses"
        s._mutation_owner = "zoidberg-weekly"  # what try_begin_mutation does under this lock
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["started"] is False
    assert s.state == fw.PipelineState.IDLE


# ── Codex review (landing 1a): no mutation may start while a scan is running ────
def _scanning_state():
    s = _state()
    s.transition(fw.PipelineState.RUNNING)
    return s


def test_diff_approve_refuses_during_a_running_scan(monkeypatch):
    monkeypatch.setattr(fw.bender, "apply_pending_diff", lambda _id: pytest.fail("applied a diff mid-scan"))
    s = _scanning_state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="d9", kind="diff", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert n.resolutions == []
    assert "scan running" in n.acknowledgements[0][1]
    assert s.mutation_owner is None


def test_stale_plan_card_approve_refuses_during_a_running_scan(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: pytest.fail("loaded a plan mid-scan"))
    monkeypatch.setattr(fw.threading, "Thread", _ForbiddenThread)
    s = _scanning_state()
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="old-plan", kind="plan", approved=True))

    fw.handle_callback({}, tg=None, notifier=n, state=s)

    assert n.resolutions == []
    assert "scan running" in n.acknowledgements[0][1]
    assert s.state == fw.PipelineState.RUNNING


def test_patchnow_refuses_during_a_running_scan(monkeypatch):
    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kw: pytest.fail("updated mid-scan"))
    s = _scanning_state()
    n = FakeNotifier()

    fw._run_update_pass(None, n, s, COMMANDS)

    assert any("scan running" in m for m in n.notifications)
    assert s.mutation_owner is None


def test_rollback_refuses_during_a_running_scan(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: {"id": pid, "rollback": [{"n": 1}]})
    monkeypatch.setattr(fw.bender, "execute_rollback", lambda p, tg: pytest.fail("rolled back mid-scan"))
    s = _scanning_state()
    n = FakeNotifier()

    fw._do_rollback(None, n, s, "p0")

    assert any("scan running" in m for m in n.notifications)


class _PruneCommands:
    """Safe prune runs as a typed `prune.safe` runbook now (slice 5b-3); this records who held the
    mutation lock when the engine was asked to run it."""

    def __init__(self, state, ran):
        self._state, self._ran = state, ran

    def run_automatic(self, runbook, *, origin, target_key, operator=None):
        from planet_express.application.command_service import AutomaticResult
        self._ran.append(self._state.mutation_owner)
        assert [step.type for step in runbook.steps] == ["prune.safe"]
        assert (origin, target_key) == ("system", "prune:safe")
        return AutomaticResult("passed", "freed 1.2GB", "e1", [])


def test_safe_prune_still_runs_inside_its_own_scan(monkeypatch):
    monkeypatch.setattr(fw, "_root_disk_alert", lambda snap: {"used_pct": 91, "alert": "high"})
    monkeypatch.setattr(fw, "_has_incomplete_stacks", lambda snap: False)
    monkeypatch.setattr(fw, "_safe_to_prune", lambda snap: True)
    monkeypatch.setattr(fw, "_has_active_rollback_candidates", lambda commands: False)
    s = _scanning_state()
    ran = []
    n = FakeNotifier()

    fw.maybe_run_safe_prune({}, n, s, _PruneCommands(s, ran))

    assert ran == ["safe-prune"]
    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.RUNNING
    assert any("Safe prune ran" in m for m in n.notifications)


def test_try_begin_mutation_rejects_a_scan_in_the_same_acquisition():
    # While the test holds _lock, the grab must wait; a scan starting during that wait
    # must make the grab fail without taking the lock.
    s = _state()
    result = {}

    def grab():
        result["got"] = s.try_begin_mutation("stack:up:media")

    with s._lock:
        t = threading.Thread(target=grab)
        t.start()
        t.join(timeout=0.2)
        assert t.is_alive(), "try_begin_mutation must wait on the same lock try_start_run uses"
        s._state = fw.PipelineState.RUNNING  # what try_start_run does under this lock
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["got"] is False
    assert s.mutation_owner is None


def test_busy_reason_names_the_lock_owner_before_the_scan():
    s = _scanning_state()
    assert s.busy_reason == "scan running"
    assert s.try_begin_mutation("safe-prune", during_scan=True)
    assert s.busy_reason == "safe-prune"


# ── Codex review (landing 1a): release + return to IDLE is one atomic step ──────
def _forbid_separate_transition(monkeypatch, s):
    def no_transition(*args, **kwargs):
        raise AssertionError("must release and reset in end_mutation, not a separate transition()")

    monkeypatch.setattr(s, "transition", no_transition)


def test_end_mutation_reset_only_happens_for_the_actual_owner():
    s = _state()
    s.transition(fw.PipelineState.EXECUTING, plan_id="new", msg_id=3)
    assert s.try_begin_mutation("plan:new")

    s.end_mutation("plan:old", reset_to_idle=True)  # a stale finisher

    assert s.mutation_owner == "plan:new"
    assert s.state == fw.PipelineState.EXECUTING
    assert s.get_pending() == ("new", 3)


def test_end_mutation_reset_releases_and_resets_together():
    s = _state()
    s.transition(fw.PipelineState.EXECUTING, plan_id="p1", msg_id=4)
    assert s.try_begin_mutation("plan:p1")

    s.end_mutation("plan:p1", reset_to_idle=True)

    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.IDLE
    assert s.get_pending() == (None, None)


def test_execute_plan_finishes_without_a_separate_transition(monkeypatch):
    monkeypatch.setattr(fw.bender, "execute", lambda plan, tg: {"final_status": "success", "steps_completed": 0})
    s = _state()
    s.transition(fw.PipelineState.EXECUTING, plan_id="p8")
    assert s.try_begin_mutation("plan:p8")
    _forbid_separate_transition(monkeypatch, s)

    fw._execute_plan(None, FakeNotifier(), s, {"id": "p8"})

    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.IDLE


def test_rollback_finishes_without_a_separate_transition(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: {"id": pid, "rollback": [{"n": 1}]})
    monkeypatch.setattr(fw.bender, "execute_rollback", lambda p, tg: {"steps_completed": 1, "errors": []})
    s = _state()
    _forbid_separate_transition(monkeypatch, s)

    fw._do_rollback(None, FakeNotifier(), s, "p1")

    assert s.mutation_owner is None
    assert s.state == fw.PipelineState.IDLE


def test_finishing_plan_cannot_clobber_a_newer_execution(monkeypatch):
    # The race Codex found: old plan's thread finishes after a newer approval already owns
    # the lock and is EXECUTING. The old thread must leave the newer state alone.
    def boom(plan, tg):
        raise RuntimeError("old plan failed")

    monkeypatch.setattr(fw.bender, "execute", boom)
    s = _state()
    s.transition(fw.PipelineState.EXECUTING, plan_id="new", msg_id=11)
    assert s.try_begin_mutation("plan:new")

    fw._execute_plan(None, FakeNotifier(), s, {"id": "old"})

    assert s.mutation_owner == "plan:new"
    assert s.state == fw.PipelineState.EXECUTING
    assert s.get_pending() == ("new", 11)


def test_unknown_stack_blocks_pruning():
    snapshot = {
        "containers": [{"name": "app", "status": "Up", "health": "healthy"}],
        "stack_completeness": [{"stack": "unreadable", "status": "unknown",
                                "missing_services": [], "alert": "MEDIUM"}],
    }
    assert fw._has_incomplete_stacks(snapshot) is True
    assert fw._safe_to_prune(snapshot) is False


@pytest.mark.parametrize("kwargs", [{}, {"during_scan": True}, {"require_idle": True}])
def test_maintenance_blocks_all_mutations_and_scans(kwargs, monkeypatch):
    window = fw.MaintenanceWindow("borg-backup", None, Path("/unused"))
    s = fw.PipelineState(maintenance=lambda: window)
    monkeypatch.setattr(s, "_persist", lambda: None)
    assert not s.try_begin_mutation("owner", **kwargs)
    assert not s.try_start_run()
    assert s.busy_reason == "maintenance: borg-backup"
    assert s.state == s.IDLE and s.mutation_owner is None
    assert s.get_pending() == (None, None)
    assert not s._lock.locked()
    window = None
    assert s.try_begin_mutation("owner", **kwargs)
    s.end_mutation("owner")
    assert s.try_start_run()


def test_maintenance_preserves_existing_owner_and_pending_plan(monkeypatch):
    window = None
    s = fw.PipelineState(maintenance=lambda: window)
    monkeypatch.setattr(s, "_persist", lambda: None)
    s.transition(s.AWAITING_APPROVAL, "plan", 123)
    assert s.try_begin_mutation("existing")
    window = fw.MaintenanceWindow("backup", None, Path("/unused"))
    assert not s.try_begin_mutation("new", during_scan=True)
    assert not s.try_start_run()
    assert s.busy_reason == "maintenance: backup"
    assert s.mutation_owner == "existing"
    assert s.state == s.AWAITING_APPROVAL and s.get_pending() == ("plan", 123)


def test_on_demand_scan_maintenance_message():
    s = fw.PipelineState(maintenance=lambda: fw.MaintenanceWindow("backup", None, Path("/unused")))
    notifier = FakeNotifier()
    fw.run_pipeline(notifier, s)
    assert notifier.notifications == ["🧰 Maintenance in progress (backup) — scan not started."]


def test_scheduler_waits_then_runs_without_deferral_notification(monkeypatch, caplog):
    window = fw.MaintenanceWindow("backup", None, Path("/unused"))
    s = fw.PipelineState(maintenance=lambda: window)
    notifier = FakeNotifier()
    sleeps = []
    runs = []

    def sleep(seconds):
        nonlocal window
        sleeps.append(seconds)
        if len(sleeps) == 2:
            window = None

    original_wait = fw._wait_for_maintenance
    monkeypatch.setattr(fw, "_wait_for_maintenance", lambda state: original_wait(state, sleep))
    monkeypatch.setattr(fw, "run_pipeline", lambda *args, **kwargs: runs.append(window))

    def scheduler_sleep(seconds):
        if seconds != 60:
            raise StopIteration

    monkeypatch.setattr(fw.time, "sleep", scheduler_sleep)
    with caplog.at_level(logging.INFO), pytest.raises(StopIteration):
        fw.scheduler_loop(notifier, s)
    assert sleeps == [60, 60] and runs == [None]
    assert notifier.notifications == []
    assert sum("waiting for maintenance" in r.message for r in caplog.records) == 1
    assert sum("maintenance wait ended" in r.message for r in caplog.records) == 1


def test_weekly_update_waits_through_maintenance(monkeypatch, caplog):
    window = fw.MaintenanceWindow("backup", None, Path("/unused"))
    s = fw.PipelineState(maintenance=lambda: window)
    calls = []

    def sleep(seconds):
        nonlocal window
        assert seconds == fw.UPDATE_BUSY_RETRY_SECONDS
        assert s.mutation_owner is None
        window = None

    monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda **kwargs: calls.append(s.mutation_owner))
    with caplog.at_level(logging.INFO):
        assert fw._run_scheduled_update_pass(None, s, sleep=sleep, commands=COMMANDS) == "ran"
    assert "maintenance: backup" in caplog.text
    assert calls == ["zoidberg-weekly"] and s.mutation_owner is None


def test_maintenance_blocks_safe_prune_inside_running_scan(monkeypatch):
    window = None
    s = fw.PipelineState(maintenance=lambda: window)
    monkeypatch.setattr(s, "_persist", lambda: None)
    assert s.try_start_run()
    window = fw.MaintenanceWindow("backup", None, Path("/unused"))
    assert not s.try_begin_mutation("safe-prune", during_scan=True)
    assert s.state == s.RUNNING and s.mutation_owner is None
    window = None
    assert s.try_begin_mutation("safe-prune", during_scan=True)


def test_scheduled_scan_admission_waits_without_maintenance_notification(monkeypatch, tmp_path):
    window = fw.MaintenanceWindow("backup", None, Path("/unused"))
    s = fw.PipelineState(maintenance=lambda: window)
    monkeypatch.setattr(s, "_persist", lambda: None)
    notifier = FakeNotifier()
    scans = []

    def wait(state):
        nonlocal window
        assert state is s and notifier.notifications == []
        window = None

    def scan():
        scans.append(window)
        raise RuntimeError("stop after scan admission")

    monkeypatch.setattr(fw, "_wait_for_maintenance", wait)
    monkeypatch.setattr(fw.leela, "run_full", scan)
    fw.run_pipeline(notifier, s, scheduled=True)
    assert scans == [None]
    assert not any("Maintenance" in text for text in notifier.notifications)
