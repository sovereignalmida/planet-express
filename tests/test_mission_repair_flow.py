"""A Repair mission end to end over fakes (slice 7a): approval -> checkpoint -> session -> tripwire -> post capture ->
Core verification -> a state decided by Core, never by the agent."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import GOOD_RESULT, build, update
from tripwire_fakes import touch
from verify_fakes import media_down

from planet_express.missions import supervisor as sup_mod
from planet_express.missions.supervisor import RepairNotReady, RepairRefused, ResumeRefused, UnsupportedTier


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


def events(st, mid):
    return [(e.kind, e.actor, json.loads(e.body_json)) for e in st.ms.events(mid)]


def noted(st, mid, name):
    return [b for k, a, b in events(st, mid) if b.get("event") == name]


def states(st, mid):
    return [b["to"] for k, a, b in events(st, mid) if k == "state" and "to" in b and not b.get("refused")]


def test_an_approved_repair_runs_to_completion_through_core_verification(stack):
    st = stack()
    made = st.propose()
    mid = made["mission_id"]
    assert made["status"] == "awaiting_decision" and not made["unattended"]
    assert st.drive(mid, 2) == "awaiting_decision" and st.runtimes == []        # nothing starts without an approval
    st.approve_all(mid)
    assert st.drive(mid) == "completed"
    mission = st.ms.get_mission(mid)
    assert mission.checkpoint_id and mission.outcome == "completed"
    # the order Core promises: checkpoint, baseline, session, post capture, verification, completion
    names = [b.get("event") for k, a, b in events(st, mid)]
    assert names.index("checkpoint.created") < names.index("tripwire.baseline") < names.index("checkpoint.post_captured") \
        < names.index("verify.result")
    assert states(st, mid)[-4:] == ["awaiting_decision", "executing", "verifying", "completed"]
    assert not st.cont.exists(st.runtimes[0].context.session.session_id)      # the session's cgroup is gone
    assert st.ms.live_session(mid) is None


def test_a_session_that_claims_success_but_fails_verification_never_completes(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()                         # the checks pass but a NEW finding appears in scope: not repaired
    st.run_log.script.clear()
    assert st.drive(mid) in ("needs_operator", "rollback_pending")
    mission = st.ms.get_mission(mid)
    assert mission.status == "needs_operator" and mission.outcome == "needs_operator"
    assert mission.outcome_flags == "partially_applied"
    assert noted(st, mid, "verify.result")[0]["status"] == "failed"


def test_on_verify_fail_rollback_goes_to_rollback_pending(stack):
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    assert st.ms.get_mission(mid).outcome == "rollback_pending"


def test_a_pending_rollback_is_restored_by_an_operator_and_the_restore_uses_the_operators_identity(stack):
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    compose = st.env.compose
    original = compose.read_text()
    compose.write_text(original + "# changed by the session\n")
    out = st.flow.roll_back("operator:alice", mid)
    assert out["status"] == "rolled_back" and compose.read_text() == original
    started = noted(st, mid, "checkpoint.restore_started")
    assert started and all(a == "operator:alice" for k, a, b in events(st, mid) if b.get("event", "").startswith("checkpoint.restore"))


def test_the_acknowledged_drift_restore_carries_the_authenticated_operator_never_core(stack):
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    seen = []
    original = st.env.svc.restore
    st.env.svc.restore = lambda cid, **kw: (seen.append(kw), original(cid, **kw))[1]
    st.flow.roll_back("operator:alice", mid, ack_drift=True)
    assert seen == [{"ack_drift": True, "actor": "operator:alice"}]


def test_the_actor_is_never_core_for_an_acknowledged_drift_restore(stack):
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    st.drive(mid)
    with pytest.raises(ValueError):
        st.sup.roll_back(mid, "core", ack_drift=True)
    with pytest.raises(ValueError):
        st.sup.roll_back(mid, "worker", ack_drift=True)


def test_a_tripwire_pause_raises_a_card_and_roll_back_restores(stack):
    st = stack(behaviours=[{"script": [update(), update(), update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) == "executing"
    compose = st.env.compose
    original = compose.read_text()
    compose.write_text(original + "# the agent edited this\n")
    touch(st.rig.fs / "etc" / "hosts", "10.0.0.1 evil\n")          # a protected change
    st.monitor.scan_once()
    assert st.status(mid) == "paused_tripwire"
    card = st.flow.card(mid)
    assert card["tripwire"]["cause"] in ("protected", "out_of_scope") and card["tripwire"]["rollback"]["available"]
    session = st.ms.get_mission(mid) and st.runtimes[0].context.session
    assert st.cont._groups[session.session_id].frozen
    st.sup.tick()                                                  # a paused mission's session is left alone, not killed
    assert st.status(mid) == "paused_tripwire" and st.cont.exists(session.session_id)
    out = st.flow.roll_back("operator:alice", mid)
    assert out["status"] == "rolled_back" and compose.read_text() == original
    st.sup.tick()
    assert st.ms.live_session(mid) is None


def test_resume_reopens_the_broker_and_the_proxy_after_reauthorising_and_then_thaws(stack):
    from planet_express.missions.tools import ToolBroker, ToolSite, default_registry
    st = stack(behaviours=[{"script": [update()] * 6}], with_proxy=True)
    broker = ToolBroker(default_registry(), missions=st.ms, secrets=lambda r: "k", sites={"x": ToolSite()},
                        stacks_root=str(st.env.stacks))
    st.sup.tool_broker = broker
    original = st.monitor.revoke_endpoints
    st.monitor.revoke_endpoints = lambda mid: (broker.revoke(mid), original(mid))
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    grant_hash = st.ms.get_mission(mid).grant_hash
    touch(st.rig.fs / "etc" / "hosts", "10.0.0.1 changed\n")
    st.monitor.scan_once()
    assert st.status(mid) == "paused_tripwire" and mid in broker._revoked and not st.proxy.usable(mid, grant_hash)
    session = st.runtimes[0].context.session
    st.flow.resume("operator:alice", mid)
    assert st.status(mid) == "executing" and mid not in broker._revoked
    assert st.proxy.usable(mid, grant_hash)                                  # a resumed Repair worker can still use its proxy
    assert not st.cont._groups[session.session_id].frozen                    # thawed last
    assert noted(st, mid, "mission.resumed")
    st.monitor.scan_once()
    assert st.status(mid) == "executing"                                     # the reviewed state is the new baseline


def test_a_resume_whose_proxy_cannot_be_reopened_stays_paused_frozen_and_closed(stack):
    st = stack(behaviours=[{"script": [update()] * 4}], with_proxy=True)
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    session = st.runtimes[0].context.session

    def boom(mission_id, grant_hash):
        raise RuntimeError("proxy down")
    st.proxy.resume = boom
    with pytest.raises(ResumeRefused):
        st.sup.resume(mid, "operator:alice")
    assert st.status(mid) == "paused_tripwire" and st.cont._groups[session.session_id].frozen


def test_resume_is_refused_when_the_approval_no_longer_covers_the_grant(stack):
    import sqlite3
    st = stack(behaviours=[{"script": [update()] * 4}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    with sqlite3.connect(st.store.path) as conn:
        conn.execute("DELETE FROM mission_approvals WHERE mission_id=?", (mid,))
    with pytest.raises(ResumeRefused, match="approval"):
        st.sup.resume(mid, "operator:alice")
    assert st.status(mid) == "paused_tripwire"


def test_a_resume_needs_a_supervised_session_so_a_core_restart_refuses_it(stack):
    st = stack(behaviours=[{"script": [update()] * 4}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    st.sup._runs.clear()                                                    # as after a restart
    with pytest.raises(ResumeRefused, match="restart"):
        st.sup.resume(mid, "operator:alice")


def test_take_over_stops_the_worker_and_hands_the_host_to_the_operator(stack):
    st = stack(behaviours=[{"script": [update()] * 4}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    session = st.runtimes[0].context.session
    st.flow.take_over("operator:alice", mid)
    assert st.status(mid) == "needs_operator" and not st.cont.exists(session.session_id)
    assert any(k == "takeover" and a == "operator:alice" for k, a, b in events(st, mid))


def test_cancel_works_on_a_paused_mission(stack):
    st = stack(behaviours=[{"script": [update()] * 4}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    st.sup.cancel(mid, "operator:alice")
    assert st.status(mid) == "cancelled" and st.ms.live_session(mid) is None


def test_wall_clock_does_not_run_while_a_mission_is_paused(stack):
    st = stack(behaviours=[{"script": [update()] * 10}])
    mid = st.propose(budgets={"wall_s": 100})["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.clock.t += 30
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    for _ in range(40):                                                     # paused for 800 s, the supervisor ticking on
        st.clock.t += 20
        st.sup.tick()
    assert st.status(mid) == "paused_tripwire"
    st.flow.resume("operator:alice", mid)
    st.sup.tick()
    assert st.status(mid) == "executing"                                    # the pause was not session time


# ── the start gates ─────────────────────────────────────────────────────────────────────────────────

def test_a_repair_launch_is_refused_while_the_monitor_is_not_running(stack):
    st = stack(start_monitor=False)
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    with pytest.raises(RepairNotReady, match="monitor"):
        st.sup.start(mid)
    st.sup.tick()
    assert st.status(mid) == "awaiting_decision" and st.runtimes == []
    waiting = [b for k, a, b in events(st, mid) if "start_waiting" in b]
    assert len(waiting) == 1 and "monitor" in waiting[0]["start_waiting"]       # noted once, not every tick
    st.monitor.start()
    assert st.drive(mid) == "completed"


def test_a_dead_monitor_thread_is_restarted_by_the_runner_loop_and_pauses_executing_missions(stack):
    from planet_express.missions.runner import MissionRunner
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) == "executing" and st.monitor.running
    st.monitor.stop()
    st.monitor._thread.join(5)
    st.monitor._stop.clear()                                               # a thread that died without being stopped
    assert not st.monitor.running
    runner = MissionRunner(st.sup, None, 0.05, tripwire=st.monitor)
    runner._thread = None
    import threading
    t = threading.Thread(target=runner._loop, daemon=True)
    t.start()
    for _ in range(100):
        if st.monitor.running:
            break
        threading.Event().wait(0.05)
    runner._stop.set()
    t.join(5)
    assert st.monitor.running
    assert st.status(mid) == "paused_tripwire"                              # nothing was watching: everything executing paused


def test_the_side_door_gate_still_refuses_and_the_mission_waits(stack):
    st = stack(gate=False)
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    with pytest.raises(RepairRefused, match="2375"):
        st.sup.start(mid)
    st.sup.tick()
    st.sup.tick()
    assert st.status(mid) == "awaiting_decision" and st.runtimes == []


def test_without_the_controls_wired_a_repair_start_is_still_unsupported(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tripwire = None
    with pytest.raises(UnsupportedTier):
        st.sup.start(mid)


def test_no_baseline_or_backup_means_no_checkpoint_and_no_session(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.env.svc.baseline_provider = lambda: (_ for _ in ()).throw(RuntimeError("scan failed"))
    st.sup.tick()
    assert st.status(mid) == "awaiting_decision" and st.runtimes == []
    assert any("checkpoint_refused" in b for k, a, b in events(st, mid))
    # after repeated failures the operator is told: needs_operator, nothing launched
    for _ in range(sup_mod.START_ATTEMPTS):
        st.sup.tick()
    assert st.status(mid) == "needs_operator" and st.runtimes == []


def test_a_baseline_the_tripwire_cannot_read_stops_the_start(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.monitor.scanner.scan = lambda: type("R", (), {"ok": False, "errors": {"ports": "x"}, "snapshot": {}})()
    st.sup.tick()
    assert st.status(mid) == "awaiting_decision" and st.runtimes == []


def test_an_approval_for_one_mission_starts_only_that_mission(stack):
    st = stack()
    first = st.propose()["mission_id"]
    second = st.propose()["mission_id"]
    st.approve_all(second)
    st.drive(second)
    assert st.status(first) == "awaiting_decision"
    assert st.status(second) == "completed"


# ── crash classification: every phase, never a replay ───────────────────────────────────────────────

def crash_after_launch(st, mid):
    st.sup.tick()
    assert st.status(mid) == "executing"
    session = st.runtimes[0].context.session
    st.cont.crash(session.session_id)
    return session


def test_a_crash_with_nothing_applied_is_classified_needs_operator_offering_resume_and_is_not_restarted(stack):
    st = stack(behaviours=[{"script": [update()] * 4}, {"script": [update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    crash_after_launch(st, mid)
    st.sup.tick()
    assert st.status(mid) == "needs_operator"
    c = noted(st, mid, "recovery.classified")[0]
    assert c["offers"] == ["resume", "cancel"] and "replay" not in c["offers"]
    assert len(c["refused_replays"]) == 1                                  # the execution intent whose effect is unknown
    assert len(st.runtimes) == 1                                           # no automatic restart of a mutating session


def test_a_crash_after_the_session_changed_files_is_recovery_required_and_never_replayed(stack):
    st = stack(behaviours=[{"script": [update()] * 4}, {"script": [update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    session = crash_after_launch(st, mid)
    st.env.compose.write_text(st.env.compose.read_text() + "# half applied\n")
    st.sup.tick()
    assert st.status(mid) in ("recovery_required", "needs_operator")
    c = noted(st, mid, "recovery.classified")[0]
    assert "rollback" in c["offers"] and c["partially_applied"] is True
    assert len(st.runtimes) == 1 and session.session_id not in [s.context.session.session_id for s in st.runtimes[1:]]
    assert st.ms.get_mission(mid).outcome_flags == "partially_applied"


def test_a_restart_at_each_phase_classifies_from_the_host_and_never_replays(stack):
    # executing, session left running: the new supervisor kills it, then classifies
    st = stack(behaviours=[{"script": [update()] * 4}, {"script": [update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) == "executing"
    survivor = st.sup.__class__(st.store, st.cont, lambda m: st.runtimes.append("restarted") or None, st.clock, enabled=True,
                                artifact_dir=st.sup.artifact_dir, workspace_dir=st.sup.workspace_dir,
                                repair_gate=st.sup.repair_gate, verifier=st.verifier, checkpoint=st.env.svc, tripwire=st.monitor)
    survivor.recover()
    for _ in range(3):
        survivor.poll_all()
    assert st.status(mid) == "needs_operator" and "restarted" not in st.runtimes
    assert noted(st, mid, "recovery.classified")


def test_a_restart_during_verification_verifies_again_and_completes_only_through_core(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.verify_and_close = lambda *a, **k: "verifying"                  # the process dies before the verifier ran
    st.sup.tick()
    assert st.status(mid) == "verifying" and st.ms.live_session(mid) is None
    fresh = st.sup.__class__(st.store, st.cont, None, st.clock, enabled=True, artifact_dir=st.sup.artifact_dir,
                             workspace_dir=st.sup.workspace_dir, repair_gate=st.sup.repair_gate, verifier=st.verifier,
                             checkpoint=st.env.svc, tripwire=st.monitor)
    fresh.recover()
    fresh._verifying[mid].result(10)
    assert st.status(mid) == "completed"


def test_a_paused_mission_is_left_to_the_monitor_by_the_supervisors_recovery(stack):
    st = stack(behaviours=[{"script": [update()] * 4}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    touch(st.rig.fs / "etc" / "hosts", "x\n")
    st.monitor.scan_once()
    st.sup._runs.clear()
    assert st.sup.recover() == []
    assert st.status(mid) == "paused_tripwire"


def test_the_session_end_always_captures_post_after_it_is_provably_dead(stack):
    st = stack(behaviours=[{"script": [update()] * 4}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    order = []
    original = st.env.svc.capture_post
    st.env.svc.capture_post = lambda cid: (order.append(("post", st.cont.is_empty(st.runtimes[0].context.session))), original(cid))[1]
    st.sup.cancel(mid, "operator:alice")
    assert order == [("post", True)]                                        # the session was empty when the manifest was taken


def test_missions_disabled_constructs_and_starts_nothing(tmp_path):
    from planet_express.missions.runner import build_mission_runner
    from config_schema import MissionsConfig
    from missions_helpers import make_store
    store, _ = make_store(tmp_path)
    assert build_mission_runner(store, MissionsConfig(enabled=False, runtime={"argv": ["/usr/bin/agent"]}),
                                repair_wiring=object(), stacks_root=str(tmp_path)) is None


def test_a_worker_result_alone_is_never_completion(stack):
    st = stack(behaviours=[{"script": [update("finished")], "result": GOOD_RESULT}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.run_log.script.clear()
    st.run_log.on(["curl"], (0, "", "", False))
    st.sup.verifier = type("Never", (), {"run": lambda self, m: (_ for _ in ()).throw(RuntimeError("verifier down"))})()
    assert st.drive(mid) == "needs_operator"
    assert st.ms.get_mission(mid).status != "completed"
