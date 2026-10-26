"""Confirming a plan option end to end over a real approval flow, supervisor, tripwire and verifier (the Repair test fakes): the repair is proposed from
the validated option, the operator authorises exactly the grant fingerprint they saw, and nothing that is stale, expired or out of scope runs."""
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
    assert grant.scope.stacks == ["media"] and grant.scope.hosts == ["local"] and grant.scope.paths == [] and grant.sudo.mode == "scoped" and grant.docker == "proxy"
    assert "service:media/gluetun" in grant.exclusions and "the gateway" in grant.exclusions                  # enforced and advisory exclusions both carried
    assert "Restart the media stack's apps." in mission.goal and "PE will check afterwards" in mission.goal and "http://localhost/ answers" in mission.goal
    assert view["backup"]["covered"] is True and view["phases"] == ["grant"] and view["needs_elevation"] is False
    assert view["scope"] == {"stacks": ["media"], "services": []} and view["checks"] == ["http://localhost/ answers"]
    assert view["limits"]["minutes"] >= 1 and view["protected"]["count"] > 0
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


def test_a_scope_with_no_recent_backup_is_refused_with_a_typed_code_and_can_be_prepared_only_when_the_operator_accepts_the_risk(tmp_path):
    rig = Rig(tmp_path, covered=False)
    try:
        rig.flow.validator = lambda grant, plan: [] if grant.backup.covered or grant.backup.acknowledged_no_backup else [
            "backup floor: the scope is not covered by a recent backup and no-backup is not acknowledged"]
        plan_id = rig.add_plan()
        with pytest.raises(ConsoleApiError) as err:
            rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris")
        assert err.value.code == "no_backup" and rig.console.executions(rig.conv.id) == []
        view = rig.api.confirm_prepare(rig.conv.id, plan_id, 0, "chris", no_backup=True)
        assert view["backup"]["covered"] is False and view["needs_elevation"] is True and "accepting that risk" in view["backup"]["text"]
        assert rig.st.status(view["mission_id"]) == "awaiting_decision"                                         # still nothing approved
    finally:
        rig.close()


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
    done = ex_mod.conversation_status(rig.st.ms.get_mission(view["mission_id"]), live=False)
    assert done["text"] == "Done and verified" and done["group"] == "Recent"
    paused = ex_mod.conversation_status(type("M", (), {"status": "paused_tripwire"})(), live=True)
    assert paused["group"] == "Needs you" and paused["level"] == "crit"


def test_authorising_without_the_elevation_a_later_phase_needs_approves_nothing(tmp_path):
    rig = Rig(tmp_path, covered=False)
    try:
        rig.flow.validator = lambda grant, plan: [] if grant.backup.covered or grant.backup.acknowledged_no_backup else [
            "backup floor: the scope is not covered by a recent backup and no-backup is not acknowledged"]
        view = rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris", no_backup=True)
        assert view["phases"] == ["grant", "no_backup"]
        with pytest.raises(ConsoleApiError) as err:
            rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris", elevated=False)
        assert err.value.code == "elevation_required"
        assert all(r["state"] == "pending" for r in rig.flow.card(view["mission_id"])["required"])           # not even the grant phase was approved
        assert rig.api.confirm_withdraw(rig.conv.id, view["execution_id"], "chris") == {"withdrawn": True}    # so it can still be backed out of
        again = rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris", no_backup=True)
        done = rig.api.confirm_authorise(rig.conv.id, again["execution_id"], again["grant_hash"], "chris", elevated=True)
        assert done["authorised"] is True and done["used_elevation"] is True
    finally:
        rig.close()


def test_an_ordinary_authorisation_reports_that_it_used_no_elevation(rig):
    view = rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
    assert rig.api.confirm_authorise(rig.conv.id, view["execution_id"], view["grant_hash"], "chris", elevated=True)["used_elevation"] is False


def test_a_refusal_for_another_reason_that_mentions_backup_is_not_offered_as_a_missing_backup(tmp_path):
    rig = Rig(tmp_path)
    try:
        rig.flow.validator = lambda grant, plan: ["scope stack 'backups' is not in the discovered inventory", "backup floor: the scope is not covered"]
        with pytest.raises(ConsoleApiError) as err:
            rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
        assert err.value.code == "bad_request"
    finally:
        rig.close()


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
