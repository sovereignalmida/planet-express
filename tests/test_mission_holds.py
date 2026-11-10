"""rc14.7.5 (state-machines.md M4/M5/M10, bugs 10-12): why a mission is held is recorded by the edge that held it and read back by
`missions.holds.hold_reason`, and every surface acts on it: Check again and Keep only after PE's checks failed, never after a failed
rollback (half restored); a refused restore and a held cancel say what they are. Also: a rolled-back mission's outcome says so, a
Check again with no verifier is refused instead of losing the hold, and Keep's intents are closed where recovery can see them."""
import pytest
from test_console_execution import Rig, _needs_operator_run

from planet_express.console import execution as ex_mod
from planet_express.missions import holds, recovery
from planet_express.missions.state import transition
from planet_express.missions.supervisor import ResumeRefused


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _hold(rig, mid):
    return holds.hold_reason(rig.st.store, rig.st.ms.get_mission(mid))


def test_a_run_whose_checks_failed_is_held_for_that_and_offers_check_again_and_keep(rig):
    view = _needs_operator_run(rig)
    assert _hold(rig, view["mission_id"]) == holds.FAILED_CHECKS
    feed = rig.feed()["execution"]
    assert feed["status"]["text"] == "Needs you: PE's checks failed"
    assert feed["controls"] == {"rollback": True, "recheck": True, "keep": True}


def test_a_failed_rollback_of_a_failed_run_never_offers_keep_or_check_again(rig, monkeypatch):
    """Bug 12: its stored result still says the checks failed, so the old rule offered "Keep the changes" on a half-restored host."""
    view = _needs_operator_run(rig)
    mid = view["mission_id"]

    def breaks(*args, **kwargs):
        raise RuntimeError("the compose up failed")
    monkeypatch.setattr(type(rig.st.sup.checkpoint), "_converge_and_compare", breaks)
    rig.st.sup.roll_back(mid, "operator:chris")
    assert rig.st.status(mid) == "needs_operator" and _hold(rig, mid) == holds.RESTORE_FAILED
    feed = rig.feed()["execution"]
    assert feed["status"]["text"] == "Needs you: the rollback did not finish"
    assert feed["controls"] == {"rollback": True, "recheck": False, "keep": False}
    assert rig.st.ms.get_mission(mid).verification_result_json                # the old rule's input is still there
    with pytest.raises(ResumeRefused):
        rig.st.sup.keep_and_close(mid, "operator:chris")
    assert rig.api.service.ms.get_mission(mid).status == "needs_operator"
    assert rig.st.sup.ms.get_mission(mid).status == "needs_operator"


def test_a_refused_restore_says_pe_did_not_roll_back_and_why(rig):
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    with rig.st.store._write() as conn:                  # how CheckpointService._stop leaves a refused restore
        conn.execute("UPDATE missions SET status='rollback_pending' WHERE id=?", (mid,))
    transition(rig.st.store, mid, "needs_operator", "core", why=holds.RESTORE_REFUSED)
    assert _hold(rig, mid) == holds.RESTORE_REFUSED
    feed = rig.feed()["execution"]
    assert feed["status"]["text"].startswith("Needs you: PE did not roll back") and feed["controls"]["rollback"] is True
    assert feed["controls"]["keep"] is False


def test_check_again_without_a_verifier_is_refused_and_the_hold_is_kept(rig):
    """Bug 11: with no verifier the check held it again as unverified, and Check again and Keep were gone for good."""
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    rig.st.sup.verifier = None
    with pytest.raises(ResumeRefused, match="cannot run its checks"):
        rig.st.sup.recheck(mid, "operator:chris")
    assert rig.st.status(mid) == "needs_operator" and _hold(rig, mid) == holds.FAILED_CHECKS


def test_a_hold_recorded_before_this_release_is_judged_by_the_stored_result(rig):
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    with rig.st.store._write() as conn:                  # an older state event carries no `why`
        conn.execute("UPDATE mission_events SET body_json=json_remove(body_json, '$.why') WHERE mission_id=? AND kind='state'", (mid,))
    assert _hold(rig, mid) == holds.FAILED_CHECKS
    with rig.st.store._write() as conn:
        conn.execute("UPDATE missions SET verification_result_json=NULL WHERE id=?", (mid,))
    assert _hold(rig, mid) == holds.UNKNOWN


def test_a_mission_not_held_has_no_hold_reason(rig):
    view = rig.start_run()
    assert _hold(rig, view["mission_id"]) is None


def test_a_rolled_back_mission_says_so_in_its_outcome(rig):
    """Bug 10: an undone run kept the outcome "completed", a failed one "needs_operator", on Telegram and the mission page."""
    view = rig.start_run()
    mid = view["mission_id"]
    assert rig.st.drive(mid) == "completed" and rig.st.ms.get_mission(mid).outcome == "completed"
    rig.st.sup.roll_back(mid, "operator:chris")
    mission = rig.st.ms.get_mission(mid)
    assert mission.status == "rolled_back" and mission.outcome == "rolled_back" and mission.outcome_flags is None


def test_keep_closes_the_runs_intents_where_recovery_can_see_it(rig):
    """Closed as the operator, recovery (which trusts only Core's and the supervisor's records) still saw them open."""
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    recovery.journal_intent(rig.st.ms, mid, recovery.PHASES[0], "restart web", actor="supervisor")
    assert recovery.open_intents(rig.st.ms.events(mid))                       # one is open before Keep
    rig.st.sup.keep_and_close(mid, "operator:chris")
    assert recovery.open_intents(rig.st.ms.events(mid)) == []


def test_every_hold_reason_has_a_phase_that_offers_a_way_out():
    for reason in holds.REASONS:
        phase = ex_mod.PHASES[ex_mod._BY_HOLD[reason]]
        assert phase.group == "Needs you" and phase.actions, reason


# ── review follow-ups (rc14.7.6) ─────────────────────────────────────────────────────────────────────────────────────────────
def test_a_hold_out_of_a_rollback_or_recovery_never_reads_as_failed_checks_even_with_no_reason(rig):
    """The run's failed result outlives a rollback; a reasonless hold that did not come out of `verifying` is not a failed check."""
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    transition(rig.st.store, mid, "rollback_pending", "operator:chris")
    transition(rig.st.store, mid, "recovery_required", "core")
    transition(rig.st.store, mid, "needs_operator", "core")                  # no `why`, as an older writer
    assert _hold(rig, mid) == holds.UNKNOWN
    assert rig.feed()["execution"]["controls"]["keep"] is False


def test_recovery_says_why_it_held_a_run(rig):
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    transition(rig.st.store, mid, "rollback_pending", "operator:chris")
    transition(rig.st.store, mid, "recovery_required", "core")
    transition(rig.st.store, mid, "needs_operator", "core", why=holds.RECOVERED)
    assert _hold(rig, mid) == holds.RECOVERED and rig.feed()["execution"]["controls"]["keep"] is False


def test_a_restore_refused_part_way_is_a_restore_that_did_not_finish(rig):
    from planet_express.missions.checkpoint import RestoreReport
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    transition(rig.st.store, mid, "rollback_pending", "operator:chris")
    report = RestoreReport(status="refused", reason="a file changed mid-write")
    report.written.append("/srv/media/compose.yaml")
    rig.st.sup.checkpoint._stop(mid, rig.st.ms.get_mission(mid).checkpoint_id, "operator:chris", report)
    assert _hold(rig, mid) == holds.RESTORE_FAILED


def test_a_verifier_that_breaks_during_check_again_leaves_check_again_offered(rig, monkeypatch):
    view = _needs_operator_run(rig)
    mid = view["mission_id"]

    def breaks(mission):
        raise OSError("network down")
    monkeypatch.setattr(rig.st.sup.verifier, "run", breaks)
    rig.st.sup.recheck(mid, "operator:chris")
    assert rig.st.sup.verify_and_close(mid, wait=True) == "needs_operator"
    assert _hold(rig, mid) == holds.UNVERIFIED
    controls = rig.feed()["execution"]["controls"]
    assert controls["recheck"] is True and controls["keep"] is False          # Keep needs checks that ran and failed


def test_passed_checks_whose_completion_is_refused_do_not_read_as_could_not_check(rig, monkeypatch):
    from planet_express.missions import supervisor as sup_mod
    from planet_express.missions.state import IllegalTransition
    view = rig.start_run()
    mid = view["mission_id"]
    real = sup_mod.transition

    def refuse_completion(store, mission_id, to, actor, why=None):
        if to == "completed":
            raise IllegalTransition("completion refused: test")
        return real(store, mission_id, to, actor, why=why)
    monkeypatch.setattr(sup_mod, "transition", refuse_completion)
    assert rig.st.drive(mid) == "needs_operator"
    assert _hold(rig, mid) == holds.UNKNOWN
