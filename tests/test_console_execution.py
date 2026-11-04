"""A confirmed plan's run as the conversation shows it: plain status, timeline, the operator's rollback, PE's own verification
(a check that could not be run is its own result and never a pass), and each progress line posted into the thread exactly once."""
import json

import pytest
from test_console_confirm import Rig as ConfirmRig, plan_doc
from verify_fakes import media_down

from planet_express.console import execution as ex_mod
from planet_express.console.api import ConsoleApiError


class Rig(ConfirmRig):
    def start_run(self, behaviours=None, **kw):
        plan_id = self.add_plan(plan_doc(**kw))
        view = self.api.confirm_prepare(self.conv.id, plan_id, 0, "chris")
        self.api.confirm_authorise(self.conv.id, view["execution_id"], view["grant_hash"], "chris")
        return view

    def feed(self):
        return self.api.feed(self.conv.id)

    def thread(self):
        return [m.text for m in self.console.messages(self.conv.id) if m.role == "system"]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_a_run_reads_starting_then_working_then_done_and_verified_with_pes_checks_listed(rig):
    view = rig.start_run()
    first = rig.feed()["execution"]
    assert first["status"] == {"text": "Starting", "level": "ok"} and first["controls"] == {"rollback": False}
    assert [s["state"] for s in first["timeline"]] == ["done", "now", "todo", "todo"] and first["timeline"][0]["meta"] == "chris"
    assert first["fingerprint"] == view["fingerprint"] and first["name"] == "Restart media" and first["verification"] is None
    assert rig.st.drive(view["mission_id"]) == "completed"
    done = rig.feed()["execution"]
    assert done["status"] == {"text": "Done and verified", "level": "ok"} and done["finished"] is True
    assert [s["state"] for s in done["timeline"]] == ["done", "done", "done", "done"]
    v = done["verification"]
    assert v["by_pe"] is True and v["headline"] == "All 1 check passed" and v["level"] == "ok"
    assert v["checks"] == [{"state": "pass", "label": "http://localhost/ answers", "evidence": v["checks"][0]["evidence"]}]
    assert "awaiting_decision" not in json.dumps(done) and "paused_tripwire" not in json.dumps(done)             # no internal state names


def test_progress_is_reported_into_the_thread_once_each_in_pes_words(rig):
    view = rig.start_run()
    rig.st.drive(view["mission_id"])
    for _ in range(3):
        rig.service.tick()                                                                                  # reporting runs on the tick, and is idempotent
    lines = rig.thread()
    assert sum("checkpoint and is carrying out" in t for t in lines) <= 1
    assert sum(t.startswith("Done and verified by PE") for t in lines) == 1
    assert rig.console.execution(view["execution_id"])["status"] == "ended"
    rig.service.tick()
    assert sum(t.startswith("Done and verified by PE") for t in rig.thread()) == 1


def test_a_failed_verification_shows_which_check_failed_and_offers_the_next_steps(tmp_path):
    rig = Rig(tmp_path)
    try:
        view = rig.start_run()
        rig.st.sup._nothing_changed = lambda mission: False            # the run changed something: it waits for the operator
        rig.st.fresh = media_down()
        rig.st.drive(view["mission_id"])
        feed = rig.feed()["execution"]
        assert feed["status"]["level"] in ("crit", "high") and feed["controls"]["rollback"] is True
        v = feed["verification"]
        assert v is not None and v["level"] in ("high", "warn") and v["by_pe"] is True
        rig.service.tick()
        assert any("Look at the run above; you can roll back" in t for t in rig.thread())          # only the controls the state really offers are named
    finally:
        rig.close()


def test_check_states_a_check_that_could_not_run_is_never_a_pass_or_a_failure_of_the_thing_checked():
    from planet_express.missions.verify.plan import CheckResult
    assert ex_mod.check_state(CheckResult(id="a", check="http", ok=True, attempts=1, detail="ok")) == "pass"
    assert ex_mod.check_state(CheckResult(id="a", check="http", ok=False, attempts=2, detail="HTTP 503")) == "fail"
    for detail in ("bad params", "unknown check kind", "timed out", "error (OSError)"):
        assert ex_mod.check_state(CheckResult(id="a", check="http", ok=False, attempts=0 if "params" in detail or "unknown" in detail else 1,
                                              detail=detail)) == "could_not_check"


def test_the_verification_headline_counts_could_not_check_separately():
    from types import SimpleNamespace

    from planet_express.missions.verify import plan as vplan
    result = vplan.VerificationResult(status="failed", grant_hash="a" * 64, at=1, reason="r", checks=[
        vplan.CheckResult(id="a", check="http", ok=True, attempts=1, detail="ok"),
        vplan.CheckResult(id="b", check="http", ok=False, attempts=0, detail="bad params"),
        vplan.CheckResult(id="c", check="http", ok=False, attempts=1, detail="HTTP 503")])
    mission = SimpleNamespace(verification_result_json=result.canonical(), grant_hash="a" * 64,
                              verification_plan_json=json.dumps({"checks": [{"id": "c", "check": "http", "params": {"url": "http://x/"}}]}))
    v = ex_mod.verification_view(mission)
    assert [c["state"] for c in v["checks"]] == ["pass", "could_not_check", "fail"]
    assert v["headline"] == "1 of 3 failed: http://x/ answers" and v["level"] == "high"
    only_unknown = vplan.VerificationResult(status="failed", grant_hash="a" * 64, at=1, checks=[
        vplan.CheckResult(id="b", check="http", ok=False, attempts=0, detail="bad params")])
    mission.verification_result_json = only_unknown.canonical()
    assert ex_mod.verification_view(mission)["headline"] == "1 of 1 check could not be run"
    mission.grant_hash = "b" * 64
    assert ex_mod.verification_view(mission) is None                       # a result for another grant is worth nothing


def test_pause_resume_and_take_over_are_not_actions_any_more(rig):
    """Trust mode removed them with the tripwire: the Console offers no such control and refuses the words as not an action."""
    view = rig.start_run()
    rig.st.sup.tick()
    assert set(rig.feed()["execution"]["controls"]) == {"rollback"} and "pause" not in rig.feed()["execution"]
    for gone in ("pause", "resume", "takeover"):
        with pytest.raises(ConsoleApiError) as err:
            rig.api.execution_action(rig.conv.id, view["execution_id"], gone, "chris")
        assert err.value.code == "bad_request"


def test_actions_are_refused_when_the_run_is_not_in_a_state_that_allows_them_and_for_other_conversations_runs(rig):
    view = rig.start_run()
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")           # nothing to roll back yet
    assert err.value.code in ("conflict", "bad_request")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(rig.conv.id, view["execution_id"], "explode", "chris")
    assert err.value.code == "bad_request"
    other = rig.service.create("chris", "another")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(other.id, view["execution_id"], "rollback", "chris")
    assert err.value.code == "not_found"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(rig.conv.id, 9999, "rollback", "chris")
    assert err.value.code == "not_found"


def test_the_controls_follow_the_state_table():
    from types import SimpleNamespace
    for status, expected in (("executing", set()), ("paused_tripwire", set()), ("needs_operator", {"rollback"}),
                             ("recovery_required", {"rollback"}), ("rollback_pending", {"rollback"}),
                             ("verifying", set()), ("completed", set()), ("awaiting_decision", set())):
        got = {k for k, v in ex_mod.controls(SimpleNamespace(status=status)).items() if v}
        assert got == expected, status


def test_a_rollback_from_the_conversation_restores_the_checkpoint_and_reports_what_the_restore_did(tmp_path):
    rig = Rig(tmp_path)
    try:
        view = rig.start_run()
        compose = rig.st.env.compose
        original = compose.read_text()
        rig.st.sup._nothing_changed = lambda mission: False            # the run changed something: it waits for the operator
        rig.st.fresh = media_down()
        rig.st.drive(view["mission_id"])
        assert rig.st.status(view["mission_id"]) == "needs_operator" and rig.feed()["execution"]["controls"]["rollback"] is True
        compose.write_text(original + "# the run changed this\n")
        out = rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
        assert out["done"] == "rollback" and out["restore"] == "done" and isinstance(out["reason"], str)
        assert rig.st.status(view["mission_id"]) == "rolled_back" and compose.read_text() == original
        assert "chris asked PE to roll back to the checkpoint." in rig.thread()
    finally:
        rig.close()


def test_a_legacy_paused_row_reads_as_needing_the_operator_never_as_a_pause():
    from types import SimpleNamespace
    mission = SimpleNamespace(status="paused_tripwire", checkpoint_id="ck", verification_result_json=None)
    assert ex_mod.repair_status(mission) == ("Needs you: PE isn't sure what state this is in", "crit")
    assert ex_mod.conversation_status(mission, False)["group"] == "Needs you"
    assert "paused" not in {s["state"] for s in ex_mod.timeline(mission, None)}


def test_a_failed_verification_marks_the_check_step_not_the_run_step_whatever_state_it_ends_in():
    mission = type("M", (), {"status": "needs_operator", "checkpoint_id": "ck", "verification_result_json": "{}"})()
    states = {s["label"]: s["state"] for s in ex_mod.timeline(mission, "operator:chris")}
    assert states["Carrying out the plan"] == "done" and states["PE checks it worked"] == "bad"
    no_check = type("M", (), {"status": "needs_operator", "checkpoint_id": "ck", "verification_result_json": None})()
    assert {s["label"]: s["state"] for s in ex_mod.timeline(no_check, None)}["Carrying out the plan"] == "bad"
