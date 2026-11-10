"""Confirming a plan option end to end over a real approval flow, supervisor, checkpoint and verifier (the Repair test fakes): the repair is proposed from
the validated option, the operator authorises exactly the grant fingerprint they saw, and nothing that is stale, expired or out of scope runs."""
import json

import pytest
from repair_fakes import build, update

from config_schema import MissionConsoleConfig
from planet_express.console import execution as ex_mod
from planet_express.console import plan as plan_mod
from planet_express.console.api import ConsoleApi, ConsoleApiError
from planet_express.console.service import ConsoleService
from planet_express.console.store import ConsoleStore
from planet_express.missions.state import transition


def plan_doc(scope=None, exclude=None):
    option = {"name": "Restart media", "recommended": True, "summary": "Restart the media stack's apps.", "changes": ["media/qbittorrent"],
              "will_not_touch": ["the gateway", "any data volume"], "risk": {"level": "low", "text": "about a minute"}, "rolls_back_to": "the checkpoint",
              "effort": "2 min", "scope": scope or {"stacks": ["media"], "services": []}, "exclude": exclude or [],
              "checks": [{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}]}
    other = {**option, "name": "Do nothing yet", "recommended": False}
    return {"known": ["a"], "unsure": ["b"], "options": [option, other]}


class Rig:
    def __init__(self, tmp_path, **build_kw):
        # The conversation's own mission is queued, so the supervisor starts it first (it consumes the first scripted run); the Repair mission gets the next.
        build_kw.setdefault("behaviours", [{"script": [update("finished")]}] * 4)
        self.st = build(tmp_path, **build_kw)
        st = self.st
        self.console = ConsoleStore(st.store)
        self.cfg = MissionConsoleConfig()
        self.service = ConsoleService(st.ms, self.console, st.sup, self.cfg, uid_range=lambda: (60000, 60999), clock=st.clock)
        self.flow = st.flow
        self.api = ConsoleApi(self.service, self.console, st.ms, st.sup, self.cfg, clock=st.clock, flow=lambda: self.flow)
        self.conv = self.service.create("chris", "the media apps lost internet")

    def add_plan(self, doc=None):
        plan = plan_mod.parse(doc or plan_doc())
        return self.console.add_plan(self.conv.id, 1, plan_mod.canonical(plan), plan_mod.plan_hash(plan))

    def close(self):
        self.st.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_preparing_a_confirmation_proposes_the_repair_and_shows_what_will_be_authorised(rig):
    plan_id = rig.add_plan(plan_doc(exclude=["service:media/gluetun"]))
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    mission = rig.st.ms.get_mission(view["mission_id"])
    assert mission.tier == "repair" and mission.status == "awaiting_decision" and mission.grant_hash == view["grant_hash"]
    assert view["fingerprint"] == view["grant_hash"][:8] and view["operator"] == "chris"
    grant = mission.grant()
    assert grant.scope.stacks == ["media"] and grant.scope.hosts == ["local"]
    assert not {"docker", "sudo", "protected", "backup", "exclusions"} & set(grant.model_dump())            # trust mode: no access rules
    assert "service:media/gluetun" in mission.goal and "the gateway" in mission.goal                          # what to leave alone is in the brief
    assert "Restart the media stack's apps." in mission.goal and "PE will check afterwards" in mission.goal and "http://localhost/ answers" in mission.goal
    assert view["phases"] == ["grant"] and view["needs_elevation"] is False and "backup" not in view and "protected" not in view
    assert "Full access" in view["access"] and view["will_not_touch"] == ["the gateway", "any data volume", "service:media/gluetun"]
    assert view["scope"] == {"stacks": ["media"], "services": []} and view["checks"] == ["http://localhost/ answers"]
    assert view["limits"]["minutes"] >= 1
    assert rig.console.plan(plan_id)["status"] == "pending" and rig.st.status(mission.id) == "awaiting_decision"        # nothing has run, nothing is approved
    assert rig.console.execution(view["execution_id"])["status"] == "proposed"


def test_authorising_records_the_approval_as_the_operator_bound_to_the_fingerprint_and_the_repair_then_runs(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    done = rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    assert done["authorised"] is True
    assert rig.console.plan(plan_id)["status"] == "confirmed" and rig.console.execution(view["execution_id"])["status"] == "authorised"
    assert rig.console.execution(view["execution_id"])["authorised_by"] == "operator:chris"
    approvals = [e for e in rig.st.ms.events(view["mission_id"]) if e.kind == "decision" and "approval" in e.body_json]
    assert any(e.actor == "operator:chris" and view["grant_hash"] in e.body_json for e in approvals)
    assert any(m.role == "system" and "Authorised by chris" in m.text and view["fingerprint"] in m.text for m in rig.console.messages(rig.conv.id))
    assert rig.st.drive(view["mission_id"]) == "completed"                                                       # PE's own verification passed


def test_a_grant_that_changed_after_the_card_was_read_is_refused_as_stale(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_authorise(rig.conv.id, view["execution_id"], "f" * 64, "chris")
    assert err.value.code == "stale" and rig.st.status(view["mission_id"]) == "awaiting_decision"
    assert rig.console.plan(plan_id)["status"] == "pending"


def test_an_expired_or_scrapped_plan_can_never_be_confirmed_or_authorised(rig):
    from planet_express.console.api import PLAN_TTL_S
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.st.clock.t += PLAN_TTL_S + 5
    with pytest.raises(ConsoleApiError, match="expired") as err:
        rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    assert err.value.code == "conflict" and rig.console.plan(plan_id)["status"] == "expired" and rig.st.status(view["mission_id"]) == "awaiting_decision"
    fresh = rig.add_plan()
    rig.api.scrap_plan(rig.conv.id, "chris")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(rig.conv.id, fresh, 0, "chris")
    assert err.value.code == "conflict"


def test_a_tampered_stored_plan_does_not_match_its_hash_and_is_refused(rig):
    import sqlite3
    plan_id = rig.add_plan()
    with sqlite3.connect(rig.st.store.path) as db:
        db.execute("UPDATE console_plans SET plan_json=replace(plan_json, 'Restart media', 'Restart everything') WHERE id=?", (plan_id,))
    with pytest.raises(ConsoleApiError, match="does not match"):
        rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")


def test_a_scope_the_grant_checks_refuse_never_becomes_a_mission(tmp_path):
    rig = Rig(tmp_path)
    try:
        rig.flow.validator = lambda grant, plan: ["scope stack 'ghost' is not in the discovered inventory"]
        plan_id = rig.add_plan(plan_doc(scope={"stacks": ["ghost"], "services": []}))
        before = len(rig.st.ms.list_missions())
        with pytest.raises(ConsoleApiError) as err:
            rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
        assert err.value.code == "bad_request" and "not valid" in str(err.value) and "ghost" in str(err.value)
        assert rig.console.executions(rig.conv.id) == [] and rig.console.plan(plan_id)["status"] == "pending"
        assert len(rig.st.ms.list_missions()) == before                                                      # no mission was made at all
    finally:
        rig.close()


def test_without_the_repair_controls_confirming_says_so_and_changes_nothing(tmp_path):
    rig = Rig(tmp_path)
    try:
        rig.flow = None
        plan_id = rig.add_plan()
        with pytest.raises(ConsoleApiError) as err:
            rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
        assert err.value.code == "unavailable" and "Repair missions" in str(err.value)
    finally:
        rig.close()


def test_withdrawing_denies_the_proposed_repair_and_leaves_the_plan_pending(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    assert rig.api.confirm_withdraw(rig.conv.id, view["execution_id"], "chris") == {"withdrawn": True}
    assert rig.st.status(view["mission_id"]) == "cancelled" and rig.console.execution(view["execution_id"])["status"] == "withdrawn"
    assert rig.console.plan(plan_id)["status"] == "pending"
    again = rig.api.confirm_prepare(rig.conv.id, plan_id, 1, "chris")                                             # another option is still possible
    assert again["execution_id"] != view["execution_id"]
    with pytest.raises(ConsoleApiError):
        rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")                  # the withdrawn one cannot be authorised


def test_a_second_prepare_withdraws_the_first_and_only_one_execution_runs_at_a_time(rig):
    plan_id = rig.add_plan()
    first = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    second = rig.api.confirm_prepare(rig.conv.id, plan_id, 1, "chris")
    assert rig.console.execution(first["execution_id"])["status"] == "withdrawn" and rig.st.status(first["mission_id"]) == "cancelled"
    rig.api.confirm_authorise(rig.conv.id, second["execution_id"], second["grant_hash"], "chris")
    fresh = rig.add_plan()
    with pytest.raises(ConsoleApiError, match="already running") as err:
        rig.api.confirm_prepare(rig.conv.id, fresh, 0, "chris")
    assert err.value.code == "conflict"


def test_a_run_held_for_the_operator_is_named_with_where_to_close_it(rig):
    # live M-33: a run held at needs_operator ran nothing, yet blocked a new confirmation with "wait for it to finish, or stop it"
    plan_id = rig.add_plan()
    first = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.api.confirm_authorise(rig.conv.id, first["execution_id"], first["grant_hash"], "chris")
    with rig.st.store._write() as conn:
        conn.execute("UPDATE missions SET status='needs_operator' WHERE id=?", (first["mission_id"],))
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
    assert err.value.code == "conflict" and first["mission_id"] in str(err.value) and "is waiting for you" in str(err.value)
    assert "Missions" in str(err.value) and "stop it first" not in str(err.value)
    # rc14.7.5: the message follows the run's phase. recovery_required that lasts is the operator's (recovery passes through it in one
    # step otherwise), and the run reads "Needs you" with a rollback offered, so it is "waiting for you" here too.
    with rig.st.store._write() as conn:
        conn.execute("UPDATE missions SET status='recovery_required' WHERE id=?", (first["mission_id"],))
    with pytest.raises(ConsoleApiError, match="is waiting for you"):
        rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
    with rig.st.store._write() as conn:                      # a run still going is "already running"
        conn.execute("UPDATE missions SET status='executing' WHERE id=?", (first["mission_id"],))
    with pytest.raises(ConsoleApiError, match="already running"):
        rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")


def test_an_option_that_does_not_exist_and_another_conversations_plan_are_refused(rig):
    plan_id = rig.add_plan()
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(rig.conv.id, plan_id, 2, "chris")
    assert err.value.code == "bad_request"
    other = rig.service.create("chris", "another")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(other.id, plan_id, 0, "chris")
    assert err.value.code == "not_found"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(rig.conv.id, 99999, 0, "chris")
    assert err.value.code == "not_found"


def test_a_closed_conversation_confirms_nothing(rig):
    plan_id = rig.add_plan()
    rig.service.archive(rig.conv.id, "chris")
    with pytest.raises(ConsoleApiError, match="closed"):
        rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")


def test_the_execution_belongs_to_its_conversation_and_to_one_status_at_a_time(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    other = rig.service.create("chris", "another")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_authorise(other.id, view["execution_id"], view["grant_hash"], "chris")
    assert err.value.code == "not_found"
    rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")                 # twice
    assert err.value.code == "conflict"


def test_there_is_no_backup_floor_and_an_older_pages_no_backup_flag_changes_nothing(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris", no_backup=True)
    assert view["phases"] == ["grant"] and view["needs_elevation"] is False
    assert rig.st.status(view["mission_id"]) == "awaiting_decision"                                             # still nothing approved


def test_a_plan_stored_before_trust_mode_is_refused_in_words(rig):
    import sqlite3
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    with sqlite3.connect(rig.st.store.path) as db:                  # what an rc13 plan row looks like: every option says restart_only
        row = db.execute("SELECT plan_json FROM console_plans WHERE id=?", (plan_id,)).fetchone()
        doc = json.loads(row[0])
        for opt in doc["options"]:
            opt["restart_only"] = True
        db.execute("UPDATE console_plans SET plan_json=? WHERE id=?", (json.dumps(doc), plan_id))
    for call in (lambda: rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris"),
                 lambda: rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")):
        with pytest.raises(ConsoleApiError, match="made before an update") as err:
            call()
        assert err.value.code == "conflict"
    assert rig.st.status(view["mission_id"]) == "awaiting_decision" and rig.st.runtimes[1:] == []


def test_a_run_approved_in_the_work_log_is_never_marked_withdrawn(rig):
    plan_id = rig.add_plan()
    first = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.st.approve_all(first["mission_id"])                                                                      # approved elsewhere, then the operator withdraws here
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_withdraw(rig.conv.id, first["execution_id"], "chris")
    assert err.value.code == "conflict" and "already approved" in str(err.value)
    assert rig.console.execution(first["execution_id"])["status"] == "authorised"                                # reconciled: it is a live run
    with pytest.raises(ConsoleApiError, match="already running"):
        rig.api.confirm_prepare(rig.conv.id, plan_id, 1, "chris")                                                # and a second run is blocked


def test_preparing_again_does_not_start_a_second_run_over_one_approved_elsewhere(rig):
    plan_id = rig.add_plan()
    first = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.st.approve_all(first["mission_id"])
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_prepare(rig.conv.id, plan_id, 1, "chris")
    assert err.value.code == "conflict" and rig.console.execution(first["execution_id"])["status"] == "authorised"
    assert len(rig.console.executions(rig.conv.id)) == 1


def test_authorising_a_grant_denied_elsewhere_starts_nothing(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.flow.deny("operator:me", view["mission_id"], view["grant_hash"])
    with pytest.raises(ConsoleApiError) as err:
        rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    assert err.value.code == "conflict" and rig.console.execution(view["execution_id"])["status"] == "proposed"
    assert rig.console.plan(plan_id)["status"] == "pending"


def test_the_conversation_status_follows_an_active_repair_not_the_last_answer(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    for step in ("investigating", "awaiting_decision"):                                                          # the conversation's own turn has finished
        transition(rig.st.store, rig.conv.mission_id, step, "core")
    shown = rig.api.feed(rig.conv.id)["conversation"]["status"]
    assert shown["group"] == "Working"
    assert rig.st.drive(view["mission_id"]) == "completed"
    done = ex_mod.conversation_status(rig.st.ms.get_mission(view["mission_id"]))
    assert done["text"] == "Done and verified" and done["group"] == "Recent"
    held = ex_mod.conversation_status(type("M", (), {"status": "recovery_required"})())
    assert held["group"] == "Needs you" and held["level"] == "crit"


def test_an_ordinary_authorisation_reports_that_it_used_no_elevation(rig):
    view = rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
    assert rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris", elevated=True)["used_elevation"] is False


def test_a_run_that_finished_does_not_hide_a_newer_plan_waiting_for_a_decision(rig):
    view = rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
    rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    for step in ("investigating", "awaiting_decision"):
        transition(rig.st.store, rig.conv.mission_id, step, "core")
    rig.st.drive(view["mission_id"])
    rig.console.consume(rig.console.pending(rig.conv.id).seq, 1) if rig.console.pending(rig.conv.id) else None
    rig.add_plan()
    shown = rig.api.feed(rig.conv.id)["conversation"]["status"]
    assert shown["group"] == "Needs you" and shown["text"] == "Needs your call", shown


def test_the_context_panel_follows_the_run_and_says_when_it_has_ended(rig):
    plan_id = rig.add_plan(plan_doc(exclude=["service:media/db"]))
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    before = rig.api.feed(rig.conv.id)["context"]
    assert before["scope"] == {"kind": "run", "stacks": ["media"], "services": []}                           # trust mode: no protected count
    assert rig.st.drive(view["mission_id"]) == "completed"
    after = rig.api.feed(rig.conv.id)["context"]
    assert after["scope"]["kind"] == "last_run"                              # an ended run is history, not something the operator can act on
    assert after["record"]["runs"] == 1 and after["record"]["events"] > before["record"]["events"]     # the run's own events are part of the record


# ── rc14.2: "go ahead" in the chat opens the Authorise card; it never approves and never reaches the agent ────────────────────
@pytest.mark.parametrize("text", ["do it", "Go ahead!", "apply it", "authorise", "Please go ahead.", "yes, do it"])
def test_a_go_ahead_with_a_valid_plan_opens_the_same_card_as_confirm_for_the_recommended_option(rig, text):
    plan_id = rig.add_plan()
    out = rig.api.post(rig.conv.id, "chris", text)
    assert out["option"] == 0 and out["confirm"]["phases"] == ["grant"] and out["confirm"]["fingerprint"] == out["confirm"]["grant_hash"][:8]
    ex = rig.console.execution(out["confirm"]["execution_id"])
    assert ex["status"] == "proposed" and ex["plan_id"] == plan_id                       # proposed only: nothing is approved by chat text
    assert rig.st.status(ex["mission_id"]) == "awaiting_decision"
    assert all(r["state"] == "pending" for r in rig.flow.card(ex["mission_id"])["required"])
    assert rig.console.pending(rig.conv.id) is None or rig.console.pending(rig.conv.id).text != text        # never sent to the agent
    said = [m for m in rig.console.messages(rig.conv.id) if m.text == text]
    assert said and said[-1].role == "operator" and said[-1].actor == "operator:chris" and said[-1].consumed_turn is not None
    assert any(m.role == "system" and m.text.startswith("PE opened the Authorise card for option A") for m in rig.console.messages(rig.conv.id))


def test_a_go_ahead_naming_an_option_opens_that_option(rig):
    rig.add_plan()
    assert rig.api.post(rig.conv.id, "chris", "go with option B")["option"] == 1


def test_anything_else_or_no_valid_plan_goes_to_the_agent_as_written(rig):
    rig.add_plan()
    out = rig.api.post(rig.conv.id, "chris", "yes but why is gluetun restarting?")
    assert "confirm" not in out and rig.console.executions(rig.conv.id) == []
    rig.api.scrap_plan(rig.conv.id, "chris")
    out = rig.api.post(rig.conv.id, "chris", "do it")                                   # no plan: the agent is asked for one (runtime)
    assert "confirm" not in out and rig.console.executions(rig.conv.id) == []


def test_the_go_ahead_phrases_are_whole_messages_only():
    for text in ("do it", "DO IT.", "go ahead", "ok, go ahead", "authorize it", "apply it", "let's do it", "approve option c"):
        assert plan_mod.goes_ahead(text)[0], text
    for text in ("do it later", "go ahead and explain", "yes but why", "no", "", "confirm the logs first", "what does option B do?"):
        assert not plan_mod.goes_ahead(text)[0], text
    assert plan_mod.goes_ahead("approve option c") == (True, 2) and plan_mod.goes_ahead("yes") == (False, None)   # a bare yes answers questions (review, rc14.2)


def test_a_completed_run_offers_undo_and_the_console_rolls_it_back_in_the_background(rig):
    plan_id = rig.add_plan()
    view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
    rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris")
    assert rig.st.drive(view["mission_id"]) == "completed"
    run = rig.api.feed(rig.conv.id)["execution"]
    assert run["controls"]["rollback"] is True and run["undo"] is True and run["status"]["text"] == "Done and verified"
    out = rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
    assert out["restore"] == "started"
    rig.st.sup._rolling_back[view["mission_id"]].result(timeout=30)
    assert rig.st.status(view["mission_id"]) == "rolled_back"
    assert any(m.text == "chris asked PE to undo this change by rolling back to the checkpoint." for m in rig.console.messages(rig.conv.id))
    run = rig.api.feed(rig.conv.id)["execution"]
    assert run["controls"]["rollback"] is False and run["undo"] is False and run["rollback"]["state"] == "done"
