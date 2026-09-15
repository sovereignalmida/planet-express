"""
planet_express/application/command_service.py (landing 1b): propose, decide, execute,
verify, startup reconciliation. Real Store (tmp SQLite) and real PipelineState; docker,
target resolution, verification and thread spawning are faked.
"""

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import Decision, FakeNotifier
from planet_express.application.command_service import CommandService
from planet_express.core.store import DEFAULT_TTL_SECONDS, Store
from planet_express.execution import actions

RESTART = actions.RESTART_SERVICE
TARGET = actions.Target("healthy", "web", "fixture-healthy")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    """Fakes for everything CommandService would otherwise do to the host."""

    def __init__(self, tmp_path):
        self.clock = Clock()
        self.store = Store(tmp_path / "data" / "planetexpress.db", clock=self.clock)
        self.store.init()
        self.notifier = FakeNotifier()
        self.state = fw.PipelineState()
        self.target = TARGET
        self.resolve_error: str | None = None
        self.resolve_calls: list[tuple] = []
        self.argv_calls: list[list[str]] = []
        self.argv_result = (0, "", "")
        self.verify_result = (True, "healthy for 15s")
        self.verify_calls: list[tuple] = []
        self.lock_owner_during_run: list = []
        self.spawned: list[tuple] = []
        self.run_spawned_immediately = True
        self.service = CommandService(
            self.store, self.notifier, self.state,
            run_argv=self._run_argv, resolve_target=self._resolve, verify=self._verify,
            restart_count=lambda container: 3, spawn=self._spawn, clock=self.clock,
        )

    def _resolve(self, stack, service, for_mutation=True):
        self.resolve_calls.append((stack, service, for_mutation))
        if self.resolve_error:
            raise actions.TargetError(self.resolve_error)
        return self.target

    def _run_argv(self, argv, timeout):
        self.lock_owner_during_run.append(self.state.mutation_owner)
        self.argv_calls.append(argv)
        return self.argv_result

    def _verify(self, container, baseline):
        self.verify_calls.append((container, baseline))
        return self.verify_result

    def _spawn(self, fn, *args):
        self.spawned.append((fn, args))
        if self.run_spawned_immediately:
            fn(*args)

    def propose(self, via="telegram", by="@chris (1001)"):
        return self.service.propose(RESTART, "healthy", "web", requested_via=via, requested_by=by)


def _tap(approval_id, approved=True, by="@chris (1001)"):
    return Decision(request_id=approval_id, kind="action", approved=approved, decided_by=by)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ── propose ─────────────────────────────────────────────────────────────────────
def test_propose_creates_one_card_and_dedups(env):
    first = env.propose()
    again = env.propose(via="dashboard", by="hermes")

    assert first.ok and first.created and first.approval_id
    assert again.ok and not again.created and again.approval_id == first.approval_id
    assert len(env.notifier.approval_requests) == 1
    text, request_id, kind = env.notifier.approval_requests[0]
    assert (request_id, kind) == (first.approval_id, "action")
    assert "healthy/web" in text and "R1" in text
    assert env.store.get_approval(first.approval_id)["message_id"] == 1


def test_racing_proposals_publish_exactly_one_card(env):
    barrier = threading.Barrier(2)
    original_resolve = env.service._resolve

    def resolve_together(*args, **kwargs):
        target = original_resolve(*args, **kwargs)
        barrier.wait()
        return target

    env.service._resolve = resolve_together
    results = []

    def propose(via):
        results.append(env.propose(via=via, by=via))

    threads = [threading.Thread(target=propose, args=(via,)) for via in ("telegram", "dashboard")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0].approval_id == results[1].approval_id
    assert sum(result.created for result in results) == 1
    assert len(env.notifier.approval_requests) == 1


def test_refused_target_creates_nothing(env):
    env.resolve_error = "stack 'ai' is forbidden"
    result = env.propose()
    assert not result.ok and "forbidden" in result.reason
    assert env.notifier.approval_requests == []
    assert any(e["kind"] == "proposal.refused" for e in env.store.list_events())


def test_unknown_action_never_resolves_a_target(env):
    result = env.service.propose("docker.rm_everything", "healthy", "web", requested_via="telegram", requested_by="x")
    assert not result.ok and "unknown action" in result.reason
    assert env.resolve_calls == []


def test_proposing_does_not_touch_pipeline_state(env):
    env.state.transition(fw.PipelineState.AWAITING_APPROVAL, plan_id="legacy", msg_id=4)
    env.propose()
    assert env.state.state == fw.PipelineState.AWAITING_APPROVAL
    assert env.state.get_pending() == ("legacy", 4)


# ── approve: happy path ─────────────────────────────────────────────────────────
def test_approve_runs_restart_under_the_lock_then_verifies(env):
    approval_id = env.propose().approval_id
    tap = _tap(approval_id)

    result = env.service.decide(approval_id, approve=True, decided_by="@chris (1001)", decision=tap)

    assert result.outcome == "started" and result.execution_id
    assert env.argv_calls == [actions.restart_argv(TARGET)]
    assert env.lock_owner_during_run == [f"act:{approval_id}"]
    assert env.verify_calls == [("fixture-healthy", 3)]
    assert env.store.get_execution(result.execution_id)["status"] == "passed"
    assert env.state.mutation_owner is None
    assert env.notifier.resolutions[0][0] is tap
    assert "approved by @chris (1001)" in env.notifier.resolutions[0][2]
    assert any("Verified good" in n for n in env.notifier.notifications)
    assert env.notifier.request_updates[-1][0] == 1  # card updated with the outcome


def test_approve_while_legacy_plan_awaits_leaves_that_plan_pending(env):
    env.state.transition(fw.PipelineState.AWAITING_APPROVAL, plan_id="legacy", msg_id=8)
    approval_id = env.propose().approval_id

    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))

    assert result.outcome == "started"
    assert env.state.state == fw.PipelineState.AWAITING_APPROVAL
    assert env.state.get_pending() == ("legacy", 8)


def test_dashboard_decision_updates_the_card_instead_of_resolving_a_tap(env):
    approval_id = env.propose().approval_id
    env.service.decide(approval_id, approve=False, decided_by="hermes", decision=None)
    assert env.notifier.resolutions == []
    assert env.notifier.request_updates[0] == (1, "❌ Restart of <code>healthy/web</code> denied by hermes.")


# ── approve: refusals ───────────────────────────────────────────────────────────
def test_approve_while_busy_leaves_the_approval_pending(env):
    approval_id = env.propose().approval_id
    assert env.state.try_begin_mutation("zoidberg-patchnow")
    tap = _tap(approval_id)

    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=tap)

    assert result.outcome == "busy" and "zoidberg-patchnow" in result.message
    assert env.store.get_approval(approval_id)["status"] == "pending"
    assert env.argv_calls == [] and env.notifier.resolutions == []
    assert env.notifier.acknowledgements[0][0] is tap
    assert env.state.mutation_owner == "zoidberg-patchnow"


def test_approve_during_a_scan_is_busy(env):
    approval_id = env.propose().approval_id
    env.state.transition(fw.PipelineState.RUNNING)
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert result.outcome == "busy" and "scan running" in result.message
    assert env.store.get_approval(approval_id)["status"] == "pending"


def test_second_decision_gets_already_decided(env):
    approval_id = env.propose().approval_id
    env.service.decide(approval_id, approve=True, decided_by="@chris (1001)", decision=_tap(approval_id))
    tap = _tap(approval_id, by="@sam (2)")

    result = env.service.decide(approval_id, approve=True, decided_by="@sam (2)", decision=tap)

    assert result.outcome == "already_decided"
    assert "approved by @chris (1001)" in result.message
    assert env.notifier.acknowledgements[-1][0] is tap
    assert len(env.argv_calls) == 1


def test_expired_request_is_refused(env):
    approval_id = env.propose().approval_id
    env.clock.t += DEFAULT_TTL_SECONDS
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert result.outcome == "expired"
    assert env.argv_calls == [] and env.state.mutation_owner is None


def test_unknown_request_is_refused(env):
    result = env.service.decide("nope", approve=True, decided_by="x", decision=_tap("nope"))
    assert result.outcome == "unknown"
    assert env.state.mutation_owner is None


def test_deny_never_takes_the_lock_and_works_while_busy(env):
    approval_id = env.propose().approval_id
    assert env.state.try_begin_mutation("plan:p1")

    result = env.service.decide(approval_id, approve=False, decided_by="@sam (2)", decision=_tap(approval_id, False))

    assert result.outcome == "denied"
    assert env.store.get_approval(approval_id)["status"] == "denied"
    assert env.state.mutation_owner == "plan:p1"
    assert env.argv_calls == []


# ── execution failures ──────────────────────────────────────────────────────────
def test_restart_command_failure_is_reported_and_skips_verification(env):
    env.argv_result = (1, "", "no such service")
    approval_id = env.propose().approval_id
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    execution = env.store.get_execution(result.execution_id)
    assert execution["status"] == "failed" and "exit 1" in execution["reason"]
    assert env.verify_calls == []
    assert env.state.mutation_owner is None


def test_failed_verification_marks_the_execution_failed(env):
    env.verify_result = (False, "healthcheck failing")
    approval_id = env.propose().approval_id
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    execution = env.store.get_execution(result.execution_id)
    assert (execution["status"], execution["reason"]) == ("failed", "healthcheck failing")
    assert any("Restart failed" in n for n in env.notifier.notifications)


def test_target_that_became_invalid_after_approval_is_not_restarted(env):
    approval_id = env.propose().approval_id
    env.resolve_error = "healthy/web has no container"
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert env.store.get_execution(result.execution_id)["status"] == "failed"
    assert env.argv_calls == []
    assert env.state.mutation_owner is None


def test_container_swapped_since_approval_is_not_restarted(env):
    approval_id = env.propose().approval_id
    env.target = actions.Target("healthy", "web", "some-other-container")
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    execution = env.store.get_execution(result.execution_id)
    assert execution["status"] == "failed" and "container changed" in execution["reason"]
    assert env.argv_calls == []


def test_crash_in_the_worker_still_releases_the_lock(env):
    def boom(argv, timeout):
        raise RuntimeError("docker socket vanished")

    env.service._run_argv = boom
    approval_id = env.propose().approval_id
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert env.store.get_execution(result.execution_id)["status"] == "failed"
    assert env.state.mutation_owner is None


def test_spawn_failure_releases_the_lock_and_fails_the_execution(env):
    def no_threads(fn, *args):
        raise RuntimeError("can't start new thread")

    env.service._spawn = no_threads
    approval_id = env.propose().approval_id
    with pytest.raises(RuntimeError):
        env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert env.state.mutation_owner is None
    events = [e["kind"] for e in env.store.list_events(approval_id)]
    assert events[-1] == "execution.failed"


def test_lock_is_held_until_the_worker_finishes(env):
    env.run_spawned_immediately = False
    approval_id = env.propose().approval_id
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))

    assert env.state.mutation_owner == f"act:{approval_id}"
    assert env.store.get_execution(result.execution_id)["status"] == "running"
    fn, args = env.spawned[0]
    fn(*args)
    assert env.state.mutation_owner is None


# ── startup reconciliation ──────────────────────────────────────────────────────
def test_reconcile_marks_unfinished_executions_interrupted_and_says_so(env):
    env.run_spawned_immediately = False
    approval_id = env.propose().approval_id
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))

    fresh = CommandService(env.store, FakeNotifier(), fw.PipelineState(), clock=env.clock)
    interrupted = fresh.reconcile_on_startup()

    assert [r["id"] for r in interrupted] == [result.execution_id]
    assert env.store.get_execution(result.execution_id)["status"] == "interrupted"
    assert any("interrupted" in n and "fixture-healthy" in n for n in fresh._notifier.notifications)
    assert fresh._notifier.request_updates[0][0] == 1
    assert fresh.reconcile_on_startup() == []


# ── Codex review (landing 1b): a failed card send must not strand the approval ──
class FlakyNotifier(FakeNotifier):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures

    def request_approval(self, text, request_id, kind):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("https://api.telegram.org/bot123:SECRET/sendMessage timed out")
        return super().request_approval(text, request_id, kind)


def test_failed_card_send_is_reported_and_the_next_request_resends_it(env, caplog):
    flaky = FlakyNotifier(failures=1)
    env.service._notifier = flaky

    first = env.propose()

    assert not first.ok and "could not send the approval card" in first.reason
    row = env.store.get_approval(first.approval_id)
    assert row["status"] == "pending" and row["message_id"] is None
    assert any(e["kind"] == "proposal.card_failed" for e in env.store.list_events(first.approval_id))
    assert "SECRET" not in caplog.text

    retry = env.propose()

    assert retry.ok and retry.approval_id == first.approval_id and not retry.created
    assert flaky.approval_requests[0][1] == first.approval_id
    assert env.store.get_approval(first.approval_id)["message_id"] == 1


def test_existing_card_is_not_resent(env):
    env.propose()
    env.propose()
    env.propose()
    assert len(env.notifier.approval_requests) == 1


class FailingDeliveryNotifier(FakeNotifier):
    def resolve(self, decision, ack_text, resolution_text):
        raise RuntimeError("Telegram unavailable")

    def notify(self, text):
        raise RuntimeError("Telegram unavailable")

    def update_request(self, message_id, text):
        raise RuntimeError("Telegram unavailable")


def test_delivery_failure_does_not_prevent_an_approved_action(env):
    env.service._notifier = FailingDeliveryNotifier()
    approval_id = env.propose().approval_id

    result = env.service.decide(
        approval_id, approve=True, decided_by="x", decision=_tap(approval_id)
    )

    assert result.outcome == "started"
    assert env.argv_calls == [actions.restart_argv(TARGET)]
    assert env.store.get_execution(result.execution_id)["status"] == "passed"
    assert env.state.mutation_owner is None


def test_delivery_failure_does_not_block_startup_reconciliation(env):
    env.run_spawned_immediately = False
    approval_id = env.propose().approval_id
    result = env.service.decide(
        approval_id, approve=True, decided_by="x", decision=_tap(approval_id)
    )
    fresh = CommandService(
        env.store, FailingDeliveryNotifier(), fw.PipelineState(), clock=env.clock
    )

    interrupted = fresh.reconcile_on_startup()

    assert [row["id"] for row in interrupted] == [result.execution_id]
    assert env.store.get_execution(result.execution_id)["status"] == "interrupted"
