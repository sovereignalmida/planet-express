"""Safe prune as a typed runbook (slice 5b-3, D38): the rollback-candidate gate now reads the
database and fails closed, where the old JSON file failed open."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import FakeNotifier
from planet_express.core.store import Store


class Commands:
    def __init__(self, store):
        self._store = store
        self.runs = []

    def run_automatic(self, runbook, *, origin, target_key, operator=None):
        from planet_express.application.command_service import AutomaticResult
        self.runs.append((origin, target_key))
        return AutomaticResult("passed", "freed 1.2GB", "e1", [])


class Unreadable:
    class _Store:
        def any_open_rollback_candidate(self, now):
            raise OSError("database is locked")

    def __init__(self):
        self._store = self._Store()
        self.runs = []

    def run_automatic(self, runbook, **_):
        raise AssertionError("a prune must not run while the window cannot be read")


@pytest.fixture
def state():
    s = fw.PipelineState()
    s.transition(fw.PipelineState.RUNNING)
    return s


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(fw, "_root_disk_alert", lambda snap: {"used_pct": 91, "alert": "high"})
    monkeypatch.setattr(fw, "_has_incomplete_stacks", lambda snap: False)
    monkeypatch.setattr(fw, "_safe_to_prune", lambda snap: True)


def _store(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.init()
    return store


def _open_candidate(store, *, expires_at):
    approval, _ = store.propose(action="docker.restart_service", target_key="media/sonarr",
                                target={"stack": "media", "service": "sonarr", "container": "c"},
                                risk="R1", requested_via="telegram", requested_by="t")
    execution = store.approve_and_create_execution(approval["id"], decided_by="t", arrived_at=1)
    store.create_steps(execution["id"], [{"type": "update.canary",
                                          "params": {"stack": "media", "service": "sonarr"},
                                          "binding": {}}])
    store.open_rollback_candidate(execution["id"], 1, stack="media", service="sonarr",
                                  image_reference="nginx:1.27", old_image_id="a" * 64,
                                  expires_at=expires_at)
    return execution["id"]


def test_prune_runs_as_a_typed_runbook_when_nothing_is_in_flight(ready, state, tmp_path):
    commands = Commands(_store(tmp_path))
    notifier = FakeNotifier()
    fw.maybe_run_safe_prune({}, notifier, state, commands)
    assert commands.runs == [("system", "prune:safe")]
    assert any("Safe prune ran" in m and "1.2GB" in m for m in notifier.notifications)


def test_an_open_rollback_window_stops_the_prune(ready, state, tmp_path):
    store = _store(tmp_path)
    _open_candidate(store, expires_at=time_far_future())
    commands = Commands(store)
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, commands)
    assert commands.runs == []


def test_an_expired_window_no_longer_stops_the_prune(ready, state, tmp_path):
    store = _store(tmp_path)
    _open_candidate(store, expires_at=1.0)
    commands = Commands(store)
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, commands)
    assert commands.runs == [("system", "prune:safe")]


def test_a_closed_window_no_longer_stops_the_prune(ready, state, tmp_path):
    store = _store(tmp_path)
    execution_id = _open_candidate(store, expires_at=time_far_future())
    assert store.close_rollback_candidate(execution_id, 1)
    commands = Commands(store)
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, commands)
    assert commands.runs == [("system", "prune:safe")]


def test_a_window_that_cannot_be_read_stops_the_prune(ready, state):
    # The old JSON gate caught its own errors and answered "nothing pending" — the wrong way for a
    # safety gate to fail.
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, Unreadable())


def test_no_command_service_means_no_prune(ready, state):
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, None)


def time_far_future() -> float:
    import time
    return time.time() + 3600


class Failing(Commands):
    def run_automatic(self, runbook, *, origin, target_key, operator=None):
        from planet_express.application.command_service import AutomaticResult
        self.runs.append((origin, target_key))
        return AutomaticResult("failed", "images: failed (permission denied)", "e1", [])


def test_a_prune_that_did_not_complete_is_not_announced_as_success(ready, state, tmp_path):
    commands = Failing(_store(tmp_path))
    notifier = FakeNotifier()
    fw.maybe_run_safe_prune({}, notifier, state, commands)
    assert commands.runs == [("system", "prune:safe")]
    text = "\n".join(notifier.notifications)
    assert "did not complete" in text and "permission denied" in text
    assert "Safe prune ran automatically" not in text


def test_a_legacy_rollback_window_still_stops_the_prune(ready, state, tmp_path, monkeypatch):
    """The upgrade can land while the previous updater's window is open; nothing imports that file,
    so the gate keeps honouring it until its entries expire (Codex, T42)."""
    import json
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "rollback_candidates.json"
    now = datetime.now(timezone.utc)
    path.write_text(json.dumps({"candidates": [
        {"stack": "media", "service": "sonarr", "old_image_id": "sha256:1",
         "recorded_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat()},
    ]}))
    monkeypatch.setattr(fw, "ROLLBACK_CANDIDATES_FILE", path)
    commands = Commands(_store(tmp_path))
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, commands)
    assert commands.runs == []

    # ...and stops stopping it once they expire
    path.write_text(json.dumps({"candidates": [
        {"stack": "media", "service": "sonarr", "old_image_id": "sha256:1",
         "recorded_at": now.isoformat(), "expires_at": (now - timedelta(minutes=1)).isoformat()},
    ]}))
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, commands)
    assert commands.runs == [("system", "prune:safe")]


def test_an_unreadable_legacy_file_stops_the_prune(ready, state, tmp_path, monkeypatch):
    path = tmp_path / "rollback_candidates.json"
    path.write_text("{ this is not json")
    monkeypatch.setattr(fw, "ROLLBACK_CANDIDATES_FILE", path)
    commands = Commands(_store(tmp_path))
    fw.maybe_run_safe_prune({}, FakeNotifier(), state, commands)
    assert commands.runs == []
