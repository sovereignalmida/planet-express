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
    assert first["status"] == {"text": "Starting", "level": "ok"} and first["controls"] == {"rollback": False, "recheck": False, "keep": False}
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
    assert set(rig.feed()["execution"]["controls"]) == {"rollback", "recheck", "keep"} and "pause" not in rig.feed()["execution"]
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
        # The request returns at once: the restore runs on the supervisor's rollback worker, and its outcome is read from the record.
        assert out["done"] == "rollback" and out["restore"] == "started" and isinstance(out["reason"], str)
        rig.st.sup._rolling_back[view["mission_id"]].result(timeout=30)
        assert rig.st.status(view["mission_id"]) == "rolled_back" and compose.read_text() == original
        assert "chris asked PE to roll back to the checkpoint." in rig.thread()
        done = rig.feed()["execution"]
        assert done["rollback"] == {"state": "done", "reason": ""} and done["status"]["text"] == "Rolled back"
    finally:
        rig.close()


def _needs_operator_run(rig):
    view = rig.start_run()
    rig.st.sup._nothing_changed = lambda mission: False
    rig.st.fresh = media_down()
    rig.st.drive(view["mission_id"])
    assert rig.st.status(view["mission_id"]) == "needs_operator"
    rig.st.env.compose.write_text(rig.st.env.compose.read_text() + "# the run changed this\n")
    return view


def test_while_a_rollback_runs_the_run_reads_rolling_back_and_offers_nothing_to_click(tmp_path):
    import threading
    rig = Rig(tmp_path)
    try:
        view = _needs_operator_run(rig)
        gate, entered = threading.Event(), threading.Event()
        real = rig.st.sup.checkpoint.restore

        def slow(*a, **kw):
            entered.set()
            assert gate.wait(30)
            return real(*a, **kw)
        rig.st.sup.checkpoint.restore = slow
        out = rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
        assert out["restore"] == "started"
        assert entered.wait(30)
        running = rig.feed()
        x = running["execution"]
        assert x["rollback"] == {"state": "running", "reason": ""} and x["controls"] == {"rollback": False, "recheck": False, "keep": False}
        assert x["status"] == {"text": "Rolling back\u2026", "level": "warn"} and x["rolling_back"] is True
        with pytest.raises(ConsoleApiError) as err:                                  # a second click while it runs is refused
            rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
        assert err.value.code == "conflict"
        gate.set()
        rig.st.sup._rolling_back[view["mission_id"]].result(timeout=30)
        after = rig.feed()["execution"]
        assert after["rollback"]["state"] == "done" and after["rolling_back"] is False
    finally:
        rig.close()


def test_a_rollback_refused_over_drift_in_the_background_offers_the_explicit_choice(tmp_path):
    rig = Rig(tmp_path)
    try:
        view = _needs_operator_run(rig)
        real = rig.st.sup.checkpoint.capture_post

        def then_someone_else_edits(cid):
            real(cid)
            rig.st.env.compose.write_text("someone else edited this\n")
        rig.st.sup.checkpoint.capture_post = then_someone_else_edits
        rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris")
        rig.st.sup._rolling_back[view["mission_id"]].result(timeout=30)
        x = rig.feed()["execution"]
        assert x["rollback"]["state"] == "refused" and "changed" in x["rollback"]["reason"] and x["controls"]["rollback"] is True
        rig.st.sup.checkpoint.capture_post = real
        rig.api.execution_action(rig.conv.id, view["execution_id"], "rollback", "chris", ack_drift=True)
        rig.st.sup._rolling_back[view["mission_id"]].result(timeout=30)
        assert rig.st.status(view["mission_id"]) == "rolled_back"
    finally:
        rig.close()


def test_rollback_pending_waits_for_the_operator_and_says_so():
    from types import SimpleNamespace
    mission = SimpleNamespace(status="rollback_pending", outcome_flags=None)
    assert ex_mod.repair_status(mission) == ("Needs you: roll back to the checkpoint", "crit")
    assert ex_mod.repair_status(mission, rolling_back=True) == ("Rolling back\u2026", "warn")
    assert ex_mod.controls(mission) == {"rollback": True, "recheck": False, "keep": False} and ex_mod.controls(mission, rolling_back=True) == {"rollback": False, "recheck": False, "keep": False}
    assert ex_mod.conversation_status(mission)["group"] == "Needs you"
    assert ex_mod.conversation_status(mission, rolling_back=True)["text"] == "Rolling back"


def test_a_legacy_paused_row_reads_as_needing_the_operator_never_as_a_pause():
    from types import SimpleNamespace
    mission = SimpleNamespace(status="paused_tripwire", checkpoint_id="ck", verification_result_json=None)
    assert ex_mod.repair_status(mission) == ("Needs you: PE isn't sure what state this is in", "crit")
    assert ex_mod.conversation_status(mission, False)["group"] == "Needs you"
    assert "paused" not in {s["state"] for s in ex_mod.timeline(mission, None)}


def test_a_run_closed_because_nothing_snapshotted_changed_says_only_that(rig):
    """The checkpoint covers some files and service settings, not the whole host: the card never claims nothing changed."""
    try:
        view = rig.start_run()
        rig.st.fresh = media_down()
        assert rig.st.drive(view["mission_id"]) == "failed"
        status = rig.feed()["execution"]["status"]
        assert status == {"text": "Didn't work \u2014 nothing PE snapshotted was changed; check the run's record for anything else it did",
                          "level": "high"}
        assert "nothing on the host" not in rig.thread()
    finally:
        rig.close()


def test_any_other_failed_run_still_reads_didnt_work():
    from types import SimpleNamespace
    assert ex_mod.repair_status(SimpleNamespace(status="failed", outcome_flags=None)) == ("Didn't work", "high")
    assert ex_mod.repair_status(SimpleNamespace(status="failed", outcome_flags="partially_applied")) == ("Didn't work", "high")


def test_a_failed_verification_marks_the_check_step_not_the_run_step_whatever_state_it_ends_in():
    mission = type("M", (), {"status": "needs_operator", "checkpoint_id": "ck", "verification_result_json": "{}"})()
    states = {s["label"]: s["state"] for s in ex_mod.timeline(mission, "operator:chris")}
    assert states["Carrying out the plan"] == "done" and states["PE checks it worked"] == "bad"
    no_check = type("M", (), {"status": "needs_operator", "checkpoint_id": "ck", "verification_result_json": None})()
    assert {s["label"]: s["state"] for s in ex_mod.timeline(no_check, None)}["Carrying out the plan"] == "bad"


# ── rc14.7: a held run can be checked again or kept (live M-34) ──────────────────────────────────────────────────────
def _disk_full():
    from verify_fakes import snapshot
    return snapshot(disk=[{"mount": "/", "source": "/dev/sda", "used_pct": 81, "alert": "HIGH"}])


def test_a_new_host_wide_finding_is_shown_but_does_not_fail_the_run(rig):
    view = rig.start_run()
    rig.st.fresh = _disk_full()                     # the root disk passed its alert line during the run, every check passes
    assert rig.st.drive(view["mission_id"]) == "completed"
    assert rig.feed()["execution"]["verification"]["level"] == "ok"


def test_host_wide_is_a_filesystem_outside_the_stacks_root_that_only_contains_the_runs_scope():
    from planet_express.missions.verify.baseline import Finding, host_wide
    scope = ["host:local/stack:services", "host:local/path:/srv/stacks/services", "host:local/path:/home/u/apps/homelink/.env"]

    def wide(rid):
        return host_wide(Finding(kind="disk_alert", resource_id=rid, key_hash="0" * 64, severity="HIGH"), scope, stacks_root="/srv/stacks")
    assert wide("host:local/path:/") and wide("host:local/mount:/home") and wide("host:local/path:/home/u")
    assert not wide("host:local/path:/srv/stacks") and not wide("host:local/path:/srv/stacks/services/data")
    assert not wide("host:local/path:/home/u/apps/homelink/.env") and not wide("host:local/stack:services") and not wide("unscoped")
    gone = Finding(kind="mount_missing", resource_id="host:local/mount:/home", key_hash="0" * 64, severity="HIGH")
    assert not host_wide(gone, scope, stacks_root="/srv/stacks")          # a mount under the run's files going away still fails it


def test_the_verification_view_lists_host_wide_findings_as_a_note():
    from types import SimpleNamespace
    from planet_express.missions.verify.plan import FindingRef, ScanResult, VerificationResult
    result = VerificationResult(status="passed", grant_hash="a" * 64, at=1, scan=ScanResult(
        ok=True, new_on_host_count=1, new_on_host=[FindingRef(kind="disk_alert", resource_id="host:local/path:/", severity="HIGH")]))
    mission = SimpleNamespace(verification_result_json=result.model_dump_json(), grant_hash="a" * 64, verification_plan_json=None)
    assert ex_mod.verification_view(mission)["host_note"] == ["disk alert on path:/ (HIGH)"]


def test_a_held_run_can_be_checked_again_and_completes_when_it_now_passes(rig):
    view = _needs_operator_run(rig)
    feed = rig.feed()["execution"]
    assert feed["controls"]["recheck"] is True and feed["controls"]["keep"] is True
    assert "service missing on service:media/web (HIGH)" in json.loads(rig.st.ms.get_mission(view["mission_id"]).verification_result_json)["reason"]
    rig.st.fresh = media_down.__globals__["snapshot"]()               # fixed meanwhile (the operator restarted it)
    rig.api.execution_action(rig.conv.id, view["execution_id"], "recheck", "chris")
    assert rig.st.sup.verify_and_close(view["mission_id"], wait=True) == "completed"
    assert any("asked PE to check the run again" in t for t in rig.thread())
    events = rig.st.ms.events(view["mission_id"])
    assert any(json.loads(e.body_json).get("event") == "verify.recheck" and e.actor == "operator:chris" for e in events)


def test_a_check_again_that_fails_holds_the_run_again_and_never_rolls_back(rig):
    view = _needs_operator_run(rig)
    with rig.st.store._write() as conn:             # a grant that asked for a rollback on a failed check
        conn.execute("UPDATE missions SET grant_json=json_set(grant_json, '$.on_verify_fail', 'rollback') WHERE id=?", (view["mission_id"],))
    rig.st.sup.recheck(view["mission_id"], "operator:chris")
    assert rig.st.sup.verify_and_close(view["mission_id"], wait=True) == "needs_operator"
    rig.st.sup._nothing_changed = lambda mission: True             # nor is it closed on its own: the operator asked to look again
    rig.st.sup.recheck(view["mission_id"], "operator:chris")
    assert rig.st.sup.verify_and_close(view["mission_id"], wait=True) == "needs_operator"


def test_keep_the_changes_closes_the_run_as_failed_kept_by_the_operator_and_frees_its_resources(rig):
    view = _needs_operator_run(rig)
    with pytest.raises(ConsoleApiError):
        rig.api.execution_action(rig.conv.id, view["execution_id"], "pause", "chris")
    rig.api.execution_action(rig.conv.id, view["execution_id"], "keep", "chris")
    mission = rig.st.ms.get_mission(view["mission_id"])
    assert mission.status == "failed" and mission.outcome_flags == "kept_by_operator"
    rig.service.tick()                                                   # the one line about it is the reporter's, naming who kept it
    assert any("chris kept what the run changed" in t for t in rig.thread())
    with rig.st.store._connect() as conn:
        held = conn.execute("SELECT COUNT(*) FROM mission_leases WHERE mission_id=?",
                            (view["mission_id"],)).fetchone()[0]
    assert held == 0
    with pytest.raises(ConsoleApiError):                                 # once closed, neither action applies
        rig.api.execution_action(rig.conv.id, view["execution_id"], "keep", "chris")


def test_check_again_and_keep_are_operator_only_and_need_a_run_that_happened(rig):
    from planet_express.missions.supervisor import ResumeRefused
    view = _needs_operator_run(rig)
    with pytest.raises(ValueError):
        rig.st.sup.keep_and_close(view["mission_id"], "core")
    with pytest.raises(ValueError):
        rig.st.sup.recheck(view["mission_id"], "agent")
    with rig.st.store._write() as conn:
        conn.execute("UPDATE missions SET checkpoint_id=NULL WHERE id=?", (view["mission_id"],))
    with pytest.raises(ResumeRefused, match="never ran"):
        rig.st.sup.keep_and_close(view["mission_id"], "operator:chris")


def test_check_again_and_keep_apply_only_after_pes_checks_failed_for_this_grant():
    from types import SimpleNamespace
    from planet_express.missions.verify.plan import VerificationResult, held_after_failed_checks
    failed = VerificationResult(status="failed", grant_hash="a" * 64, at=1).model_dump_json()
    passed = VerificationResult(status="passed", grant_hash="a" * 64, at=1).model_dump_json()
    base = dict(status="needs_operator", tier="repair", checkpoint_id="C-1", grant_hash="a" * 64, verification_result_json=failed)
    assert held_after_failed_checks(SimpleNamespace(**base))
    assert not held_after_failed_checks(SimpleNamespace(**{**base, "verification_result_json": passed}))     # a failed undo of a passed run
    assert not held_after_failed_checks(SimpleNamespace(**{**base, "verification_result_json": None}))       # never checked
    assert not held_after_failed_checks(SimpleNamespace(**{**base, "grant_hash": "b" * 64}))                 # another grant's result
    assert not held_after_failed_checks(SimpleNamespace(**{**base, "checkpoint_id": None}))
    assert not held_after_failed_checks(SimpleNamespace(**{**base, "status": "rollback_pending"}))


def test_a_check_again_is_known_from_the_record_so_a_restart_mid_check_still_never_rolls_back(rig):
    view = _needs_operator_run(rig)
    sup = rig.st.sup
    assert sup._operator_recheck(view["mission_id"]) is False
    sup.verify_and_close = lambda mission_id, **kw: "verifying"       # the daemon stops before the check runs
    sup.recheck(view["mission_id"], "operator:chris")
    assert rig.st.status(view["mission_id"]) == "verifying" and sup._operator_recheck(view["mission_id"]) is True


def test_check_again_replaces_a_cancel_pe_was_holding(rig):
    view = _needs_operator_run(rig)
    with rig.st.store._write() as conn:                     # live M-34: Cancel pressed, PE held it (the run changed things)
        conn.execute("UPDATE missions SET cancel_requested_at=1, cancel_requested_by='operator:chris' WHERE id=?", (view["mission_id"],))
    rig.st.fresh = media_down.__globals__["snapshot"]()
    rig.st.sup.recheck(view["mission_id"], "operator:chris")
    rig.st.sup._finish_pending_repair_cancels()             # the pending-cancel pass no longer takes it back out of verifying
    assert rig.st.sup.verify_and_close(view["mission_id"], wait=True) == "completed"
    assert rig.st.ms.get_mission(view["mission_id"]).cancel_requested_at is None
