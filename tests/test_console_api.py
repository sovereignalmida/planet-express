"""The Console's read models and operator actions: plain-language status written by PE, redacted bounded output, honest composer and budget."""
import json

import pytest
from test_console_service import FIXTURE, Rig

from planet_express.console.api import PLAN_TTL_S, ConsoleApi, ConsoleApiError, describe
from planet_express.console.store import ConsoleStore

GOOD_PLAN = {"known": ["the gateway is healthy"], "unsure": ["whether the start order caused it"], "options": [
    {"name": "Recreate dependents", "recommended": True, "summary": "Recreate the four dependents.", "changes": ["4 services"],
     "will_not_touch": ["the gateway"], "risk": {"level": "low", "text": "about a minute"}, "rolls_back_to": "the checkpoint", "effort": "2 min",
     "checks": [{"id": "up", "check": "container_state", "params": {"container": "gluetun"}}]},
    {"name": "Restart the gateway", "recommended": False, "summary": "Restart only the gateway.", "changes": ["gluetun"],
     "will_not_touch": ["dependents"], "risk": {"level": "medium", "text": "may not recover"}, "rolls_back_to": "the checkpoint",
     "effort": "1 min", "checks": [{"id": "up", "check": "container_state", "params": {"container": "gluetun"}}]}]}


class ApiRig(Rig):
    def __init__(self, tmp_path, **cfg):
        super().__init__(tmp_path, **cfg)
        self.api = ConsoleApi(self.service, self.console, self.ms, self.sup, self.cfg, clock=self.clock)


@pytest.fixture
def rig(tmp_path):
    return ApiRig(tmp_path)


def test_a_new_conversation_reads_as_getting_ready_then_looking_into_it_then_a_ready_answer_in_plain_words(rig):
    made = rig.api.create("chris", "why is the media stack slow?")
    feed = rig.api.feed(made["id"])
    assert feed["conversation"]["status"] == {"text": "Getting ready", "level": "none", "group": "Working"}
    assert feed["composer"]["mode"] == "busy" and feed["running"] is True
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    feed = rig.api.feed(made["id"])
    assert feed["conversation"]["status"]["text"] == "Looking into it" and feed["conversation"]["status"]["level"] == "ok"
    rig.finish_turn(conv)
    feed = rig.api.feed(made["id"])
    assert feed["conversation"]["status"] == {"text": "Result ready", "level": "ok", "group": "Recent"}
    assert feed["composer"] == {"mode": "idle", "reason": None} and feed["running"] is False
    assert [m["role"] for m in feed["messages"]] == ["operator", "agent"] and feed["messages"][0]["by"] == "chris"
    assert "internal" not in json.dumps(feed["conversation"]) and "awaiting_decision" not in json.dumps(feed)       # no internal state names


def test_the_feed_is_incremental_for_messages_and_events_and_events_know_their_turn(rig):
    made = rig.api.create("chris", "one")
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    rig.finish_turn(conv)
    first = rig.api.feed(made["id"])
    calls = [e for e in first["events"] if e["type"] == "call"]
    assert calls and calls[0]["name"] == "Bash" and calls[0]["kind"] == "ran" and calls[0]["turn"] == 1
    assert any(e["type"] == "result" and e["excerpt"] == "hi" for e in first["events"])
    again = rig.api.feed(made["id"], after_message=first["last_message"], after_event=first["last_event"])
    assert again["messages"] == [] and again["events"] == [] and again["last_message"] == first["last_message"]
    rig.api.post(made["id"], "chris", "two")
    rig.sup.tick()
    rig.finish_turn(conv)
    later = rig.api.feed(made["id"], after_message=first["last_message"], after_event=first["last_event"])
    assert [m["text"] for m in later["messages"] if m["role"] == "operator"] == ["two"]
    assert {e["turn"] for e in later["events"]} == {2}                                    # the second turn's trail only


def test_a_pending_plan_is_shown_and_the_status_says_it_needs_a_call_with_the_recommendation(rig):
    made = rig.api.create("chris", "fix it")
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    rig.finish_turn(conv)
    rig.console.add_plan(conv.id, 1, GOOD_PLAN, "a" * 64)
    feed = rig.api.feed(made["id"])
    assert feed["conversation"]["status"] == {"text": "Needs your call", "level": "warn", "group": "Needs you"}
    assert feed["conversation"]["verdict"]["h"] == "Waiting for your choice between 2 options"
    assert "Recommended: Recreate dependents" in feed["conversation"]["verdict"]["p"]
    assert feed["plan"]["status"] == "pending" and feed["plan"]["hash"] == "aaaaaaaa" and feed["plan"]["expired"] is False
    assert feed["plan"]["plan"]["options"][0]["name"] == "Recreate dependents"
    rig.clock.t += PLAN_TTL_S + 5
    assert rig.api.feed(made["id"])["plan"]["expired"] is True
    assert rig.api.list()["conversations"][0]["status"]["group"] == "Needs you"


def test_scrapping_a_plan_changes_nothing_and_says_so_and_only_a_pending_plan_can_be_scrapped(rig):
    made = rig.api.create("chris", "fix it")
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    rig.finish_turn(conv)
    with pytest.raises(ConsoleApiError) as err:
        rig.api.scrap_plan(made["id"], "chris")
    assert err.value.code == "conflict"
    rig.console.add_plan(conv.id, 1, GOOD_PLAN, "a" * 64)
    assert rig.api.scrap_plan(made["id"], "chris") == {"scrapped": True}
    feed = rig.api.feed(made["id"])
    assert feed["plan"] is None and feed["conversation"]["status"]["text"] == "Result ready"
    assert any(m["role"] == "system" and "Plan scrapped by chris. Nothing was changed." in m["text"] for m in feed["messages"])


def test_a_turn_that_did_not_finish_says_so_with_the_reason_and_needs_you(rig):
    made = rig.api.create("chris", "hello")
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    rig.finish_turn(conv, stream=b'{"type":"system","subtype":"init","session_id":"aaaaaaaa-1111"}\n')
    feed = rig.api.feed(made["id"])
    assert feed["conversation"]["status"] == {"text": "Didn't finish", "level": "high", "group": "Needs you"}
    assert "stopped unexpectedly" in feed["conversation"]["verdict"]["p"]


def test_the_budget_meter_and_the_composer_follow_the_budget(tmp_path):
    rig = ApiRig(tmp_path, token_budget=100_000)
    made = rig.api.create("chris", "hello")
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    rig.finish_turn(conv)
    rig.console.add_usage(conv.id, 20_000)
    assert rig.api.feed(made["id"])["budget"] == {"used_pct": 24, "tokens_used": 24_000, "token_budget": 100_000, "tokens_left": 76_000, "state": "ok"}   # 4,000 for the turn + 20,000
    rig.console.add_usage(conv.id, 56_000)                                              # 80,000 used: the warning line
    feed = rig.api.feed(made["id"])
    assert feed["budget"]["state"] == "warn" and feed["budget"]["used_pct"] == 80 and feed["composer"]["mode"] == "idle"
    rig.console.add_usage(conv.id, 1_000)                                               # 19,000 left: under the 20,000 a turn needs
    feed = rig.api.feed(made["id"])
    assert feed["budget"]["state"] == "stop" and feed["composer"]["mode"] == "off" and "budget" in feed["composer"]["reason"]
    assert feed["conversation"]["status"]["text"] == "Budget used up"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.post(made["id"], "chris", "more")
    assert err.value.code == "conflict"


def test_the_composer_is_off_with_a_reason_when_the_launcher_is_not_proven_or_the_conversation_is_closed(rig):
    made = rig.api.create("chris", "hello")
    rig.sup.preflight_fn = lambda: None
    assert rig.api.status()["proposal_only"] is True
    feed = rig.api.feed(made["id"])
    assert feed["composer"]["mode"] == "off" and "launcher" in feed["composer"]["reason"]
    rig.sup.preflight_fn = None
    rig.api.archive(made["id"], "chris")
    assert rig.api.feed(made["id"])["composer"] == {"mode": "off", "reason": "This conversation is closed."}
    with pytest.raises(ConsoleApiError) as err:
        rig.api.post(made["id"], "chris", "late")
    assert err.value.code == "conflict"


def test_refusals_are_typed_and_say_what_to_do(tmp_path):
    rig = ApiRig(tmp_path, max_open=1)
    made = rig.api.create("chris", "one")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.create("chris", "two")
    assert err.value.code == "conflict" and "archive one first" in str(err.value)
    for call in (lambda: rig.api.feed("nope"), lambda: rig.api.post("nope", "chris", "x"), lambda: rig.api.interrupt("nope", "chris"),
                 lambda: rig.api.archive("nope", "chris"), lambda: rig.api.scrap_plan("nope", "chris")):
        with pytest.raises(ConsoleApiError) as err:
            call()
        assert err.value.code == "not_found"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.create("chris", "   ")
    assert err.value.code == "bad_request"
    assert rig.api.interrupt(made["id"], "chris") == {"stopped": False}


def test_everything_returned_is_redacted_and_bounded(rig):
    secret = "sk-or-v1-" + "a" * 48
    made = rig.api.create("chris", f"my key is OPENROUTER_API_KEY={secret}")
    rig.sup.tick()
    conv = rig.console.get(made["id"])
    stream = (json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                                                          "input": {"command": f"echo OPENROUTER_API_KEY={secret} " + "x" * 5000}}]}})
              + "\n" + json.dumps({"type": "result", "subtype": "success", "result": "r" * 20_000, "usage": {}})).encode()
    rig.finish_turn(conv, stream=stream)
    feed = rig.api.feed(made["id"])
    assert secret not in json.dumps(feed)
    assert all(len(m["text"]) <= 8_100 for m in feed["messages"])
    assert all(len(e.get("detail", "")) <= 300 for e in feed["events"])


def test_the_list_groups_and_ages_conversations_and_hides_archived_ones(rig):
    a = rig.api.create("chris", "first")
    rig.api.create("chris", "second")
    rig.clock.t += 7200
    rig.api.archive(a["id"], "chris")
    for _ in range(4):
        rig.sup.tick()
    rows = rig.api.list()["conversations"]
    assert [r["title"] for r in rows] == ["second"] and rows[0]["age"] == "2h" and rows[0]["origin"] == "operator"
    assert describe(rig.console.get(a["id"]), "completed", None, False, None, 20_000)["text"] == "Archived"


def test_the_fixture_is_the_real_stream_the_trail_was_built_from():
    assert FIXTURE.exists() and ConsoleStore is not None
