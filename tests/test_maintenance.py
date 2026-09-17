import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.core.maintenance import (
    MAINTENANCE_STALE_SECONDS,
    MaintenanceWindow,
    active_window,
    marker_path,
)

NOW = 100_000
UNREADABLE = "maintenance marker present (unreadable)"


def marker(tmp_path, content, age=0):
    path = tmp_path / "maintenance"
    path.write_text(content)
    os.utime(path, (NOW - age, NOW - age))
    return path


def test_absent(tmp_path):
    assert active_window(tmp_path / "absent") is None


def test_valid(tmp_path):
    path = marker(tmp_path, '{"reason":"borg-backup --weekly","started_at":123,"pid":42}')
    assert active_window(path, now=lambda: NOW) == MaintenanceWindow("borg-backup --weekly", 123.0, path)


@pytest.mark.parametrize("content", ["garbage", "{}", "[]", '{"reason":null}', '{"reason":42}'])
def test_unreadable_content(tmp_path, content):
    path = marker(tmp_path, content)
    assert active_window(path, now=lambda: NOW) == MaintenanceWindow(UNREADABLE, None, path)


@pytest.mark.parametrize("operation", ["stat", "read_text"])
def test_permission_error(tmp_path, monkeypatch, operation):
    path = marker(tmp_path, "{}")

    def denied(*args, **kwargs):
        raise PermissionError

    monkeypatch.setattr(Path, operation, denied)
    assert active_window(path, now=lambda: NOW) == MaintenanceWindow(UNREADABLE, None, path)


def test_stale_warns_once_per_mtime(tmp_path, caplog):
    path = marker(tmp_path, "garbage", MAINTENANCE_STALE_SECONDS + 1)
    with caplog.at_level(logging.WARNING):
        assert active_window(path, now=lambda: NOW) is None
        assert active_window(path, now=lambda: NOW) is None
        assert len(caplog.records) == 1
        os.utime(path, (1, 1))
        assert active_window(path, now=lambda: NOW) is None
        assert len(caplog.records) == 2


def test_exact_boundary_is_active(tmp_path):
    path = marker(tmp_path, '{"reason":"backup"}', MAINTENANCE_STALE_SECONDS)
    assert active_window(path, now=lambda: NOW).reason == "backup"


def test_env_override_at_call_time(tmp_path, monkeypatch):
    monkeypatch.delenv("CASA_MAINTENANCE_MARKER", raising=False)
    assert marker_path() == Path("/var/lib/planetexpress/maintenance")
    path = marker(tmp_path, '{"reason":"backup"}')
    monkeypatch.setenv("CASA_MAINTENANCE_MARKER", str(path))
    assert marker_path() == path
    assert active_window(now=lambda: NOW).path == path
