"""Shared fixtures for the mission tests (plain helpers, no conftest in this repo)."""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core.store import Store, write_snapshot_marker
from planet_express.missions.models import Grant, VerificationPlan
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


# Every repair/change/critical mission needs a verification plan the grant names (slice 8, enforced by the store).
DEFAULT_PLAN = VerificationPlan.model_validate(
    {"catalogue_v": 1, "checks": [{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}]})


def repair_grant(**over) -> Grant:
    body = {"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["local"]},
            "verification": {"checks": ["default"], "plan_sha256": DEFAULT_PLAN.hash()}}
    body.update(over)
    return Grant.model_validate(body)


def with_default_plan(grant: Grant) -> Grant:
    """A mutating grant that names no plan gets the default one (observe/propose are left alone)."""
    if grant.tier in ("observe", "propose") or grant.verification.plan_sha256:
        return grant
    body = grant.model_dump(mode="json")
    body["verification"] = {**body["verification"], "checks": ["default"], "plan_sha256": DEFAULT_PLAN.hash()}
    return Grant.model_validate(body)


def add_incident(store: Store, incident_id: str = "c" * 12) -> str:
    """A real incident row (missions.incident_id is a foreign key to it)."""
    with store._write() as conn:
        conn.execute("INSERT OR IGNORE INTO incident_reconciliations VALUES ('scan1', 'ts', 1, 1)")
        conn.execute("INSERT INTO incidents VALUES (?, ?, 1, 'container_health', 'media/plex', 'open', "
                     "'failing', 'HIGH', 'plex is unhealthy', '{}', 'scan1', 1, 1, NULL, 1)",
                     (incident_id, "fp-" + incident_id))
    return incident_id


def new_mission(ms: MissionStore, grant: Grant | None = None, **kw):
    grant = with_default_plan(grant or repair_grant())
    origin = kw.pop("origin", "operator")
    mission = ms.create_mission(title="t", origin=origin, grant=grant, goal="g", success_criteria="s",
                                runtime="fake", **kw)
    if grant.verification.plan_sha256 == DEFAULT_PLAN.hash():
        mission = ms.set_verification_plan(mission.id, DEFAULT_PLAN)
    return mission


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
