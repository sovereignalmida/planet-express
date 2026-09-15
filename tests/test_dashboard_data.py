"""
Tests for dashboard_data.py -- pure logic, no real Docker/host dependency. Reuses the
same fixture shapes as tests/test_state_models.py and the same
monkeypatch.setattr(config, "STATE_*", ...) pattern tests/test_sudo_allowlist.py
establishes for pointing a module's config constant at a tmp_path fixture file.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml"))

import config
import dashboard_data


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


# ── load_*() ──────────────────────────────────────────────────────────────────────

def test_load_monitor_reads_fixture(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "containers": [{"name": "CASA_DOZZLE", "status": "Up"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.load_monitor()
    assert result is not None
    assert result.containers[0]["name"] == "CASA_DOZZLE"


def test_load_monitor_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "does_not_exist.json")
    assert dashboard_data.load_monitor() is None


def test_load_monitor_malformed_json_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.load_monitor() is None


def test_load_monitor_schema_violation_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    # mode must be one of full/status/updates -- this violates the Literal constraint.
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "not_a_real_mode"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.load_monitor() is None


def test_load_findings_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope.json")
    assert dashboard_data.load_findings() is None


def test_load_update_history_reads_fixture(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    _write(path, {
        "entries": [
            {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "dozzle",
             "old_id": "sha256:abc", "new_id": "sha256:def", "status": "updated"},
        ],
    })
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.load_update_history()
    assert result is not None
    assert result.entries[0].service == "dozzle"


# ── summarize_*() ─────────────────────────────────────────────────────────────────

def test_summarize_health_no_state_available(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope2.json")
    health = dashboard_data.summarize_health()
    assert health["status"] == "unknown"
    assert health["state_available"] is False
    assert health["open_findings"] == 0


def test_summarize_health_critical_status(tmp_path, monkeypatch):
    findings_path = tmp_path / "latest_findings.json"
    _write(findings_path, {
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [{"id": "f1", "severity": "CRITICAL"}],
        "has_critical": True,
        "has_high": False,
    })
    monkeypatch.setattr(config, "STATE_FINDINGS", findings_path)
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    health = dashboard_data.summarize_health()
    assert health["status"] == "critical"
    assert health["open_findings"] == 1


def test_summarize_health_reflects_monitor_severity_with_no_findings_yet(tmp_path, monkeypatch):
    # Regression test: the window between Leela writing STATE_MONITOR and Hermes
    # finishing analysis (every pipeline run has one) previously showed "ok" no
    # matter how bad the monitor snapshot looked, since only findings.has_critical/
    # has_high were consulted.
    monitor_path = tmp_path / "latest_monitor.json"
    _write(monitor_path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "containers": [{"name": "CASA_BAD", "status": "Restarting", "crash_looping": True}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", monitor_path)
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope.json")
    health = dashboard_data.summarize_health()
    assert health["status"] == "critical"
    assert health["crash_looping_count"] == 1


def test_summarize_health_disk_critical_without_findings(tmp_path, monkeypatch):
    monitor_path = tmp_path / "latest_monitor.json"
    _write(monitor_path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "disk": [{"mount": "/", "source": "/dev/sdb2", "used_pct": 95, "alert": "CRITICAL"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", monitor_path)
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope.json")
    assert dashboard_data.summarize_health()["status"] == "critical"


# ── mode-gated availability (an /updates or /status run overwrites STATE_MONITOR
# with a partial snapshot -- fields that mode doesn't populate must never be shown
# as confirmed-zero real data) ──────────────────────────────────────────────────

def test_summarize_containers_unavailable_after_updates_mode_run(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "updates"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.summarize_containers()
    assert result["available"] is False


def test_summarize_containers_available_after_status_mode_run(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00", "mode": "status",
        "containers": [{"name": "CASA_OK", "status": "Up"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.summarize_containers()
    assert result["available"] is True
    assert result["total"] == 1


def test_summarize_stack_completeness_unavailable_after_status_mode_run(tmp_path, monkeypatch):
    # stack_completeness is full-mode-only, unlike containers (full+status).
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "status", "containers": []})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.summarize_stack_completeness()["available"] is False


def test_summarize_disk_unavailable_after_updates_mode_run(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "updates"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.summarize_disk() == {"list": [], "available": False}


def test_summarize_system_and_backups_unavailable_outside_full_mode(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "status"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.summarize_system_and_backups()["available"] is False


# ── backup freshness / verdict ──────────────────────────────────────────────────────

def _fmt_systemd_local(dt_utc: datetime) -> str:
    """Build a systemd-style local-time string (e.g. "Sat 2026-07-25 03:10:09 WEST")
    from a UTC-aware datetime, for round-tripping through _parse_systemd_local_time --
    mirrors what `systemctl show`'s human-readable timestamps actually look like."""
    # deliberately naive -- mirrors systemd's own naive local-time timestamp string
    local_naive = datetime.fromtimestamp(dt_utc.timestamp())  # noqa: DTZ006
    return local_naive.strftime("%a %Y-%m-%d %H:%M:%S") + " LOCALTZ"


def test_parse_systemd_local_time_round_trips(monkeypatch):
    target = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
    parsed = dashboard_data._parse_systemd_local_time(_fmt_systemd_local(target))
    assert abs((parsed - target).total_seconds()) < 2


def test_parse_systemd_local_time_handles_placeholders():
    for v in ("n/a", "unknown", "never", "-", "", None):
        assert dashboard_data._parse_systemd_local_time(v) is None


def test_format_age_human():
    assert dashboard_data._format_age_human(0.4) == "24m"
    assert dashboard_data._format_age_human(15) == "15h"
    assert dashboard_data._format_age_human(220) == "9d 4h"


def test_format_countdown_human():
    now = datetime.now(timezone.utc)
    assert dashboard_data._format_countdown_human(now + timedelta(minutes=5), now) == "in 5m"
    assert dashboard_data._format_countdown_human(now + timedelta(hours=9, minutes=48), now) == "in 9h 48m"
    assert dashboard_data._format_countdown_human(now + timedelta(days=2, hours=3), now) == "in 2d 3h"
    assert dashboard_data._format_countdown_human(now - timedelta(minutes=1), now) == "overdue"


def test_job_freshness_tiers():
    # fresh: well under 1.5x cadence, armed, succeeded
    assert dashboard_data._job_freshness(10, 24, "success", True) == "fresh"
    # stale: timer not armed even though age is fine
    assert dashboard_data._job_freshness(10, 24, "success", False) == "stale"
    # stale: age crosses 1.5x cadence
    assert dashboard_data._job_freshness(37, 24, "success", True) == "stale"
    # overdue: age crosses 3x cadence
    assert dashboard_data._job_freshness(75, 24, "success", True) == "overdue"
    # failed always wins regardless of age
    assert dashboard_data._job_freshness(1, 24, "failed", True) == "failed"
    # unknown age (unparseable last_run) treated cautiously, not silently fresh
    assert dashboard_data._job_freshness(None, 24, "success", True) == "stale"
    # timer_armed=None (legacy snapshot, "next_run" never collected) judges on age alone --
    # unknown timer state must not retroactively read as "confirmed disarmed"
    assert dashboard_data._job_freshness(10, 24, "success", None) == "fresh"


def test_summarize_system_and_backups_computes_freshness_from_real_now(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": now.isoformat(), "mode": "full",
        "system": {}, "certs": [],
        "backups": {
            "daily": {
                "state": "inactive", "result": "success", "exit_code": "0",
                "last_run": _fmt_systemd_local(now - timedelta(hours=1)),
                "next_run": _fmt_systemd_local(now + timedelta(hours=23)),
                "cadence_hours": 24,
            },
        },
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)

    result = dashboard_data.summarize_system_and_backups()
    daily = result["backups"]["daily"]
    assert daily["freshness"] == "fresh"
    assert daily["timer_armed"] is True
    assert abs(daily["age_hours"] - 1) < 0.05
    assert daily["age_human"] == "1h"
    assert daily["window_caption"] == "4% through the 24h window"
    assert result["verdict"]["level"] == "ok"
    assert result["verdict"]["title"] == "DATA IS SAFE"


def test_summarize_system_and_backups_legacy_record_uses_job_name_cadence(tmp_path, monkeypatch):
    # A snapshot from before check_backups() started emitting cadence_hours itself --
    # the weekly job must fall back to its own 168h cadence, not the daily job's 24h one.
    now = datetime.now(timezone.utc)
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": now.isoformat(), "mode": "full",
        "system": {}, "certs": [],
        "backups": {
            "weekly": {
                "state": "inactive", "result": "success", "exit_code": "0",
                "last_run": _fmt_systemd_local(now - timedelta(hours=144)),
                "next_run": _fmt_systemd_local(now + timedelta(hours=24)),
            },
        },
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)

    result = dashboard_data.summarize_system_and_backups()
    weekly = result["backups"]["weekly"]
    assert weekly["freshness"] != "overdue"
    # the resolved fallback cadence must be written back -- the template reads b.cadence_hours
    # directly for the window caption, and would otherwise render "through the 0h window"
    assert weekly["cadence_hours"] == 168
    assert weekly["window_caption"] == "86% through the 7d window"


def test_summarize_system_and_backups_timer_still_armed_after_next_run_elapses(tmp_path, monkeypatch):
    # next_run (systemd's NextElapseUSecRealtime) is always forward-looking as of scan
    # time -- reading as "in the past" relative to render time just means the snapshot is
    # a bit stale, not that the real systemd timer disarmed itself.
    now = datetime.now(timezone.utc)
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": now.isoformat(), "mode": "full",
        "system": {}, "certs": [],
        "backups": {
            "daily": {
                "state": "inactive", "result": "success", "exit_code": "0",
                "last_run": _fmt_systemd_local(now - timedelta(hours=1)),
                "next_run": _fmt_systemd_local(now - timedelta(minutes=5)),
                "cadence_hours": 24,
            },
        },
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)

    result = dashboard_data.summarize_system_and_backups()
    assert result["backups"]["daily"]["timer_armed"] is True


def test_summarize_system_and_backups_legacy_missing_next_run_does_not_force_stale(tmp_path, monkeypatch):
    # A snapshot from before check_backups() started emitting "next_run" at all has no such
    # key -- that's unknown timer state, not a confirmed-disarmed one, so an otherwise fresh
    # legacy job shouldn't retroactively read as DATA AGING until the next full scan.
    now = datetime.now(timezone.utc)
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": now.isoformat(), "mode": "full",
        "system": {}, "certs": [],
        "backups": {
            "daily": {
                "state": "inactive", "result": "success", "exit_code": "0",
                "last_run": _fmt_systemd_local(now - timedelta(hours=1)),
            },
        },
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)

    result = dashboard_data.summarize_system_and_backups()
    assert result["backups"]["daily"]["freshness"] == "fresh"
    # tri-state: None ("unknown"), not False ("confirmed disarmed") -- the key never existed
    assert result["backups"]["daily"]["timer_armed"] is None


def test_backup_verdict_crit_when_job_overdue():
    backups = {
        "daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"},
        "weekly": {"freshness": "overdue", "timer_armed": False, "age_hours": None, "age_human": "never"},
    }
    verdict = dashboard_data._backup_verdict(backups, [])
    assert verdict["level"] == "crit"


def test_backup_verdict_crit_when_cert_expiring():
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(backups, [{"status": "expiring", "days_remaining": 3}])
    assert verdict["level"] == "crit"


def test_backup_verdict_warn_when_cert_renew_soon():
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(backups, [{"status": "renew_soon", "days_remaining": 20}])
    assert verdict["level"] == "warn"


def test_backup_verdict_ok_mentions_soonest_cert_expiry():
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(
        backups, [{"status": "valid", "days_remaining": 64}, {"status": "valid", "days_remaining": 311}]
    )
    assert verdict["level"] == "ok"
    assert "64d" in verdict["detail"]


def test_backup_verdict_unknown_when_no_backup_data():
    # A "full" mode snapshot with an empty backups mapping shouldn't read as a clean bill
    # of health -- it's a blind spot, same as sb.available == False.
    verdict = dashboard_data._backup_verdict({}, [])
    assert verdict["level"] == "unknown"


def test_backup_verdict_crit_when_cert_row_is_unreadable_error_kind():
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(backups, [{"kind": "error", "error": "missing on disk"}])
    assert verdict["level"] == "crit"
    assert "Certificate Vault" in verdict["detail"]


def test_backup_verdict_crit_when_cert_row_is_legacy_bare_error():
    # A snapshot written by the pre-redesign check_certs() encoded the same failure with
    # neither "kind" nor "status" set at all -- must not be silently dropped.
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(backups, [{"error": "openssl failed to read cert"}])
    assert verdict["level"] == "crit"


def test_backup_verdict_warn_when_cert_row_is_legacy_healthy_shape():
    # A snapshot written by the pre-redesign check_certs() represents even a *healthy* cert
    # with no "status" key at all (only domain/sans/expires) -- the Certificate Vault card
    # already renders that as "NO DATA" rather than implying it's valid, so the verdict must
    # not silently default it to "valid" either (that could hide an already-expired cert).
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(
        backups, [{"domain": "example.com", "sans": [], "resolver": "example", "expires": "?"}]
    )
    assert verdict["level"] == "warn"


def test_backup_verdict_crit_detail_names_both_when_job_and_cert_are_bad():
    backups = {"daily": {"freshness": "overdue", "timer_armed": False, "age_hours": None, "age_human": "never"}}
    verdict = dashboard_data._backup_verdict(backups, [{"status": "expired", "days_remaining": -3}])
    assert verdict["level"] == "crit"
    assert "job" in verdict["detail"] and "certificate" in verdict["detail"]


def test_backup_verdict_ok_survives_cert_with_unparseable_expiry():
    # status == "valid" with days_remaining == None (expiry parse failed) must not crash
    # the min() over an otherwise-empty generator.
    backups = {"daily": {"freshness": "fresh", "timer_armed": True, "age_hours": 1, "age_human": "1h"}}
    verdict = dashboard_data._backup_verdict(backups, [{"status": "valid", "days_remaining": None}])
    assert verdict["level"] == "ok"


# ── build_professor_lines() backups sidebar line ───────────────────────────────────

def _minimal_professor_ctx(backup_jobs: dict) -> dict:
    """Just enough of build_dashboard_context()'s shape for build_professor_lines() to run
    without crashing, with everything except system_and_backups deliberately boring."""
    return {
        "containers": {"available": True, "total": 3, "healthy": 3, "down": 0, "degraded": 0},
        "findings": {"counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}},
        "system_and_backups": {"available": True, "backups": backup_jobs},
        "traefik": {"available": False},
        "adguard": {},
        "health": {"last_scan_mode": "full"},
        "pipeline_status": {"state": "idle"},
        "pending_plan": None,
    }


def test_build_professor_lines_backups_line_reflects_aging_not_just_result():
    # A job that reported "success" but is stale/overdue must not get "all reporting
    # success" copy in the sidebar while the tab itself shows DATA AGING/AT RISK.
    ctx = _minimal_professor_ctx({
        "daily": {"result": "success", "freshness": "overdue"},
    })
    lines = dashboard_data.build_professor_lines(ctx)
    assert "reporting success" not in lines["backups"]
    assert "daily" in lines["backups"]


def test_build_professor_lines_backups_line_all_fresh():
    ctx = _minimal_professor_ctx({
        "daily": {"result": "success", "freshness": "fresh"},
    })
    lines = dashboard_data.build_professor_lines(ctx)
    assert "reporting success" in lines["backups"]


# ── update history alert classification ──────────────────────────────────────────

def test_update_history_successful_update_not_flagged_as_alert(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    _write(path, {"entries": [
        {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "dozzle",
         "old_id": "sha256:1", "new_id": "sha256:2", "status": "updated"},
    ]})
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.summarize_update_history()
    assert result[0]["is_alert"] is False


def test_update_history_rollback_failed_flagged_as_alert(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    _write(path, {"entries": [
        {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "planka",
         "old_id": "sha256:1", "new_id": "sha256:2", "status": "rollback_failed",
         "reason": "crash-looped after update"},
    ]})
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.summarize_update_history()
    assert result[0]["is_alert"] is True


def test_summarize_findings_counts_and_sorts_by_severity(tmp_path, monkeypatch):
    path = tmp_path / "latest_findings.json"
    _write(path, {
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [
            {"id": "f1", "severity": "LOW", "resource": "a"},
            {"id": "f2", "severity": "CRITICAL", "resource": "b"},
            {"id": "f3", "severity": "MEDIUM", "resource": "c"},
        ],
        "has_critical": True,
        "has_high": False,
    })
    monkeypatch.setattr(config, "STATE_FINDINGS", path)
    result = dashboard_data.summarize_findings()
    assert result["counts"] == {"critical": 1, "high": 0, "medium": 1, "low": 1}
    # sorted CRITICAL -> LOW
    assert [f["id"] for f in result["list"]] == ["f2", "f3", "f1"]


def test_summarize_containers_filters_to_issues_only(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "containers": [
            {"name": "CASA_OK", "status": "Up"},
            {"name": "CASA_BAD", "status": "Restarting", "issue": "crash-looping"},
        ],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.summarize_containers()
    assert result["total"] == 2
    assert result["healthy"] == 1
    assert len(result["issues"]) == 1
    assert result["issues"][0]["name"] == "CASA_BAD"


def test_summarize_rollback_candidates_excludes_expired(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    path = tmp_path / "rollback_candidates.json"
    _write(path, {
        "candidates": [
            {"stack": "services", "service": "expired_one", "old_image_id": "sha256:1",
             "recorded_at": (now - timedelta(hours=2)).isoformat(),
             "expires_at": (now - timedelta(hours=1)).isoformat()},
            {"stack": "services", "service": "still_open", "old_image_id": "sha256:2",
             "recorded_at": now.isoformat(),
             "expires_at": (now + timedelta(hours=1)).isoformat()},
        ],
    })
    monkeypatch.setattr(config, "ROLLBACK_CANDIDATES_FILE", path)
    result = dashboard_data.summarize_rollback_candidates()
    assert len(result) == 1
    assert result[0]["service"] == "still_open"


def test_summarize_update_history_newest_first_and_capped(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    entries = [
        {"ts": f"2026-07-{d:02d}T00:00:00+00:00", "stack": "services", "service": f"svc{d}",
         "old_id": "sha256:1", "new_id": "sha256:2", "status": "updated"}
        for d in range(1, 6)
    ]
    _write(path, {"entries": entries})
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.summarize_update_history(limit=3)
    assert len(result) == 3
    assert result[0]["service"] == "svc5"  # newest first


def test_summarize_pending_plan_none_when_no_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PLAN", tmp_path / "nope.json")
    assert dashboard_data.summarize_pending_plan() is None


def test_summarize_pending_plan_hides_step_commands(tmp_path, monkeypatch):
    plan_path = tmp_path / "pending_plan.json"
    _write(plan_path, {
        "planned_at": "2026-07-15T14:37:15+00:00",
        "plans": [{"id": "p1", "priority": "medium", "title": "test",
                   "steps": [{"command": "sudo systemctl restart x"}, {"command": "echo done"}],
                   "rollback": []}],
    })
    monkeypatch.setattr(config, "STATE_PLAN", plan_path)
    status_path = tmp_path / "run_status.json"
    _write(status_path, {
        "state": "awaiting_approval", "pending_plan_id": "p1", "updated_at": "2026-07-15T14:37:20+00:00",
    })
    monkeypatch.setattr(config, "STATE_STATUS", status_path)

    result = dashboard_data.summarize_pending_plan()
    assert result["plans"][0]["step_count"] == 2
    assert result["plans"][0]["fix_steps"] == []
    assert "command" not in json.dumps(result)  # step commands never surface here


def test_summarize_pending_plan_forwards_descriptions_never_commands(tmp_path, monkeypatch):
    plan_path = tmp_path / "pending_plan.json"
    _write(plan_path, {
        "planned_at": "2026-07-15T14:37:15+00:00",
        "plans": [{"id": "p1", "priority": "high", "title": "test",
                   "steps": [
                       {"command": "sudo systemctl restart x", "description": "Restart x"},
                       {"command": "echo done", "description": "Confirm done"},
                   ],
                   "rollback": [
                       {"command": "sudo systemctl stop x", "description": "Stop x"},
                   ]}],
    })
    monkeypatch.setattr(config, "STATE_PLAN", plan_path)
    status_path = tmp_path / "run_status.json"
    _write(status_path, {
        "state": "awaiting_approval", "pending_plan_id": "p1", "updated_at": "2026-07-15T14:37:20+00:00",
    })
    monkeypatch.setattr(config, "STATE_STATUS", status_path)

    result = dashboard_data.summarize_pending_plan()
    plan = result["plans"][0]
    assert plan["fix_steps"] == ["Restart x", "Confirm done"]
    assert plan["rollback_steps"] == ["Stop x"]
    assert "command" not in json.dumps(result)
    assert "sudo systemctl" not in json.dumps(result)


def test_summarize_pending_plan_hidden_once_run_status_moves_on(tmp_path, monkeypatch):
    # pending_plan.json is never deleted after resolution -- RunStatus is the only
    # live signal that a plan is still genuinely pending, not just "the file still
    # has an old plan in it."
    plan_path = tmp_path / "pending_plan.json"
    _write(plan_path, {
        "planned_at": "2026-07-15T14:37:15+00:00",
        "plans": [{"id": "p1", "priority": "medium", "title": "test", "steps": [], "rollback": []}],
    })
    monkeypatch.setattr(config, "STATE_PLAN", plan_path)
    status_path = tmp_path / "run_status.json"
    _write(status_path, {"state": "idle", "pending_plan_id": None, "updated_at": "2026-07-15T15:00:00+00:00"})
    monkeypatch.setattr(config, "STATE_STATUS", status_path)

    assert dashboard_data.summarize_pending_plan() is None


# ── build_dashboard_context() ─────────────────────────────────────────────────────

def test_build_dashboard_context_never_raises_with_no_state(tmp_path, monkeypatch):
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS",
        "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    ctx = dashboard_data.build_dashboard_context()

    assert ctx["health"]["state_available"] is False
    assert ctx["findings"]["list"] == []
    assert ctx["containers"]["total"] == 0
    assert ctx["pending_plan"] is None
    assert ctx["update_history"] == []
    assert ctx["rollback_candidates"] == []


def test_permission_error_warns_once_per_path(tmp_path, monkeypatch, caplog):
    def denied(path, *args, **kwargs):
        raise PermissionError('denied')

    monkeypatch.setattr(Path, 'read_text', denied)
    paths = [tmp_path / 'one.json', tmp_path / 'two.json']
    for path in paths * 3:
        assert dashboard_data._load(path, dashboard_data.MonitorSnapshot) is None
    assert len(caplog.records) == 2
    for path, record in zip(paths, caplog.records):
        assert str(path) in record.message
        assert record.levelname == 'WARNING'


def test_missing_state_silent(tmp_path, caplog):
    assert dashboard_data._load(tmp_path / 'missing', dashboard_data.MonitorSnapshot) is None
    assert not caplog.records
