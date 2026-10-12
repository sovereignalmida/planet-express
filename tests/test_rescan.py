"""After a fix, look again: the merged re-scan wish, the quiet re-scan, and the prune that is measured after it ran."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import FakeNotifier


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(fw.config, "STATE_STATUS", tmp_path / "run_status.json")
    monkeypatch.setattr(fw.config, "STATE_MONITOR", tmp_path / "monitor.json")
    monkeypatch.setattr(fw.config, "STATE_FINDINGS", tmp_path / "findings.json")
    monkeypatch.setattr(fw.config, "ensure_dirs", lambda: None)


class Window:
    reason = "backup"


def state_with_starter(maintenance=lambda: None):
    state = fw.PipelineState(maintenance=maintenance)
    started = []
    state.set_rescan_starter(lambda: started.append(1))
    return state, started


# -- the wish ---------------------------------------------------------------------------------------------------------
def test_a_request_when_everything_is_quiet_starts_one_scan_now():
    state, started = state_with_starter()
    state.request_rescan("fixed")
    assert started == [1]


def test_while_a_mutation_holds_the_lock_nothing_starts_until_it_is_released_and_then_exactly_once():
    state, started = state_with_starter()
    assert state.try_begin_mutation("act:1")
    for reason in ("one", "two", "three"):                 # a pass that fixed 26 services asks 26 times
        state.request_rescan(reason)
    assert started == []                                    # the scan would have refused the next action
    state.end_mutation("act:1")
    assert started == [1]
    state.end_mutation("act:1")                              # a stale release changes nothing
    assert started == [1]


def test_a_running_scan_defers_the_re_scan_to_when_it_finishes():
    state, started = state_with_starter()
    assert fw.admit_pipeline_run(state).ok
    state.request_rescan("fixed")
    assert started == []
    state.transition(fw.PipelineState.IDLE)
    assert started == [1]


def test_nothing_starts_during_maintenance_and_the_wish_is_kept():
    window = [Window()]
    state, started = state_with_starter(maintenance=lambda: window[0] if window else None)
    state.request_rescan("fixed")
    assert started == []
    window.clear()
    state.transition(fw.PipelineState.IDLE)                  # the next return to idle fires it
    assert started == [1]


def test_a_starter_that_fails_never_breaks_the_release_of_the_lock():
    state = fw.PipelineState()

    def boom():
        raise RuntimeError("thread limit")
    state.set_rescan_starter(boom)
    assert state.try_begin_mutation("act:1")
    state.request_rescan("fixed")
    state.end_mutation("act:1")
    assert state.mutation_owner is None and state.try_begin_mutation("act:2")     # the lock is free and usable


def test_without_a_starter_a_request_is_harmless():
    state = fw.PipelineState()
    state.request_rescan("fixed")
    assert state.state == fw.PipelineState.IDLE


def test_the_starter_runs_after_the_lock_is_gone_so_it_cannot_block_the_next_action():
    state = fw.PipelineState()
    seen = []
    state.set_rescan_starter(lambda: seen.append((state.mutation_owner, state.try_begin_mutation("next"))))
    assert state.try_begin_mutation("act:1")
    state.request_rescan("fixed")
    state.end_mutation("act:1")
    assert seen == [(None, True)]


# -- the quiet scan -----------------------------------------------------------------------------------------------------
def snapshot(disk="60%"):
    return {"timestamp": "2026-10-11T10:00:00+00:00", "mode": "full", "containers": [], "disk": [{"mount": "/", "used_pct": disk}]}


def stub_pipeline(monkeypatch, findings, snap=None):
    monkeypatch.setattr(fw.leela, "run_full", lambda: snap or snapshot())
    monkeypatch.setattr(fw, "maybe_run_safe_prune", lambda *a: False)
    monkeypatch.setattr(fw.hermes, "analyze", lambda s: findings)
    monkeypatch.setattr(fw.hermes, "save_findings", lambda f: fw.config.STATE_FINDINGS.write_text(__import__("json").dumps(f)))
    planned = []
    monkeypatch.setattr(fw, "_propose_typed_plans", lambda *a: planned.append(1))
    return planned


def finding(resource, severity="MEDIUM", category="container"):
    return {"id": "f1", "severity": severity, "category": category, "resource": resource,
            "description": "d", "suggested_action": "a"}


def save_previous(*findings):
    fw.config.STATE_FINDINGS.write_text(__import__("json").dumps({"findings": list(findings)}))


def test_a_quiet_re_scan_with_nothing_new_says_nothing_and_plans_nothing(monkeypatch):
    save_previous(finding("CASA_X"))
    planned = stub_pipeline(monkeypatch, {"findings": [finding("CASA_X")]})
    n = FakeNotifier()
    fw.run_pipeline(n, fw.PipelineState(), quiet=True)
    assert n.notifications == [] and planned == []


def test_a_quiet_re_scan_that_finds_something_new_reports_and_plans_like_a_normal_scan(monkeypatch):
    save_previous(finding("CASA_X"))
    planned = stub_pipeline(monkeypatch, {"findings": [finding("CASA_X"), finding("CASA_Y", "HIGH")]})
    n = FakeNotifier()
    fw.run_pipeline(n, fw.PipelineState(), quiet=True)
    assert any("CASA_Y" in m for m in n.notifications) and planned == [1]
    assert not any("Leela scanning" in m or "Hermes filing" in m for m in n.notifications)       # still no progress chatter


def test_a_quiet_re_scan_that_clears_findings_says_so_in_one_line(monkeypatch):
    save_previous(finding("CASA_X", "HIGH"), finding("CASA_Z"))
    stub_pipeline(monkeypatch, {"findings": [finding("CASA_Z")]})
    n = FakeNotifier()
    fw.run_pipeline(n, fw.PipelineState(), quiet=True)
    assert n.notifications == ["✅ Re-checked after the fix: 1 finding(s) cleared, 1 remaining."]


def test_findings_are_compared_by_what_they_are_not_by_their_renumbered_ids(monkeypatch):
    save_previous({**finding("CASA_X"), "id": "f7"})
    stub_pipeline(monkeypatch, {"findings": [{**finding("CASA_X"), "id": "f1"}]})
    n = FakeNotifier()
    fw.run_pipeline(n, fw.PipelineState(), quiet=True)
    assert n.notifications == []


def test_a_normal_scan_is_unchanged_and_still_announces_itself(monkeypatch):
    stub_pipeline(monkeypatch, {"findings": []})
    n = FakeNotifier()
    fw.run_pipeline(n, fw.PipelineState())
    assert any("Leela scanning" in m for m in n.notifications) and any("No findings" in m for m in n.notifications)


def test_a_quiet_scan_that_cannot_start_keeps_the_wish_and_tells_no_one(monkeypatch):
    state, started = state_with_starter()
    assert state.try_begin_mutation("act:1")
    n = FakeNotifier()
    fw.run_pipeline(n, state, quiet=True)                        # busy: cannot be admitted
    assert n.notifications == []
    state.end_mutation("act:1")
    assert started == [1]                                         # fires when the mutation ends


def test_the_re_scan_starter_waits_for_things_to_settle_then_runs_a_quiet_scan(monkeypatch):
    calls, slept = [], []
    monkeypatch.setattr(fw, "run_pipeline", lambda *a, **k: calls.append(k))
    start = fw.make_rescan_starter(FakeNotifier(), fw.PipelineState(), None, None, settle_seconds=7, sleep=slept.append)

    class Inline:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(fw.threading, "Thread", Inline)
    start()
    assert slept == [7] and calls[0]["quiet"] is True


# -- the prune is measured AFTER it ran ---------------------------------------------------------------------------------------
def test_after_a_prune_that_passed_the_disk_is_measured_again_before_anyone_reports_it(monkeypatch):
    seen = {}
    monkeypatch.setattr(fw.leela, "run_full", lambda: snapshot("81%"))
    monkeypatch.setattr(fw, "maybe_run_safe_prune", lambda *a: True)
    monkeypatch.setattr(fw.leela, "check_disk", lambda: [{"mount": "/", "used_pct": "69%"}])
    monkeypatch.setattr(fw.hermes, "analyze", lambda s: seen.update(hermes=s["disk"][0]["used_pct"]) or {"findings": []})
    monkeypatch.setattr(fw.hermes, "save_findings", lambda f: None)
    fw.run_pipeline(FakeNotifier(), fw.PipelineState())
    assert seen["hermes"] == "69%"                                                       # Hermes saw the post-prune reading
    import json
    assert json.loads(fw.config.STATE_MONITOR.read_text())["disk"][0]["used_pct"] == "69%"   # and so did the dashboard


def test_when_no_prune_ran_the_disk_is_not_measured_again(monkeypatch):
    monkeypatch.setattr(fw.leela, "run_full", lambda: snapshot("81%"))
    monkeypatch.setattr(fw, "maybe_run_safe_prune", lambda *a: False)
    monkeypatch.setattr(fw.leela, "check_disk", lambda: pytest.fail("a second disk read for no reason"))
    monkeypatch.setattr(fw.hermes, "analyze", lambda s: {"findings": []})
    monkeypatch.setattr(fw.hermes, "save_findings", lambda f: None)
    fw.run_pipeline(FakeNotifier(), fw.PipelineState())


def test_a_prune_that_raises_never_stops_the_scan(monkeypatch):
    monkeypatch.setattr(fw.leela, "run_full", lambda: snapshot("81%"))

    def boom(*a):
        raise RuntimeError("docker is gone")
    monkeypatch.setattr(fw, "maybe_run_safe_prune", boom)
    seen = []
    monkeypatch.setattr(fw.hermes, "analyze", lambda s: seen.append(s["disk"][0]["used_pct"]) or {"findings": []})
    monkeypatch.setattr(fw.hermes, "save_findings", lambda f: None)
    fw.run_pipeline(FakeNotifier(), fw.PipelineState())
    assert seen == ["81%"]


# -- the update pass asks once ----------------------------------------------------------------------------------------------
def test_an_update_pass_that_changed_something_asks_for_one_scan_and_a_quiet_pass_asks_for_none(monkeypatch):
    for results, expected in (([{"status": "no_change"}, {"status": "skipped"}], 0),
                              ([{"status": "updated"}] * 26 + [{"status": "no_change"}], 1),
                              ([{"status": "rolled_back"}], 1)):
        state, started = state_with_starter()
        monkeypatch.setattr(fw.zoidberg, "run_update_pass", lambda results=results, **kw: results)
        assert fw._run_scheduled_update_pass(None, state, commands=object()) == "ran"
        assert len(started) == expected


# -- requests during the settle pause join the pending scan ------------------------------------------------------------------
def test_requests_during_the_settle_pause_join_the_pending_scan_instead_of_starting_more():
    state, started = state_with_starter()
    state.request_rescan("first fix")
    assert started == [1]                                    # handed to a worker that is still waiting to settle
    state.request_rescan("second fix, a moment later")
    state.request_rescan("third")
    assert started == [1]                                    # joined, not multiplied
    state.rescan_claimed()                                    # the worker reached the front door: the scan is looking now
    state.request_rescan("a fix after that scan began")       # it may have looked before this one: a genuinely new wish
    assert started == [1, 1]


def test_a_worker_that_died_before_claiming_cannot_stop_re_scans_for_good(monkeypatch):
    state, started = state_with_starter()
    state.request_rescan("fixed")
    assert started == [1]
    later = fw.time.monotonic() + state.RESCAN_PENDING_TTL_SECONDS + 1
    monkeypatch.setattr(fw.time, "monotonic", lambda: later)
    state.request_rescan("fixed again")
    assert started == [1, 1]


def test_a_failed_start_releases_the_pending_marker():
    state = fw.PipelineState()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("no threads")
    state.set_rescan_starter(flaky)
    state.request_rescan("fixed")
    state.request_rescan("fixed again")
    assert calls == [1, 1]


def test_the_worker_claims_the_slot_whether_or_not_it_was_admitted(monkeypatch):
    state, started = state_with_starter()
    state.request_rescan("fixed")
    assert state._rescan_pending()
    assert state.try_begin_mutation("act:1")                 # the pipeline is busy when the worker arrives
    stub_pipeline(monkeypatch, {"findings": []})
    fw.run_pipeline(FakeNotifier(), state, quiet=True)
    assert not state._rescan_pending()                        # claimed even though it could not run
    state.end_mutation("act:1")
    assert started == [1, 1]                                  # and its wish was kept, so it runs when the mutation ends


def test_a_quiet_scan_reports_and_plans_only_the_new_findings(monkeypatch):
    save_previous(finding("CASA_X"), finding("CASA_Z", "HIGH"))
    stub_pipeline(monkeypatch, {"findings": [finding("CASA_X"), finding("CASA_Z", "HIGH"), finding("CASA_NEW", "HIGH")]})
    got = {}
    monkeypatch.setattr(fw, "_propose_typed_plans", lambda notifier, findings, *a: got.update(findings=findings))
    n = FakeNotifier()
    fw.run_pipeline(n, fw.PipelineState(), quiet=True)
    report = " ".join(n.notifications)
    assert "CASA_NEW" in report and "CASA_X" not in report and "CASA_Z" not in report
    assert [f["resource"] for f in got["findings"]["findings"]] == ["CASA_NEW"]
    import json
    assert len(json.loads(fw.config.STATE_FINDINGS.read_text())["findings"]) == 3          # the saved set is complete
