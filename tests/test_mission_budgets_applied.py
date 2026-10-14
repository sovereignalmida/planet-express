"""Carried in from Codex G3 on slice 3 (P1): `missions.budgets.*` must actually reach the grants a mission is created with."""
import pytest
from missions_helpers import make_store

from config_schema import MissionBudgetsConfig
from planet_express.application.mission_service import MissionService
from planet_express.missions.models import Budgets
from planet_express.missions.store import MissionStore

INCIDENT = {"id": "c" * 12, "kind": "container_health", "resource": "media/plex", "summary": "s", "severity": "HIGH",
            "status": "open", "events": []}


def service(tmp_path, budgets=None):
    store, _ = make_store(tmp_path)
    with store._write() as conn:
        conn.execute("INSERT INTO incident_reconciliations VALUES ('scan1', 'ts', 1, 1)")
        conn.execute("INSERT INTO incidents VALUES (?, 'fp', 1, 'container_health', 'media/plex', 'open', "
                     "'failing', 'HIGH', 's', '{}', 'scan1', 1, 1, NULL, 1)", ("c" * 12,))
    kwargs = {} if budgets is None else {"budgets": budgets}
    return MissionService(store, enabled=lambda: True, incident_context=lambda i: INCIDENT, **kwargs), MissionStore(store)


def grant_of(svc_store, mission):
    return svc_store.get_mission(mission["id"]).grant()


def test_configured_budgets_are_applied_to_an_operator_mission(tmp_path):
    cfg = MissionBudgetsConfig(wall_seconds=120, tool_calls=7, output_bytes=4096, retries=0)
    svc, ms = service(tmp_path, lambda: cfg)
    made = svc.create_mission(operator="telegram:1", title="t", goal="g")
    assert grant_of(ms, made).budgets == Budgets(wall_s=120, tool_calls=7, output_bytes=4096, retries=0)


def test_configured_budgets_are_applied_to_an_incident_mission(tmp_path):
    cfg = MissionBudgetsConfig(wall_seconds=300, tool_calls=11, output_bytes=1000, retries=1)
    svc, ms = service(tmp_path, lambda: cfg)
    made = svc.investigate_incident("c" * 12, operator="telegram:1")["mission"]
    assert grant_of(ms, made).budgets == Budgets(wall_s=300, tool_calls=11, output_bytes=1000, retries=1)


def test_budgets_are_clamped_to_the_grant_model_maxima(tmp_path):
    cfg = MissionBudgetsConfig(wall_seconds=10**9, tool_calls=10**9, output_bytes=2**50, retries=10**6)
    svc, ms = service(tmp_path, lambda: cfg)
    budgets = grant_of(ms, svc.create_mission(operator="telegram:1", title="t", goal="g")).budgets
    assert budgets == Budgets(wall_s=86400 * 7, tool_calls=100000, output_bytes=2**40, retries=100)


def test_the_live_config_is_read_at_creation_time(tmp_path):
    current = {"cfg": MissionBudgetsConfig(wall_seconds=60)}
    svc, ms = service(tmp_path, lambda: current["cfg"])
    first = svc.create_mission(operator="telegram:1", title="a", goal="g")
    current["cfg"] = MissionBudgetsConfig(wall_seconds=90)
    second = svc.create_mission(operator="telegram:1", title="b", goal="g")
    assert grant_of(ms, first).budgets.wall_s == 60 and grant_of(ms, second).budgets.wall_s == 90


def test_without_configured_budgets_the_model_defaults_stand(tmp_path):
    svc, ms = service(tmp_path)
    assert grant_of(ms, svc.create_mission(operator="telegram:1", title="t", goal="g")).budgets == Budgets()


def test_the_daemon_passes_the_configured_budgets_to_the_service():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "casa_farnsworth.py").read_text()
    assert "budgets=lambda: config.MISSIONS.budgets" in text


@pytest.mark.parametrize("field", ["wall_seconds", "tool_calls", "output_bytes"])
def test_a_zero_budget_is_still_refused_by_the_config(field):
    with pytest.raises(ValueError):
        MissionBudgetsConfig(**{field: 0})
