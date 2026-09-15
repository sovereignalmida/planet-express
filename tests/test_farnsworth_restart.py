"""
Telegram wiring for typed restarts (landing 1b): /restart parsing, the action-callback
branch, and how proposal results are reported. CommandService itself is faked here;
its behavior is covered in tests/test_command_service.py.
"""

import os
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import Decision, FakeNotifier
from planet_express.application.command_service import DecideResult, ProposeResult


class FakeTg:
    chat_id = "42"


class FakeCommands:
    def __init__(self, propose_result=None):
        self.proposals: list[tuple] = []
        self.decisions: list[tuple] = []
        self.propose_result = propose_result or ProposeResult(True, "a1b2c3d4e5f6", True, "awaiting approval")

    def propose(self, action, stack, service, *, requested_via, requested_by):
        self.proposals.append((action, stack, service, requested_via, requested_by))
        return self.propose_result

    def decide(self, approval_id, *, approve, decided_by, decision=None):
        self.decisions.append((approval_id, approve, decided_by, decision))
        return DecideResult("started", "ok")


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
