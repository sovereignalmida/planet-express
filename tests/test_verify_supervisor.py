"""The supervisor's verification worker and intent journal (slice 8 review fixes): verification runs off the poll thread
and outside the mission lock, a superseded result decides nothing, outcomes name the state reached, intents are
journaled first and closed when their effect is known."""
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor_fakes import Rig, update
from verify_fakes import build

from planet_express.missions import recovery
from planet_express.missions import supervisor as sup_mod
from planet_express.missions.state import IllegalTransition
from planet_express.missions.supervisor import Supervisor
from planet_express.missions.verify.plan import VerificationResult


def supervisor(rig, verifier=None):
    return Supervisor(rig.store, None, None, enabled=True, verifier=verifier or rig.verifier)


def passed_for(rig, mission):
    return VerificationResult(status="passed", grant_hash=mission.grant_hash, checkpoint_id=mission.checkpoint_id,
                              plan_sha256=rig.plan.hash(), at=1)


class SlowVerifier:
    """Blocks inside `run` until released, then behaves like the real one (stores a passed result if it still can)."""

    def __init__(self, rig):
        self.rig, self.started, self.release, self.calls = rig, threading.Event(), threading.Event(), 0

    def run(self, mission):
        self.calls += 1
        self.started.set()
        self.release.wait(10)
        return self.rig.verifier.run(mission.id)


def outcome(rig):
    return rig.ms.get_mission(rig.mid).outcome


def test_verification_runs_off_the_poll_thread_and_cancel_stays_responsive(tmp_path):
    rig = build(tmp_path)
    slow = SlowVerifier(rig)
    sup = supervisor(rig, slow)
    t0 = time.monotonic()
    assert sup.verify_and_close(rig.mid) == "verifying"                  # returns at once
    assert slow.started.wait(5) and time.monotonic() - t0 < 1
    # the mission stays verifying and the daemon's poll and tick are not blocked by it
    t0 = time.monotonic()
    assert sup.poll_all() == {} and sup.tick() is None
    assert rig.ms.get_mission(rig.mid).status == "verifying" and time.monotonic() - t0 < 1
    # a cancel during the long verification is applied immediately (the mission lock is free)
    t0 = time.monotonic()
    sup.cancel(rig.mid, "operator:me")
    assert time.monotonic() - t0 < 2 and rig.ms.get_mission(rig.mid).status == "cancelled"
    slow.release.set()
    sup._verifying[rig.mid].result(10)
    m = rig.ms.get_mission(rig.mid)
    assert m.status == "cancelled" and m.outcome is None                  # the finished verification decided nothing
    assert any("verify_superseded" in e.body_json for e in rig.ms.events(rig.mid))


def test_a_duplicate_request_joins_the_running_verification_and_never_completes_twice(tmp_path):
    rig = build(tmp_path)
    slow = SlowVerifier(rig)
    sup = supervisor(rig, slow)
    sup.verify_and_close(rig.mid)
    assert slow.started.wait(5)
    sup.verify_and_close(rig.mid)                                          # joins, does not start a second run
    slow.release.set()
    assert sup._verifying[rig.mid].result(10) == "completed" and slow.calls == 1
    # a restart-time re-run (or a stray duplicate job) finds the mission moved on and changes nothing
    assert sup._verify_job(rig.mid) == "completed" and slow.calls == 1
    assert outcome(rig) == "completed"
    assert sum('"to": "completed"' in e.body_json for e in rig.ms.events(rig.mid) if e.kind == "state") == 1


def test_wait_returns_the_final_state(tmp_path):
    rig = build(tmp_path)
    assert supervisor(rig).verify_and_close(rig.mid, wait=True) == "completed"


def test_a_pause_during_verification_is_left_alone(tmp_path):
    rig = build(tmp_path)
    slow = SlowVerifier(rig)
    sup = supervisor(rig, slow)
    sup.verify_and_close(rig.mid)
    assert slow.started.wait(5)
    with sqlite3.connect(rig.store.path) as conn:                         # e.g. a tripwire pause moved it
        conn.execute("UPDATE missions SET status='paused_tripwire' WHERE id=?", (rig.mid,))
    slow.release.set()
    assert sup._verifying[rig.mid].result(10) == "paused_tripwire"
    assert outcome(rig) is None


def test_the_outcome_is_the_state_reached_never_the_intended_one(tmp_path, monkeypatch):
    rig = build(tmp_path)

    def refuse(*a, **k):
        raise IllegalTransition("refused")

    monkeypatch.setattr(sup_mod, "transition", refuse)
    sup = supervisor(rig)
    assert sup.verify_and_close(rig.mid, wait=True) == "verifying"        # both edges refused: still verifying
    assert outcome(rig) is None and rig.ms.get_mission(rig.mid).outcome_flags is None


def test_a_terminal_mission_gets_no_outcome_after_a_refused_transition(tmp_path, monkeypatch):
    rig = build(tmp_path)
    real = sup_mod.transition

    def cancel_then_refuse(store, mid, to, actor):
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE missions SET status='cancelled' WHERE id=?", (mid,))
        return real(store, mid, to, actor)                                # now illegal: the mission is terminal

    monkeypatch.setattr(sup_mod, "transition", cancel_then_refuse)
    assert supervisor(rig).verify_and_close(rig.mid, wait=True) == "cancelled"
    assert outcome(rig) is None


def test_failed_verification_outcome_names_the_state_and_flags(tmp_path):
    rig = build(tmp_path)
    from verify_fakes import media_down
    rig.fresh = media_down()
    assert supervisor(rig).verify_and_close(rig.mid, wait=True) == "needs_operator"
    m = rig.ms.get_mission(rig.mid)
    assert m.outcome == "needs_operator" and m.outcome_flags == "partially_applied"


def test_completing_closes_the_missions_intents(tmp_path):
    rig = build(tmp_path)
    recovery.journal_intent(rig.ms, rig.mid, "execution", "apply", actor="supervisor")
    assert recovery.open_intents(rig.ms.events(rig.mid)) != []
    supervisor(rig).verify_and_close(rig.mid, wait=True)
    assert recovery.open_intents(rig.ms.events(rig.mid)) == []


def test_a_failed_verification_leaves_the_unknown_intent_open(tmp_path):
    rig = build(tmp_path)
    from verify_fakes import media_down
    rig.fresh = media_down()
    iid = recovery.journal_intent(rig.ms, rig.mid, "execution", "apply", actor="supervisor")
    supervisor(rig).verify_and_close(rig.mid, wait=True)
    assert recovery.open_intents(rig.ms.events(rig.mid)) == [iid]


# ── intents around the session ──────────────────────────────────────────────
def test_the_intent_is_journaled_first_and_a_failed_write_starts_nothing(tmp_path, monkeypatch):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(recovery, "journal_intent", broken)
    with pytest.raises(OSError):
        rig.sup.start(rig.mid)
    assert rig.sessions() == [] and rig.runtimes == [] and rig.status() == "queued"
    assert rig.cont.created == [] if hasattr(rig.cont, "created") else True


def test_the_intent_exists_before_the_session_row(tmp_path, monkeypatch):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    seen = {}
    real = sup_mod.issue_session

    def spy(*a, **k):
        seen["open_before_session"] = recovery.open_intents(rig.ms.events(rig.mid))
        return real(*a, **k)

    monkeypatch.setattr(sup_mod, "issue_session", spy)
    rig.sup.start(rig.mid)
    assert len(seen["open_before_session"]) == 1


def test_a_refused_session_closes_its_own_intent(tmp_path, monkeypatch):
    rig = Rig(tmp_path)

    def refuse(*a, **k):
        raise RuntimeError("no uid")

    monkeypatch.setattr(sup_mod, "issue_session", refuse)
    with pytest.raises(RuntimeError):
        rig.sup.start(rig.mid)
    assert recovery.open_intents(rig.ms.events(rig.mid)) == [] and rig.sessions() == []


def test_a_settled_observe_session_leaves_no_open_intent(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}])
    rig.sup.start(rig.mid)
    for _ in range(10):
        rig.sup.poll(rig.mid)
        if rig.status() != "investigating":
            break
    assert rig.status() == "awaiting_decision"
    assert recovery.open_intents(rig.ms.events(rig.mid)) == []
