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
from tests.binding_fakes import FakeBinder

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
            clock=self.clock, binder=FakeBinder(),
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


def _stack_service(env, *, up=(True, "up verified"), down=(True, "down verified")):
    env.service._resolve_stack = lambda action, stack, approved_target=None: (
        actions.AllStacksTarget(tuple(
            (approved_target or {"stacks": ["network", "media", "archive"]})["stacks"]
        ))
        if action in actions.ALL_STACK_ACTIONS else actions.StackTarget(stack)
    )
    env.service._verify_stack_up = lambda stack: up
    env.service._verify_stack_down = lambda stack: down


def test_up_stack_is_telegram_direct_and_audited(env):
    _stack_service(env)
    result = env.service.request_action(
        actions.UP_STACK, "media", operator="@chris", origin="telegram-direct", timeout=4,
    )
    assert result.outcome == "started"
    approval = env.store.get_approval(result.approval_id)
    assert approval["requested_via"] == "telegram-direct"
    assert env.argv_calls == [actions.stack_argv(actions.UP_STACK, "media")]
    status = env.service.get_status(result.execution_id)
    assert status["action"] == actions.UP_STACK
    assert status["target"] == {"stack": "media"}
    assert status["summary"] == "Bring media up"


def test_stack_proposals_have_expected_risks(env):
    _stack_service(env)
    for action, target, risk in (
        (actions.DOWN_STACK, "media", "R2"),
        (actions.UP_ALL, "all", "R2"),
        (actions.DOWN_ALL, "all", "R3"),
        (actions.DOWN_INGRESS, "network", "R3"),
    ):
        result = env.service.propose(
            action, target, requested_via="telegram", requested_by="chris",
        )
        assert result.ok
        assert env.store.get_approval(result.approval_id)["risk"] == risk


def test_stack_all_stops_after_first_failed_verification(env):
    _stack_service(env)
    env.service._verify_stack_up = lambda stack: (
        (True, "ok") if stack == "network" else (False, "unhealthy")
    )
    proposal = env.service.propose(
        actions.UP_ALL, "all", requested_via="telegram", requested_by="chris",
    )
    result = env.service.decide(proposal.approval_id, approve=True, decided_by="chris")
    assert result.outcome == "started"
    assert env.argv_calls == [
        actions.stack_argv(actions.UP_ALL, "network"),
        actions.stack_argv(actions.UP_ALL, "media"),
    ]
    status = env.service.get_status(result.execution_id)
    assert status["status"] == "failed"
    assert "passed: network" in status["reason"]
    assert "failed: media (unhealthy)" in status["reason"]
    assert "not attempted: archive" in status["reason"]


def test_stack_set_change_fails_before_any_command(env):
    _stack_service(env)
    proposal = env.service.propose(
        actions.UP_ALL, "all", requested_via="telegram", requested_by="chris",
    )

    def changed(action, stack, approved_target=None):
        if approved_target is not None:
            raise actions.TargetError("stack set changed since approval")
        return actions.AllStacksTarget(("network", "media"))

    env.service._resolve_stack = changed
    result = env.service.decide(proposal.approval_id, approve=True, decided_by="chris")
    status = env.service.get_status(result.execution_id)
    assert status["status"] == "failed"
    assert status["reason"] == "target no longer valid: stack set changed since approval"
    assert env.argv_calls == []


def test_r1_removed_from_direct_risks_can_be_proposed(env, monkeypatch):
    import config
    from config_schema import AutonomyConfig

    _stack_service(env)
    monkeypatch.setattr(config, "AUTONOMY", AutonomyConfig(direct_request_risks=[]))
    direct_result = env.service.request_action(
        actions.UP_STACK, "media", operator="chris", origin="telegram-direct",
    )
    assert direct_result.outcome == "refused"
    proposal = env.service.propose(
        actions.UP_STACK, "media", requested_via="telegram", requested_by="chris",
    )
    assert proposal.ok and proposal.created


def test_stack_policy_is_rechecked_before_execution(env, monkeypatch):
    import config
    from config_schema import AutonomyConfig

    _stack_service(env)
    proposal = env.service.propose(
        actions.DOWN_STACK, "media", requested_via="telegram", requested_by="chris",
    )
    monkeypatch.setattr(
        config, "AUTONOMY",
        AutonomyConfig(forbidden_risks=["R2", "R4"], direct_request_risks=["R1"]),
    )
    result = env.service.decide(proposal.approval_id, approve=True, decided_by="chris")
    assert result.outcome == "refused"
    assert env.store.get_approval(proposal.approval_id)["status"] == "denied"
    assert env.argv_calls == []


def test_stack_action_busy_writes_nothing(env):
    _stack_service(env)
    env.state.try_begin_mutation("scan")
    result = env.service.request_action(
        actions.UP_STACK, "media", operator="chris", origin="telegram-direct",
    )
    assert result.outcome == "busy"
    assert_no_actions(env)


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
        assert kwargs['for_mutation'] is True and 0 < kwargs['timeout'] <= 4
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


def _seed_attempts(env, ages):
    now = env.clock.t
    for age in ages:
        env.clock.t = now - age
        env.store.create_direct_execution(
            action=RESTART, target_key=TARGET.key, target=TARGET.as_dict(), risk='R1',
            operator='chris', arrived_at=env.clock.t,
        )
    env.clock.t = now


@pytest.mark.parametrize(('ages', 'reason'), [
    ([60], 'cooling down: last attempt 1m ago, cooldown 30m'),
    ([2000, 4000, 6000], 'attempt cap reached: 3 in 24h (max 3)'),
])
def test_incident_limits_refuse_without_row_or_card(env, ages, reason):
    _seed_attempts(env, ages)
    with env.store._connect() as conn:
        before = conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0]
    result = env.propose(via='incident')
    assert not result.ok and result.approval_id is None and not result.created
    assert result.reason == reason
    assert env.notifier.approval_requests == []
    with env.store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == before
    event = env.store.list_events()[-1]
    assert event['kind'] == 'proposal.refused'
    assert event['payload']['reason'] == reason
    assert event['payload']['requested_via'] == 'incident'


def test_incident_allowed_when_limits_clear(env):
    _seed_attempts(env, [60, 120, 180])
    env.clock.t += 86400
    result = env.propose(via='incident')
    assert result.ok and result.created
    assert len(env.notifier.approval_requests) == 1


@pytest.mark.parametrize('origin', ['telegram', 'dashboard', 'dashboard-direct'])
def test_operator_proposals_bypass_limits(env, origin):
    _seed_attempts(env, [0] * 5)
    assert env.propose(via=origin).ok
    assert len(env.notifier.approval_requests) == 1


def test_direct_requests_bypass_limits(env):
    _seed_attempts(env, [0] * 5)
    assert direct(env).outcome == 'started'


def test_direct_request_uses_configured_risks(env, monkeypatch):
    import config
    from config_schema import AutonomyConfig

    monkeypatch.setattr(config, 'AUTONOMY', AutonomyConfig(direct_request_risks=[]))
    result = direct(env)
    assert result.outcome == 'refused'
    assert result.message == 'direct requests are not allowed for R1'
    assert env.store.list_pending() == []
    assert env.spawned == []



def test_incident_cooldown_longer_than_a_day_is_enforced(env, monkeypatch):
    import config
    from config_schema import AutonomyConfig

    monkeypatch.setattr(config, 'AUTONOMY', AutonomyConfig(cooldown_seconds=48 * 3600))
    _seed_attempts(env, [25 * 3600])            # outside the 24h cap window, inside the 48h cooldown
    result = env.propose(via='incident')
    assert not result.ok
    assert result.reason.startswith('cooling down: last attempt 1500m ago')
    assert env.notifier.approval_requests == []


def test_pending_approval_is_refused_when_its_risk_becomes_forbidden(env, monkeypatch):
    import config
    from config_schema import AutonomyConfig

    proposed = env.propose()
    assert proposed.ok
    monkeypatch.setattr(config, 'AUTONOMY', AutonomyConfig(forbidden_risks=['R1', 'R4'], direct_request_risks=[]))
    tap = _tap(proposed.approval_id)
    result = env.service.decide(proposed.approval_id, approve=True, decided_by='@chris (1001)', decision=tap)

    assert result.outcome == 'refused'
    assert 'refused by current policy' in result.message and 'never allowed' in result.message
    assert env.spawned == [] and env.argv_calls == []
    assert env.state.mutation_owner is None
    row = env.store.get_approval(proposed.approval_id)
    assert row['status'] == 'denied' and row['decided_by'] == 'policy'
    assert env.store.list_events()[-1]['kind'] == 'approval.refused_by_policy'
    assert env.notifier.resolutions and env.notifier.resolutions[-1][1] == 'Refused by policy.'
    # a second tap can't resurrect it
    again = env.service.decide(proposed.approval_id, approve=True, decided_by='@chris (1001)', decision=tap)
    assert again.outcome == 'already_decided'


def test_deny_still_works_for_a_newly_forbidden_risk(env, monkeypatch):
    import config
    from config_schema import AutonomyConfig

    proposed = env.propose()
    monkeypatch.setattr(config, 'AUTONOMY', AutonomyConfig(forbidden_risks=['R1', 'R4'], direct_request_risks=[]))
    result = env.service.decide(proposed.approval_id, approve=False, decided_by='@chris (1001)')
    assert result.outcome == 'denied'


def test_stack_compose_failure_fails_fast_redacted_and_releases_lock(env):
    # Own review, T37: a failing `docker compose up -d` must fail with compose's error rather
    # than waiting out verification, and that text reaches Telegram, so it is redacted.
    _stack_service(env)
    verified = []
    env.service._verify_stack_up = lambda stack: verified.append(stack) or (True, "ok")
    env.argv_result = (1, "", "pull access denied; API_KEY=hunter2secret")
    result = env.service.request_action(
        actions.UP_STACK, "media", operator="chris", origin="telegram-direct",
    )
    status = env.service.get_status(result.execution_id)
    assert status["status"] == "failed"
    assert "compose exited 1" in status["reason"]
    assert "hunter2secret" not in status["reason"]
    assert verified == []
    assert env.state.mutation_owner is None


def test_stack_verifier_crash_fails_and_releases_lock(env):
    _stack_service(env)

    def boom(stack):
        raise RuntimeError("docker went away")

    env.service._verify_stack_down = boom
    proposal = env.service.propose(
        actions.DOWN_STACK, "media", requested_via="telegram", requested_by="chris",
    )
    result = env.service.decide(proposal.approval_id, approve=True, decided_by="chris")
    status = env.service.get_status(result.execution_id)
    assert status["status"] == "failed" and "crashed" in status["reason"]
    assert env.state.mutation_owner is None


# ── T39: every typed action is a stored, hashed one-step runbook run by the engine ─────────
def test_proposal_stores_a_verified_plan_with_server_side_origin(env):
    import json as _json

    from planet_express.execution import runbook as runbooks
    result = env.propose(via="incident", by="alice")
    row = env.store.get_approval(result.approval_id)
    plan = runbooks.load_stored_plan(row["plan_json"], row["plan_sha256"])
    assert row["origin"] == "incident"
    assert "origin" not in _json.loads(row["plan_json"])
    assert [step.type for step in plan.steps] == ["service.restart"]
    assert plan.steps[0].binding["container"] == "fixture-healthy"


def test_tampered_plan_is_refused_and_nothing_runs(env):
    approval_id = env.propose().approval_id
    with env.store._write() as conn:
        conn.execute("UPDATE approvals SET plan_json = replace(plan_json, 'healthy', 'unhealthy') "
                     "WHERE id = ?", (approval_id,))
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    assert result.outcome == "refused" and "fingerprint" in result.message
    assert env.store.get_approval(approval_id)["status"] == "denied"
    assert env.argv_calls == [] and env.state.mutation_owner is None


def test_drifted_pending_card_answers_already_awaiting(env):
    first = env.propose()
    env.service._binder.container_ids["fixture-healthy"] = "fedcba987654"  # recreated since
    again = env.propose()
    assert again.ok and not again.created and again.approval_id == first.approval_id
    assert again.reason == "already awaiting approval"


def test_status_carries_steps_and_a_recreated_container_is_refused(env):
    approval_id = env.propose().approval_id
    env.service._binder.container_ids["fixture-healthy"] = "fedcba987654"
    result = env.service.decide(approval_id, approve=True, decided_by="x", decision=_tap(approval_id))
    status = env.service.get_status(result.execution_id)
    assert status["status"] == "failed" and "recreated since approval" in status["reason"]
    assert [(s["type"], s["status"], s["effect"], s["label"]) for s in status["steps"]] == [
        ("service.restart", "failed", "not_applied", "Restart healthy/web")]
    assert env.argv_calls == []


def test_startup_and_status_survive_non_legacy_runbook_approvals(env):
    # T39 VM rehearsal: an interrupted execution of an approval whose action is not a legacy typed
    # action (planner runbooks from 5b-2; a rehearsal row) crashed core at startup with KeyError.
    from planet_express.execution import runbook as runbooks
    plan = runbooks.Runbook.model_validate({"title": "Resync VPN port forward", "artifacts": {},
                                            "steps": [{"type": "wait", "params": {"seconds": 1},
                                                       "binding": {}}]})
    row, _ = env.store.propose_runbook(
        action="runbook", target_key="vpn", target={"title": plan.title}, risk="R0",
        requested_via="planner", plan_json=runbooks.canonical_json(plan),
        plan_sha256=runbooks.plan_sha256(plan), origin="planner",
    )
    execution = env.store.approve_and_create_execution(row["id"], decided_by="x", arrived_at=env.clock())
    assert env.service.get_status(execution["id"])["summary"] == "Resync VPN port forward"
    fresh = CommandService(env.store, FakeNotifier(), fw.PipelineState(), clock=env.clock)
    interrupted = fresh.reconcile_on_startup()
    assert [r["id"] for r in interrupted] == [execution["id"]]
    assert any("Resync VPN port forward" in n for n in fresh._notifier.notifications)
