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

import casa_scruffy
import config


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
    assert b"cockpit.css" in resp.data
    assert b"dashboard.css" not in resp.data


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


def test_widget_with_no_state_returns_200_unknown(tmp_path, monkeypatch):
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS",
        "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    resp = _client().get("/api/widget")
    assert resp.status_code == 200
    assert resp.content_type == "application/json"
    data = resp.get_json()
    assert data["status"] == "unknown"
    assert data["state_available"] is False


def test_widget_with_real_findings(tmp_path, monkeypatch):
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

    resp = _client().get("/api/widget")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "critical"
    assert data["open_findings"] == 1
    assert data["state_available"] is True


# ── 1r.1: certificate vault attention count (Codex finding on 1r) ───────────────
def _render_with_certs(tmp_path, monkeypatch, certs):
    monitor = tmp_path / "latest_monitor.json"
    monitor.write_text(json.dumps({"timestamp": "2026-09-15T12:00:00+00:00", "mode": "full", "certs": certs}))
    monkeypatch.setattr(config, "STATE_MONITOR", monitor)
    for attr in ("STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS", "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE"):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_traefik_routers", lambda: {"available": False, "routers": []})
    monkeypatch.setattr(casa_scruffy.casa_scruffy_net, "fetch_adguard_stats", lambda: {"available": False})
    resp = _client().get("/")
    assert resp.status_code == 200
    return resp.data.decode()


def test_cert_attention_count_includes_legacy_status_only_snapshots(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [
        {"domain": "a.example", "status": "expiring", "days_remaining": 3},
        {"domain": "b.example", "status": "renew_soon", "days_remaining": 20},
        {"domain": "c.example", "status": "valid", "days_remaining": 200},
    ])
    assert "3 collected" in html
    assert "2 need attention" in html


def test_cert_attention_count_uses_tier_when_present(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [
        {"domain": "a.example", "tier": "expired", "days_left": -2},
        {"domain": "b.example", "tier": "valid", "days_left": 90},
    ])
    assert "1 needs attention" in html


def test_cert_attention_chip_absent_when_all_valid(tmp_path, monkeypatch):
    html = _render_with_certs(tmp_path, monkeypatch, [
        {"domain": "a.example", "tier": "valid", "days_left": 90},
        {"domain": "b.example", "status": "valid", "days_remaining": 120},
    ])
    assert "need attention" not in html and "needs attention" not in html
