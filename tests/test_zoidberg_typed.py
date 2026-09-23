"""Zoidberg's canary pass on the engine (slice 5b-3, D34): one typed runbook per eligible service,
and today's status vocabulary derived from the step row rather than from ad-hoc shell results."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_zoidberg as zoidberg
from planet_express.application.command_service import AutomaticResult
from planet_express.execution import actions
from tests.binding_fakes import FakeBinder

STACK = Path("/home/casaroot/stacks/media")


class Commands:
    def __init__(self, result):
        self.binder = FakeBinder()
        self.result = result
        self.runbooks = []
        self.events = []

    def run_automatic(self, runbook, *, origin, target_key, operator=None):
        self.runbooks.append((runbook, origin, target_key))
        return self.result

    def record_event(self, kind, **fields):
        self.events.append((kind, fields))


def step(status, effect, reason, output=None):
    return {"n": 1, "type": "update.canary", "status": status, "effect": effect,
            "reason": reason, "output": output}


OUTPUT = {"image_reference": "nginx:1.27", "old_image_id": "a" * 64, "new_image_id": "b" * 64}


@pytest.fixture(autouse=True)
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "resolve_target",
                        lambda stack, service, **_: actions.Target(stack, service, "media-web-1"))
    monkeypatch.setattr(zoidberg, "service_image_ref", lambda stack_dir, service: reference[0])
    reference[0] = "nginx:1.27-alpine"
    monkeypatch.setattr(zoidberg, "_log_update_history", lambda entry: history.append(entry))
    monkeypatch.setattr(zoidberg, "_report_failed_update",
                        lambda *args, **kwargs: reported.append(args[3:]))
    history.clear()
    reported.clear()


history: list = []
reported: list = []
reference: list = ["nginx:1.27-alpine"]


def run(result):
    commands = Commands(result)
    return commands, zoidberg.canary_update_service(STACK, "web", tg=None, commands=commands)


def test_one_typed_runbook_per_service_with_the_zoidberg_origin():
    commands, outcome = run(AutomaticResult("passed", "ok", "e1",
                                            [step("passed", "applied", "updated", OUTPUT)]))
    runbook, origin, target_key = commands.runbooks[0]
    assert [s.type for s in runbook.steps] == ["update.canary"]
    assert runbook.steps[0].params == {"stack": "media", "service": "web"}
    assert runbook.steps[0].binding["container"] == "media-web-1"
    assert (origin, target_key) == ("zoidberg", "media/web")
    assert outcome["status"] == "updated"
    assert (outcome["old_id"], outcome["new_id"]) == (OUTPUT["old_image_id"], OUTPUT["new_image_id"])
    assert history[0]["status"] == "updated"


def test_an_unchanged_service_is_no_change_and_writes_no_history():
    _commands, outcome = run(AutomaticResult("passed", "ok", "e1",
                                             [step("passed", "not_applied", "already current",
                                                   OUTPUT)]))
    assert outcome["status"] == "no_change"
    assert history == [] and reported == []


def test_a_rolled_back_update_is_reported_and_logged():
    _commands, outcome = run(AutomaticResult(
        "failed", "x", "e1",
        [step("failed", "not_applied", "canary watch failed: restarting; rolled back to aaa", OUTPUT)]))
    assert outcome["status"] == "rolled_back"
    assert history[0]["status"] == "rolled_back"
    assert reported and reported[0][0] == "rolled_back"


def test_a_failed_rollback_is_reported_as_such():
    _commands, outcome = run(AutomaticResult(
        "failed", "x", "e1",
        [step("failed", "unknown", "watch failed; rollback also failed: tag failed", OUTPUT)]))
    assert outcome["status"] == "rollback_failed"
    assert reported[0][0] == "rollback_failed"


def test_an_unexpected_failure_after_dispatch_is_interrupted_not_no_rollback():
    """The rollback window is open and holds the old image; claiming there was none would send the
    operator looking for a problem that does not exist (Codex, T42)."""
    _commands, outcome = run(AutomaticResult(
        "failed", "x", "e1", [step("failed", "unknown", "crashed: docker went away")]))
    assert outcome["status"] == "interrupted"
    assert history[0]["status"] == "interrupted"
    assert reported[0][0] == "interrupted"


def test_a_pull_failure_keeps_its_own_status():
    _commands, outcome = run(AutomaticResult(
        "failed", "x", "e1", [step("failed", "not_applied", "pull failed: no such host")]))
    assert outcome["status"] == "pull_failed"
    assert history == [] and reported == []


def test_a_digest_pinned_service_never_becomes_a_runbook_but_is_recorded():
    reference[0] = "ghcr.io/org/app@sha256:" + "a" * 64
    commands, outcome = run(AutomaticResult("passed", "unused", "e1", []))
    assert outcome["status"] == "skipped" and "not a canary-eligible" in outcome["reason"]
    assert commands.runbooks == []          # nothing was built, nothing was executed
    assert commands.events[0][0] == "canary.ineligible"
    assert commands.events[0][1]["service"] == "web"


def test_a_build_only_service_is_recorded_the_same_way():
    reference[0] = None
    commands, outcome = run(AutomaticResult("passed", "unused", "e1", []))
    assert outcome["status"] == "skipped" and "build-only" in outcome["reason"]
    assert commands.runbooks == [] and commands.events[0][0] == "canary.ineligible"


def test_a_refused_run_reports_why_and_touches_nothing():
    _commands, outcome = run(AutomaticResult("refused", "update.canary media/web: cooling down"))
    assert outcome["status"] == "skipped" and "cooling down" in outcome["reason"]
    assert history == [] and reported == []


def test_a_real_pass_without_the_command_service_is_a_programming_error():
    with pytest.raises(ValueError, match="command service"):
        zoidberg.canary_update_service(STACK, "web", tg=None)
