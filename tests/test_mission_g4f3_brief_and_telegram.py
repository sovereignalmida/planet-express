"""G4-F3 #14 (incident evidence reaches the brief), #16 (tier-specific instructions), #15 (/rollback M-<n>)."""
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build
from supervisor_fakes import Rig

import casa_farnsworth as fw
from planet_express.missions.brief import INSTRUCTIONS, build_brief


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, start_monitor=False, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


# ── #14 incident evidence reaches the brief ─────────────────────────────────────────────────────────

def test_incident_evidence_reaches_the_brief(tmp_path):
    rig = Rig(tmp_path)
    rig.ms.append_event(rig.mid, "note", "operator:alice", {"incident_evidence": {
        "id": "c" * 12, "kind": "unhealthy", "resource": "media/plex", "summary": "plex is unhealthy",
        "events": ["health check failed token=hunter2hunter2"]}})
    rig.ms.append_event(rig.mid, "note", "worker", {"incident_evidence": {"summary": "FORGED by the worker"}})
    rig.sup.start(rig.mid)
    brief = rig.sup._runs[rig.mid].brief
    assert "plex is unhealthy" in brief and "media/plex" in brief and "health check failed" in brief
    assert "hunter2hunter2" not in brief and "FORGED" not in brief
    assert "(none provided)" not in brief


def test_a_mission_without_incident_evidence_still_says_none(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.start(rig.mid)
    assert "(none provided)" in rig.sup._runs[rig.mid].brief


# ── #16 a Repair brief carries Repair instructions ──────────────────────────────────────────────────

def test_a_repair_brief_does_not_say_observe_and_propose_only(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    text = build_brief(st.ms.get_mission(mid), [], []).text
    assert "observe and propose only" not in text and INSTRUCTIONS not in text
    assert "only what the grant's scope names" in text and "verification" in text


def test_observe_and_propose_briefs_keep_their_instructions(tmp_path):
    rig = Rig(tmp_path)
    assert INSTRUCTIONS in build_brief(rig.mission, [], []).text


# ── #15 /rollback M-<n> goes to MissionService, /rollback <execution> stays v3 ─────────────────────

def _send(monkeypatch, text, missions):
    seen = []
    monkeypatch.setattr(fw, "_run_mission_command", lambda missions, notifier, text, operator: seen.append(text))
    monkeypatch.setattr(fw, "_run_execution_control", lambda *a: seen.append(("v3", a[2], a[3])))
    tg = Mock()
    tg.chat_id = "42"
    notifier = Mock()
    fw.handle_message({"message": {"text": text, "chat": {"id": 42}, "from": {"id": 1}}}, tg, notifier,
                      fw.PipelineState(), commands=Mock(), missions=missions)
    for _ in range(100):
        if seen or notifier.notify.called:
            break
        time.sleep(0.02)
    return seen, notifier


def _missions(enabled=True):
    missions = Mock()
    missions.enabled.return_value = enabled
    missions.TELEGRAM_COMMANDS = frozenset({"/rollback"})
    return missions


def test_rollback_with_a_mission_id_is_a_mission_command(monkeypatch):
    seen, _ = _send(monkeypatch, "/rollback M-12", _missions())
    assert seen == ["/rollback M-12"]


def test_rollback_with_an_execution_id_stays_the_v3_handler(monkeypatch):
    seen, _ = _send(monkeypatch, "/rollback " + "a" * 12, _missions())
    assert seen == [("v3", "rollback", "a" * 12)]


def test_a_mission_id_with_missions_disabled_is_not_a_v3_execution(monkeypatch):
    seen, notifier = _send(monkeypatch, "/rollback M-12", _missions(enabled=False))
    assert seen == [] and "execution_id" in notifier.notify.call_args[0][0]


def test_help_text_matches_the_two_rollbacks(monkeypatch):
    _, notifier = _send(monkeypatch, "/help", _missions())
    text = notifier.notify.call_args[0][0]
    assert "/rollback `<execution_id>`" in text and "/rollback `M-<n>`" in text
