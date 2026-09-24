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
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import FakeNotifier


def _state():
    return fw.PipelineState()


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


# The plan and diff approval flows went with the shell executor in slice 5b-5; a card of
# either kind now answers "no longer approvable" (tests/test_legacy_cards.py).
