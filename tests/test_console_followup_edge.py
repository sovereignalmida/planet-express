"""awaiting_decision -> investigating: the operator reopens a finished observe/propose mission for another turn (and nothing else may)."""
import pytest
from missions_helpers import advance_to, make_store, new_mission
from test_missions_state_events import approve_all, force_status

from planet_express.missions.models import Grant
from planet_express.missions.state import IllegalTransition, transition
from planet_express.missions.store import MissionStore


def finished(tmp_path, tier="observe"):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    mission = ms.create_mission(title="t", origin="operator", grant=Grant.model_validate({"tier": tier}), goal="g",
                                success_criteria="s", runtime="console")
    advance_to(ms, mission.id, "queued")
    force_status(store, mission.id, "awaiting_decision")
    return store, ms, mission


@pytest.mark.parametrize("tier", ["observe", "propose"])
def test_an_operator_reopens_a_finished_observe_or_propose_mission(tmp_path, tier):
    store, ms, mission = finished(tmp_path, tier)
    assert transition(store, mission.id, "investigating", "operator:chris") == "awaiting_decision"
    assert ms.get_mission(mission.id).status == "investigating"
    last = ms.events(mission.id)[-1]
    assert last.actor == "operator:chris" and '"to": "investigating"' in last.body_json       # the audit trail names the operator


def test_nothing_but_an_operator_may_reopen(tmp_path):
    store, ms, mission = finished(tmp_path)
    for actor in ("supervisor", "core", "system", "agent"):
        with pytest.raises(IllegalTransition, match="only an operator"):
            transition(store, mission.id, "investigating", actor)
    assert ms.get_mission(mission.id).status == "awaiting_decision"


def test_a_mission_that_may_mutate_is_never_reopened(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    mission = new_mission(ms, Grant.model_validate({"tier": "repair"}))
    approve_all(store, mission)
    force_status(store, mission.id, "awaiting_decision")
    with pytest.raises(IllegalTransition, match="cannot be reopened"):
        transition(store, mission.id, "investigating", "operator:chris")
    assert ms.get_mission(mission.id).status == "awaiting_decision"


def test_a_terminal_mission_cannot_be_reopened(tmp_path):
    store, ms, mission = finished(tmp_path)
    transition(store, mission.id, "completed", "operator:chris")
    with pytest.raises(IllegalTransition):
        transition(store, mission.id, "investigating", "operator:chris")
