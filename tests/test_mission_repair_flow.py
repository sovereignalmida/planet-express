"""A Repair mission end to end over fakes (slice 7a): approval -> checkpoint -> session -> post capture -> Core verification ->
a state decided by Core, never by the agent. Trust mode removed the tripwire: nothing pauses a run; rollback is the operator's."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import GOOD_RESULT, build, update
from verify_fakes import media_down

from planet_express.missions import supervisor as sup_mod
from planet_express.missions.supervisor import ResumeRefused, UnsupportedTier


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
    # the order Core promises: checkpoint, session, post capture, verification, completion
    names = [b.get("event") for k, a, b in events(st, mid)]
    assert names.index("checkpoint.created") < names.index("checkpoint.post_captured") < names.index("verify.result")
    assert not [k for k, a, b in events(st, mid) if k in ("tripwire", "takeover")]         # trust mode: no tripwire, no baseline
    assert states(st, mid)[-4:] == ["awaiting_decision", "executing", "verifying", "completed"]
    assert not st.cont.exists(st.runtimes[0].context.session.session_id)      # the session's cgroup is gone
    assert st.ms.live_session(mid) is None


def test_a_session_that_claims_success_but_fails_verification_never_completes(stack):
    st = stack(behaviours=[{"script": [update()] * 3 + [update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()                                   # started: the checkpoint is taken
    st.env.compose.write_text(st.env.compose.read_text() + "# the session changed this\n")
    st.fresh = media_down()                         # the checks pass but a NEW finding appears in scope: not repaired
    st.run_log.script.clear()
    assert st.drive(mid) in ("needs_operator", "rollback_pending")
    mission = st.ms.get_mission(mid)
    assert mission.status == "needs_operator" and mission.outcome == "needs_operator"
    assert mission.outcome_flags == "partially_applied"
    assert noted(st, mid, "verify.result")[0]["status"] == "failed"
    assert noted(st, mid, "checkpoint.post_captured")[-1]["changed"], "something changed, so it waits for the operator"


def test_a_failed_verification_that_changed_nothing_closes_failed_and_releases_its_leases(stack):
    """Trust mode, stage 5: with nothing the checkpoint covers changed there is nothing to roll back, so the mission does not hold its leases
    waiting for the operator: it closes `failed` and the next run on the same stack can start."""
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    st.run_log.script.clear()
    assert st.drive(mid) == "failed"
    mission = st.ms.get_mission(mid)
    assert mission.outcome == "failed" and mission.outcome_flags == "nothing_checkpointed_changed"
    assert noted(st, mid, "checkpoint.post_captured")[-1]["changed"] == []
    closed = noted(st, mid, "verify.closed")[-1]
    # Only what the checkpoint covers is known to be unchanged: never "nothing on the host changed".
    assert closed["closed_failed"] == "verification failed and nothing the checkpoint covers changed"
    from planet_express.console.execution import conversation_status, run_phase
    text, level = run_phase(mission).label, run_phase(mission).level
    assert text == "Didn't work \u2014 nothing PE snapshotted was changed; check the run's record for anything else it did"
    assert level == "high" and "nothing on the host" not in text
    assert "anything else it did" in conversation_status(mission)["verdict"]["p"]
    assert [b for k, a, b in events(st, mid) if k == "state" and b.get("to") == "failed"][0]["from"] == "verifying"
    with st.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mission_leases WHERE mission_id=?", (mid,)).fetchone()[0] == 0


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


def test_cancelling_a_session_that_already_changed_the_host_holds_the_mission_for_the_operator(stack):
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    original = st.env.compose.read_text()
    st.env.compose.write_text(original + "# half applied\n")
    st.sup.cancel(mid, "operator:alice")
    mission = st.ms.get_mission(mid)
    assert mission.status == "needs_operator" and mission.outcome_flags == "partially_applied"      # never straight to cancelled
    held = noted(st, mid, "cancel.held")
    assert held and held[0]["offers"] == ["rollback"]
    assert mission.cancel_requested_by == "operator:alice"                                            # the cancel is on the record
    assert st.ms.live_session(mid) is None
    st.sup.tick()
    st.sup.tick()
    assert len(noted(st, mid, "cancel.held")) == 1 and st.status(mid) == "needs_operator"            # reported once, not every tick
    out = st.flow.roll_back("operator:alice", mid)
    assert out["status"] == "rolled_back" and st.env.compose.read_text() == original


def test_cancelling_a_session_that_changed_nothing_cancels(stack):
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.sup.cancel(mid, "operator:alice")
    assert st.status(mid) == "cancelled" and not noted(st, mid, "cancel.held")


def test_the_operator_surface_cannot_close_a_possibly_changed_repair_mission_as_cancelled(stack):
    from planet_express.application.mission_service import MissionService
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.env.compose.write_text(st.env.compose.read_text() + "# half applied\n")
    st.cont.crash(st.runtimes[0].context.session.session_id)
    st.sup.tick()
    assert st.status(mid) in ("needs_operator", "recovery_required") and st.ms.live_session(mid) is None
    svc = MissionService(st.store, enabled=lambda: True)
    out = svc.cancel_mission(mid, operator="alice")                       # no live session: this used to cancel at once
    assert out["status"] in ("needs_operator", "recovery_required") and out["cancel_requested"] is True
    st.sup.tick()
    assert st.status(mid) in ("needs_operator", "recovery_required") and noted(st, mid, "cancel.held")


def test_a_requested_cancel_of_a_mission_whose_host_matches_the_checkpoint_closes_cancelled(stack):
    from planet_express.application.mission_service import MissionService
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.cont.crash(st.runtimes[0].context.session.session_id)
    st.sup.tick()
    assert st.status(mid) == "needs_operator"
    MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    st.sup.tick()
    assert st.status(mid) == "cancelled"


# ── the start gates ─────────────────────────────────────────────────────────────────────────────────

def test_a_repair_starts_with_no_proxy_gate_protected_set_or_backup(stack):
    """Trust mode: nothing stands between an approved Repair and its session but the checkpoint and the verifier."""
    st = stack()
    for gone in ("repair_gate", "docker_proxy"):
        assert not hasattr(st.sup, gone)
    mid = st.propose()["mission_id"]
    grant = st.ms.get_mission(mid).grant().model_dump(mode="json")
    assert not {"docker", "sudo", "protected", "backup", "exclusions"} & set(grant)
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) in ("executing", "verifying", "completed") and len(st.runtimes) == 1
    assert "executing" in states(st, mid)
    created = noted(st, mid, "checkpoint.created")[0]
    assert "no_backup" not in created


def test_without_the_controls_wired_a_repair_start_is_still_unsupported(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.verifier = None
    with pytest.raises(UnsupportedTier):
        st.sup.start(mid)


def test_no_baseline_means_no_checkpoint_and_no_session(stack):
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
    assert c["offers"] == ["cancel"] and "replay" not in c["offers"]        # trust mode: no resume
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
                                verifier=st.verifier, checkpoint=st.env.svc)
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
                             workspace_dir=st.sup.workspace_dir, verifier=st.verifier,
                             checkpoint=st.env.svc)
    fresh.recover()
    fresh._verifying[mid].result(10)
    assert st.status(mid) == "completed"


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
    from missions_helpers import make_store

    from config_schema import MissionsConfig
    from planet_express.missions.runner import build_mission_runner
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


def test_cancel_does_not_terminalize_a_revoked_session_that_is_not_yet_proven_dead(stack):
    """Codex G4: the supervisor revokes the token before the cgroup is proven empty; cancel must not read that as 'no session'."""
    from planet_express.application.mission_service import MissionService
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    live = st.ms.live_session(mid)
    assert live is not None and live["session_uid"] is not None and live["uid_released_at"] is None
    st.ms.revoke_session(live["id"])                                      # token gone, process possibly still running
    with st.store._write() as conn:                                       # isolate the live-is-None path from the checkpoint hold
        conn.execute("UPDATE missions SET checkpoint_id=NULL WHERE id=?", (mid,))
    out = MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    assert out["status"] not in ("cancelled", "failed", "completed") and out["cancel_requested"] is True


def test_a_restore_that_raises_hands_the_mission_to_the_operator_instead_of_leaving_it_stuck(stack):
    from planet_express.missions.checkpoint import CheckpointNotFound
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"

    def purged(cid, **kw):
        raise CheckpointNotFound(cid)
    st.env.svc.restore = purged
    st.flow.roll_back("operator:alice", mid)
    assert st.status(mid) == "needs_operator"                                 # defined state, not rollback_pending forever
    incomplete = [b for k, a, b in events(st, mid) if b.get("event") == "mission.rollback_incomplete"]
    assert incomplete and "CheckpointNotFound" in incomplete[-1]["reason"]
    assert st.ms.live_session(mid) is None


def test_a_post_change_capture_failure_refuses_without_changing_the_mission(stack):
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"

    def broken(cid):
        raise OSError("disk gone")
    st.env.svc.capture_post = broken
    with pytest.raises(Exception, match="post-change capture failed"):
        st.sup.roll_back(mid, "operator:alice")
    assert st.status(mid) == "rollback_pending"                               # unchanged: the operator can try again


def fail_launch(st):
    """The start gets as far as its checkpoint, then the launch fails: no session (until `del st.sup._launch`)."""
    def boom(mission, *a, **k):
        raise RuntimeError("the launch failed")
    st.sup._launch = boom


def test_a_retried_start_takes_a_fresh_checkpoint_when_the_first_start_launched_no_session(stack):
    import hashlib
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    fail_launch(st)
    st.sup.tick()                                                    # a checkpoint is taken, then the start fails: no session
    first = st.ms.get_mission(mid).checkpoint_id
    assert first and st.ms.live_session(mid) is None and not st.runtimes
    edited = st.env.compose.read_text() + "# the operator edited this while the mission waited\n"
    st.env.compose.write_text(edited)
    del st.sup._launch
    st.sup.tick()
    second = st.ms.get_mission(mid).checkpoint_id
    assert second and second != first                                # the stale picture was retired, not reused
    assert noted(st, mid, "checkpoint.retired")[0]["checkpoint"] == first
    created = noted(st, mid, "checkpoint.created")
    assert created[-1]["hashes"][str(st.env.compose)] == hashlib.sha256(edited.encode()).hexdigest()   # it describes the host NOW
    assert not st.env.svc.store.exists(first) if hasattr(st.env.svc.store, "exists") else True


def test_a_checkpoint_a_session_ran_under_is_never_retired(stack):
    st = stack(behaviours=[{"script": [update(), update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    kept = st.ms.get_mission(mid).checkpoint_id
    assert kept and st.ms.live_session(mid) is not None
    assert st.env.svc.retire_unused(mid) is None                    # a session exists: the checkpoint is the record of the pre-change host
    assert st.ms.get_mission(mid).checkpoint_id == kept


def test_retiring_needs_a_checkpoint_and_a_live_mission(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    assert st.env.svc.retire_unused(mid) is None                    # nothing to retire yet


def test_retire_unused_only_acts_on_a_mission_still_waiting_for_its_decision(stack):
    st = stack(behaviours=[{"script": [update(), update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    fail_launch(st)
    st.sup.tick()                                                    # checkpoint taken, start failed, no session
    first = st.ms.get_mission(mid).checkpoint_id
    assert first and st.status(mid) == "awaiting_decision"
    with st.env.svc.missions.store._write() as conn:                 # the mission is somewhere an operator could not retry from
        conn.execute("UPDATE missions SET status='needs_operator' WHERE id=?", (mid,))
    assert st.env.svc.retire_unused(mid) is None and st.ms.get_mission(mid).checkpoint_id == first


def test_a_stale_checkpoint_that_cannot_be_retired_defers_the_start_instead_of_being_used(stack):
    st = stack(behaviours=[{"script": [update(), update("finished")]}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    fail_launch(st)
    st.sup.tick()
    first = st.ms.get_mission(mid).checkpoint_id
    del st.sup._launch
    st.env.compose.write_text(st.env.compose.read_text() + "# edited while the mission waited\n")
    real_remove = st.env.svc.store.remove

    def cannot_remove(cid):
        raise OSError("device busy")
    st.env.svc.store.remove = cannot_remove
    st.sup.tick()
    assert st.ms.live_session(mid) is None and st.status(mid) == "awaiting_decision"       # no launch under the stale picture
    assert st.ms.get_mission(mid).checkpoint_id == first and not noted(st, mid, "checkpoint.retired")
    assert any("checkpoint_retire_failed" in b for k, a, b in events(st, mid))                # and it says why it is waiting
    st.env.svc.store.remove = real_remove
    st.sup.tick()                                                                           # the disk recovers: retired, retaken, started
    assert st.ms.get_mission(mid).checkpoint_id not in (None, first) and st.ms.live_session(mid) is not None


# ── trust mode: rollback built on the checkpoint service, no pause, resume or take-over ─────────────────

def crashed_with_changes(st, mid):
    """A Repair run whose session crashed after editing a checkpointed file: recovery_required (or needs_operator), no session."""
    st.sup.tick()
    assert st.status(mid) == "executing"
    original = st.env.compose.read_text()
    st.env.compose.write_text(original + "# half applied\n")
    st.cont.crash(st.runtimes[0].context.session.session_id)
    st.sup.tick()
    return original


def test_a_repair_starts_with_no_tripwire_wired(stack):
    st = stack(behaviours=[{"script": [update()] * 4}])
    assert not hasattr(st.sup, "tripwire") and st.sup.mutating_ready              # checkpoint and verifier are the whole set
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    assert st.status(mid) == "executing" and st.ms.get_mission(mid).checkpoint_id
    assert not [b for k, a, b in events(st, mid) if "baseline_refused" in b or str(b.get("event", "")).startswith("tripwire.")]


def test_nothing_can_enter_the_retired_paused_state():
    from planet_express.missions.state import TRANSITIONS, is_legal
    assert not any("paused_tripwire" in edges for edges in TRANSITIONS.values())
    assert not is_legal("paused_tripwire", "executing")
    assert is_legal("paused_tripwire", "recovery_required") and is_legal("paused_tripwire", "cancelled")


def test_a_tripwire_or_takeover_event_can_no_longer_be_written_but_an_old_one_still_reads(stack):
    from planet_express.missions.events import insert_event
    st = stack()
    mid = st.propose()["mission_id"]
    with st.store._write() as conn:
        for kind in ("tripwire", "takeover"):
            with pytest.raises(ValueError, match="unknown event kind"):
                insert_event(conn, mid, kind, "core", {}, 1)
        conn.execute("INSERT INTO mission_events (mission_id, at, kind, actor, body_json, redaction_applied) "
                     "VALUES (?, 1, 'tripwire', 'tripwire', '{\"event\": \"tripwire.paused\"}', 1)", (mid,))
    assert [e.kind for e in st.ms.events(mid)][-1] == "tripwire"                  # a pre-trust-mode row still loads


def test_the_pause_resume_and_take_over_controls_are_gone(stack):
    st = stack()
    for name in ("pause", "resume", "take_over"):
        assert not hasattr(st.sup, name) and not hasattr(st.flow, name)


def test_rollback_from_recovery_required_restores_the_checkpoint_and_ends_rolled_back(stack):
    st = stack(behaviours=[{"script": [update()] * 4}], grant_over=None)
    mid = st.propose(on_verify_fail="needs_operator")["mission_id"]
    st.approve_all(mid)
    original = crashed_with_changes(st, mid)
    assert st.status(mid) in ("recovery_required", "needs_operator") and st.ms.live_session(mid) is None
    with st.store._write() as conn:                                     # pin the state under test, whichever the classifier chose
        conn.execute("UPDATE missions SET status='recovery_required' WHERE id=?", (mid,))
    out = st.flow.roll_back("operator:alice", mid)
    assert out["status"] == "rolled_back" and out["restore"] == "done" and st.env.compose.read_text() == original
    assert states(st, mid)[-2:] == ["rollback_pending", "rolled_back"]
    assert all(a == "operator:alice" for k, a, b in events(st, mid) if b.get("event", "").startswith("checkpoint.restore"))


def test_rollback_from_needs_operator_restores_the_checkpoint_and_ends_rolled_back(stack):
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    original = st.env.compose.read_text()
    st.env.compose.write_text(original + "# half applied\n")
    st.sup.cancel(mid, "operator:alice")                                  # held for the operator: needs_operator
    assert st.status(mid) == "needs_operator"
    out = st.sup.roll_back(mid, "operator:alice")
    assert out.ok and st.status(mid) == "rolled_back" and st.env.compose.read_text() == original
    assert st.ms.live_session(mid) is None


def test_rollback_kills_a_leftover_session_first_and_refuses_while_one_is_supervised(stack):
    st = stack(behaviours=[{"script": [update()] * 8}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    with st.store._write() as conn:                                       # as if something moved it on while the session ran
        conn.execute("UPDATE missions SET status='needs_operator' WHERE id=?", (mid,))
    with pytest.raises(ResumeRefused, match="cancel it first"):
        st.sup.roll_back(mid, "operator:alice")                           # a supervised session is still running
    session = st.runtimes[0].context.session
    st.sup._runs.clear()                                                  # as after a Core restart: the cgroup is left behind
    assert st.cont.exists(session.session_id) and not st.cont.is_empty(session)
    st.sup.roll_back(mid, "operator:alice")
    assert st.cont.is_empty(session) and st.status(mid) == "rolled_back"


def test_a_failed_restore_hands_the_mission_to_the_operator_with_a_summary(stack):
    from planet_express.missions.checkpoint import RestoreReport
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    st.env.svc.restore = lambda cid, **kw: RestoreReport(status="failed", written=[str(st.env.compose)], reason="disk full")
    out = st.flow.roll_back("operator:alice", mid)
    assert out["restore"] == "failed" and st.status(mid) == "needs_operator"     # handed over, not left in rollback_pending
    incomplete = noted(st, mid, "mission.rollback_incomplete")
    assert incomplete and incomplete[-1]["reason"] == "disk full" and incomplete[-1]["summary"]["partial"] is True
    assert incomplete[-1]["summary"]["written"] == [str(st.env.compose)]


def test_a_restore_refused_for_drift_waits_for_the_operators_explicit_overwrite(stack):
    from planet_express.missions.checkpoint import RestoreReport
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    real = st.env.svc.restore
    st.env.svc.restore = lambda cid, **kw: (real(cid, **kw) if kw.get("ack_drift")
                                            else RestoreReport(status="refused", drifted=[{"path": "x"}], reason="files changed"))
    out = st.flow.roll_back("operator:alice", mid)
    assert out["restore"] == "refused" and st.status(mid) == "rollback_pending"   # nothing written: the operator decides
    assert not noted(st, mid, "mission.rollback_incomplete")
    assert st.flow.roll_back("operator:alice", mid, ack_drift=True)["status"] == "rolled_back"


def test_a_paused_tripwire_row_from_before_trust_mode_is_recovered_as_recovery_required(stack):
    st = stack(behaviours=[{"script": [update()] * 8}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    session = st.runtimes[0].context.session
    st.cont.freeze(session)                                               # the old monitor froze it
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET status='paused_tripwire' WHERE id=?", (mid,))
    st.sup._runs.clear()                                                  # a Core restart onto the new code
    st.sup.recover()
    assert st.status(mid) == "recovery_required"
    assert noted(st, mid, "mission.legacy_pause_recovered")
    assert st.cont.is_empty(session) and st.ms.live_session(mid) is None   # its frozen session is killed and proven gone
    original = st.env.compose.read_text()
    st.env.compose.write_text(original + "# what the paused run had done\n")
    assert st.flow.roll_back("operator:alice", mid)["status"] == "rolled_back" and st.env.compose.read_text() == original


def test_a_paused_tripwire_row_does_not_break_the_list_views(stack):
    from planet_express.application.mission_service import MissionService
    st = stack()
    mid = st.propose()["mission_id"]
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET status='paused_tripwire' WHERE id=?", (mid,))
    svc = MissionService(st.store, enabled=lambda: True)
    assert any(m["id"] == mid for m in svc.list_missions()["missions"])
    assert svc.get_mission(mid)["status"] == "paused_tripwire"


def test_a_restore_that_left_a_service_unrestored_hands_over_with_compose_s_reason(stack):
    # Live M-19: `unrestored: [{"fields": ["recreate"], ...}]` with no reason anywhere, and the handover said only "did not complete cleanly".
    from planet_express.missions.checkpoint import RestoreReport
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    st.env.svc.restore = lambda cid, **kw: RestoreReport(
        status="done", written=[str(st.env.compose)], actions={"up": ["media/web"]},
        unrestored=[{"service": "media/web", "fields": ["up"], "reason": "media: compose up timed out after 150s"}])
    st.flow.roll_back("operator:alice", mid)
    assert st.status(mid) == "needs_operator"
    incomplete = noted(st, mid, "mission.rollback_incomplete")[-1]
    assert incomplete["reason"] == "media/web: media: compose up timed out after 150s"
    assert incomplete["summary"]["unrestored"] == [{"service": "media/web", "fields": ["up"], "reason": "media: compose up timed out after 150s"}]
    assert incomplete["summary"]["actions"] == {"up": ["media/web"]}


def test_a_background_rollback_that_cannot_run_is_on_record(stack):
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"

    def refuse(*a, **kw):
        raise ResumeRefused("the session is not provably dead")
    st.sup._stop_session_for_restore = refuse
    assert st.sup.roll_back_start(mid, "operator:alice") == "started"
    st.sup._rolling_back[mid].result(timeout=30)
    assert not st.sup.rollback_in_flight(mid) and st.status(mid) == "rollback_pending"
    notes = [b for k, a, b in events(st, mid) if "rollback_not_run" in b]
    assert notes and notes[-1]["rollback_not_run"] == "the session is not provably dead"
    with pytest.raises(ValueError):
        st.sup.roll_back_start(mid, "core")                               # an operator decision, as roll_back


def test_a_repair_naming_a_check_this_host_cannot_run_is_refused_before_anyone_is_asked(stack):
    from planet_express.missions.approval_flow import FlowError
    st = stack()
    exec_check = {"id": "responds", "check": "exec_probe", "params": {"container": "web", "argv": ["curl", "-sf", "http://localhost/"]}}
    with pytest.raises(FlowError, match="cannot run on this host: exec_probe is not available"):
        st.propose(checks=[exec_check])


def test_shutdown_waits_for_a_rollback_that_is_already_running(stack):
    import threading
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    entered, real = threading.Event(), st.env.svc.restore

    def slow(*a, **kw):
        entered.set()
        import time
        time.sleep(0.5)
        return real(*a, **kw)
    st.env.svc.restore = slow
    st.sup.roll_back_start(mid, "operator:alice")
    assert entered.wait(10)
    st.sup.shutdown()
    assert st.sup._rolling_back[mid].done() and st.status(mid) == "rolled_back"


def test_the_tick_does_not_wait_for_a_running_rollback_to_settle_a_pending_cancel(stack):
    """Review, rc14.1: a Console rollback holds the mission lock for the whole restore; the poll thread must not block on it."""
    import threading
    st = stack()
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET cancel_requested_at=1, cancel_requested_by='operator:alice' WHERE id=?", (mid,))
    release = threading.Event()
    lock = st.sup._lock_for(mid)
    lock.acquire()                                       # what a running rollback holds
    from concurrent.futures import Future
    running = Future()
    st.sup._rolling_back[mid] = running                  # not done: a rollback is in flight
    done = threading.Event()
    threading.Thread(target=lambda: (st.sup._finish_pending_repair_cancels(), done.set()), daemon=True).start()
    try:
        assert done.wait(5), "the tick blocked on the mission lock"
    finally:
        running.set_result(None)
        lock.release()
        release.set()


def test_a_rollback_start_racing_shutdown_is_either_waited_for_or_refused(stack):
    """Codex, rc14.1: shutdown sets its flag under the same guard roll_back_start submits under."""
    import inspect
    st = stack()
    src = inspect.getsource(type(st.sup).shutdown)
    assert "with self._verify_guard:" in src and src.index("with self._verify_guard:") < src.index("rollback_pool.shutdown")
    mid = st.propose(on_verify_fail="rollback")["mission_id"]
    st.approve_all(mid)
    st.fresh = media_down()
    assert st.drive(mid) == "rollback_pending"
    st.sup.shutdown()
    with pytest.raises(ResumeRefused, match="shutting down"):
        st.sup.roll_back_start(mid, "operator:alice")


# ── rc14.2: undo a completed Repair (roll it back to its checkpoint) ────────────────────────────────────────────────────────
from planet_express.missions import leases as leases_mod  # noqa: E402


def _completed_with_a_change(st):
    """A completed Repair whose session changed the compose file (the post-change manifest records it, as at a session's end)."""
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    assert st.drive(mid) == "completed"
    original = st.env.compose.read_text()
    st.env.compose.write_text(original + "# the verified change\n")
    st.env.svc.capture_post(st.ms.get_mission(mid).checkpoint_id)
    assert leases_mod.held_by(st.store, mid) == []                          # a completed mission holds nothing
    return mid, original


def test_an_operator_can_undo_a_completed_repair_and_its_leases_are_held_only_while_it_restores(stack):
    st = stack()
    mid, original = _completed_with_a_change(st)
    held, real = [], st.env.svc.restore

    def watching(*a, **kw):
        held.append(leases_mod.held_by(st.store, mid))
        return real(*a, **kw)
    st.env.svc.restore = watching
    out = st.flow.roll_back("operator:alice", mid)
    assert out["status"] == "rolled_back" and st.env.compose.read_text() == original
    assert held and "host:local/stack:media" in held[0]                       # re-acquired before the restore
    assert leases_mod.held_by(st.store, mid) == []                            # and freed at the end
    assert states(st, mid)[-3:] == ["completed", "rollback_pending", "rolled_back"]
    undo = [(a, b) for k, a, b in events(st, mid) if k == "state" and b.get("from") == "completed" and not b.get("refused")]
    assert undo == [("operator:alice", {"from": "completed", "to": "rollback_pending"})]
    assert st.ms.get_mission(mid).closed_at is not None


def test_undo_is_refused_in_words_while_another_mission_holds_the_stack(stack):
    st = stack()
    mid, original = _completed_with_a_change(st)
    other = st.propose()["mission_id"]
    leases_mod.acquire(st.store, ["host:local/stack:media"], other, 600)
    for start in (st.sup.roll_back, st.sup.roll_back_start):
        with pytest.raises(ResumeRefused, match=f"cannot be rolled back now: {other} is holding the media stack"):
            start(mid, "operator:alice")
    assert st.status(mid) == "completed" and st.env.compose.read_text() != original           # nothing touched
    assert leases_mod.held_by(st.store, mid) == []


def test_undo_is_refused_once_the_checkpoint_is_past_retention_or_removed(stack):
    st = stack()
    mid, _ = _completed_with_a_change(st)
    st.clock.t += st.env.svc.settings.retention_days * 86400 + 5
    with pytest.raises(ResumeRefused, match="past the 90-day retention"):
        st.sup.roll_back_start(mid, "operator:alice")
    st.clock.t -= st.env.svc.settings.retention_days * 86400 + 5
    st.env.svc.purge(st.ms.get_mission(mid).checkpoint_id)
    with pytest.raises(ResumeRefused, match="no longer kept"):
        st.sup.roll_back(mid, "operator:alice")
    assert st.status(mid) == "completed"


def test_undo_never_overwrites_what_changed_after_the_run_without_the_operators_word(stack):
    st = stack()
    mid, original = _completed_with_a_change(st)
    st.env.compose.write_text(original + "# someone else's later edit\n")    # after the run: not the session's change
    out = st.flow.roll_back("operator:alice", mid)
    assert out["restore"] == "refused" and st.status(mid) == "needs_operator"     # nothing written: handed to the operator
    assert st.env.compose.read_text().endswith("# someone else's later edit\n")
    assert leases_mod.held_by(st.store, mid) == []                            # a refused undo does not keep the stack
    out = st.flow.roll_back("operator:alice", mid, ack_drift=True)            # the operator's explicit overwrite
    assert out["status"] == "rolled_back" and st.env.compose.read_text() == original


def test_a_second_undo_start_is_refused_while_the_first_runs_and_after_it_landed(stack):
    import threading
    st = stack()
    mid, original = _completed_with_a_change(st)
    entered, release, real = threading.Event(), threading.Event(), st.env.svc.restore

    def slow(*a, **kw):
        entered.set()
        release.wait(10)
        return real(*a, **kw)
    st.env.svc.restore = slow
    assert st.sup.roll_back_start(mid, "operator:alice") == "started"
    assert entered.wait(10)
    with pytest.raises(ResumeRefused, match="already running"):
        st.sup.roll_back_start(mid, "operator:alice")
    release.set()
    st.sup._rolling_back[mid].result(timeout=30)
    assert st.status(mid) == "rolled_back" and st.env.compose.read_text() == original
    with pytest.raises(ResumeRefused, match="rolled_back"):
        st.sup.roll_back_start(mid, "operator:alice")


def test_only_an_operator_can_undo_and_only_a_repair(stack):
    st = stack()
    mid, _ = _completed_with_a_change(st)
    for actor in ("core", "supervisor"):
        with pytest.raises(ValueError):
            st.sup.roll_back(mid, actor)
    assert st.status(mid) == "completed"


# ── rc14.3: a rollback reverses only what THIS run changed ─────────────────────────────────────────────────────────────────
def _another_mission_updates_web(st):
    """A later mission's legitimate change to a service this run never touched (M-25 updating sonarr after M-22)."""
    st.env.docker.containers[("media", "web")]["Image"] = "sha256:" + "5" * 64


def test_undo_leaves_a_later_missions_change_alone_and_ends_rolled_back(stack):
    from planet_express.console.execution import rollback_view
    st = stack()
    mid, original = _completed_with_a_change(st)
    _another_mission_updates_web(st)
    out = st.flow.roll_back("operator:alice", mid)
    assert out["status"] == "rolled_back" and st.env.compose.read_text() == original
    assert st.env.docker.up_calls == [] and st.env.docker.containers[("media", "web")]["Image"] == "sha256:" + "5" * 64
    done = noted(st, mid, "checkpoint.restore_done")[-1]
    assert done["left_alone"] == [{"service": "media/web", "note": "changed since this run, left as it is"}]
    assert done["unrestored"] == [] and done["actions"] == {}
    view = rollback_view(st.ms, st.ms.get_mission(mid), False)
    assert view == {"state": "done", "reason": "", "left_alone": ["media/web"]}


def test_a_cancel_is_not_held_by_another_missions_later_change(stack):
    from planet_express.application.mission_service import MissionService
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.cont.crash(st.runtimes[0].context.session.session_id)
    st.sup.tick()
    assert st.status(mid) == "needs_operator"                                  # the run changed nothing it covers
    _another_mission_updates_web(st)                                           # something else changed web afterwards
    MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    st.sup.tick()
    assert st.status(mid) == "cancelled" and not noted(st, mid, "cancel.held")


def test_a_cancel_is_still_held_by_what_this_run_changed(stack):
    from planet_express.application.mission_service import MissionService
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.env.compose.write_text(st.env.compose.read_text() + "# half applied\n")
    st.cont.crash(st.runtimes[0].context.session.session_id)
    st.sup.tick()
    _another_mission_updates_web(st)
    MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    st.sup.tick()
    assert st.status(mid) in ("needs_operator", "recovery_required") and noted(st, mid, "cancel.held")


def test_an_undo_refuses_a_snapshot_older_than_the_last_session(stack):
    """Review, rc14.3: if the last session's capture failed, the kept snapshot predates it and would hide that session's changes."""
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    assert st.drive(mid) == "completed"
    with st.store._write() as conn:
        conn.execute("UPDATE mission_sessions SET issued_at=issued_at+100000 WHERE mission_id=?", (mid,))
    with pytest.raises(ResumeRefused, match="predates its last session"):
        st.sup.roll_back(mid, "operator:alice")


# ── rc14.4: a cancel on a run that already passed verification is acknowledged, never judged against the host ─────────────────
def test_cancelling_a_verified_run_whose_undo_did_not_finish_closes_it_as_dismissed(stack):
    """Live M-22: a completed run, its undo refused and handed to the operator, then the operator's cancel. Before rc14.4 the cancel
    compared the host with the checkpoint and was held for good by a later mission's change."""
    from planet_express.application.mission_service import MissionService
    from planet_express.console.execution import PHASES, run_phase
    st = stack()
    mid, original = _completed_with_a_change(st)
    st.env.compose.write_text(original + "# someone else's later edit\n")
    assert st.flow.roll_back("operator:alice", mid)["restore"] == "refused" and st.status(mid) == "needs_operator"
    MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    st.sup.tick()
    mission = st.ms.get_mission(mid)
    assert mission.status == "cancelled" and mission.outcome == "dismissed" and not noted(st, mid, "cancel.held")
    assert [b["cancel_requested_by"] for b in noted(st, mid, "cancel.dismissed")] == ["operator:alice"]
    assert st.env.compose.read_text().endswith("# someone else's later edit\n")          # the host was not touched or judged
    assert leases_mod.held_by(st.store, mid) == []
    assert run_phase(mission).name == "dismissed"
    st.sup.tick()
    assert len(noted(st, mid, "cancel.dismissed")) == 1


def test_a_cancel_recorded_on_a_completed_run_is_cleared_once_and_the_run_stays_completed(stack):
    st = stack()
    mid, _ = _completed_with_a_change(st)
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET cancel_requested_at=1, cancel_requested_by='operator:alice' WHERE id=?", (mid,))
    for _ in range(3):
        st.sup.tick()
    mission = st.ms.get_mission(mid)
    assert mission.status == "completed" and mission.cancel_requested_at is None
    assert len(noted(st, mid, "cancel.dismissed")) == 1 and not noted(st, mid, "cancel.held")


def test_a_cancel_of_a_run_that_never_completed_is_still_judged_against_the_host(stack):
    """The dismissal is only for a run that passed verification: a crashed run that changed the host is still held."""
    from planet_express.application.mission_service import MissionService
    st = stack(behaviours=[{"script": [update()] * 6}])
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    st.sup.tick()
    st.env.compose.write_text(st.env.compose.read_text() + "# half applied\n")
    st.cont.crash(st.runtimes[0].context.session.session_id)
    st.sup.tick()
    MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    st.sup.tick()
    assert st.status(mid) in ("needs_operator", "recovery_required") and noted(st, mid, "cancel.held")
    assert not noted(st, mid, "cancel.dismissed")


def test_a_cancel_after_an_undo_that_wrote_part_way_is_still_judged_against_the_host(stack):
    from planet_express.application.mission_service import MissionService
    st = stack()
    mid, original = _completed_with_a_change(st)
    st.env.compose.write_text(original + "# someone else's later edit\n")
    st.flow.roll_back("operator:alice", mid)
    assert st.status(mid) == "needs_operator"
    st.ms.append_event(mid, "decision", "supervisor", {"event": "mission.rollback_incomplete", "reason": "x",
                                                      "summary": {"status": "failed", "partial": True, "written": ["compose.yaml"]}})
    MissionService(st.store, enabled=lambda: True).cancel_mission(mid, operator="alice")
    st.sup.tick()
    assert st.status(mid) == "needs_operator" and not noted(st, mid, "cancel.dismissed") and noted(st, mid, "cancel.held")
