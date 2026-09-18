import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault(
    "CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml")
)

import casa_farnsworth as fw
from notifier import FakeNotifier


def _snapshot(mode="full"):
    return {
        "timestamp": "2026-09-18T10:00:00+00:00",
        "mode": mode,
        "containers": [{"name": "CASA_APP", "status": "Up", "image": "app"}],
    }


class RecordingStore:
    def __init__(self, monitor_path, order):
        self.monitor_path = monitor_path
        self.order = order
        self.calls = []

    def reconcile_incidents(self, current_scan_id, timestamp, observations):
        self.order.append("reconcile")
        saved = json.loads(self.monitor_path.read_text())
        assert saved["timestamp"] == timestamp
        assert current_scan_id == fw.scan_id(saved)
        self.calls.append((current_scan_id, timestamp, observations))


def test_full_pipeline_reconciles_saved_snapshot_before_hermes(monkeypatch, tmp_path):
    monitor_path = tmp_path / "monitor.json"
    monkeypatch.setattr(fw.config, "STATE_MONITOR", monitor_path)
    monkeypatch.setattr(fw.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(fw.leela, "run_full", lambda: _snapshot())
    monkeypatch.setattr(fw, "maybe_run_safe_prune", lambda *args: None)
    order = []

    def analyze(snapshot):
        order.append("hermes")
        return {"findings": []}

    monkeypatch.setattr(fw.hermes, "analyze", analyze)
    monkeypatch.setattr(fw.hermes, "save_findings", lambda findings: None)
    store = RecordingStore(monitor_path, order)
    notifier = FakeNotifier()

    fw.run_pipeline(notifier, fw.PipelineState(), incident_store=store)

    assert order == ["reconcile", "hermes"]
    assert len(store.calls) == 1
    assert any(row["resource"] == "CASA_APP" for row in store.calls[0][2])


def test_incident_failure_notifies_but_hermes_continues(monkeypatch, tmp_path, caplog):
    monitor_path = tmp_path / "monitor.json"
    monkeypatch.setattr(fw.config, "STATE_MONITOR", monitor_path)
    monkeypatch.setattr(fw.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(fw.leela, "run_full", lambda: _snapshot())
    monkeypatch.setattr(fw, "maybe_run_safe_prune", lambda *args: None)
    analyzed = []
    monkeypatch.setattr(
        fw.hermes,
        "analyze",
        lambda snapshot: analyzed.append(snapshot) or {"findings": []},
    )
    monkeypatch.setattr(fw.hermes, "save_findings", lambda findings: None)

    class BrokenStore:
        def reconcile_incidents(self, *args):
            raise RuntimeError("database unavailable")

    notifier = FakeNotifier()
    fw.run_pipeline(notifier, fw.PipelineState(), incident_store=BrokenStore())

    assert analyzed
    assert "Incident reconciliation failed" in caplog.text
    assert sum("Incident history was not updated" in message for message in notifier.notifications) == 1


def test_short_pipeline_modes_never_reconcile(monkeypatch, tmp_path):
    monkeypatch.setattr(fw.config, "STATE_MONITOR", tmp_path / "monitor.json")
    monkeypatch.setattr(fw.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(fw.leela, "run_status", lambda: _snapshot("status"))
    monkeypatch.setattr(fw, "_send_status_report", lambda *args: None)

    class ForbiddenStore:
        def reconcile_incidents(self, *args):
            raise AssertionError("short scan reconciled incidents")

    fw.run_pipeline(
        FakeNotifier(), fw.PipelineState(), mode="status", incident_store=ForbiddenStore()
    )
