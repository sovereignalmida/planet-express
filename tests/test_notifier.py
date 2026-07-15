"""
Exercises casa_farnsworth.handle_callback's four decision cases (approve/cancel plan,
approve/cancel diff) against FakeNotifier, with no real Docker/Telegram/filesystem
side effects. Bender/Zoidberg's own direct TelegramClient usage is out of scope for
this suite -- see notifier.py's module docstring and Spec 2's plan writeup.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml"))

import casa_farnsworth as fw
from notifier import Decision, FakeNotifier


def _state():
    return fw.PipelineState()


def test_approve_plan_not_found(monkeypatch):
    monkeypatch.setattr(fw, "load_pending_plan", lambda plan_id: None)
    notifier = FakeNotifier()
    notifier.queue_decision(Decision(request_id="p1", kind="plan", approved=True))
    state = _state()

    fw.handle_callback({}, tg=None, notifier=notifier, state=state)

    assert notifier.resolutions[0][0].request_id == "p1"
    assert "approved" in notifier.resolutions[0][2]
    assert any("not found or expired" in n for n in notifier.notifications)
    assert state.state == fw.PipelineState.IDLE


def test_approve_plan_found_spawns_execution(monkeypatch):
    plan_data = {"id": "p2", "steps": []}
    monkeypatch.setattr(fw, "load_pending_plan", lambda plan_id: plan_data)

    spawned = {}
    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            spawned["target"] = target
            spawned["args"] = args
        def start(self):
            spawned["started"] = True
    monkeypatch.setattr(fw.threading, "Thread", FakeThread)

    notifier = FakeNotifier()
    notifier.queue_decision(Decision(request_id="p2", kind="plan", approved=True))
    state = _state()

    fw.handle_callback({}, tg="fake-tg", notifier=notifier, state=state)

    assert notifier.resolutions[0][1] == "Good news, everyone! Executing..."
    assert state.state == fw.PipelineState.EXECUTING
    assert spawned["target"] is fw._execute_plan
    assert spawned["args"] == ("fake-tg", notifier, state, plan_data)
    assert spawned["started"]


def test_cancel_plan(monkeypatch):
    notifier = FakeNotifier()
    notifier.queue_decision(Decision(request_id="p3", kind="plan", approved=False))
    state = _state()
    state.transition(fw.PipelineState.AWAITING_APPROVAL, plan_id="p3")

    fw.handle_callback({}, tg=None, notifier=notifier, state=state)

    assert notifier.resolutions[0] == (
        notifier.resolutions[0][0], "Plan cancelled.", "❌ Plan #p3 *cancelled*."
    )
    assert state.state == fw.PipelineState.IDLE


def test_approve_diff(monkeypatch):
    monkeypatch.setattr(
        fw.bender, "apply_pending_diff",
        lambda diff_id: {"backup_path": "/tmp/fake-backup.bak"},
    )
    notifier = FakeNotifier()
    notifier.queue_decision(Decision(request_id="d1", kind="diff", approved=True))
    state = _state()

    fw.handle_callback({}, tg=None, notifier=notifier, state=state)

    assert notifier.resolutions[0][1] == "Applying diff..."
    assert any("Applied" in n for n in notifier.notifications)


def test_cancel_diff(monkeypatch):
    discarded = {}
    monkeypatch.setattr(
        fw.bender, "discard_pending_diff",
        lambda diff_id: discarded.setdefault("id", diff_id),
    )
    notifier = FakeNotifier()
    notifier.queue_decision(Decision(request_id="d2", kind="diff", approved=False))
    state = _state()

    fw.handle_callback({}, tg=None, notifier=notifier, state=state)

    assert notifier.resolutions[0][1] == "Diff discarded."
    assert discarded["id"] == "d2"
