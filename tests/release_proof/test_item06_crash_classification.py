"""Release proof item 6:

    A crash during investigation, execution and verification yields an honest resumable or recovery state with no replay of
    an unknown mutation.

Investigation is read-only, so it may be restarted within its retry budget (nothing was changed that a restart could repeat);
execution is classified from the host and never restarted; verification is re-run by Core and completes only through Core."""
import json

import pytest
from repair_fakes import build, update
from supervisor_fakes import Rig

from planet_express.missions.supervisor import Supervisor
from release_proof.support import ROOT  # noqa: F401  (sys.path)


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def notes(st, mid, name):
    return [json.loads(e.body_json) for e in st.ms.events(mid) if json.loads(e.body_json).get("event") == name]


def started_repair(st):
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) == "executing"
    return mid, st.runtimes[0].context.session


def new_supervisor(st, factory=None):
    return Supervisor(st.store, st.cont, factory, st.clock, enabled=True, artifact_dir=st.sup.artifact_dir,
                      workspace_dir=st.sup.workspace_dir, repair_gate=st.sup.repair_gate, verifier=st.verifier,
                      checkpoint=st.env.svc, tripwire=st.monitor)


# ── investigation ───────────────────────────────────────────────────────────────────────────────────

def test_a_crash_during_investigation_is_retried_only_inside_its_budget_and_never_mutates(tmp_path):
    rig = Rig(tmp_path, tier="propose", budgets={"retries": 1})
    rig.sup.start(rig.mid)
    first = rig.session_ids()[0]
    rig.cont.crash(first)
    rig.sup.poll(rig.mid)
    assert rig.status() == "investigating" and len(rig.runtimes) == 2        # a read-only restart, within the budget
    rig.cont.crash(next(s for s in rig.session_ids() if s != first))
    rig.sup.poll(rig.mid)
    assert rig.status() == "needs_operator" and len(rig.runtimes) == 2 and rig.ms.live_session(rig.mid) is None
    assert not any('"executing"' in b for b in rig.state_path())              # never touched the mutating state


def test_a_core_restart_during_investigation_kills_the_old_session_before_a_new_one_starts(tmp_path):
    rig = Rig(tmp_path, budgets={"retries": 1})
    rig.sup.start(rig.mid)
    old = rig.session_ids()[0]
    rig.sup = rig.new_supervisor()
    assert rig.sup.recover() == [rig.mid]
    assert rig.cont.kill_calls(old) >= 1 and not rig.cont.exists(old)
    assert rig.ms.live_session(rig.mid)["id"] != old


# ── execution ───────────────────────────────────────────────────────────────────────────────────────

def test_a_crash_during_execution_with_nothing_applied_is_resumable_and_not_restarted(stack):
    st = stack(behaviours=[{"script": [update()] * 4}, {"script": [update("finished")]}])
    mid, session = started_repair(st)
    st.cont.crash(session.session_id)
    st.sup.tick()
    assert st.status(mid) == "needs_operator"
    c = notes(st, mid, "recovery.classified")[0]
    assert c["offers"] == ["resume", "cancel"] and "replay" not in c["offers"]
    assert len(c["refused_replays"]) == 1                    # the intent whose effect is unknown is named, not repeated
    assert len(st.runtimes) == 1


def test_a_crash_during_execution_after_a_change_is_a_recovery_state_offering_rollback_and_never_replays(stack):
    st = stack(behaviours=[{"script": [update()] * 4}, {"script": [update("finished")]}])
    mid, session = started_repair(st)
    st.env.compose.write_text(st.env.compose.read_text() + "# half applied\n")
    st.cont.crash(session.session_id)
    st.sup.tick()
    assert st.status(mid) in ("recovery_required", "needs_operator")
    c = notes(st, mid, "recovery.classified")[0]
    assert "rollback" in c["offers"] and c["partially_applied"] is True and "replay" not in c["offers"]
    assert len(st.runtimes) == 1 and st.ms.get_mission(mid).outcome_flags == "partially_applied"


def test_a_core_restart_during_execution_classifies_from_the_host_and_never_replays(stack):
    st = stack(behaviours=[{"script": [update()] * 4}, {"script": [update("finished")]}])
    mid, _ = started_repair(st)
    launched = []
    survivor = new_supervisor(st, lambda m: launched.append(m.id))
    survivor.recover()
    for _ in range(3):
        survivor.poll_all()
    assert st.status(mid) == "needs_operator" and launched == [] and notes(st, mid, "recovery.classified")


# ── verification ────────────────────────────────────────────────────────────────────────────────────

def test_a_crash_during_verification_verifies_again_and_completes_only_through_core(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.verify_and_close = lambda *a, **k: "verifying"      # Core dies before the verifier ran
    st.sup.tick()
    assert st.status(mid) == "verifying" and st.ms.live_session(mid) is None
    fresh = new_supervisor(st)
    fresh.recover()
    assert "restarted" not in st.runtimes and len(st.runtimes) == 1          # the agent is not run again
    fresh._verifying[mid].result(10)
    assert st.status(mid) == "completed"
    assert notes(st, mid, "verify.result")


def test_a_verification_that_cannot_pass_after_a_restart_does_not_complete(stack):
    from verify_fakes import media_down
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.verify_and_close = lambda *a, **k: "verifying"
    st.sup.tick()
    st.fresh = media_down()
    fresh = new_supervisor(st)
    fresh.recover()
    fresh._verifying[mid].result(10)
    assert st.status(mid) != "completed"
