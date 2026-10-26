"""The console.* RPC methods: exact params, the same disabled/unavailable answers as the mission methods, typed errors."""
import pytest
from test_console_api import ApiRig

from planet_express.application.mission_service import MissionService
from planet_express.integrations.rpc import RpcError


@pytest.fixture
def rig(tmp_path):
    rig = ApiRig(tmp_path)
    rig.service_rpc = MissionService(rig.store, enabled=True)
    rig.service_rpc.console_api = rig.api
    rig.call = lambda method, params: rig.service_rpc.handlers()[method](params)
    return rig


def test_the_methods_exist_and_a_full_round_trip_works(rig):
    made = rig.call("console.create", {"operator": "chris", "text": "why is it slow?"})
    assert rig.call("console.status", {})["enabled"] is True
    assert [c["id"] for c in rig.call("console.list", {})["conversations"]] == [made["id"]]
    feed = rig.call("console.feed", {"conversation_id": made["id"], "after_message": 0, "after_event": 0})
    assert feed["messages"][0]["text"] == "why is it slow?"
    assert rig.call("console.post", {"conversation_id": made["id"], "operator": "chris", "text": "and the logs?"})["seq"] > 0
    assert rig.call("console.interrupt", {"conversation_id": made["id"], "operator": "chris"}) == {"stopped": False}
    assert rig.call("console.archive", {"conversation_id": made["id"], "operator": "chris"}) == {"archiving": True}


@pytest.mark.parametrize("method, params", [
    ("console.create", {"operator": "chris"}),                                            # missing text
    ("console.create", {"operator": "chris", "text": "x", "tier": "repair"}),            # an extra key: no tier, grant or scope can be sent
    ("console.create", {"operator": "chris", "text": ""}),
    ("console.create", {"operator": "chris", "text": "x" * 8001}),
    ("console.create", {"operator": "operator:chris", "text": "x"}),                      # an operator name is plain, never a kind:name
    ("console.create", {"operator": "chris", "text": "x", "incident_id": "nope"}),
    ("console.feed", {"conversation_id": "../etc"}),
    ("console.feed", {"conversation_id": "0123456789abcdef", "after_message": -1}),
    ("console.feed", {"conversation_id": "0123456789abcdef", "after_event": True}),
    ("console.post", {"conversation_id": "0123456789abcdef", "text": "x"}),               # no operator
    ("console.status", {"x": 1}),
])
def test_bad_params_are_refused_before_anything_happens(rig, method, params):
    with pytest.raises(RpcError) as err:
        rig.call(method, params)
    assert err.value.code == "bad_request"
    assert rig.console.count_open() == 0


def test_typed_errors_reach_the_caller_with_their_codes(rig):
    with pytest.raises(RpcError) as err:
        rig.call("console.feed", {"conversation_id": "0123456789abcdef"})
    assert err.value.code == "not_found"
    rig.call("console.create", {"operator": "chris", "text": "a"})
    rig.cfg_open = rig.cfg.max_open
    for i in range(rig.cfg.max_open - 1):
        rig.call("console.create", {"operator": "chris", "text": f"c{i}"})
    with pytest.raises(RpcError) as err:
        rig.call("console.create", {"operator": "chris", "text": "one too many"})
    assert err.value.code == "conflict"


def test_disabled_and_unavailable_answer_like_the_mission_methods(tmp_path):
    off = MissionService(ApiRig(tmp_path / "a").store, enabled=False)
    with pytest.raises(RpcError) as err:
        off.handlers()["console.list"]({})
    assert err.value.code == "disabled"
    on_without_console = MissionService(ApiRig(tmp_path / "b").store, enabled=True)
    for method in ("console.status", "console.list"):
        with pytest.raises(RpcError) as err:
            on_without_console.handlers()[method]({})
        assert err.value.code == "unavailable"
