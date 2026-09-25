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
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

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


def test_summarize_disk_sorts_fullest_mount_first(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00", "mode": "status",
        "disk": [
            {"mount": "/small", "used_pct": 12},
            {"mount": "/full", "used_pct": 91},
            {"mount": "/middle", "used_pct": 54},
        ],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert [d["used_pct"] for d in dashboard_data.summarize_disk()["list"]] == [91, 54, 12]


def _unavailable_overview_context():
    return {
        "health": {"last_scan_mode": "updates"},
        "containers": {"available": False},
        "services": {"total_stacks": 0},
        "findings": {"available": False},
        "pipeline_status": {},
        "system_and_backups": {"available": False},
        "certs": {"available": False, "list": []},
    }


def test_unavailable_fleet_tile_is_not_a_false_zero():
    tiles = dashboard_data.summarize_overview_tiles(
        _unavailable_overview_context(), {"available": False}, {"available": False}
    )
    assert tiles["fleet"]["level"] == "none"
    assert tiles["fleet"]["hero"] == "—" and tiles["fleet"]["sub"]


def test_unavailable_hull_tile_is_not_a_false_all_clear():
    tiles = dashboard_data.summarize_overview_tiles(
        _unavailable_overview_context(), {"available": False}, {"available": False}
    )
    assert tiles["hull"]["level"] == "none"
    assert tiles["hull"]["hero"] == "—" and tiles["hull"]["sub"]


def test_unavailable_backup_tile_is_not_a_false_zero():
    tiles = dashboard_data.summarize_overview_tiles(
        _unavailable_overview_context(), {"available": False}, {"available": False}
    )
    assert tiles["backups"]["level"] == "none"
    assert tiles["backups"]["hero"] == "—" and tiles["backups"]["sub"]


def test_unavailable_network_tile_is_not_a_false_zero():
    tiles = dashboard_data.summarize_overview_tiles(
        _unavailable_overview_context(), {"available": False}, {"available": False}
    )
    assert tiles["network"]["level"] == "none"
    assert tiles["network"]["hero"] == "—" and tiles["network"]["sub"]


def test_unavailable_system_tile_is_not_a_false_zero():
    tiles = dashboard_data.summarize_overview_tiles(
        _unavailable_overview_context(), {"available": False}, {"available": False}
    )
    assert tiles["system"]["level"] == "none"
    assert tiles["system"]["hero"] == "—" and tiles["system"]["sub"]


# ── T45.2 review: every one of these was a tile reporting healthier than the host ────
def _healthy_overview_context(**overrides):
    ctx = {
        "health": {"last_scan_mode": "full"},
        "containers": {"available": True, "healthy": 2, "total": 2, "down": 0,
                       "degraded": 0, "paused": 0, "cells": ["online", "online"]},
        "services": {"available": True, "total_stacks": 1},
        "findings": {"available": True, "list": [],
                     "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}},
        "pipeline_status": {"state": "idle", "pending_plan_id": None},
        "system_and_backups": {"available": True, "system": {}, "backups": {
            "weekly": {"freshness": "fresh", "age_hours": 12.0, "age_human": "12h",
                       "next_human": "in 6d"},
        }},
        "certs": {"available": True, "list": [{"tier": "valid", "days_left": 400}]},
    }
    ctx.update(overrides)
    return ctx


def test_a_failed_daily_is_not_hidden_behind_a_fresh_weekly():
    """The tile takes the worst job, the hero takes the newest. Reading severity off one
    job let a failed daily render green next to a healthy weekly."""
    ctx = _healthy_overview_context()
    ctx["system_and_backups"]["backups"]["daily"] = {
        "freshness": "failed", "age_hours": 30.0, "age_human": "30h", "next_human": "in 1h"}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["backups"]["level"] == "crit"
    assert tiles["backups"]["hero"] == "12h"   # newest snapshot, not the failed one


def test_an_expiring_certificate_is_crit_on_the_tile_as_it_is_on_the_tab():
    ctx = _healthy_overview_context()
    ctx["certs"]["list"] = [{"tier": "expiring", "days_left": 4}]
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["backups"]["level"] == "crit"


def test_an_unreadable_certificate_row_is_crit_not_a_shrug():
    ctx = _healthy_overview_context()
    ctx["certs"]["list"] = [{"kind": "error", "error": "could not parse"}]
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["backups"]["level"] == "crit"


def test_router_health_survives_adguard_being_absent():
    """AdGuard is optional. Gating the router count on it replaced a genuinely down router
    with a neutral 'unavailable' tile."""
    routers = {"available": True, "routers": [
        {"status": "enabled"}, {"status": "disabled"}]}
    tiles = dashboard_data.summarize_overview_tiles(
        _healthy_overview_context(), routers, {"available": False, "configured": False})
    assert tiles["network"]["level"] == "crit"
    assert tiles["network"]["hero"] == "1"
    assert "adguard" in tiles["network"]["detail"]


def test_an_unreadable_run_status_does_not_hide_a_critical_finding():
    """findings and run_status are separate files. An unknown pipeline means the pending-plan
    count is unknown, not that the findings are."""
    ctx = _healthy_overview_context()
    ctx["findings"]["counts"]["critical"] = 1
    ctx["pipeline_status"] = {"state": "unknown", "pending_plan_id": None}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["hull"]["level"] == "crit"
    assert tiles["hull"]["critical"] == 1
    assert tiles["hull"]["plans"] == "?"


def test_disk_rows_use_the_documented_ramp_not_the_old_bar_thresholds(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "full", "disk": [
        {"mount": "/a", "used_pct": 63}, {"mount": "/b", "used_pct": 80},
        {"mount": "/c", "used_pct": 92}, {"mount": "/d", "used_pct": 97},
        {"mount": "/e", "used_pct": None},
    ]})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    levels = {d["mount"]: d["level"] for d in dashboard_data.summarize_disk()["list"]}
    assert levels == {"/a": "ok", "/b": "warn", "/c": "high", "/d": "crit", "/e": "none"}


def test_a_valid_certificate_cannot_turn_a_backup_blind_spot_green():
    """The reverse of the test below, and a bug my own fix introduced: making certs
    independent of jobs let a valid cert supply an "ok" for a snapshot with no borg job in
    it at all. _backup_verdict() calls zero job data unknown; so does the tile."""
    ctx = _healthy_overview_context()
    ctx["system_and_backups"]["backups"] = {}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["backups"]["level"] == "none"
    assert tiles["backups"]["hero"] == "—"
    assert tiles["backups"]["cert_count"] == 1      # the certs are still real information


def test_traefik_answering_with_no_routers_is_not_an_all_clear():
    """0 of 0 routers up is not a healthy fleet, it is no observation."""
    tiles = dashboard_data.summarize_overview_tiles(
        _healthy_overview_context(), {"available": True, "routers": []}, {"available": False})
    assert tiles["network"]["level"] == "none"
    assert tiles["network"]["hero"] == "—"
    assert "no routers" in tiles["network"]["sub"]


def test_an_unreadable_cert_list_does_not_hide_a_failed_backup():
    """Jobs and certificates are separate sensors. Requiring both let an unreadable cert list
    blank the one thing this tile exists to show."""
    ctx = _healthy_overview_context()
    ctx["certs"] = {"available": False, "list": []}
    ctx["system_and_backups"]["backups"] = {
        "weekly": {"freshness": "failed", "age_hours": 3.0, "age_human": "3h", "next_human": "in 4d"}}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["backups"]["level"] == "crit"
    assert tiles["backups"]["cert_count"] == "—"


def test_a_missing_findings_snapshot_does_not_swallow_a_pending_approval():
    """Findings and approvals come from different sources. An approval waiting on the
    operator is the most actionable thing this tile carries."""
    ctx = _healthy_overview_context()
    ctx["findings"] = {"available": False, "list": [], "counts": {}}
    ctx["pending_approvals"] = 1
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["hull"]["hero"] == "1 PLAN WAITING"
    assert tiles["hull"]["level"] == "warn"
    assert tiles["hull"]["show_pills"] is True
    # ...and it must not invent a clean bill of health for the counts it cannot see.
    assert tiles["hull"]["critical"] == "?" and tiles["hull"]["high"] == "?"


def test_an_unreadable_uptime_does_not_hide_memory_pressure():
    """uptime and free -h are separately parsed and fail separately. Gating the tile on both
    hid a 94%-full memory bar behind "no system metrics"."""
    ctx = _healthy_overview_context()
    ctx["system_and_backups"]["system"] = {"uptime_parsed": {}, "memory": {"used_pct": 94}}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["system"]["level"] == "crit"
    assert tiles["system"]["show_memory"] is True


def test_paused_containers_do_not_tint_a_healthy_fleet():
    """PAUSED_CONTAINERS are stopped on purpose and are not counted as unhealthy, so letting
    them raise the level rendered "85 of 85 healthy" as a warning."""
    ctx = _healthy_overview_context()
    ctx["containers"]["paused"] = 2
    ctx["containers"]["cells"] = ["online", "online", "paused", "paused"]
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["fleet"]["level"] == "ok"


def test_pending_approvals_come_from_core_not_the_retired_run_status_field():
    """RunStatus.pending_plan_id was the shell planner's signal; nothing writes it now, so
    reading it reported "0 PLANS" with approvals genuinely waiting."""
    ctx = _healthy_overview_context()
    ctx["pending_approvals"] = 2
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["hull"]["plans"] == 2
    assert tiles["hull"]["hero"] == "2 PLANS WAITING"
    assert tiles["hull"]["level"] == "warn"


def test_an_unreachable_core_marks_the_plan_count_unknown_not_zero():
    tiles = dashboard_data.summarize_overview_tiles(
        _healthy_overview_context(), {"available": False}, {"available": False})
    assert tiles["hull"]["plans"] == "?"
    assert tiles["hull"]["hero"] == "NO FINDINGS"


def test_a_finding_with_an_unrecognised_severity_is_not_all_nominal():
    """summarize_findings() keeps it in "top" on purpose; counting only the four known
    buckets turned it into a green tile while Hull Diagnostics displayed it."""
    ctx = _healthy_overview_context()
    ctx["pending_approvals"] = 0
    ctx["findings"]["list"] = [{"id": "f1", "severity": "URGENT"}]
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["hull"]["hero"] == "1 FINDING"
    assert tiles["hull"]["level"] == "high"


def test_a_status_scan_still_reports_the_fleet():
    """status mode collects containers but not stack completeness. Gating the tile on
    services made a perfectly good fleet reading vanish after one."""
    ctx = _healthy_overview_context()
    ctx["services"] = {"available": False, "total_stacks": 0}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["fleet"]["level"] == "ok"
    assert tiles["fleet"]["hero"] == "2"
    assert tiles["fleet"]["note"] == "stacks —"


def test_clean_findings_with_an_unknown_plan_count_is_not_all_nominal():
    ctx = _healthy_overview_context()
    ctx["pipeline_status"] = {"state": "unknown", "pending_plan_id": None}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["hull"]["hero"] == "NO FINDINGS"
    assert tiles["hull"]["plans"] == "?"


def test_a_failed_jobs_age_is_labelled_an_attempt_not_a_snapshot():
    """systemd stamps the completion time even when the run produced nothing."""
    ctx = _healthy_overview_context()
    ctx["system_and_backups"]["backups"] = {
        "weekly": {"freshness": "failed", "age_hours": 3.0, "age_human": "3h", "next_human": "in 4d"}}
    tiles = dashboard_data.summarize_overview_tiles(ctx, {"available": False}, {"available": False})
    assert tiles["backups"]["sub"] == "since last attempt"
    assert tiles["backups"]["level"] == "crit"


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


def test_rollback_candidates_are_not_read_from_the_dashboard_process():
    """They live in the core's database, which this process is denied (`scripts/web_access.py`);
    `casa_scruffy.index()` fetches them over the `canary.candidates` RPC instead (slice 5b-3)."""
    assert dashboard_data.summarize_rollback_candidates() == []
    assert dashboard_data.build_dashboard_context()["rollback_candidates"] == []


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


# ── build_dashboard_context() ─────────────────────────────────────────────────────

def test_build_dashboard_context_never_raises_with_no_state(tmp_path, monkeypatch):
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_STATUS",
        "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    ctx = dashboard_data.build_dashboard_context()

    assert ctx["health"]["state_available"] is False
    assert ctx["findings"]["list"] == []
    assert ctx["containers"]["total"] == 0
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


def test_summarize_health_medium_stack_alert_is_warning(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00", "mode": "full",
        "stack_completeness": [{"stack": "app", "status": "unknown", "alert": "MEDIUM",
                                "missing_services": [], "services": {}, "error": "unreadable"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "no_findings.json")
    assert dashboard_data.summarize_health()["status"] == "warning"


def test_backup_job_subset_and_legacy_template(tmp_path, monkeypatch):
    from flask import Flask, render_template

    import casa_scruffy_net
    from casa_scruffy import register_template_helpers

    monkeypatch.setattr(config, 'BACKUP_JOBS', ['weekly'])
    for constant in ('STATE_MONITOR', 'STATE_FINDINGS', 'STATE_STATUS', 'UPDATE_HISTORY_FILE'):
        monkeypatch.setattr(config, constant, tmp_path / constant)
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent.parent / 'templates'))
    app.add_url_rule('/logout', endpoint='logout', view_func=lambda: '', methods=['POST'])
    app.jinja_env.globals['csrf_token'] = lambda: ''
    register_template_helpers(app)
    now = datetime.now(timezone.utc)
    # Includes a pre-upgrade two-job snapshot and both no-data fallback paths.
    for names, mode in [(['weekly'], 'full'), (['daily', 'weekly'], 'full'),
                        ([], 'full'), ([], 'quick')]:
        _write(config.STATE_MONITOR, {
            'timestamp': now.isoformat(), 'mode': mode,
            'backups': {name: {
                'result': 'success', 'last_run': _fmt_systemd_local(now - timedelta(hours=1)),
                'next_run': _fmt_systemd_local(now + timedelta(hours=24)),
            } for name in names},
        })
        ctx = dashboard_data.build_dashboard_context()
        ctx.update(traefik={'available': False, 'routers': []}, adguard={'available': False},
                   telegram_bot_username='')
        # Both are added by the route, not by build_dashboard_context(); render with them
        # present so this exercises the real wiring, not the template's fallback.
        ctx['router_zones'] = casa_scruffy_net.group_routers(ctx['traefik']['routers'])
        ctx['adguard_stats'] = dashboard_data.summarize_adguard(ctx['adguard'])
        ctx['professor_lines'] = dashboard_data.build_professor_lines(ctx)
        summary = ctx['system_and_backups']
        assert list(summary['backups']) == names
        if names:
            assert summary['verdict']['level'] == 'ok'
            assert f'All {len(names)} borg job(s)' in summary['verdict']['detail']
            assert all(b['freshness'] == 'fresh' for b in summary['backups'].values())
        with app.test_request_context('/'):
            html = render_template('dashboard.html', ctx=ctx)
        assert '<span class="cryo-name">WEEKLY</span>' in html
        assert ('<span class="cryo-name">DAILY</span>' in html) == ('daily' in names)
        if names:
            assert f'{len(names)}/{len(names)} JOBS OK' in html


def test_services_roll_up_levels_notes_and_sorting(tmp_path, monkeypatch):
    path = tmp_path / "monitor.json"
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    _write(path, {"timestamp": "2026-09-17T12:00:00Z", "mode": "full",
        "stack_completeness": [
            {"stack": "large-ok", "services": {
                "web": {"status": "healthy", "state": "running(healthy)"},
                "worker": {"status": "healthy", "state": "running"},
            }},
            {"stack": "zeta-crit", "services": {
                "replica": {"status": "failing", "state": "running(healthy), exited(2)"},
                "missing": {"status": "failing", "state": "absent"},
            }},
            {"stack": "alpha-crit", "services": {
                "restart": {"status": "failing", "state": "restarting"},
            }},
            {"stack": "warning", "services": {
                "degraded": {"status": "failing", "state": "running(unhealthy)"},
                "booting": {"status": "unknown", "state": "running(starting)"},
            }},
            {"stack": "paused", "services": {
                "batch": {"status": "healthy", "state": "exited(0)"},
            }},
        ]})

    result = dashboard_data.summarize_services()

    assert result == {
        "available": True,
        "stacks": [
            {"name": "zeta-crit", "up": 0, "total": 2, "level": "crit",
             "note": "missing down · replica down", "members": [
                 {"service": "missing", "level": "crit", "word": "down", "state": "absent"},
                 {"service": "replica", "level": "crit", "word": "down",
                 "state": "running(healthy), exited(2)"},
             ]},
            {"name": "alpha-crit", "up": 0, "total": 1, "level": "crit",
             "note": "restart down", "members": [
                 {"service": "restart", "level": "crit", "word": "down", "state": "restarting"},
             ]},
            {"name": "warning", "up": 0, "total": 2, "level": "warn",
             "note": "booting starting · degraded degraded", "members": [
                 {"service": "booting", "level": "warn", "word": "starting",
                  "state": "running(starting)"},
                 {"service": "degraded", "level": "warn", "word": "degraded",
                  "state": "running(unhealthy)"},
             ]},
            {"name": "paused", "up": 0, "total": 1, "level": "idle",
             "note": "batch paused", "members": [
                 {"service": "batch", "level": "idle", "word": "paused", "state": "exited(0)"},
             ]},
            {"name": "large-ok", "up": 2, "total": 2, "level": "ok", "note": "", "members": [
                {"service": "web", "level": "ok", "word": "", "state": "running(healthy)"},
                {"service": "worker", "level": "ok", "word": "", "state": "running"},
            ]},
        ],
        "total_stacks": 5, "up": 2, "total": 8, "attention": 4,
    }


def test_services_sort_ties_by_name_after_level_and_size(tmp_path, monkeypatch):
    path = tmp_path / "monitor.json"
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    _write(path, {"timestamp": "2026-09-17T12:00:00Z", "mode": "full",
        "stack_completeness": [
            {"stack": "zeta", "services": {"svc": {"status": "healthy", "state": "running"}}},
            {"stack": "alpha", "services": {"svc": {"status": "healthy", "state": "running"}}},
            {"stack": "bigger", "services": {
                "one": {"status": "healthy", "state": "running"},
                "two": {"status": "healthy", "state": "running"},
            }},
        ]})
    assert [stack["name"] for stack in dashboard_data.summarize_services()["stacks"]] == [
        "bigger", "alpha", "zeta",
    ]


def test_services_unreadable_and_malformed_entries(tmp_path, monkeypatch):
    path = tmp_path / "monitor.json"
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    _write(path, {"timestamp": "2026-09-17T12:00:00Z", "mode": "full",
        "stack_completeness": [
            {"stack": "unreadable", "status": "unknown", "error": "no socket", "services": {}},
            {"stack": "bad-services", "services": []},
            {"stack": "bad-member", "services": {"broken": "not a mapping"}},
        ]})
    result = dashboard_data.summarize_services()
    unreadable = next(stack for stack in result["stacks"] if stack["name"] == "unreadable")
    assert unreadable == {
        "name": "unreadable", "up": 0, "total": 0, "level": "warn",
        "note": "state unreadable", "members": [],
    }
    assert result["available"] is True
    assert result["attention"] == 1


def test_services_status_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "monitor.json"
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    _write(path, {"timestamp": "2026-09-17T12:00:00Z", "mode": "status"})
    assert dashboard_data.build_dashboard_context()["services"] == {
        "available": False, "stacks": [], "total_stacks": 0,
        "up": 0, "total": 0, "attention": 0,
    }


# The pending-plan panel went with the shell planner in slice 5b-5: a proposal is an approval in
# the store, shown on the actions screen, not a plan file summarised here.
