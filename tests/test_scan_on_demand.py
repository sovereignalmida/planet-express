"""The dashboard's SCAN button, end to end on the core side.

The control was `<a class="refresh-btn scan-btn" href="/">SCAN ✈</a>` — it said SCAN and
reloaded the page, and the Backups tab carried a line of copy admitting the dashboard could
not trigger one. These are the pieces that make it true: an admission check that answers
synchronously, and a starter that hands the run slot to the same pipeline /check runs.
"""
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import FakeNotifier
from planet_express.integrations.rpc import RpcError, build_core_handlers


class _Window:
    reason = "borg backup"


def _state(maintenance=lambda: None):
    return fw.PipelineState(maintenance=maintenance)


# ── admission ───────────────────────────────────────────────────────────────────

def test_an_idle_pipeline_admits_the_run_and_holds_the_slot():
    state = _state()
    admission = fw.admit_pipeline_run(state)
    assert admission.ok
    # The slot is HELD on ok=True -- whoever called owns putting it back.
    assert state.state == fw.PipelineState.RUNNING
    assert not fw.admit_pipeline_run(state).ok


def test_a_running_scan_refuses_a_second_one_with_a_reason():
    state = _state()
    assert fw.admit_pipeline_run(state).ok
    admission = fw.admit_pipeline_run(state)
    assert not admission.ok
    assert admission.kind == "running"
    assert admission.reason == "a scan is already running"


def test_a_host_mutation_refuses_a_scan_and_names_the_owner():
    """Scanning a host mid-mutation yields inconsistent snapshots and false remediation
    plans, which is why try_start_run() refuses it."""
    state = _state()
    assert state.try_begin_mutation("act:abc123")
    admission = fw.admit_pipeline_run(state)
    assert not admission.ok
    assert admission.kind == "busy" and "act:abc123" in admission.reason


def test_maintenance_refuses_an_operator_scan_rather_than_blocking_on_it():
    """A scheduled run waits for the window; someone holding a mouse button must not."""
    state = _state(maintenance=lambda: _Window())
    admission = fw.admit_pipeline_run(state, scheduled=False)
    assert not admission.ok
    assert admission.kind == "maintenance" and "borg backup" in admission.reason
    assert state.state == fw.PipelineState.IDLE


def test_the_refusal_message_is_the_one_telegram_already_sent():
    """run_pipeline's own refusals are unchanged; they route through Admission now."""
    assert fw.Admission(False, "maintenance", "borg backup").message == (
        "🧰 Maintenance in progress (borg backup) — scan not started.")
    assert fw.Admission(False, "busy", "act:abc").message == (
        "⏳ Host busy (act:abc) — scan not started. Try again shortly.")
    assert fw.Admission(False, "running").message == (
        "⚠️ Pipeline already running or awaiting approval. Please wait.")


def test_run_pipeline_refuses_and_says_so_when_it_admits_itself(monkeypatch):
    state = _state()
    assert state.try_begin_mutation("act:abc123")
    notifier = FakeNotifier()
    monkeypatch.setattr(fw.leela, "run_full", Mock(side_effect=AssertionError("must not scan")))

    fw.run_pipeline(notifier, state, "full")

    assert any("Host busy" in message for message in notifier.notifications)


def test_an_already_admitted_run_does_not_ask_for_the_slot_again(monkeypatch):
    """The slot is RUNNING when the thread starts, so a second admission would refuse its
    own caller's scan -- the button would report started and nothing would happen."""
    state = _state()
    assert fw.admit_pipeline_run(state).ok
    monkeypatch.setattr(fw.leela, "run_status", Mock(return_value={
        "timestamp": "2026-09-26T12:00:00+00:00", "agent": "casa_leela", "mode": "status"}))
    notifier = FakeNotifier()

    fw.run_pipeline(notifier, state, "status", admitted=True)

    assert state.state == fw.PipelineState.IDLE      # and it released the slot
    assert not any("already running" in message for message in notifier.notifications)


# ── the starter ─────────────────────────────────────────────────────────────────

def test_the_starter_reports_busy_without_starting_anything(monkeypatch):
    state = _state()
    assert state.try_begin_mutation("act:abc123")
    monkeypatch.setattr(threading, "Thread", Mock(side_effect=AssertionError("no thread")))
    start = fw.make_scan_starter(FakeNotifier(), state, None, None)

    assert start("alice") == {"status": "busy", "reason": "host busy (act:abc123)"}


def test_the_starter_admits_then_runs_the_same_pipeline_check_runs(monkeypatch):
    state = _state()
    started = {}

    class _Thread:
        def __init__(self, **kwargs):
            started.update(kwargs)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(threading, "Thread", _Thread)
    notifier = FakeNotifier()
    store, commands = object(), object()

    result = fw.make_scan_starter(notifier, state, store, commands)("alice")

    assert result == {"status": "started"}
    assert started["started"] is True
    assert started["args"] == ("alice",)
    assert state.state == fw.PipelineState.RUNNING
    # The announcement is NOT on the RPC path: nothing has been sent yet.
    assert notifier.notifications == []

    # Running the thread's target announces who asked, then runs the pipeline it was told to.
    ran = {}
    monkeypatch.setattr(fw, "run_pipeline", lambda *a, **k: ran.update(args=a, kwargs=k))
    started["target"]("alice")
    assert any("alice" in message for message in notifier.notifications)
    assert ran["args"] == (notifier, state, "full")
    assert ran["kwargs"] == {"incident_store": store, "commands": commands, "admitted": True}


def test_a_thread_that_will_not_start_gives_the_run_slot_back(monkeypatch):
    """Otherwise the slot is held by a run that never happened, and core refuses every scan
    -- from the dashboard, Telegram and the scheduler alike -- until it restarts."""
    class _Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", _Thread)
    state = _state()

    with pytest.raises(RuntimeError):
        fw.make_scan_starter(FakeNotifier(), state, None, None)("alice")

    assert state.state == fw.PipelineState.IDLE
    assert fw.admit_pipeline_run(state).ok


# ── the RPC method ──────────────────────────────────────────────────────────────

def _handlers(start_scan=None):
    return build_core_handlers(Mock(), Mock(), start_scan=start_scan)


def test_scan_start_passes_the_operator_through():
    seen = []
    handlers = _handlers(lambda operator: seen.append(operator) or {"status": "started"})
    assert handlers["scan.start"]({"operator": "alice"}) == {"status": "started"}
    assert seen == ["alice"]


@pytest.mark.parametrize("params", [
    {},
    {"operator": "alice", "extra": 1},
    {"operator": ""},
    {"operator": "Alice"},          # the operator charset is lowercase
    {"operator": "a" * 33},
    {"operator": 1},
    {"operator": "?"},              # the unauthenticated placeholder must not scan
])
def test_scan_start_refuses_bad_params(params):
    handlers = _handlers(lambda operator: {"status": "started"})
    with pytest.raises(RpcError) as error:
        handlers["scan.start"](params)
    assert error.value.code == "bad_request"


def test_scan_start_says_so_when_core_has_no_scanner_wired():
    """build_core_handlers is also built in tests and tooling without a pipeline behind it;
    that must be an honest error, not an AttributeError."""
    with pytest.raises(RpcError) as error:
        _handlers(None)["scan.start"]({"operator": "alice"})
    assert error.value.code == "unavailable"


def test_a_telegram_outage_does_not_stop_the_scan_it_was_announcing(monkeypatch):
    """The announcement used to sit between thread.start() and the RPC return: Telegram can
    block for tens of seconds while the dashboard's RPC client gives up in five, so the
    browser said the scan could not start while it was already running. A notify() that
    RAISED there was worse -- the run slot stayed held by a pipeline that never ran."""
    class _Thread:
        def __init__(self, **kwargs):
            self._target, self._args = kwargs["target"], kwargs["args"]

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(threading, "Thread", _Thread)
    ran = {}
    monkeypatch.setattr(fw, "run_pipeline", lambda *a, **k: ran.update(ok=True))
    notifier = FakeNotifier()
    notifier.notify = Mock(side_effect=RuntimeError("telegram is down"))
    state = _state()

    assert fw.make_scan_starter(notifier, state, None, None)("alice") == {"status": "started"}
    assert ran == {"ok": True}
