"""The Console end to end over the real supervisor and the fake containment: a conversation's turns, its one uid, its state
directory, interrupts, crashes, the budget and the archive sequence."""
import json
import os
import stat
from pathlib import Path

import pytest
from missions_helpers import make_store

from config_schema import MissionConsoleConfig
from planet_express.console.runtime import ConsoleRuntime
from planet_express.console.service import (
    BudgetSpent,
    ConsoleError,
    ConsoleFull,
    ConsoleService,
)
from planet_express.console.store import ConsoleStore
from planet_express.missions.containment import FakeContainment
from planet_express.missions.models import Grant
from planet_express.missions.store import MissionStore
from planet_express.missions.supervisor import Supervisor, UnsupportedTier

FIXTURE = Path(__file__).parent / "fixtures" / "console_stream_tool_turn.jsonl"
STREAM_ID = "2dc11a5c-9cef-4ce6-98ca-232a0476de7a"


class Broker:
    def __init__(self, tokens=4000):
        self.tokens = tokens

    def set_mission_cap(self, mission_id, tokens):
        pass

    def usage(self, session_id):
        return {"requests": 2, "tokens": self.tokens, "refused": 0}


class Rig:
    def __init__(self, tmp_path, **cfg):
        self.store, self.clock = make_store(tmp_path)
        self.ms, self.console = MissionStore(self.store), ConsoleStore(self.store)
        self.cont = FakeContainment()
        self.cfg = MissionConsoleConfig(**cfg)
        self.runtime = ConsoleRuntime(self.cont, self.console, self.cfg, worker_socket="/run/w.sock", broker=Broker())
        self.root = tmp_path / "ws"
        self.sup = Supervisor(self.store, self.cont, lambda mission: self.runtime, self.clock, enabled=True,
                              artifact_dir=tmp_path / "artifacts", workspace_dir=self.root)
        self.service = ConsoleService(self.ms, self.console, self.sup, self.cfg, uid_range=lambda: self.cont.uid_range, clock=self.clock)
        self.sup.turn_ended, self.sup.console_tick = self.service.turn_ended, self.service.tick

    def open(self, text="why is the media stack slow?", operator="chris"):
        return self.service.create(operator, text)

    def sessions(self, conv):
        return [r for r in self._rows(conv)]

    def _rows(self, conv):
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM mission_sessions WHERE mission_id=? ORDER BY rowid", (conv.mission_id,))]

    def live_session(self, conv):
        return self.ms.live_session(conv.mission_id)["id"]

    def status(self, conv):
        return self.ms.get_mission(conv.mission_id).status

    def finish_turn(self, conv, stream=None):
        sid = self.live_session(conv)
        self.cont.write_output(sid, FIXTURE.read_bytes() if stream is None else stream)
        self.cont.crash(sid)
        self.sup.poll(conv.mission_id)

    def spawns(self, conv):
        return [self.cont.spawns(r["id"])[0] if self.cont.spawns(r["id"]) else None for r in self._rows(conv)]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_creating_a_conversation_reserves_its_uid_and_queues_its_mission_and_the_first_turn_starts_at_the_tick(rig):
    conv = rig.open()
    assert conv.state == "open" and conv.uid == rig.ms.reserved_uid(conv.mission_id) == 60000
    mission = rig.ms.get_mission(conv.mission_id)
    assert mission.runtime == "console" and mission.tier == "observe" and mission.status == "queued" and mission.origin == "operator"
    assert mission.grant().budgets.retries == 0 and mission.grant().budgets.wall_s == rig.cfg.turn_wall_s
    assert [(m.role, m.actor) for m in rig.console.messages(conv.id)] == [("operator", "operator:chris")]
    assert rig.ms.reserved_uid("M-nope") is None
    rig.sup.tick()
    assert rig.status(conv) == "investigating"
    spawn = rig.spawns(conv)[0]
    assert spawn["uid"] == 60000 and spawn["state_dir"] == conv.state_dir and spawn["argv"][0] == "/usr/local/bin/claude"
    assert (rig.root / conv.state_dir).is_dir() and stat.S_IMODE((rig.root / conv.state_dir).stat().st_mode) == 0o700     # Core made it, empty


def test_a_finished_turn_waits_for_the_operator_and_the_next_message_reopens_it_as_that_operator_with_the_same_uid(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.finish_turn(conv)
    assert rig.status(conv) == "awaiting_decision"
    assert [m.text for m in rig.console.messages(conv.id) if m.role == "agent"] == ["all done here"]
    first_rows = rig._rows(conv)
    assert first_rows[0]["uid_released_at"] is not None                        # the session's uid row is free; the reservation is not
    assert rig.ms.reserved_uid(conv.mission_id) == 60000
    rig.service.post(conv.id, "chris", "and the second thing?")
    rig.sup.tick()
    assert rig.status(conv) == "investigating"
    rows = rig._rows(conv)
    assert len(rows) == 2 and rows[1]["session_uid"] == 60000
    spawn = rig.cont.spawns(rows[1]["id"])[0]
    assert spawn["argv"][spawn["argv"].index("--resume") + 1] == STREAM_ID and spawn["state_dir"] == conv.state_dir
    moves = [e for e in rig.ms.events(conv.mission_id) if e.kind == "state" and '"to": "investigating"' in e.body_json]
    assert [e.actor for e in moves] == ["supervisor", "operator:chris"]          # the first turn is the supervisor's start, the follow-up the operator's


def test_a_message_posted_while_a_turn_runs_waits_for_it_and_is_then_taken_exactly_once(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.service.post(conv.id, "chris", "one more thing")
    rig.sup.tick()
    rig.sup.tick()
    assert len(rig._rows(conv)) == 1                                           # the running turn was not disturbed
    rig.finish_turn(conv)
    rig.sup.tick()
    assert len(rig._rows(conv)) == 2
    rig.finish_turn(conv)
    for _ in range(3):
        rig.sup.tick()
    assert len(rig._rows(conv)) == 2 and rig.console.pending(conv.id) is None


def test_interrupting_a_turn_keeps_what_it_said_and_leaves_the_conversation_open(rig):
    conv = rig.open()
    rig.sup.tick()
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, json.dumps({"type": "assistant", "session_id": "bbbbbbbb-2222-3333", "message": {"content": [
        {"type": "text", "text": "I was checking the gateway"}]}}).encode() + b"\n")
    assert rig.service.interrupt(conv.id, "chris") is True
    assert rig.status(conv) == "awaiting_decision" and rig.console.get(conv.id).state == "open"
    texts = [(m.role, m.text) for m in rig.console.messages(conv.id)]
    assert ("agent", "I was checking the gateway") in texts and ("system", "This turn was stopped before it finished.") in texts
    assert rig.console.get(conv.id).agent_session_id == "bbbbbbbb-2222-3333"
    assert rig.service.interrupt(conv.id, "chris") is False                    # nothing is running now
    rig.service.post(conv.id, "chris", "carry on")
    rig.sup.tick()
    assert rig.status(conv) == "investigating"


def test_a_turn_that_dies_is_reported_and_never_restarted_by_itself_and_the_operator_can_go_on(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.finish_turn(conv, stream=b'{"type":"system","subtype":"init","session_id":"aaaaaaaa-1111"}\n')
    assert rig.status(conv) == "needs_operator" and len(rig._rows(conv)) == 1          # no automatic second attempt
    assert any(m.role == "system" and "stopped unexpectedly" in m.text for m in rig.console.messages(conv.id))
    for _ in range(3):
        rig.sup.tick()
    assert len(rig._rows(conv)) == 1
    rig.service.post(conv.id, "chris", "try again")
    rig.sup.tick()
    assert rig.status(conv) == "investigating" and len(rig._rows(conv)) == 2


def test_the_open_conversation_cap_and_the_budget_and_empty_messages_are_refused_in_plain_words(tmp_path):
    rig = Rig(tmp_path, max_open=2, token_budget=30_000)
    one, two = rig.open("a"), rig.open("b")
    with pytest.raises(ConsoleFull, match="2 conversations"):
        rig.open("c")
    assert rig.ms.reserved_uid(one.mission_id) == 60000 and rig.ms.reserved_uid(two.mission_id) == 60001
    with pytest.raises(ConsoleError, match="message"):
        rig.service.post(one.id, "chris", "   ")
    with pytest.raises(ConsoleError, match="starts with a message"):
        rig.service.create("chris", "")
    with pytest.raises(ConsoleError, match="operator name"):
        rig.service.post(one.id, "  ", "hello")
    rig.console.add_usage(one.id, 15_000)                                       # 15,000 left: under the 20,000 a turn needs
    with pytest.raises(BudgetSpent):
        rig.service.post(one.id, "chris", "hello")
    assert rig.console.count_open() == 2 and len(rig.console.messages(one.id)) == 1


def test_a_failed_creation_leaves_nothing_reserved_and_nothing_to_start(tmp_path):
    rig = Rig(tmp_path)
    rig.cont.uid_range = (60000, 60000)
    rig.open("a")
    with pytest.raises(ConsoleFull, match="no session identity"):
        rig.open("b")
    assert rig.console.count_open() == 1
    cancelled = [m for m in rig.ms.list_missions() if m.status == "cancelled"]
    assert len(cancelled) == 1 and rig.ms.reserved_uid(cancelled[0].id) is None


def test_archiving_closes_the_mission_empties_the_state_directory_frees_the_uid_and_a_new_conversation_may_reuse_it(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.finish_turn(conv)
    state = rig.root / conv.state_dir
    (state / "cfg").mkdir()
    (state / "cfg" / "transcript.jsonl").write_text("{}")
    assert rig.service.archive(conv.id, "chris") is True and rig.service.archive(conv.id, "chris") is False
    with pytest.raises(Exception, match="closed|archiving"):
        rig.service.post(conv.id, "chris", "late")
    rig.sup.tick()
    assert rig.status(conv) == "completed" and not state.exists()
    done = rig.console.get(conv.id)
    assert done.state == "archived" and done.uid is None and rig.ms.reserved_uid(conv.mission_id) is None
    again = rig.open("a new one")
    assert again.uid == 60000                                                     # the uid is back in the pool


def test_an_archive_whose_state_directory_core_cannot_remove_uses_a_cleanup_session_and_holds_the_uid_until_it_is_empty(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.finish_turn(conv)
    stuck = rig.root / conv.state_dir / ".cache"
    stuck.mkdir()
    (stuck / "blob").write_text("x")
    stuck.chmod(0o500)
    try:
        rig.service.archive(conv.id, "chris")
        rig.sup.tick()
        from planet_express.missions.supervisor import cleanup_id
        assert rig.console.get(conv.id).state == "archiving" and rig.ms.reserved_uid(conv.mission_id) == 60000     # the uid is still held
        assert rig.cont.exists(cleanup_id(conv.state_dir))
        spawn = rig.cont.spawns(cleanup_id(conv.state_dir))[0]
        assert spawn["uid"] == 60000 and spawn["argv"][-1] == str(rig.root / conv.state_dir) and spawn["state_dir"] is None
        stuck.chmod(0o700)                                                        # what the cleanup session does as the owner
        rig.cont.crash(cleanup_id(conv.state_dir))
        rig.sup.tick()
        assert rig.console.get(conv.id).state == "archived" and not (rig.root / conv.state_dir).exists()
        assert rig.ms.reserved_uid(conv.mission_id) is None
    finally:
        if stuck.exists():
            stuck.chmod(0o700)


def test_archiving_a_conversation_with_a_turn_running_stops_the_turn_first(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.service.archive(conv.id, "chris")
    rig.sup.tick()
    assert not rig.sup.has_run(conv.mission_id)
    rig.sup.tick()
    assert rig.console.get(conv.id).state == "archived" and rig.status(conv) == "completed"


def test_an_idle_conversation_is_archived_after_the_configured_days_and_a_busy_one_is_not(tmp_path):
    rig = Rig(tmp_path, archive_after_days=3)
    old, busy = rig.open("old"), rig.open("busy")
    rig.sup.tick()
    rig.finish_turn(old)
    rig.finish_turn(busy)
    rig.clock.t += 2 * 86_400
    rig.service.post(busy.id, "chris", "still here")                                # activity resets the idle clock
    rig.clock.t += 2 * 86_400                                                       # old: 4 days idle, busy: 2
    rig.sup.tick()
    assert rig.console.get(old.id).state in ("archiving", "archived") and rig.console.get(busy.id).state == "open"
    for _ in range(3):
        rig.sup.tick()
    assert rig.console.get(old.id).state == "archived"


def test_follow_up_refusals(rig):
    conv = rig.open()
    with pytest.raises(Exception, match="waiting for the operator"):
        rig.sup.start_followup(conv.mission_id, "operator:chris")                  # queued: the supervisor starts the first turn itself
    plain = rig.ms.create_mission(title="p", origin="operator", grant=Grant.model_validate({"tier": "observe"}), goal="g",
                                  success_criteria="s", runtime="command")
    with pytest.raises(UnsupportedTier):
        rig.sup.start_followup(plain.id, "operator:chris")                          # not a Console conversation
    with pytest.raises(ValueError, match="operator"):
        rig.sup.start_followup(conv.mission_id, "supervisor")
    with pytest.raises(UnsupportedTier):
        rig.sup.interrupt_turn(plain.id, "operator:chris")
    with pytest.raises(ValueError):
        rig.sup.interrupt_turn(conv.mission_id, "agent")


def test_the_conversation_uid_is_never_given_to_an_ordinary_mission_while_the_conversation_lives(rig):
    conv = rig.open()
    other = rig.ms.create_mission(title="o", origin="operator", grant=Grant.model_validate({"tier": "observe"}), goal="g",
                                  success_criteria="s", runtime="command")
    from planet_express.missions.sessions import issue_session
    cs, _ = issue_session(rig.ms, other.id, "S-other", rig.cont.cgroup_path("S-other"), session_ttl_s=60, lease_ttl_s=60,
                          uid_range=rig.cont.uid_range) if False else (None, None)
    from missions_helpers import advance_to
    advance_to(rig.ms, other.id, "queued")
    cs, _ = issue_session(rig.ms, other.id, "S-other", rig.cont.cgroup_path("S-other"), session_ttl_s=60, lease_ttl_s=60,
                          uid_range=rig.cont.uid_range)
    assert cs.session_uid != 60000 and conv.uid == 60000


def test_every_message_and_event_is_redacted_and_the_state_directory_name_is_the_launchers(rig):
    secret = "sk-or-v1-" + "a" * 48
    conv = rig.open(f"my key is OPENROUTER_API_KEY={secret}")
    rig.sup.tick()
    stream = json.dumps({"type": "result", "subtype": "success", "result": f"you pasted OPENROUTER_API_KEY={secret}", "usage": {}}).encode()
    rig.finish_turn(conv, stream=stream)
    everything = json.dumps([m.text for m in rig.console.messages(conv.id)]) + json.dumps([e.body_json for e in rig.ms.events(conv.mission_id)])
    assert secret not in everything and os.path.isdir(rig.root)


# ── regressions from the second review ───────────────────────────────────────────────────────────

def test_a_turn_that_cannot_start_is_reported_once_and_never_retried_by_itself(tmp_path):
    rig = Rig(tmp_path, token_budget=30_000)
    conv = rig.open()
    rig.console.add_usage(conv.id, 15_000)                                        # 15,000 left: a turn needs 20,000
    for _ in range(8):
        rig.sup.tick()
    assert len(rig._rows(conv)) == 1 and rig.status(conv) == "needs_operator"     # one attempt, no storm
    system = [m.text for m in rig.console.messages(conv.id) if m.role == "system"]
    assert len(system) == 2 and "budget is used up" in system[0] and system[1] == "The turn could not start."
    assert rig.console.pending(conv.id) is None


def test_a_conversation_that_failed_its_last_turn_can_still_be_archived_and_its_uid_comes_back(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.finish_turn(conv, stream=b'{"type":"system","subtype":"init","session_id":"aaaaaaaa-1111"}\n')
    assert rig.status(conv) == "needs_operator"
    rig.service.archive(conv.id, "chris")
    for _ in range(4):
        rig.sup.tick()
    assert rig.console.get(conv.id).state == "archived" and rig.status(conv) == "cancelled"
    assert rig.ms.reserved_uid(conv.mission_id) is None
    assert rig.open("again").uid == 60000                                           # the pool did not leak


def test_a_conversation_idle_in_needs_operator_is_archived_by_the_idle_policy(tmp_path):
    rig = Rig(tmp_path, archive_after_days=3)
    conv = rig.open()
    rig.sup.tick()
    rig.finish_turn(conv, stream=b"{}\n")
    assert rig.status(conv) == "needs_operator"
    rig.clock.t += 4 * 86_400
    for _ in range(4):
        rig.sup.tick()
    assert rig.console.get(conv.id).state == "archived" and rig.ms.reserved_uid(conv.mission_id) is None
