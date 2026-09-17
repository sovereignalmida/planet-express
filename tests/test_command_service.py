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
        self.background_jobs: list = []
        self.run_background_immediately = True
        self.service = CommandService(
            self.store, self.notifier, self.state,
            run_argv=self._run_argv, resolve_target=self._resolve, verify=self._verify,
            restart_count=lambda container: 3, spawn=self._spawn, background=self._background,
            clock=self.clock,
        )

    def _resolve(self, stack, service, for_mutation=True, timeout=actions.DOCKER_TIMEOUT_SECONDS):
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

    def _background(self, fn):
        self.background_jobs.append(fn)
        if self.run_background_immediately:
            fn()

    def run_background_jobs(self):
        jobs, self.background_jobs = self.background_jobs, []
        for job in jobs:
            job()

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


def direct(env, action=RESTART):
    return env.service.request_action(action, 'healthy', 'web', operator='chris', timeout=4)


def assert_no_actions(env):
    with env.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM executions').fetchone()[0] == 0
    assert env.argv_calls == []


def test_direct_runs_with_approval_lock_verification_and_fyi(env):
    result = direct(env)
    assert result.outcome == 'started'
    row = env.store.get_approval(result.approval_id)
    assert row['decided_by'] == row['requested_by'] == 'chris'
    assert row['requested_via'] == 'dashboard-direct'
    assert row['status'] == 'approved' and row['message_id'] is None
    assert env.lock_owner_during_run[0].startswith('act:direct:')
    assert env.argv_calls == [actions.restart_argv(TARGET)]
    assert env.verify_calls == [('fixture-healthy', 3)]
    assert env.service.get_status(result.execution_id)['status'] == 'passed'
    assert result.capabilities == actions.REGISTRY[RESTART].capabilities()
    assert env.service.get_status(result.execution_id)['capabilities'] == result.capabilities
    assert env.state.mutation_owner is None
    assert '🛠 chris restarted <code>healthy/web</code> from the dashboard.' in env.notifier.notifications
    assert env.notifier.request_updates == env.notifier.approval_requests == []


def test_direct_busy_writes_nothing(env):
    env.state.try_begin_mutation('zoidberg')
    result = direct(env)
    assert result.outcome == 'busy' and 'zoidberg' in result.message
    assert env.state.mutation_owner == 'zoidberg'
    assert env.store.list_events() == []
    assert_no_actions(env)


def test_direct_refused_target(env):
    env.resolve_error = 'forbidden'
    assert direct(env).outcome == 'refused'
    assert_no_actions(env)
    event, = env.store.list_events()
    assert event['kind'] == 'proposal.refused'
    assert event['payload']['requested_via'] == 'dashboard-direct'


def test_direct_unknown_action_does_not_resolve(env):
    assert direct(env, 'unknown').outcome == 'refused'
    assert env.resolve_calls == []
    assert_no_actions(env)


def test_stats_cannot_be_proposed_or_requested(env):
    result = env.service.propose(actions.STATS_SERVICE, 'healthy', 'web',
                                 requested_via='telegram', requested_by='chris')
    assert not result.ok and 'runs automatically' in result.reason
    result = direct(env, actions.STATS_SERVICE)
    assert result.outcome == 'refused' and 'is a read, not an action' in result.message
    assert_no_actions(env)


@pytest.mark.parametrize('risk', ['R2', 'R3', 'R4', 'R9'])
def test_direct_risk_restriction(env, monkeypatch, risk):
    monkeypatch.setitem(actions.REGISTRY, RESTART, actions.ActionSpec(RESTART, risk, 'restart'))
    assert direct(env).outcome == 'refused'
    assert_no_actions(env)


def test_direct_and_propose_timeout_create_nothing(env):
    def timeout(stack, service, **kwargs):
        assert kwargs == {'for_mutation': True, 'timeout': 4}
        raise actions.TargetTimeout('host slow')
    env.service._resolve = timeout
    assert direct(env).outcome == 'timeout'
    result = env.service.propose(RESTART, 'healthy', 'web', requested_via='dashboard',
                                 requested_by='chris', timeout=4)
    assert not result.ok and result.reason == 'host slow, retry'
    assert env.store.list_events() == []
    assert_no_actions(env)


def test_direct_adopts_telegram_card(env):
    proposal = env.propose()
    result = direct(env)
    assert result.approval_id == proposal.approval_id
    assert 'approved by chris from the dashboard' in env.notifier.request_updates[0][1]
    assert all(update[0] == 1 for update in env.notifier.request_updates)
    with env.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 1
    tap = env.service.decide(result.approval_id, approve=True, decided_by='sam', decision=_tap(result.approval_id))
    assert tap.outcome == 'already_decided' and tap.message == 'Already approved by chris.'
    assert len(env.argv_calls) == 1


def test_direct_spawn_failure(env):
    def fail(*args):
        raise RuntimeError('cannot spawn')
    env.service._spawn = fail
    with pytest.raises(RuntimeError, match='cannot spawn'):
        direct(env)
    assert env.state.mutation_owner is None
    event = env.store.list_events()[-1]
    assert event['kind'] == 'execution.failed'
    assert env.store.get_execution(event['execution_id'])['reason'] == 'failed to start'


def test_direct_notifier_failure_does_not_block(env, caplog):
    env.service._notifier = FailingDeliveryNotifier()
    result = direct(env)
    assert result.outcome == 'started'
    assert env.store.get_execution(result.execution_id)['status'] == 'passed'
    assert 'Telegram unavailable' not in caplog.text


def test_reconcile_direct_has_no_card(env):
    env.run_spawned_immediately = False
    result = direct(env)
    assert env.state.mutation_owner.startswith('act:direct:')
    fresh = CommandService(env.store, env.notifier, fw.PipelineState())
    assert fresh.reconcile_on_startup()[0]['id'] == result.execution_id
    assert env.store.get_execution(result.execution_id)['status'] == 'interrupted'
    assert env.notifier.request_updates == []


def test_direct_waits_for_pending_card_delivery_before_adoption(env):
    entered, release = threading.Event(), threading.Event()
    original = env.notifier.request_approval
    results = {}

    def send(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    env.notifier.request_approval = send
    proposal_thread = threading.Thread(target=lambda: results.update(proposal=env.propose()))
    direct_thread = threading.Thread(target=lambda: results.update(direct=direct(env)))
    proposal_thread.start()
    try:
        assert entered.wait(1)
        direct_thread.start()
    finally:
        release.set()
        proposal_thread.join(3)
        direct_thread.join(3)
    assert not proposal_thread.is_alive() and not direct_thread.is_alive()
    assert results['proposal'].approval_id == results['direct'].approval_id
    assert len(env.notifier.approval_requests) == 1
    assert env.notifier.request_updates[0][0] == 1
    assert 'approved by chris from the dashboard' in env.notifier.request_updates[0][1]


def test_direct_store_failure_releases_lock(env, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError('storage failed')
    monkeypatch.setattr(env.store, 'create_direct_execution', fail)
    with pytest.raises(RuntimeError, match='storage failed'):
        direct(env)
    assert env.state.mutation_owner is None
    assert_no_actions(env)


# ── Codex review (T14): RPC replies never wait on Telegram ──────────────────────
def test_direct_request_acknowledges_before_the_fyi_is_sent(env):
    env.run_background_immediately = False
    result = direct(env)
    assert result.outcome == 'started' and result.execution_id
    assert env.notifier.notifications == []
    env.run_background_jobs()
    assert env.notifier.notifications[0] == '🛠 chris restarted <code>healthy/web</code> from the dashboard.'
    assert env.notifier.notifications[-1].startswith('🟢')


def test_dashboard_decision_returns_before_the_card_is_edited_and_edits_stay_in_order(env):
    proposal = env.propose()
    env.run_background_immediately = False
    result = env.service.decide(proposal.approval_id, approve=True, decided_by='chris', decision=None)
    assert result.outcome == 'started'
    assert env.notifier.request_updates == []
    env.run_background_jobs()
    texts = [text for _message_id, text in env.notifier.request_updates]
    assert 'approved by chris' in texts[0] and texts[-1].startswith('🟢')


def test_a_telegram_tap_is_still_answered_inline(env):
    proposal = env.propose()
    env.run_background_immediately = False
    env.service.decide(proposal.approval_id, approve=False, decided_by='@chris (1001)', decision=_tap(proposal.approval_id, False))
    assert env.notifier.resolutions and env.background_jobs == []


def test_direct_spawn_failure_still_reports_after_the_fyi(env):
    def broken_spawn(fn, *args):
        raise RuntimeError('thread limit')

    env.service._spawn = broken_spawn
    with pytest.raises(RuntimeError):
        direct(env)
    assert env.notifier.notifications[0].startswith('🛠 chris restarted')
    assert 'failed to start' in env.notifier.notifications[-1]
    assert env.state.mutation_owner is None


def test_direct_request_gives_up_when_a_card_send_holds_the_lock_past_the_budget(env):
    env.service._proposal_card_lock.acquire()          # a propose() mid Telegram send
    try:
        result = env.service.request_action(RESTART, 'healthy', 'web', operator='chris', timeout=0.2)
    finally:
        env.service._proposal_card_lock.release()
    assert result.outcome == 'timeout'
    assert env.state.mutation_owner is None
    assert_no_actions(env)


def test_direct_request_budget_spent_by_resolution_creates_nothing(env):
    def slow_resolve(stack, service, **kwargs):
        env.clock.t += 5
        return TARGET

    env.service._resolve = slow_resolve
    result = env.service.request_action(RESTART, 'healthy', 'web', operator='chris', timeout=4)
    assert result.outcome == 'timeout'
    assert env.state.mutation_owner is None
    assert_no_actions(env)


def test_direct_request_that_waited_on_the_database_past_its_budget_creates_nothing(env):
    original = env.store.create_direct_execution

    def contended(**kwargs):
        env.clock.t += 5                               # BEGIN IMMEDIATE waited behind another writer
        return original(**kwargs)

    env.store.create_direct_execution = contended
    result = env.service.request_action(RESTART, 'healthy', 'web', operator='chris', timeout=4)
    assert result.outcome == 'timeout'
    assert env.state.mutation_owner is None
    assert not env.service._proposal_card_lock.locked()
    assert_no_actions(env)


def test_approve_during_maintenance_stays_pending(env, monkeypatch):
    approval_id = env.propose().approval_id
    monkeypatch.setattr(
        env.state, "maintenance",
        lambda: fw.MaintenanceWindow("borg-backup", None, Path("/unused")),
    )
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert result.outcome == "busy"
    assert "maintenance: borg-backup" in result.message
    assert env.store.get_approval(approval_id)["status"] == "pending"
    assert env.argv_calls == [] and env.state.mutation_owner is None
