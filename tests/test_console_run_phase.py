"""rc14.7.5: one run phase every surface reads (docs/designs/state-machines.md C2/C3/C5/C6), and the Console bugs it fixed: an undo's
outcome never reached the thread, a kept run read "Didn't work" under "Needs you", a scrapped or superseded plan left its Repair waiting,
a Work-log approval never showed, a new run could be prepared during an undo, and an idle conversation could be archived mid-run."""
import json
from types import SimpleNamespace
from typing import get_args

import pytest
from test_console_execution import Rig, _needs_operator_run
from test_console_confirm import plan_doc

from planet_express.console import execution as ex_mod
from planet_express.console import service as service_mod
from planet_express.console.api import ConsoleApiError
from planet_express.missions.models import Status


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


# ── the table ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_every_mission_status_has_a_phase_and_every_phase_is_coherent():
    for status in get_args(Status):
        assert ex_mod.run_phase(SimpleNamespace(status=status)).name in ex_mod.PHASES, status
    for phase in ex_mod.PHASES.values():
        assert set(phase.actions) <= set(ex_mod.ACTIONS), phase.name
        assert not (phase.final and phase.actions), f"{phase.name}: nothing can be done to a final run"
        assert phase.group in ("Working", "Needs you", "Recent")
        # The one exception: a legacy paused row, which startup recovery moves on and Missions closes; the Console offers nothing.
        assert phase.group != "Needs you" or phase.actions or phase.name == "held_legacy", f"{phase.name}: 'Needs you', nothing to do"


def test_the_controls_the_dashboard_and_telegram_accept_are_the_tables():
    from planet_express.console import telegram
    assert telegram.ACTIONS == ex_mod.ACTIONS


def test_a_kept_run_reads_as_kept_and_is_not_waiting_on_the_operator(rig):
    view = _needs_operator_run(rig)
    rig.api.execution_action(rig.conv.id, view["execution_id"], "keep", "chris")
    feed = rig.feed()
    assert feed["execution"]["status"] == {"text": "Kept as it was — PE's checks had not passed", "level": "warn"}
    assert feed["conversation"]["status"]["group"] != "Needs you"
    rig.service.tick()
    assert any("chris kept what the run changed and closed it" in t for t in rig.thread())


def test_a_held_run_says_pes_checks_failed_and_the_thread_names_check_again_and_keep(rig):
    view = _needs_operator_run(rig)
    assert rig.feed()["execution"]["status"]["text"] == "Needs you: PE's checks failed"
    rig.service.tick()
    line = next(t for t in rig.thread() if t.startswith("Needs you: PE's checks failed"))
    assert "check it again" in line and "keep the changes and close it" in line and "roll back" in line


def test_telegram_can_check_a_held_run_again_and_keep_it(rig):
    from planet_express.console.telegram import ConsoleTelegram
    sent = []
    tg = ConsoleTelegram(rig.api, rig.console, sent.append)
    view = _needs_operator_run(rig)
    out = tg._run(rig.conv, "keep", "chris")
    assert "kept what the run changed" in out
    assert rig.st.ms.get_mission(view["mission_id"]).outcome_flags == "kept_by_operator"


# ── the thread follows the run after it ended ─────────────────────────────────────────────────────────────────────────────
def test_an_undo_of_a_completed_run_reaches_the_thread(rig):
    view = rig.start_run()
    assert rig.st.drive(view["mission_id"]) == "completed"
    rig.service.tick()
    assert rig.console.execution(view["execution_id"])["status"] == "ended"
    rig.st.sup.roll_back(view["mission_id"], "operator:chris")          # the dashboard's and Telegram's way: on the request
    assert rig.st.status(view["mission_id"]) == "rolled_back"
    rig.service.tick()
    assert sum(t.startswith("Rolled back.") for t in rig.thread()) == 1
    rig.service.tick()
    assert sum(t.startswith("Rolled back.") for t in rig.thread()) == 1          # once


def test_a_run_reported_before_this_release_is_not_reported_again(rig):
    view = rig.start_run()
    rig.st.drive(view["mission_id"])
    with rig.st.store._write() as conn:                                  # the shape a pre-rc14.7.5 row has on the host
        conn.execute("UPDATE console_executions SET status='ended', reported='executing,verifying,completed' WHERE id=?",
                     (view["execution_id"],))
    before = list(rig.thread())
    rig.service.tick()
    assert rig.thread() == before
    assert any(k.startswith("seen@") for k in rig.console.reported_keys(view["execution_id"]))


def test_a_rollback_started_on_a_request_is_seen_as_running_and_a_second_is_refused(rig):
    """The dashboard and Telegram roll back on the request, not on the supervisor's worker; until rc14.7.5 that rollback was not in
    the in-flight map, so the Console offered a live button during it and a second restore could be queued (state-machines.md bug 9)."""
    from planet_express.missions.supervisor import ResumeRefused
    view = _needs_operator_run(rig)
    mid = view["mission_id"]
    seen = {}
    real = rig.st.sup._roll_back

    def watching(mission_id, actor, ack_drift=False):
        seen["in_flight"] = rig.st.sup.rollback_in_flight(mission_id)
        seen["feed"] = rig.feed()["execution"]
        with pytest.raises(ResumeRefused):
            rig.st.sup.roll_back(mission_id, "operator:other")
        return real(mission_id, actor, ack_drift=ack_drift)
    rig.st.sup._roll_back = watching
    rig.st.sup.roll_back(mid, "operator:chris")
    assert seen["in_flight"] is True
    assert seen["feed"]["status"]["text"] == "Rolling back\u2026" and not any(seen["feed"]["controls"].values())
    assert not rig.st.sup.rollback_in_flight(mid)                       # and it is gone once the rollback returns
    assert rig.st.status(mid) == "rolled_back"


def test_a_rollback_refused_after_it_finished_offers_the_controls_again(rig):
    """Once a rollback returns, nothing reads it as running, whatever state it left (a refused restore waits for the operator)."""
    view = _needs_operator_run(rig)
    rig.st.sup._roll_back = lambda mission_id, actor, ack_drift=False: {"restore": "refused", "reason": "drift"}
    rig.st.sup.roll_back(view["mission_id"], "operator:chris")
    assert rig.feed()["execution"]["controls"]["rollback"] is True


def test_the_kept_line_is_posted_once_and_names_who_kept_it(rig):
    view = _needs_operator_run(rig)
    rig.st.sup.keep_and_close(view["mission_id"], "operator:sam")         # from the mission page, not the Console
    rig.service.tick()
    rig.service.tick()
    assert sum("sam kept what the run changed and closed it" in t for t in rig.thread()) == 1


def test_a_run_reported_before_this_release_stays_quiet_on_later_ticks(rig):
    view = _needs_operator_run(rig)
    with rig.st.store._write() as conn:                                  # mid-run at the upgrade: already told it needs the operator
        conn.execute("UPDATE console_executions SET reported='executing,verifying,ended:needs_operator' WHERE id=?",
                     (view["execution_id"],))
    before = list(rig.thread())
    for _ in range(3):
        rig.service.tick()
    assert rig.thread() == before


def test_an_outcome_flag_written_after_the_close_still_reaches_the_thread(rig):
    view = _needs_operator_run(rig)
    rig.st.sup.keep_and_close(view["mission_id"], "operator:sam")
    rig.service.tick()
    lines = len(rig.thread())
    rig.st.ms.set_outcome(view["mission_id"], "failed", "nothing_checkpointed_changed")       # a later write, no state change
    rig.service.tick()
    assert len(rig.thread()) == lines + 1 and rig.thread()[-1].startswith("Didn't work \u2014 nothing PE snapshotted")


# ── a confirmation follows what happened to it elsewhere ─────────────────────────────────────────────────────────────────────
def _prepare(rig):
    plan_id = rig.add_plan(plan_doc())
    return plan_id, rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")


def test_scrapping_the_plan_withdraws_its_confirmation_and_cancels_the_waiting_repair(rig):
    _plan_id, view = _prepare(rig)
    rig.api.scrap_plan(rig.conv.id, "chris")
    rig.service.tick()
    assert rig.console.execution(view["execution_id"])["status"] == "withdrawn"
    assert rig.st.status(view["mission_id"]) == "cancelled"
    assert any("withdrawn because its plan was scrapped" in t for t in rig.thread())
    events = [json.loads(e.body_json) for e in rig.st.ms.events(view["mission_id"])]
    assert {"event": "console.confirmation_withdrawn", "why": "its plan was scrapped"} in events      # the reason is on record


def test_a_superseded_plan_withdraws_its_confirmation_too(rig):
    _plan_id, view = _prepare(rig)
    rig.add_plan(plan_doc())                                             # the agent's next turn wrote a newer plan
    rig.service.tick()
    assert rig.console.execution(view["execution_id"])["status"] == "withdrawn"
    assert rig.st.status(view["mission_id"]) == "cancelled"


def test_a_confirmation_approved_in_the_work_log_is_not_cancelled_and_is_adopted_once_it_runs(rig, monkeypatch):
    _plan_id, view = _prepare(rig)
    flow = rig.api._require_flow()
    for needed in [r for r in flow.card(view["mission_id"])["required"] if r["state"] == "pending"]:
        flow.approve("operator:alex", view["mission_id"], needed["phase"],
                     view["grant_hash"] if needed["phase"] != "plan" else needed["bound"])
    rig.api.scrap_plan(rig.conv.id, "chris")                             # the plan goes, but the run was approved
    rig.service.tick()
    assert rig.st.status(view["mission_id"]) != "cancelled"
    assert rig.console.execution(view["execution_id"])["status"] == "proposed"
    rig.st.sup.tick()                                                    # the supervisor starts it
    assert rig.st.status(view["mission_id"]) != "awaiting_decision"
    rig.service.tick()
    assert rig.console.execution(view["execution_id"])["status"] == "proposed"     # within the grace: Authorise may still be writing
    monkeypatch.setattr(service_mod, "ADOPT_GRACE_S", -1)
    rig.service.tick()
    ex = rig.console.execution(view["execution_id"])
    assert ex["status"] in ("authorised", "ended") and ex["authorised_by"] == "operator:alex"     # it may have finished already
    assert any("Authorised from the Work log by alex" in t for t in rig.thread())


def test_a_confirmation_denied_in_the_work_log_is_withdrawn(rig):
    _plan_id, view = _prepare(rig)
    rig.api._require_flow().deny("operator:alex", view["mission_id"], view["grant_hash"])
    rig.service.tick()
    assert rig.console.execution(view["execution_id"])["status"] == "withdrawn"


# ── a run that is not finished holds the conversation ────────────────────────────────────────────────────────────────────────
def test_no_new_run_is_prepared_while_an_ended_runs_undo_is_under_way(rig):
    view = rig.start_run()
    rig.st.drive(view["mission_id"])
    rig.service.tick()
    with rig.st.store._write() as conn:                                  # undo asked for, not landed
        conn.execute("UPDATE missions SET status='rollback_pending', closed_at=NULL WHERE id=?", (view["mission_id"],))
    plan_id = rig.add_plan(plan_doc())
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    assert err.value.code == "conflict"


def test_an_idle_conversation_is_not_archived_while_its_run_is_unfinished(rig, monkeypatch):
    _needs_operator_run(rig)
    assert rig.service._run_unsettled(rig.conv.id)
    with rig.st.store._write() as conn:
        conn.execute("UPDATE console_conversations SET last_activity_at=0 WHERE id=?", (rig.conv.id,))
    rig.service.tick()
    assert rig.console.get(rig.conv.id).state == "open"
