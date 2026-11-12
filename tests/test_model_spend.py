"""rc14.8.2: what a conversation and a mission cost at the model, read from the broker's own `model.request` records."""
import pytest
from test_console_execution import Rig



@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _call(ms, mission_id, charged, cost=None):
    body = {"event": "model.request", "path": "/v1/messages", "status": 200, "charged": charged}
    if cost is not None:
        body["cost_usd"] = cost
    ms.append_event(mission_id, "tool", "model-broker", body)


def test_a_conversation_counts_its_own_turns_and_every_run_it_started_and_never_guesses_a_price(rig):
    ms = rig.st.ms
    assert rig.feed()["spend"] == {"calls": 0, "tokens": 0, "cost_usd": None, "unpriced": 0}
    view = rig.start_run()
    _call(ms, rig.conv.mission_id, 10_000, 0.05)
    _call(ms, rig.conv.mission_id, 4_000)                         # recorded before rc14.7, or by a provider that prices nothing
    _call(ms, view["mission_id"], 6_000, 0.0213)                  # the run is its own mission
    ms.append_event(view["mission_id"], "tool", "model-broker", {"event": "model.refused", "charged": 99_999})  # not a call
    assert rig.feed()["spend"] == {"calls": 3, "tokens": 20_000, "cost_usd": 0.0713, "unpriced": 1}
    assert ms.model_spend([view["mission_id"]]) == {"calls": 1, "tokens": 6_000, "cost_usd": 0.0213, "unpriced": 0}


def test_only_unpriced_calls_report_no_cost_rather_than_zero(rig):
    _call(rig.st.ms, rig.conv.mission_id, 3_000)
    assert rig.st.ms.model_spend([rig.conv.mission_id, "", rig.conv.mission_id]) == {
        "calls": 1, "tokens": 3_000, "cost_usd": None, "unpriced": 1}
    assert rig.st.ms.model_spend([]) == {"calls": 0, "tokens": 0, "cost_usd": None, "unpriced": 0}
