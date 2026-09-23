"""casa_farnsworth's planning step (slice 5b-2): typed runbooks by default, legacy plans behind the
switch, and refusals that reach the operator instead of a card."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import FakeNotifier
from planet_express.core.store import Store
from planet_express.execution import actions
from tests.binding_fakes import FakeBinder

FINDINGS = {"findings": [{"id": "f1", "severity": "HIGH", "resource": "CASA_SONARR",
                          "description": "sonarr is unhealthy"}]}


class Commands:
    def __init__(self, result=None):
        self.binder = FakeBinder()
        self.proposed = []
        self.result = result or SimpleNamespace(ok=True, reason="awaiting approval", approval_id="a" * 12)

    def propose_plan(self, runbook, *, requested_by=None, finding_ids=None, origin="planner"):
        self.proposed.append((runbook, requested_by, finding_ids))
        return self.result


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.init()
    return store


def _typed(monkeypatch, response):
    monkeypatch.setattr(fw, "_devise_typed_plans", lambda findings: response)
    monkeypatch.setattr(
        actions, "container_compose_identities", lambda names, **_: {"CASA_SONARR": ("media", "sonarr")})
    monkeypatch.setattr(
        actions, "resolve_target",
        lambda stack, service, for_mutation=True, timeout=None: actions.Target(
            stack, service, f"{stack}-{service}-1"))


def test_a_typed_plan_becomes_one_proposal(monkeypatch, store):
    _typed(monkeypatch, '{"plans": [{"id": "p1", "title": "Restart sonarr", "finding_ids": ["f1"],'
                        ' "steps": [{"type": "service.restart", "container": "CASA_SONARR"}]}]}')
    commands, notifier = Commands(), FakeNotifier()
    fw._propose_typed_plans(notifier, FINDINGS, commands, store)
    assert len(commands.proposed) == 1
    runbook, requested_by, finding_ids = commands.proposed[0]
    assert runbook.title == "Restart sonarr" and requested_by == "Farnsworth"
    assert [s.type for s in runbook.steps] == ["service.restart"]
    assert finding_ids == ["f1"]
    assert notifier.notifications == []  # the card speaks for itself


def test_a_plan_the_catalogue_cannot_express_is_refused_recorded_and_explained(monkeypatch, store):
    _typed(monkeypatch, '{"plans": [{"id": "p1", "title": "Fix the mount", "finding_ids": ["f2"],'
                        ' "steps": [{"type": "shell", "params": {"command": "mount -a"}}]}],'
                        ' "needs_human": [{"finding_ids": ["f2"], "why": "fstab needs a human"}]}')
    commands, notifier = Commands(), FakeNotifier()
    fw._propose_typed_plans(notifier, FINDINGS, commands, store)
    assert commands.proposed == []
    text = "\n".join(notifier.notifications)
    assert "No typed remediation" in text and "fstab needs a human" in text and "unknown step type" in text
    events = [e for e in store.list_events() if e["kind"] == "planner.refused"]
    assert len(events) == 1 and "unknown step type" in events[0]["payload"]["reason"]


def test_a_refusal_from_the_command_service_is_reported_too(monkeypatch, store):
    _typed(monkeypatch, '{"plans": [{"id": "p1", "title": "Restart sonarr",'
                        ' "steps": [{"type": "service.restart", "container": "CASA_SONARR"}]}]}')
    refusing = Commands(result=SimpleNamespace(ok=False, reason="cooling down: last attempt 2m ago",
                                               approval_id=None))
    notifier = FakeNotifier()
    fw._propose_typed_plans(notifier, FINDINGS, refusing, store)
    assert "cooling down" in "\n".join(notifier.notifications)
    assert [e["kind"] for e in store.list_events()] == ["planner.refused"]


def test_unparseable_planner_output_never_takes_the_pipeline_down(monkeypatch, store):
    _typed(monkeypatch, "I am a language model, not JSON")
    commands, notifier = Commands(), FakeNotifier()
    fw._propose_typed_plans(notifier, FINDINGS, commands, store)
    assert commands.proposed == []
    assert "No plan" in "\n".join(notifier.notifications)
    assert [e["kind"] for e in store.list_events()] == ["planner.refused"]


def test_the_switch_chooses_between_typed_and_legacy_planning(monkeypatch, tmp_path):
    import config as config_module
    calls = []
    monkeypatch.setattr(fw, "_propose_typed_plans",
                        lambda notifier, findings, commands, store: calls.append("typed"))
    monkeypatch.setattr(fw, "plan", lambda findings: (calls.append("legacy"), {"plans": []})[1])
    monkeypatch.setattr(fw, "save_plans", lambda plans: None)
    monkeypatch.setattr(fw.leela, "run_full", lambda: {"timestamp": "2026-09-23T00:00:00+00:00"})
    monkeypatch.setattr(fw.hermes, "analyze", lambda snapshot: FINDINGS)
    monkeypatch.setattr(fw.hermes, "save_findings", lambda findings: None)
    monkeypatch.setattr(fw, "maybe_run_safe_prune", lambda *a, **k: None)
    monkeypatch.setattr(config_module, "STATE_MONITOR", tmp_path / "monitor.json")

    for enabled, expected in ((False, "typed"), (True, "legacy")):
        calls.clear()
        monkeypatch.setattr(config_module, "LEGACY_PLANS_ENABLED", enabled)
        state = fw.PipelineState()
        fw.run_pipeline(FakeNotifier(), state, "full", commands=Commands())
        assert calls == [expected]
        assert state.state == fw.PipelineState.IDLE
