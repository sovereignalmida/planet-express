"""Checkpoint config validation (trust mode removed backup freshness and its sources) and the startup scan's unreferenced report."""
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
from planet_express.missions.checkpoint.service import CheckpointSettings


def test_config_defaults_and_validation():
    cfg = MissionsConfig().checkpoint
    assert cfg.max_file_bytes == 1024 * 1024 and cfg.max_total_bytes == 256 * 1024 * 1024
    assert cfg.max_store_bytes == 2 * 1024 * 1024 * 1024 and cfg.retention_days == 90 and cfg.root is None
    assert CheckpointSettings.from_config(cfg) == CheckpointSettings()
    with pytest.raises(ValidationError):
        CheckpointConfig(root="relative")
    with pytest.raises(ValidationError):
        CheckpointConfig(max_file_bytes=0)
    # An old config.yaml's backup sources are accepted and ignored, whatever they hold.
    assert not hasattr(CheckpointConfig(backup_sources=[{"path": "x", "marker": "/m"}]), "backup_sources")
    assert MissionsConfig().enabled is False


def test_startup_scan_reports_unreferenced_committed(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    assert env.svc.startup_scan() == {"quarantined": [], "unreferenced": [], "purge_pending": []}
    with env.ms.store._write() as conn:
        conn.execute("UPDATE missions SET checkpoint_id=NULL WHERE id=?", (env.mission.id,))
    assert env.svc.startup_scan()["unreferenced"] == [cid]
