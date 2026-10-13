"""S4: /missions, /mission <id> and /cancel_mission <id> over Telegram."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest

import casa_farnsworth as fw
from missions_helpers import make_store
from notifier import FakeNotifier
from planet_express.application.mission_service import MissionService
from planet_express.missions.store import MissionStore


class FakeTg:
    chat_id = "42"


@pytest.fixture
def svc(tmp_path):
    store, _ = make_store(tmp_path)
    return MissionService(store, enabled=lambda: True)


class InlineThread:
    """Runs the target at start(); records that it was started as a daemon, never called directly."""
    started: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs, self.daemon = target, args, kwargs or {}, daemon

    def start(self):
        InlineThread.started.append(self)
        self.target(*self.args, **self.kwargs)


@pytest.fixture(autouse=True)
def inline_threads(monkeypatch):
    InlineThread.started = []
    monkeypatch.setattr(fw.threading, "Thread", InlineThread)


def send(text, svc, *, chat=42, sender=None):
    n = FakeNotifier()
    msg = {"text": text, "chat": {"id": chat}, "from": sender or {"id": 7, "username": "alice"}}
    fw.handle_message({"message": msg}, FakeTg(), n, fw.PipelineState(), None, None, missions=svc)
    return n.notifications


def make(svc, title="Why is plex slow"):
    return svc.create_mission(operator="alice", title=title, goal="g")["id"]


def test_missions_lists_active_only(svc):
    a, b = make(svc, "First"), make(svc, "Second <b>x</b>")
    svc.cancel_mission(a, operator="alice")
    (reply,) = send("/missions", svc)
    assert b in reply and "[queued/propose]" in reply and a not in reply
    assert "&lt;b&gt;" in reply and "<b>x</b>" not in reply        # Telegram HTML mode: dynamic text escaped


def test_missions_empty(svc):
    assert send("/missions", svc) == ["No active missions."]


def test_mission_summary(svc):
    mid = make(svc)
    (reply,) = send(f"/mission {mid}", svc)
    assert mid in reply and "Why is plex slow" in reply and "queued" in reply and "propose" in reply


def test_cancel_mission_queued_is_cancelled_and_attributed(svc):
    mid = make(svc)
    (reply,) = send(f"/cancel_mission {mid}", svc)
    assert "cancelled" in reply
    m = MissionStore(svc.store).get_mission(mid)
    assert m.status == "cancelled"
    assert [e for e in MissionStore(svc.store).events(mid) if e.actor == "operator:tg.7" and "cancelled" in e.body_json]


def test_cancel_mission_in_flight_only_requests(svc):
    mid = make(svc)
    MissionStore(svc.store).start_session(mid, kind="local_uid", token_hash="a" * 64, session_ttl_s=60,
                                          lease_ttl_s=60, session_uid=4000)
    (reply,) = send(f"/cancel_mission {mid}", svc)
    assert "cancel requested" in reply
    assert MissionStore(svc.store).get_mission(mid).status == "investigating"


def test_unknown_and_malformed_ids(svc):
    assert "No such mission" in send("/mission M-99", svc)[0]
    assert "No such mission" in send("/cancel_mission M-99", svc)[0]
    for text in ("/mission", "/mission nope", "/mission M-1 M-2", "/cancel_mission", "/cancel_mission ; rm"):
        (reply,) = send(text, svc)
        assert reply.startswith("Usage:")
    assert "Usage" in send("/missions extra", svc)[0]


def test_closed_mission_cancel_is_refused(svc):
    mid = make(svc)
    svc.cancel_mission(mid, operator="alice")
    assert "already cancelled" in send(f"/cancel_mission {mid}", svc)[0]


def test_only_the_configured_chat_is_obeyed(svc):
    mid = make(svc)
    assert send(f"/cancel_mission {mid}", svc, chat=999) == []
    assert send("/missions", svc, chat=999) == []
    assert MissionStore(svc.store).get_mission(mid).status == "queued"


def test_disabled_changes_nothing(tmp_path):
    store, _ = make_store(tmp_path)
    off = MissionService(store, enabled=lambda: False)
    assert send("/missions", off) == []
    assert send("/mission M-1", off) == []
    assert send("/cancel_mission M-1", off) == []


def test_no_service_changes_nothing():
    n = FakeNotifier()
    fw.handle_message({"message": {"text": "/missions", "chat": {"id": 42}}}, FakeTg(), n, fw.PipelineState())
    assert n.notifications == []


def test_secrets_in_titles_are_masked(svc):
    mid = svc.create_mission(operator="alice", title="x password=hunter2hunter2", goal="g")["id"]
    assert "hunter2hunter2" not in send(f"/mission {mid}", svc)[0] + send("/missions", svc)[0]


def test_commands_run_on_a_daemon_thread_never_the_polling_thread(svc, monkeypatch):
    ran = []
    monkeypatch.setattr(MissionService, "telegram", lambda self, text, op: ran.append(text) or "ok")

    class Deferred(InlineThread):
        def start(self):
            InlineThread.started.append(self)        # recorded, deliberately not run

    monkeypatch.setattr(fw.threading, "Thread", Deferred)
    n = FakeNotifier()
    msg = {"text": "/missions", "chat": {"id": 42}, "from": {"id": 7}}
    fw.handle_message({"message": msg}, FakeTg(), n, fw.PipelineState(), None, None, missions=svc)
    assert ran == [] and n.notifications == []             # nothing happened on the calling thread
    assert len(InlineThread.started) == 1 and InlineThread.started[0].daemon is True
    InlineThread.started[0].target(*InlineThread.started[0].args)
    assert ran == ["/missions"] and n.notifications == ["ok"]


def test_a_failing_command_does_not_kill_the_thread_silently(svc, monkeypatch):
    def boom(self, text, op):
        raise RuntimeError("db locked")
    monkeypatch.setattr(MissionService, "telegram", boom)
    (reply,) = send("/missions", svc)
    assert "went wrong" in reply and "db locked" not in reply


def _help(missions):
    n = FakeNotifier()
    fw.handle_message({"message": {"text": "/help", "chat": {"id": 42}}}, FakeTg(), n, fw.PipelineState(),
                      None, None, missions=missions)
    return n.notifications[0]


def test_help_lists_mission_commands_only_when_enabled(svc, tmp_path):
    store, _ = make_store(tmp_path)
    off = MissionService(store, enabled=lambda: False)
    baseline = _help(None)
    assert _help(off) == baseline and "/missions" not in baseline
    on = _help(svc)
    assert on.startswith(baseline)
    for cmd in ("/missions", "/mission ", "/cancel_mission"):
        assert cmd in on
