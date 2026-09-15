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


# ── Landing 1b: typed-action callbacks, decided_by, update_request ──────────────
import logging

from notifier import TelegramNotifier
from telegram_client import TelegramClient


class _FakeTelegram:
    chat_id = "42"

    def __init__(self, fail_edit=False):
        self.sent, self.edits, self.answers = [], [], []
        self.fail_edit = fail_edit

    def send(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))
        return {"message_id": 77}

    def edit(self, message_id, text, **kwargs):
        if self.fail_edit:
            raise RuntimeError("https://api.telegram.org/bot123:SECRET-TOKEN/editMessageText 400")
        self.edits.append((message_id, text))

    def answer_callback(self, callback_query_id, text=""):
        self.answers.append((callback_query_id, text))


def _callback(data, sender=None):
    cb = {"id": "cb1", "data": data, "message": {"message_id": 5, "chat": {"id": 42}}}
    if sender is not None:
        cb["from"] = sender
    return {"callback_query": cb}


def test_action_request_uses_the_act_keyboard():
    client = _FakeTelegram()
    n = TelegramNotifier(client)
    assert n.request_approval("restart healthy/web?", "a1b2c3d4e5f6", "action") == 77
    markup = client.sent[0][1]
    assert markup == TelegramClient.act_keyboard("a1b2c3d4e5f6")
    datas = [b["callback_data"] for b in markup["inline_keyboard"][0]]
    assert datas == ["act_ok:a1b2c3d4e5f6", "act_no:a1b2c3d4e5f6"]
    assert all(len(d.encode()) <= 64 for d in datas)


def test_act_ok_is_an_approved_action_decision_with_username():
    n = TelegramNotifier(_FakeTelegram())
    d = n.interpret_decision(_callback("act_ok:a1b2", {"id": 1001, "username": "chris"}))
    assert (d.kind, d.approved, d.request_id, d.decided_by) == ("action", True, "a1b2", "@chris (1001)")


def test_act_no_without_username_records_the_numeric_id():
    n = TelegramNotifier(_FakeTelegram())
    d = n.interpret_decision(_callback("act_no:a1b2", {"id": 1002}))
    assert (d.kind, d.approved, d.decided_by) == ("action", False, "1002")


def test_legacy_plan_callbacks_still_parse_and_now_carry_decided_by():
    n = TelegramNotifier(_FakeTelegram())
    d = n.interpret_decision(_callback("approve:p1", {"id": 7, "username": "sam"}))
    assert (d.kind, d.approved, d.decided_by) == ("plan", True, "@sam (7)")


def test_update_request_edits_by_message_id():
    client = _FakeTelegram()
    TelegramNotifier(client).update_request(77, "Approved by @chris")
    assert client.edits == [(77, "Approved by @chris")]


def test_update_request_without_message_id_is_a_no_op():
    client = _FakeTelegram()
    TelegramNotifier(client).update_request(None, "x")
    assert client.edits == []


def test_update_request_swallows_edit_failures_without_logging_the_token(caplog):
    with caplog.at_level(logging.WARNING, logger="planetexpress.notifier"):
        TelegramNotifier(_FakeTelegram(fail_edit=True)).update_request(77, "x")
    assert "SECRET-TOKEN" not in caplog.text
    assert "Failed to update request message" in caplog.text


def test_fake_notifier_records_request_updates():
    n = FakeNotifier()
    n.update_request(9, "Denied by @sam")
    assert n.request_updates == [(9, "Denied by @sam")]
