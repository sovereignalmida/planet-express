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
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml"))

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


def test_parse_cert_file_expiry_status_tiers(tmp_path):
    critical = _make_cert(tmp_path, "critical", "soon.casalan.com", ["soon.casalan.com"], days=5)
    soon = _make_cert(tmp_path, "soon", "renew.casalan.com", ["renew.casalan.com"], days=20)
    valid = _make_cert(tmp_path, "valid", "fine.casalan.com", ["fine.casalan.com"], days=400)
    assert casa_leela._parse_cert_file(critical)["status"] == "expiring"
    assert casa_leela._parse_cert_file(soon)["status"] == "renew_soon"
    assert casa_leela._parse_cert_file(valid)["status"] == "valid"


def test_cert_expiry_status_thresholds_directly():
    assert casa_leela._cert_expiry_status(None) == "valid"
    assert casa_leela._cert_expiry_status(31) == "valid"
    assert casa_leela._cert_expiry_status(30) == "renew_soon"
    assert casa_leela._cert_expiry_status(8) == "renew_soon"
    assert casa_leela._cert_expiry_status(7) == "expiring"
    assert casa_leela._cert_expiry_status(0) == "expiring"
    assert casa_leela._cert_expiry_status(-1) == "expired"


def test_parse_cert_file_falls_back_to_san_when_cn_not_a_hostname(tmp_path):
    # Mirrors real Cloudflare Origin Certs: fixed non-hostname CN, real domain in SANs.
    crt = _make_cert(tmp_path, "cf-origin", "CloudFlare Origin Certificate",
                      ["*.casaalmida.com", "casaalmida.com"])
    result = casa_leela._parse_cert_file(crt)
    assert result["domain"] == "*.casaalmida.com"
    assert result["sans"] == ["*.casaalmida.com", "casaalmida.com"]


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
    monkeypatch.setattr(casa_leela.stackctl, "check_backups", lambda: [])  # timer query failed/empty

    result = casa_leela.check_backups()
    assert result["daily"]["next_run"] == "n/a"
    assert result["daily"]["last_trigger"] == "n/a"
