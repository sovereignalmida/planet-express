"""
Telegram wiring for typed restarts (landing 1b): /restart parsing, the action-callback
branch, and how proposal results are reported. CommandService itself is faked here;
its behavior is covered in tests/test_command_service.py.
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import Decision, FakeNotifier
from planet_express.application.command_service import (
    DecideResult,
    ProposeResult,
    RequestResult,
)


class FakeTg:
    chat_id = "42"


class FakeCommands:
    def __init__(self, propose_result=None):
        self.proposals: list[tuple] = []
        self.decisions: list[tuple] = []
        self.requests: list[tuple] = []
        self.propose_result = propose_result or ProposeResult(True, "a1b2c3d4e5f6", True, "awaiting approval")

    def propose(self, action, stack, service=None, *, requested_via, requested_by):
        self.proposals.append((action, stack, service, requested_via, requested_by))
        return self.propose_result

    def decide(self, approval_id, *, approve, decided_by, decision=None):
        self.decisions.append((approval_id, approve, decided_by, decision))
        return DecideResult("started", "ok")

    def request_action(self, action, stack, service=None, *, operator, origin):
        self.requests.append((action, stack, service, operator, origin))
        return RequestResult("started", "started", "approval", "execution", {})


class RecordingThread:
    started: ClassVar[list] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        RecordingThread.started.append((self.target, self.args))


def _message(text, sender=None):
    msg = {"text": text, "chat": {"id": 42}}
    if sender is not None:
        msg["from"] = sender
    return {"message": msg}


def _send(text, commands, monkeypatch, sender=None):
    RecordingThread.started = []
    monkeypatch.setattr(fw.threading, "Thread", RecordingThread)
    n = FakeNotifier()
    fw.handle_message(_message(text, sender), FakeTg(), n, fw.PipelineState(), commands)
    return n


def test_restart_usage_error(monkeypatch):
    n = _send("/restart healthy", FakeCommands(), monkeypatch)
    assert any("Usage: `/restart <stack> <service>`" in m for m in n.notifications)
    assert RecordingThread.started == []


def test_restart_without_command_service_is_unavailable(monkeypatch):
    n = _send("/restart healthy web", None, monkeypatch)
    assert any("unavailable" in m for m in n.notifications)


def test_restart_proposes_in_a_background_thread_with_the_sender(monkeypatch):
    commands = FakeCommands()
    n = _send("/restart healthy web", commands, monkeypatch, sender={"id": 1001, "username": "chris"})
    assert RecordingThread.started == [(fw._run_restart_request, (commands, n, "healthy", "web", "@chris (1001)"))]
    assert commands.proposals == []  # not on the poll thread


def test_restart_request_reports_a_refusal():
    n = FakeNotifier()
    commands = FakeCommands(ProposeResult(False, None, False, "stack 'ai' is forbidden"))
    fw._run_restart_request(commands, n, "ai", "web", "1001")
    assert commands.proposals == [(fw.actions.RESTART_SERVICE, "ai", "web", "telegram", "1001")]
    assert any("refused" in m and "forbidden" in m for m in n.notifications)


def test_restart_request_reports_an_existing_proposal():
    n = FakeNotifier()
    fw._run_restart_request(FakeCommands(ProposeResult(True, "abc123", False, "already")), n, "healthy", "web", "x")
    assert any("already awaiting approval" in m and "abc123" in m for m in n.notifications)


def test_new_proposal_sends_no_extra_message():
    n = FakeNotifier()
    fw._run_restart_request(FakeCommands(), n, "healthy", "web", "x")
    assert n.notifications == []  # the approval card itself is the message


def test_action_callback_goes_to_command_service_only(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda pid: (_ for _ in ()).throw(AssertionError("legacy path")))
    commands = FakeCommands()
    n = FakeNotifier()
    tap = Decision(request_id="a1b2c3d4e5f6", kind="action", approved=True, decided_by="@chris (1001)")
    n.queue_decision(tap)

    fw.handle_callback({}, FakeTg(), n, fw.PipelineState(), commands)

    assert commands.decisions == [("a1b2c3d4e5f6", True, "@chris (1001)", tap)]
    assert n.resolutions == [] and n.acknowledgements == []


def test_action_callback_without_command_service_is_acknowledged():
    n = FakeNotifier()
    n.queue_decision(Decision(request_id="a1", kind="action", approved=True))
    fw.handle_callback({}, FakeTg(), n, fw.PipelineState(), None)
    assert "unavailable" in n.acknowledgements[0][1]


def test_help_lists_restart(monkeypatch):
    n = _send("/help", FakeCommands(), monkeypatch)
    assert any("/restart `<stack>` `<service>`" in m for m in n.notifications)


@pytest.mark.parametrize("text", ["/up", "/up media extra", "/down", "/down media extra"])
def test_stack_action_usage_errors(monkeypatch, text):
    commands = FakeCommands()
    n = _send(text, commands, monkeypatch)
    assert any("Usage:" in message for message in n.notifications)
    assert RecordingThread.started == []


@pytest.mark.parametrize(("text", "action", "target"), [
    ("/up media", fw.actions.UP_STACK, "media"),
    ("/up all", fw.actions.UP_ALL, "all"),
    ("/down media", fw.actions.DOWN_STACK, "media"),
    ("/down network", fw.actions.DOWN_INGRESS, "network"),
    ("/down all", fw.actions.DOWN_ALL, "all"),
])
def test_stack_actions_route_to_typed_actions(monkeypatch, text, action, target):
    commands = FakeCommands()
    n = _send(text, commands, monkeypatch, sender={"id": 1001, "username": "chris"})
    assert RecordingThread.started == [
        (fw._run_stack_action_request, (commands, n, action, target, "@chris (1001)"))
    ]


def test_up_stack_direct_reply_names_execution(monkeypatch):
    monkeypatch.setattr(fw.policy, "allows_direct_request", lambda risk: True)
    commands = FakeCommands()
    notifier = FakeNotifier()
    fw._run_stack_action_request(commands, notifier, fw.actions.UP_STACK, "media", "chris")
    assert commands.requests == [
        (fw.actions.UP_STACK, "media", None, "chris", "telegram-direct")
    ]
    assert any("Bringing <code>media</code> up" in item and "execution" in item
               for item in notifier.notifications)


def test_approval_gated_stack_action_reports_card(monkeypatch):
    monkeypatch.setattr(fw.policy, "allows_direct_request", lambda risk: False)
    commands = FakeCommands()
    notifier = FakeNotifier()
    fw._run_stack_action_request(commands, notifier, fw.actions.DOWN_STACK, "media", "chris")
    assert commands.proposals == [(fw.actions.DOWN_STACK, "media", None, "telegram", "chris")]
    assert any("Approval card sent" in item for item in notifier.notifications)


def test_farnsworth_has_no_stackctl_up_down_calls():
    source = Path(fw.__file__).read_text()
    assert "stackctl.stack_up" not in source
    assert "stackctl.stack_down" not in source


def test_direct_refusal_quoting_user_input_is_escaped(monkeypatch):
    # Codex review, T37: `/up <x>` must not break Telegram's HTML parse of the refusal.
    monkeypatch.setattr(fw.policy, "allows_direct_request", lambda risk: True)
    commands = FakeCommands()
    commands.request_action = lambda *a, **k: RequestResult(
        "refused", "target refused: invalid stack name '<x>'", None, None, {}
    )
    notifier = FakeNotifier()
    fw._run_stack_action_request(commands, notifier, fw.actions.UP_STACK, "<x>", "chris")
    assert notifier.notifications == ["target refused: invalid stack name '&lt;x&gt;'"]


# ── T40: Telegram abort / rollback of a typed execution ────────────────────────
def test_abort_and_execution_rollback_route_to_the_command_service(monkeypatch):
    calls = []

    class Commands:
        def abort(self, execution_id, *, operator):
            calls.append(("abort", execution_id, operator))
            return SimpleNamespace(message="Abort requested.")

        def rollback(self, execution_id, *, operator):
            calls.append(("rollback", execution_id, operator))
            return SimpleNamespace(message="Rolling back.")

    notifier = FakeNotifier()
    fw._run_execution_control(Commands(), notifier, "abort", "a" * 12, "@chris (1)")
    fw._run_execution_control(Commands(), notifier, "rollback", "b" * 12, "@chris (1)")
    assert calls == [("abort", "a" * 12, "@chris (1)"), ("rollback", "b" * 12, "@chris (1)")]
    assert notifier.notifications == ["Abort requested.", "Rolling back."]


def test_legacy_plan_ids_still_use_the_legacy_rollback(monkeypatch):
    # A 12-hex id is a typed execution; anything else (p1, …) stays on the legacy plan path
    # until slice 5b-5 retires it.
    seen = []
    controls = []
    monkeypatch.setattr(fw, "_do_rollback", lambda tg, notifier, state, plan_id: seen.append(plan_id))
    monkeypatch.setattr(fw, "_run_execution_control",
                        lambda commands, notifier, control, target, operator: controls.append((control, target)))
    tg = Mock()
    tg.chat_id = "42"
    notifier = FakeNotifier()

    def send(text):
        fw.handle_message({"message": {"text": text, "chat": {"id": 42}, "from": {"id": 1}}},
                          tg, notifier, fw.PipelineState(), commands=Mock())

    send("/rollback p1")
    send("/rollback " + "a" * 12)
    send("/abort " + "b" * 12)
    for _ in range(50):
        if len(controls) >= 2:
            break
        time.sleep(0.02)
    assert seen == ["p1"]
    assert sorted(controls) == [("abort", "b" * 12), ("rollback", "a" * 12)]
