"""A confirmed plan's run as the conversation shows it: plain status, timeline, a pause's reason and the operator's controls, PE's own verification
(a check that could not be run is its own result and never a pass), and each progress line posted into the thread exactly once."""
import json

import pytest
from repair_fakes import update
from test_console_confirm import Rig as ConfirmRig, plan_doc
from tripwire_fakes import touch
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
    assert first["status"] == {"text": "Starting", "level": "ok"} and first["controls"] == {"pause": False, "resume": False, "rollback": False, "takeover": False}
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


def test_an_operator_pause_and_resume_work_from_the_conversation_and_are_recorded_in_the_thread(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}, {"script": [update()] * 8}])
    try:
        view = rig.start_run()
        rig.st.sup.tick()
        assert rig.st.status(view["mission_id"]) == "executing"
        assert rig.feed()["execution"]["controls"]["pause"] is True
        done = rig.api.execution_action(rig.conv.id, view["execution_id"], "pause", "chris")
        assert done["done"] == "pause" and rig.st.status(view["mission_id"]) == "paused_tripwire"
        paused = rig.feed()["execution"]
        assert paused["status"] == {"text": "Paused by you", "level": "warn"} and paused["pause"]["by_operator"] is True and paused["pause"]["who"] == "operator:chris"
        assert paused["controls"] == {"pause": False, "resume": True, "rollback": True, "takeover": True}
        assert paused["pause"]["rollback_available"] is True and [s["state"] for s in paused["timeline"]][2] == "paused"
        assert "chris paused the run." in rig.thread()
        rig.service.tick()
        assert any(t.startswith("The run is paused.") for t in rig.thread())
        rig.api.execution_action(rig.conv.id, view["execution_id"], "resume", "chris")
        assert rig.st.status(view["mission_id"]) == "executing" and rig.feed()["execution"]["status"]["text"] == "Working"
    finally:
        rig.close()


def test_a_tripwire_pause_is_shown_with_what_changed_and_never_as_the_operators(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}, {"script": [update()] * 8}])
    try:
        view = rig.start_run()
        rig.st.sup.tick()
        touch(rig.st.rig.fs / "etc" / "hosts", "10.0.0.1 changed\n")
        rig.st.monitor.scan_once()
        paused = rig.feed()["execution"]
        assert paused["status"] == {"text": "Paused: something unexpected changed", "level": "crit"}
        assert paused["pause"]["by_operator"] is False and paused["pause"]["changes"] and paused["controls"]["resume"] is True
        rig.service.tick()
        assert any(t.startswith("PE paused the run:") for t in rig.thread())
        rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
        assert rig.st.status(view["mission_id"]) != "paused_tripwire"
    finally:
        rig.close()


def test_take_over_stops_the_agent_and_says_plainly_that_pe_checks_nothing_until_asked(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}, {"script": [update()] * 8}])
    try:
        view = rig.start_run()
        rig.st.sup.tick()
        rig.api.execution_action(rig.conv.id, view["execution_id"], "takeover", "chris")
        assert any("took over" in t and "PE checks nothing until you roll back" in t for t in rig.thread())
    finally:
        rig.close()


def test_actions_are_refused_when_the_run_is_not_in_a_state_that_allows_them_and_for_other_conversations_runs(rig):
    view = rig.start_run()
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(rig.conv.id, view["execution_id"], "resume", "chris")             # not paused
    assert err.value.code in ("conflict", "bad_request")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(rig.conv.id, view["execution_id"], "explode", "chris")
    assert err.value.code == "bad_request"
    other = rig.service.create("chris", "another")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(other.id, view["execution_id"], "pause", "chris")
    assert err.value.code == "not_found"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.execution_action(rig.conv.id, 9999, "pause", "chris")
    assert err.value.code == "not_found"


def test_the_controls_follow_the_state_table():
    from types import SimpleNamespace
    for status, expected in (("executing", {"pause", "takeover"}), ("paused_tripwire", {"resume", "rollback", "takeover"}),
                             ("needs_operator", {"rollback"}), ("verifying", set()), ("completed", set()), ("awaiting_decision", set())):
        got = {k for k, v in ex_mod.controls(SimpleNamespace(status=status)).items() if v}
        assert got == expected, status


def test_a_rollback_reports_what_the_restore_did_so_a_refusal_is_never_a_silent_success(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}, {"script": [update()] * 8}])
    try:
        view = rig.start_run()
        rig.st.sup.tick()
        rig.api.execution_action(rig.conv.id, view["execution_id"], "pause", "chris")
        out = rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
        assert out["done"] == "rollback" and out["restore"] in ("done", "refused", "failed", "unknown") and isinstance(out["reason"], str)
    finally:
        rig.close()


def test_an_operator_pause_shows_what_changed_since_the_last_scan_so_a_resume_cannot_hide_it(tmp_path):
    rig = Rig(tmp_path, behaviours=[{"script": [update("finished")]}, {"script": [update()] * 8}])
    try:
        view = rig.start_run()
        rig.st.sup.tick()
        touch(rig.st.rig.fs / "etc" / "hosts", "10.0.0.1 changed between scans\n")                       # no scan has run since
        rig.api.execution_action(rig.conv.id, view["execution_id"], "pause", "chris")
        paused = rig.feed()["execution"]["pause"]
        assert paused["by_operator"] is True and any("/etc/hosts" in c for c in paused["changes"])
    finally:
        rig.close()


def test_a_failed_verification_marks_the_check_step_not_the_run_step_whatever_state_it_ends_in():
    mission = type("M", (), {"status": "needs_operator", "checkpoint_id": "ck", "verification_result_json": "{}"})()
    states = {s["label"]: s["state"] for s in ex_mod.timeline(mission, "operator:chris")}
    assert states["Carrying out the plan"] == "done" and states["PE checks it worked"] == "bad"
    no_check = type("M", (), {"status": "needs_operator", "checkpoint_id": "ck", "verification_result_json": None})()
    assert {s["label"]: s["state"] for s in ex_mod.timeline(no_check, None)}["Carrying out the plan"] == "bad"
