"""
Tests for casa_leela.py's certificate discovery/parsing -- ACME is disabled on this
host (certificatesResolvers commented out in traefik.yml), so check_certs() reads the
TLS certs Traefik's file provider actually declares (~/apps/network/proxy/dynamic/*.yml)
and inspects the real cert files with openssl, same real-binary-invocation style as
tests/test_setup_wizard.py's visudo -c test.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_leela


def _make_cert(dir_: Path, name: str, cn: str, sans: list[str], days: int = 1) -> Path:
    key = dir_ / f"{name}.key"
    crt = dir_ / f"{name}.crt"
    san_ext = "subjectAltName=" + ",".join(f"DNS:{s}" for s in sans)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", str(days), "-nodes",
         "-keyout", str(key), "-out", str(crt), "-subj", f"/CN={cn}", "-addext", san_ext],
        capture_output=True, text=True, check=True,
    )
    return crt


def _write_dynamic_yml(dynamic_dir: Path, name: str, cert_files: list[str]) -> None:
    entries = "\n".join(
        f'    - certFile: "/etc/traefik/certs/{c}"\n      keyFile: "/etc/traefik/certs/{c.replace(".crt", ".key")}"'
        for c in cert_files
    )
    (dynamic_dir / name).write_text(f"tls:\n  certificates:\n{entries}\n")


# ── _discover_cert_files() ──────────────────────────────────────────────────────────

def test_discover_cert_files_reads_dynamic_yml(tmp_path, monkeypatch):
    dynamic_dir = tmp_path / "dynamic"
    dynamic_dir.mkdir()
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    _write_dynamic_yml(dynamic_dir, "casalan-certs.yml", ["a.crt", "b.crt"])
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", dynamic_dir)
    monkeypatch.setattr(casa_leela, "_TRAEFIK_CERTS_HOST_DIR", certs_dir)

    found = casa_leela._discover_cert_files()
    assert found == [certs_dir / "a.crt", certs_dir / "b.crt"]


def test_discover_cert_files_dedupes_and_ignores_other_paths(tmp_path, monkeypatch):
    dynamic_dir = tmp_path / "dynamic"
    dynamic_dir.mkdir()
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (dynamic_dir / "dup1.yml").write_text(
        'tls:\n  certificates:\n'
        '    - certFile: "/etc/traefik/certs/a.crt"\n      keyFile: "/etc/traefik/certs/a.key"\n'
        '    - certFile: "/some/other/path/b.crt"\n      keyFile: "/some/other/path/b.key"\n'
    )
    (dynamic_dir / "dup2.yml").write_text(
        'tls:\n  certificates:\n'
        '    - certFile: "/etc/traefik/certs/a.crt"\n      keyFile: "/etc/traefik/certs/a.key"\n'
    )
    (dynamic_dir / "unrelated.yml").write_text("http:\n  routers: {}\n")
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", dynamic_dir)
    monkeypatch.setattr(casa_leela, "_TRAEFIK_CERTS_HOST_DIR", certs_dir)

    found = casa_leela._discover_cert_files()
    assert found == [certs_dir / "a.crt"]  # dedup'd, and the non-/etc/traefik/certs/ path dropped


def test_discover_cert_files_missing_dynamic_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", tmp_path / "nope")
    assert casa_leela._discover_cert_files() == []


def test_discover_cert_files_logs_warning_on_unparseable_yml(tmp_path, monkeypatch, caplog):
    dynamic_dir = tmp_path / "dynamic"
    dynamic_dir.mkdir()
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (dynamic_dir / "broken.yml").write_text("tls:\n  certificates:\n  - certFile: [unterminated\n")
    _write_dynamic_yml(dynamic_dir, "casalan-certs.yml", ["a.crt"])
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", dynamic_dir)
    monkeypatch.setattr(casa_leela, "_TRAEFIK_CERTS_HOST_DIR", certs_dir)

    with caplog.at_level("WARNING", logger="planetexpress.leela"):
        found = casa_leela._discover_cert_files()

    assert found == [certs_dir / "a.crt"]  # the well-formed file is still picked up
    assert any("broken.yml" in rec.message for rec in caplog.records)


# ── _parse_cert_file() ───────────────────────────────────────────────────────────────

def test_parse_cert_file_extracts_domain_sans_expiry(tmp_path):
    crt = _make_cert(tmp_path, "leaf", "wildcard.casalan.com", ["*.casalan.com"], days=400)
    result = casa_leela._parse_cert_file(crt)
    assert result["domain"] == "wildcard.casalan.com"
    assert result["sans"] == ["*.casalan.com"]
    assert result["resolver"] == "leaf"
    assert result["expires"] != "?"
    assert "error" not in result
    # A self-signed test cert's issuer == its subject, so the CN round-trips.
    assert result["issuer"] == "wildcard.casalan.com"
    assert result["status"] == "valid"
    assert result["days_remaining"] in (399, 400)  # +/-1 for wall-clock skew during the test run
    assert result["tier"] == "valid"
    assert result["days_left"] == result["days_remaining"]
    assert result["life_pct"] == 100


def test_parse_cert_file_expiry_status_tiers(tmp_path):
    critical = _make_cert(tmp_path, "critical", "soon.casalan.com", ["soon.casalan.com"], days=5)
    soon = _make_cert(tmp_path, "soon", "renew.casalan.com", ["renew.casalan.com"], days=20)
    valid = _make_cert(tmp_path, "valid", "fine.casalan.com", ["fine.casalan.com"], days=400)
    assert casa_leela._parse_cert_file(critical)["status"] == "expiring"
    assert casa_leela._parse_cert_file(soon)["status"] == "renew_soon"
    assert casa_leela._parse_cert_file(valid)["status"] == "valid"
    assert casa_leela._parse_cert_file(critical)["tier"] == "expiring"
    assert casa_leela._parse_cert_file(soon)["life_pct"] in (5, 6)


def test_cert_expiry_status_thresholds_directly():
    assert casa_leela._cert_expiry_status(None) == "valid"
    assert casa_leela._cert_expiry_status(31) == "valid"
    assert casa_leela._cert_expiry_status(30) == "renew_soon"
    assert casa_leela._cert_expiry_status(8) == "renew_soon"
    assert casa_leela._cert_expiry_status(7) == "expiring"
    assert casa_leela._cert_expiry_status(0) == "expired"
    assert casa_leela._cert_expiry_status(-1) == "expired"


def test_parse_cert_file_falls_back_to_san_when_cn_not_a_hostname(tmp_path):
    # Mirrors real Cloudflare Origin Certs: fixed non-hostname CN, real domain in SANs.
    crt = _make_cert(tmp_path, "cf-origin", "CloudFlare Origin Certificate",
                      ["*.casaalmida.com", "casaalmida.com"])
    result = casa_leela._parse_cert_file(crt)
    assert result["domain"] == "*.casaalmida.com"
    assert result["sans"] == ["*.casaalmida.com", "casaalmida.com"]


def test_dn_value_handles_openssl_rfc2253_quoting():
    # Real-world regression: this host has two openssl binaries -- a Homebrew one on
    # interactive PATHs (never quotes DN values) and /usr/bin/openssl, what
    # casa-planetexpress.service's minimal systemd PATH actually resolves to (quotes a
    # value containing a comma, e.g. Cloudflare's `O = "CloudFlare, Inc."`). The live
    # service showed a mangled issuer ('"CloudFlare' truncated at the quoted comma)
    # until this was handled -- interactive testing with the Homebrew openssl never
    # caught it because that binary doesn't quote at all.
    quoted = casa_leela._CERT_O_RE.search('issuer=C = US, O = "CloudFlare, Inc.", OU = X')
    unquoted = casa_leela._CERT_O_RE.search("issuer=C=US, O=CloudFlare, Inc., OU=X")
    assert casa_leela._dn_value(quoted) == "CloudFlare, Inc."
    assert casa_leela._dn_value(unquoted) == "CloudFlare"  # unquoted form truncates at the comma, unavoidably
    assert casa_leela._dn_value(None) is None


def test_parse_cert_file_missing_on_disk(tmp_path):
    result = casa_leela._parse_cert_file(tmp_path / "nope.crt")
    assert "error" in result
    assert "missing on disk" in result["error"]


# ── check_certs() ────────────────────────────────────────────────────────────────────

def test_check_certs_end_to_end(tmp_path, monkeypatch):
    dynamic_dir = tmp_path / "dynamic"
    dynamic_dir.mkdir()
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    _make_cert(certs_dir, "wildcard.casalan.com.fullchain", "*.casalan.com", ["*.casalan.com"])
    _write_dynamic_yml(dynamic_dir, "casalan-certs.yml", ["wildcard.casalan.com.fullchain.crt"])
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", dynamic_dir)
    monkeypatch.setattr(casa_leela, "_TRAEFIK_CERTS_HOST_DIR", certs_dir)

    result = casa_leela.check_certs()
    assert len(result) == 1
    assert result[0]["domain"] == "*.casalan.com"
    assert "error" not in result[0] and "note" not in result[0]


def test_check_certs_no_declarations_returns_note(tmp_path, monkeypatch):
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", tmp_path / "nope")
    result = casa_leela.check_certs()
    assert len(result) == 1
    assert "note" in result[0]


def test_check_certs_all_declared_certs_missing_returns_error(tmp_path, monkeypatch):
    dynamic_dir = tmp_path / "dynamic"
    dynamic_dir.mkdir()
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    _write_dynamic_yml(dynamic_dir, "casalan-certs.yml", ["ghost.crt"])
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", dynamic_dir)
    monkeypatch.setattr(casa_leela, "_TRAEFIK_CERTS_HOST_DIR", certs_dir)

    result = casa_leela.check_certs()
    assert len(result) == 1
    assert "error" in result[0]
    assert result[0]["kind"] == "error"
    assert result[0]["resolver"] == "ghost"


def test_check_certs_partial_failure_keeps_good_certs_and_surfaces_bad_one(tmp_path, monkeypatch):
    # One declared cert loads fine, the other is missing on disk -- the bad one must
    # not be silently dropped just because a good one exists.
    dynamic_dir = tmp_path / "dynamic"
    dynamic_dir.mkdir()
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    _make_cert(certs_dir, "wildcard.casalan.com.fullchain", "*.casalan.com", ["*.casalan.com"])
    _write_dynamic_yml(dynamic_dir, "casalan-certs.yml", ["wildcard.casalan.com.fullchain.crt", "ghost.crt"])
    monkeypatch.setattr(casa_leela, "_TRAEFIK_DYNAMIC_DIR", dynamic_dir)
    monkeypatch.setattr(casa_leela, "_TRAEFIK_CERTS_HOST_DIR", certs_dir)

    result = casa_leela.check_certs()
    assert len(result) == 2
    assert result[0]["domain"] == "*.casalan.com"
    assert "error" not in result[0] and "kind" not in result[0]
    assert result[1]["kind"] == "error"
    assert result[1]["resolver"] == "ghost"
    assert "missing on disk" in result[1]["error"]
    assert "domain" not in result[1]  # never smuggled through domain


# ── check_backups() ─────────────────────────────────────────────────────────────────

def _fake_service_show(cmd, timeout=30):
    # InactiveEnterTimestamp (when the oneshot last *finished*) deliberately differs from
    # the timer's last_run_at (when it last *started*, ~2h45m earlier on this host) to
    # catch a regression back to the wrong (start-time) property.
    if "daily-borg-backup.service" in cmd:
        props = "ActiveState=inactive\nResult=success\nExecMainStatus=0\nInactiveEnterTimestamp=Sat 2026-07-25 05:54:03 WEST\n"
    else:
        props = "ActiveState=inactive\nResult=success\nExecMainStatus=0\nInactiveEnterTimestamp=Sun 2026-07-19 04:10:00 WEST\n"
    return 0, props, ""


def test_check_backups_merges_service_and_timer_data(monkeypatch):
    monkeypatch.setattr(casa_leela, "_run", _fake_service_show)
    monkeypatch.setattr(casa_leela.stackctl, "check_backups", lambda: [
        {"label": "daily", "result": "success", "exit_status": "0",
         "last_run_at": "Sat 2026-07-25 03:10:09 WEST", "next_run_at": "Sun 2026-07-26 03:10:00 WEST"},
        {"label": "weekly", "result": "success", "exit_status": "0",
         "last_run_at": "Sun 2026-07-19 02:30:20 WEST", "next_run_at": "Sun 2026-07-26 02:30:00 WEST"},
    ])

    result = casa_leela.check_backups()
    # "last_run" is the service's own completion time (InactiveEnterTimestamp), distinct
    # from the timer's last *trigger* (start) time -- they must not be conflated.
    assert result["daily"]["last_run"] == "Sat 2026-07-25 05:54:03 WEST"
    assert result["daily"]["last_trigger"] == "Sat 2026-07-25 03:10:09 WEST"
    assert result["daily"]["next_run"] == "Sun 2026-07-26 03:10:00 WEST"
    assert result["daily"]["cadence_hours"] == 24
    assert result["weekly"]["last_run"] == "Sun 2026-07-19 04:10:00 WEST"
    assert result["weekly"]["next_run"] == "Sun 2026-07-26 02:30:00 WEST"
    assert result["weekly"]["cadence_hours"] == 168
    # state stays in the payload (dashboard just stops leading with it) -- never dropped.
    assert result["daily"]["state"] == "inactive"


def test_check_backups_missing_timer_data_degrades_to_na(monkeypatch):
    monkeypatch.setattr(casa_leela, "_run", _fake_service_show)
    monkeypatch.setattr(casa_leela.stackctl, "check_backups", list)  # timer query failed/empty

    result = casa_leela.check_backups()
    assert result["daily"]["next_run"] == "n/a"
    assert result["daily"]["last_trigger"] == "n/a"


def test_check_backups_journal_fallback_carries_failure_not_just_timestamp(monkeypatch):
    # Regression test for a Codex-flagged P2: when InactiveEnterTimestamp is wiped (empty)
    # by a daemon-reload/reboot, systemd resets Result to its default "success" too -- not
    # just the timestamp -- so blindly keeping props["Result"] here would report a job whose
    # actual last run failed as both fresh AND successful. The fallback's own verdict from
    # the journal must override it.
    def fake_show(cmd, timeout=30):
        # ActiveState/Result/ExecMainStatus all at their post-reset defaults; no
        # InactiveEnterTimestamp line at all since this boot has no invocation yet.
        return 0, "ActiveState=inactive\nResult=success\nExecMainStatus=0\n", ""

    monkeypatch.setattr(casa_leela, "_run", fake_show)
    monkeypatch.setattr(casa_leela.stackctl, "check_backups", list)
    monkeypatch.setattr(
        casa_leela, "_last_journal_completion",
        lambda unit: ("Sun 2026-07-19 04:10:00 WEST", "failed"),
    )

    result = casa_leela.check_backups()
    assert result["daily"]["last_run"] == "Sun 2026-07-19 04:10:00 WEST"
    assert result["daily"]["result"] == "failed"


def test_check_backups_query_failure_does_not_trigger_journal_fallback(monkeypatch):
    # Regression test for a Codex-flagged P2: an empty InactiveEnterTimestamp is only a
    # legitimate "reboot wiped transient state" fallback trigger when the systemctl query
    # itself succeeded (rc == 0). If the query failed outright (bad unit, systemd
    # unreachable, etc.), props is empty for an unrelated reason -- falling back to the
    # journal here too would silently paper over a real query failure with a stale-but-real
    # historical result, instead of surfacing "unknown"/"n/a" the way a query failure should.
    journal_called = []

    def fake_show(cmd, timeout=30):
        return 1, "", "Failed to connect to bus: Operation not permitted"

    def fake_journal(unit):
        journal_called.append(unit)
        return "Sun 2026-07-19 04:10:00 WEST", "success"

    monkeypatch.setattr(casa_leela, "_run", fake_show)
    monkeypatch.setattr(casa_leela.stackctl, "check_backups", list)
    monkeypatch.setattr(casa_leela, "_last_journal_completion", fake_journal)

    result = casa_leela.check_backups()
    assert journal_called == []
    assert result["daily"]["last_run"] == "n/a"
    assert result["daily"]["result"] == "unknown"


# ── _last_journal_completion() ──────────────────────────────────────────────────────

def _journal_line(message: str, realtime_us: int) -> str:
    import json as _json
    return _json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": str(realtime_us)})


def test_last_journal_completion_prefers_latest_success_over_older_ones(monkeypatch):
    lines = "\n".join([
        _journal_line("Finished weekly-borg-backup.service - Weekly Borg Backup.", 1_000_000_000_000),
        _journal_line("Finished weekly-borg-backup.service - Weekly Borg Backup.", 2_000_000_000_000),
    ])
    monkeypatch.setattr(casa_leela, "_run", lambda cmd, timeout=30: (0, lines, ""))
    last_run, result = casa_leela._last_journal_completion("weekly-borg-backup.service")
    assert last_run != ""
    assert result == "success"


def test_last_journal_completion_reports_a_later_failure_not_an_earlier_success(monkeypatch):
    # Regression test for a Codex-flagged P2: if InactiveEnterTimestamp was wiped by a
    # daemon-reload/reboot after the *latest* run of this unit failed, the fallback must
    # not silently pick an older successful run instead -- that would misreport a broken
    # backup as healthy on the dashboard and to Hermes.
    lines = "\n".join([
        _journal_line("Finished weekly-borg-backup.service - Weekly Borg Backup.", 1_000_000_000_000),
        _journal_line("Failed to start weekly-borg-backup.service - Weekly Borg Backup.", 2_000_000_000_000),
    ])
    monkeypatch.setattr(casa_leela, "_run", lambda cmd, timeout=30: (0, lines, ""))
    last_run, result = casa_leela._last_journal_completion("weekly-borg-backup.service")
    # The later (failed) entry's timestamp -- and its "failed" verdict -- win over the
    # earlier success.
    from datetime import datetime
    expected = datetime.fromtimestamp(2_000_000_000_000 / 1_000_000).astimezone()
    assert last_run == expected.strftime("%a %Y-%m-%d %H:%M:%S %Z")
    assert result == "failed"


def test_last_journal_completion_ignores_unrelated_messages(monkeypatch):
    lines = "\n".join([
        _journal_line("Starting weekly-borg-backup.service - Weekly Borg Backup...", 1_000_000_000_000),
        _journal_line("Some unrelated log line.", 3_000_000_000_000),
    ])
    monkeypatch.setattr(casa_leela, "_run", lambda cmd, timeout=30: (0, lines, ""))
    assert casa_leela._last_journal_completion("weekly-borg-backup.service") == ("", "")


# ── check_nfs_mount_health() ────────────────────────────────────────────────────────

def test_check_nfs_mount_health_reports_stale_file_handle_as_high(monkeypatch):
    monkeypatch.setattr(casa_leela, "NFS_MOUNT_WATCHLIST", [("CASA_TESTAPP", "/data/files")])
    monkeypatch.setattr(
        casa_leela, "_run",
        lambda cmd, timeout=30: (1, "", "stat: cannot statx '/data/files': Stale file handle"),
    )
    results = casa_leela.check_nfs_mount_health()
    assert results == [{
        "container": "CASA_TESTAPP",
        "path": "/data/files",
        "error": "stat: cannot statx '/data/files': Stale file handle",
        "alert": "HIGH",
    }]


def test_check_nfs_mount_health_reports_other_errors_as_medium(monkeypatch):
    monkeypatch.setattr(casa_leela, "NFS_MOUNT_WATCHLIST", [("CASA_TESTAPP", "/data/files")])
    monkeypatch.setattr(
        casa_leela, "_run",
        lambda cmd, timeout=30: (1, "", "Error: No such container: CASA_TESTAPP"),
    )
    results = casa_leela.check_nfs_mount_health()
    assert results[0]["alert"] == "MEDIUM"


def test_check_nfs_mount_health_clean_probe_has_no_alert(monkeypatch):
    monkeypatch.setattr(casa_leela, "NFS_MOUNT_WATCHLIST", [("CASA_TESTAPP", "/data/files")])
    monkeypatch.setattr(
        casa_leela, "_run",
        lambda cmd, timeout=30: (0, "  File: /data/files\n  Size: 4096", ""),
    )
    results = casa_leela.check_nfs_mount_health()
    assert results == [{"container": "CASA_TESTAPP", "path": "/data/files"}]
    assert "alert" not in results[0]
    assert "error" not in results[0]


# ── check_stack_completeness() ────────────────────────────────────────────────────

def _setup_stack_completeness(tmp_path, monkeypatch, stacks, previous=None):
    import json

    root = tmp_path / "stacks"
    for name in stacks:
        stack_dir = root / name
        stack_dir.mkdir(parents=True)
        (stack_dir / "docker-compose.yml").touch()
    snapshot = tmp_path / "latest_monitor.json"
    if previous is not None:
        snapshot.write_text(json.dumps({"stack_completeness": previous}))
    monkeypatch.setattr(casa_leela.config, "STACKS_ROOT", root)
    monkeypatch.setattr(casa_leela.config, "FORBIDDEN_STACKS", [])
    monkeypatch.setattr(casa_leela.config, "STATE_MONITOR", snapshot)
    calls = []

    def fake_run(cmd: str | list, timeout: int = 30, env: dict | None = None):
        assert isinstance(cmd, list)
        calls.append(cmd)
        name = Path(cmd[3]).parent.name
        prefix = ["docker", "compose", "-f", str(root / name / "docker-compose.yml")]
        stack = stacks[name]
        if cmd[4:] == ["config", "--services"]:
            assert cmd == prefix + ["config", "--services"]
            return stack["discovery"]
        if cmd[4:] == ["ps", "-a", "--format", "json"]:
            assert cmd == prefix + ["ps", "-a", "--format", "json"]
            if "ps" in stack:
                return stack["ps"]
            containers = [
                c if isinstance(c, dict) else
                {"Service": c[0], "Name": c[0], "State": c[1], "ExitCode": 0}
                for c in stack["containers"]
            ]
            output = (json.dumps(containers) if stack.get("json_array") else
                      "\n".join(json.dumps(c) for c in containers))
            return 0, output, ""
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(casa_leela, "_run", fake_run)
    return calls


def test_stack_completeness_complete(tmp_path, monkeypatch):
    """A stack with every expected service present has no alert."""
    calls = _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (0, "web\ndb", ""),
                "containers": [("web", "running"), ("db", "running")]},
    })
    result = casa_leela.check_stack_completeness()
    assert result == [{"stack": "app", "expected_count": 2, "present_count": 2,
                       "missing_services": [], "status": "complete",
                       "services": {name: {"status": "healthy", "state": "running"}
                                    for name in ("web", "db")}}]
    assert "alert" not in result[0]
    compose = tmp_path / "stacks" / "app" / "docker-compose.yml"
    assert calls == [
        ["docker", "compose", "-f", str(compose), "config", "--services"],
        ["docker", "compose", "-f", str(compose), "ps", "-a", "--format", "json"],
    ]


def test_stack_completeness_all_missing_without_history(tmp_path, monkeypatch):
    """Without history, a fully missing stack is CRITICAL."""
    _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (0, "web\ndb", ""), "containers": []},
    })
    assert casa_leela.check_stack_completeness() == [
        {"stack": "app", "expected_count": 2, "present_count": 0,
         "missing_services": ["web", "db"], "alert": "CRITICAL", "status": "incomplete",
         "services": {name: {"status": "failing", "state": "absent"}
                      for name in ("web", "db")}},
    ]


def test_stack_completeness_some_missing_without_history(tmp_path, monkeypatch):
    """Without history, a partially missing stack is HIGH."""
    _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (0, "web\ndb", ""), "containers": [("db", "running")]},
    })
    assert casa_leela.check_stack_completeness() == [
        {"stack": "app", "expected_count": 2, "present_count": 1,
         "missing_services": ["web"], "alert": "HIGH", "status": "incomplete",
         "services": {"web": {"status": "failing", "state": "absent"},
                      "db": {"status": "healthy", "state": "running"}}},
    ]


def test_stack_completeness_previously_complete_stays_urgent(tmp_path, monkeypatch):
    """Complete history does not downgrade new total or partial losses."""
    _setup_stack_completeness(tmp_path, monkeypatch, {
        "all": {"discovery": (0, "web\ndb", ""), "containers": []},
        "some": {"discovery": (0, "web\ndb", ""), "containers": [("db", "running")]},
    }, previous=[
        {"stack": name, "expected_count": 2, "present_count": 2, "missing_services": []}
        for name in ("all", "some")
    ])
    results = {entry["stack"]: entry for entry in casa_leela.check_stack_completeness()}
    assert results["all"]["alert"] == "CRITICAL"
    assert results["some"]["alert"] == "HIGH"


def test_stack_completeness_already_incomplete_keeps_worsening_urgent(tmp_path, monkeypatch):
    """Only unchanged failures are downgraded; newly failing services stay urgent."""
    _setup_stack_completeness(tmp_path, monkeypatch, {
        name: {"discovery": (0, "web\ndb\nworker\ncache", ""), "containers": containers}
        for name, containers in {
            "all": [], "some": [("db", "running")],
            "same": [("db", "running"), ("worker", "running"), ("cache", "running")],
        }.items()
    }, previous=[
        {"stack": name, "expected_count": 4, "present_count": 3,
         "missing_services": ["web"], "alert": "HIGH"}
        for name in ("all", "some", "same")
    ])
    results = {entry["stack"]: entry for entry in casa_leela.check_stack_completeness()}
    assert results["all"]["missing_services"] == ["web", "db", "worker", "cache"]
    assert results["some"]["missing_services"] == ["web", "worker", "cache"]
    assert results["same"]["missing_services"] == ["web"]
    assert results["all"]["alert"] == "CRITICAL"
    assert results["some"]["alert"] == "HIGH"
    assert results["same"]["alert"] == "LOW"


def test_stack_completeness_discovery_failure_reports_unknown(tmp_path, monkeypatch, caplog):
    """Empty or failed discovery retains the unreadable stacks."""
    calls = _setup_stack_completeness(tmp_path, monkeypatch, {
        "empty": {"discovery": (0, "", "")},
        "failed": {"discovery": (1, "", "compose failed")},
        "good": {"discovery": (0, "web", ""), "containers": [("web", "running")]},
    })
    with caplog.at_level("WARNING", logger="planetexpress.leela"):
        results = casa_leela.check_stack_completeness()
    by_name = {entry["stack"]: entry for entry in results}
    for name in ("empty", "failed"):
        assert by_name[name] == {
            "stack": name, "status": "unknown", "alert": "MEDIUM",
            "error": "compose failed" if name == "failed" else "Compose returned no services",
            "missing_services": [], "services": {},
        }
    assert by_name["good"] == {
        "stack": "good", "expected_count": 1, "present_count": 1,
        "missing_services": [], "status": "complete",
        "services": {"web": {"status": "healthy", "state": "running"}},
    }
    for name in ("empty", "failed"):
        assert any(rec.levelname == "WARNING" and
                   f"Could not determine services for stack {name}:" in rec.message
                   for rec in caplog.records)
    assert len(calls) == 4
    assert len([cmd for cmd in calls if cmd[4:] == ["config", "--services"]]) == 3
    assert [cmd for cmd in calls if cmd[4:] == ["ps", "-a", "--format", "json"]] == [
        ["docker", "compose", "-f", str(tmp_path / "stacks/good/docker-compose.yml"),
         "ps", "-a", "--format", "json"],
    ]


def test_stack_completeness_all_exited_is_critical(tmp_path, monkeypatch):
    """Exited containers fail every service even when the exit code is zero."""
    calls = _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (0, "web\ndb", ""),
                "containers": [("web", "exited"), ("db", "exited")]},
    })
    results = casa_leela.check_stack_completeness()
    assert results == [{
        "stack": "app", "status": "incomplete", "expected_count": 2, "present_count": 0,
        "missing_services": ["web", "db"], "alert": "CRITICAL",
        "services": {name: {"status": "failing", "state": "exited(0)"}
                     for name in ("web", "db")},
    }]
    assert len(calls) == 2
    assert calls[1][4:] == ["ps", "-a", "--format", "json"]


def test_stack_completeness_malformed_history_stays_urgent(tmp_path, monkeypatch, caplog):
    """Malformed snapshot JSON warns and falls back to no-history severities."""
    _setup_stack_completeness(tmp_path, monkeypatch, {
        "all": {"discovery": (0, "web\ndb", ""), "containers": []},
        "some": {"discovery": (0, "web\ndb", ""), "containers": [("db", "running")]},
    })
    casa_leela.config.STATE_MONITOR.write_text('{"stack_completeness": [')
    with caplog.at_level("WARNING", logger="planetexpress.leela"):
        results = {entry["stack"]: entry for entry in casa_leela.check_stack_completeness()}
    assert results["all"]["alert"] == "CRITICAL"
    assert results["some"]["alert"] == "HIGH"
    assert any(rec.levelname == "WARNING" and
               "Could not load previous snapshot for stack-completeness comparison" in rec.message
               for rec in caplog.records)


def test_stack_completeness_service_observations(tmp_path, monkeypatch):
    cases = [
        ({"State": "exited", "ExitCode": 1}, "failing", "exited(1)", "HIGH"),
        ({"State": "running", "Health": "unhealthy"}, "failing", "running(unhealthy)", "HIGH"),
        ({"State": "running", "Health": "starting"}, "unknown", "running(starting)", None),
        ({"State": "running", "Health": "healthy"}, "healthy", "running(healthy)", None),
        ({"State": "exited", "ExitCode": 0, "Name": "intentional"},
         "healthy", "exited(0)", None),
    ]
    monkeypatch.setattr(casa_leela.config, "PAUSED_CONTAINERS", ["intentional"])
    for index, (container, status, state, alert) in enumerate(cases):
        _setup_stack_completeness(tmp_path / str(index), monkeypatch, {
            "app with spaces": {"discovery": (0, "web\ndb", ""), "containers": [
                {"Service": "web", "Name": "web", **container}, ("db", "running"),
            ]},
        })
        entry, = casa_leela.check_stack_completeness()
        assert entry["services"]["web"] == {"status": status, "state": state}
        assert entry.get("alert") == alert
        assert entry["status"] == ("incomplete" if alert else "complete")
        assert entry["missing_services"] == (["web"] if alert else [])
        assert entry["present_count"] == (1 if alert else 2)


def test_stack_completeness_replicas_order_and_json_formats(tmp_path, monkeypatch):
    results = []
    for json_array in (False, True):
        _setup_stack_completeness(tmp_path / str(json_array), monkeypatch, {
            "app": {"discovery": (0, "worker\nweb\ncache\ndb", ""),
                    "json_array": json_array, "containers": [
                        ("db", "running"), ("web", "running"), ("worker", "running"),
                        {"Service": "web", "State": "running", "Health": "starting"},
                        {"Service": "worker", "State": "running", "Health": "starting"},
                        {"Service": "worker", "State": "exited", "ExitCode": 1},
                    ]},
        })
        entry, = casa_leela.check_stack_completeness()
        assert list(entry["services"]) == ["worker", "web", "cache", "db"]
        assert entry["missing_services"] == ["worker", "cache"]
        assert entry["services"]["worker"] == {
            "status": "failing", "state": "running, running(starting), exited(1)",
        }
        assert entry["services"]["web"] == {
            "status": "unknown", "state": "running, running(starting)",
        }
        assert entry["alert"] == "HIGH"
        results.append(entry)
    assert results[0] == results[1]


def test_stack_completeness_ps_errors_are_unknown(tmp_path, monkeypatch, caplog):
    for index, ps in enumerate([(1, "", "failure" * 100), (0, "not json", ""),
                                (0, '{"Service":"web"}\ninvalid', ""),
                                (0, '[null]', '')]):
        _setup_stack_completeness(tmp_path / str(index), monkeypatch, {
            "app": {"discovery": (0, "web", ""), "ps": ps},
        })
        entry, = casa_leela.check_stack_completeness()
        assert entry["status"] == "unknown"
        assert entry["alert"] == "MEDIUM"
        assert entry["services"] == {}
        assert entry["missing_services"] == []
        assert 0 < len(entry["error"]) <= 300
        assert "expected_count" not in entry
        assert "present_count" not in entry
        if ps[0]:
            assert entry["error"] == ps[2][:300]
    assert "Could not determine containers for stack app:" in caplog.text


def test_stack_completeness_failed_discovery_with_stdout(tmp_path, monkeypatch):
    calls = _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (1, "web", "discovery failed")},
    })
    entry, = casa_leela.check_stack_completeness()
    assert entry["status"] == "unknown"
    assert entry["error"] == "discovery failed"
    assert entry["alert"] == "MEDIUM"
    assert len(calls) == 1


def test_stack_completeness_unknown_history_stays_urgent(tmp_path, monkeypatch):
    _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (0, "web", ""), "containers": []},
    }, previous=[{"stack": "app", "status": "unknown", "missing_services": ["web"]}])
    entry, = casa_leela.check_stack_completeness()
    assert entry["alert"] == "CRITICAL"


def test_stack_completeness_improving_history_is_low(tmp_path, monkeypatch):
    _setup_stack_completeness(tmp_path, monkeypatch, {
        "app": {"discovery": (0, "web\ndb", ""), "containers": [("db", "running")]},
    }, previous=[{"stack": "app", "status": "incomplete", "missing_services": ["web", "db"]}])
    entry, = casa_leela.check_stack_completeness()
    assert entry["missing_services"] == ["web"]
    assert entry["alert"] == "LOW"
