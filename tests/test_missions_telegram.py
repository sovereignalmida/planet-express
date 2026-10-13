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
    assert m.status == "cancelled" and m.cancel_requested_by == "operator:tg.7"


def test_cancel_mission_in_flight_only_requests(svc):
    from planet_express.missions.state import transition
    mid = make(svc)
    transition(svc.store, mid, "investigating", "core")
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
