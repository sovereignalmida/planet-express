"""Shared fixtures for the mission tests (plain helpers, no conftest in this repo)."""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core.store import Store, write_snapshot_marker
from planet_express.missions.models import Grant
from planet_express.missions.store import MissionStore


class Clock:
    def __init__(self, t: float = 1_000_000):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_store(tmp_path: Path, clock: Clock | None = None) -> tuple[Store, Clock]:
    clock = clock or Clock()
    db = tmp_path / "state" / "pe.db"
    write_snapshot_marker(db, now=clock.t)
    store = Store(db, clock=clock)
    store.init()
    return store, clock


def repair_grant(**over) -> Grant:
    body = {"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["local"]}}
    body.update(over)
    return Grant.model_validate(body)


def new_mission(ms: MissionStore, grant: Grant | None = None, **kw):
    grant = grant or repair_grant()
    return ms.create_mission(title="t", origin="operator", grant=grant, goal="g", success_criteria="s",
                             runtime="fake", **kw)


def advance_to(ms: MissionStore, mission_id: str, *states: str, actor: str = "operator:me"):
    from planet_express.missions.state import transition
    for s in states:
        transition(ms.store, mission_id, s, actor)


def token(n: int = 1) -> str:
    return f"{n:064x}"


def v5_database(path: Path) -> None:
    """A real v5 database: a fresh v5 schema with the version stamped 5, plus a row to prove survival."""
    from planet_express.core.store import _SCHEMA_V5
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA_V5)
        conn.execute("PRAGMA user_version = 5")
        conn.execute("INSERT INTO events (ts, kind, payload) VALUES (1, 'preexisting', '{}')")
