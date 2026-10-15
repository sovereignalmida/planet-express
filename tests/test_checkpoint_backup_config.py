"""Backup freshness states, checkpoint config validation, and the startup scan's unreferenced report."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from checkpoint_fakes import make_env
from pydantic import ValidationError

from config_schema import CheckpointConfig, MissionsConfig
from planet_express.missions.checkpoint import ConfiguredBackupFreshness
from planet_express.missions.checkpoint.backup import BackupSource
from planet_express.missions.checkpoint.service import CheckpointSettings


def freshness(sources, mtime):
    return ConfiguredBackupFreshness([BackupSource(*s) for s in sources], mtime=mtime)


def test_backup_states():
    now = 1_000_000
    assert freshness([], lambda p: None).status("/d", now=now, recent_s=100).state == "unverifiable"
    assert freshness([("/x", "/m")], lambda p: now).status("/d", now=now, recent_s=100).state == "absent"
    assert freshness([("/d", "/m")], lambda p: None).status("/d/sub", now=now, recent_s=100).state == "unverifiable"
    assert freshness([("/d", "/m")], lambda p: now - 101).status("/d/sub", now=now, recent_s=100).state == "stale"
    ok = freshness([("/d", "/m")], lambda p: now - 100).status("/d/sub", now=now, recent_s=100)
    assert ok.state == "ok" and ok.last_success_at == now - 100
    # a marker dated in the future is not "recent": it cannot be trusted
    assert freshness([("/d", "/m")], lambda p: now + 5).status("/d", now=now, recent_s=100).state == "stale"
    # /data does not cover /database
    assert freshness([("/data", "/m")], lambda p: now).status("/database", now=now, recent_s=100).state == "absent"


def test_config_defaults_and_validation():
    cfg = MissionsConfig().checkpoint
    assert cfg.max_file_bytes == 1024 * 1024 and cfg.max_total_bytes == 256 * 1024 * 1024
    assert cfg.max_store_bytes == 2 * 1024 * 1024 * 1024 and cfg.retention_days == 90 and cfg.root is None
    assert CheckpointSettings.from_config(cfg) == CheckpointSettings()
    with pytest.raises(ValidationError):
        CheckpointConfig(root="relative")
    with pytest.raises(ValidationError):
        CheckpointConfig(max_file_bytes=0)
    with pytest.raises(ValidationError):
        CheckpointConfig(backup_sources=[{"path": "x", "marker": "/m"}])
    assert MissionsConfig().enabled is False


def test_startup_scan_reports_unreferenced_committed(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    assert env.svc.startup_scan() == {"quarantined": [], "unreferenced": []}
    with env.ms.store._write() as conn:
        conn.execute("UPDATE missions SET checkpoint_id=NULL WHERE id=?", (env.mission.id,))
    assert env.svc.startup_scan()["unreferenced"] == [cid]
