"""planet_express/execution/policy.py (landing 1b): fail closed, R0 automatic, above R0 approval."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import actions, policy


def test_unknown_action_is_denied():
    d = policy.decide("docker.rm_everything")
    assert not d.allowed and "unknown action" in d.reason


def test_restart_needs_approval():
    d = policy.decide(actions.RESTART_SERVICE)
    assert d.allowed and d.needs_approval and d.risk == "R1"


def test_refused_target_is_denied_even_for_a_known_action():
    d = policy.decide(actions.RESTART_SERVICE, target_error="stack 'ai' is forbidden")
    assert not d.allowed and "stack 'ai' is forbidden" in d.reason


def test_r0_action_runs_automatically(monkeypatch):
    monkeypatch.setitem(actions.REGISTRY, "docker.logs_tail", actions.ActionSpec("docker.logs_tail", "R0", "logs"))
    d = policy.decide("docker.logs_tail")
    assert d.allowed and not d.needs_approval


def test_r4_action_is_never_allowed(monkeypatch):
    monkeypatch.setitem(actions.REGISTRY, "host.format_disk", actions.ActionSpec("host.format_disk", "R4", "no"))
    d = policy.decide("host.format_disk")
    assert not d.allowed


def test_unknown_risk_class_is_denied(monkeypatch):
    monkeypatch.setitem(actions.REGISTRY, "weird.thing", actions.ActionSpec("weird.thing", "R9", "?"))
    d = policy.decide("weird.thing")
    assert not d.allowed and "unknown risk class" in d.reason


def test_restart_action_declares_no_abort_rollback_or_resume():
    spec = actions.REGISTRY[actions.RESTART_SERVICE]
    assert (spec.abortable, spec.rollbackable, spec.resumable) == (False, False, False)
