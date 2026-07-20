"""
Route-level tests for casa_scruffy.py using Flask's test_client() -- no real socket,
no real Docker/host dependency. Same config.STATE_* monkeypatch pattern as
test_dashboard_data.py.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml"))

import config
import casa_scruffy


def _client():
    casa_scruffy.app.testing = True
    return casa_scruffy.app.test_client()


def test_index_with_no_state_returns_200_not_500(tmp_path, monkeypatch):
    # The single most important case per the design principle: a fresh install with
    # no pipeline run yet must never 500 -- it should render a "waiting" placeholder.
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS",
        "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"waiting for the first scheduled run" in resp.data


def test_index_renders_real_findings(tmp_path, monkeypatch):
    findings_path = tmp_path / "latest_findings.json"
    findings_path.write_text(json.dumps({
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [{
            "id": "f1", "severity": "CRITICAL",
            "resource": "backups.daily", "description": "Backup timer has not run in 9 days",
        }],
        "has_critical": True,
        "has_high": False,
    }))
    monkeypatch.setattr(config, "STATE_FINDINGS", findings_path)
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_PLAN", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_STATUS", tmp_path / "nope.json")
    monkeypatch.setattr(config, "ROLLBACK_CANDIDATES_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", tmp_path / "nope.json")

    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"backups.daily" in resp.data
    assert b"Backup timer has not run in 9 days" in resp.data
    assert b"1 critical" in resp.data
