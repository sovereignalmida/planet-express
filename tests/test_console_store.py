"""The conversation record: PE's own copy of the thread, redacted and bounded, with one-shot message consumption and a one-way state."""
import sqlite3

import pytest
from missions_helpers import make_store

from planet_express.console.store import (
    MAX_MESSAGE_CHARS,
    ConsoleStore,
    ConversationClosed,
    ConversationNotFound,
)
from planet_express.core.store import Store
from planet_express.missions.models import Grant
from planet_express.missions.store import MissionStore


def rig(tmp_path):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    mission = ms.create_mission(title="t", origin="operator", grant=Grant.model_validate({"tier": "observe"}), goal="g",
                                success_criteria="s", runtime="console")
    cs = ConsoleStore(store)
    return store, clock, mission, cs, cs.create(mission.id, title="why is the media stack slow?\nsecond line", origin="operator",
                                                token_budget=150_000)


def test_a_conversation_is_created_open_with_a_one_line_title_and_its_own_state_directory_name(tmp_path):
    _, _, mission, cs, conv = rig(tmp_path)
    assert conv.state == "open" and conv.mission_id == mission.id and conv.title == "why is the media stack slow?"
    assert conv.state_dir == f"C-{conv.id}" and conv.tokens_left == 150_000 and conv.turns == 0 and conv.uid is None
    from planet_express.missions.launcher.spec import STATE_DIR
    assert STATE_DIR.fullmatch(conv.state_dir)                              # the launcher accepts the name


def test_one_conversation_per_mission_and_a_positive_budget(tmp_path):
    store, _, mission, cs, _ = rig(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        cs.create(mission.id, title="x", origin="operator", token_budget=1)
    with pytest.raises(ValueError):
        cs.create(mission.id, title="x", origin="operator", token_budget=0)
    assert cs.by_mission(mission.id).mission_id == mission.id and cs.by_mission("M-999") is None


def test_messages_are_redacted_bounded_and_ordered(tmp_path):
    _, _, _, cs, conv = rig(tmp_path)
    cs.add_message(conv.id, "operator", "the key is OPENROUTER_API_KEY=sk-or-v1-" + "a" * 40 + " ok?")
    cs.add_message(conv.id, "agent", "x" * (MAX_MESSAGE_CHARS + 500), turn=1)
    first, second = cs.messages(conv.id)
    assert "sk-or-v1-" + "a" * 40 not in first.text and first.role == "operator"
    assert len(second.text) <= MAX_MESSAGE_CHARS and second.text.endswith("[message truncated]") and second.turn == 1
    assert [m.seq for m in cs.messages(conv.id, after=first.seq)] == [second.seq]


def test_a_pending_operator_message_is_taken_by_exactly_one_turn(tmp_path):
    _, _, _, cs, conv = rig(tmp_path)
    assert cs.pending(conv.id) is None and cs.has_pending() == []
    seq = cs.add_message(conv.id, "operator", "first")
    cs.add_message(conv.id, "agent", "reply", turn=1)
    assert cs.pending(conv.id).seq == seq and cs.has_pending() == [conv.id]
    assert cs.consume(seq, 1) is True and cs.consume(seq, 2) is False          # a second turn cannot take it again
    assert cs.pending(conv.id) is None and cs.has_pending() == []


def test_an_unknown_conversation_is_refused(tmp_path):
    _, _, _, cs, _ = rig(tmp_path)
    for call in (lambda: cs.get("nope"), lambda: cs.add_message("nope", "operator", "x"), lambda: cs.set_state("nope", "archiving")):
        with pytest.raises(ConversationNotFound):
            call()


def test_state_only_moves_forward_one_step_and_a_closed_conversation_takes_no_messages(tmp_path):
    _, _, _, cs, conv = rig(tmp_path)
    assert cs.set_state(conv.id, "archived") is False and cs.get(conv.id).state == "open"        # no skipping
    assert cs.set_state(conv.id, "archiving") is True
    with pytest.raises(ConversationClosed):
        cs.add_message(conv.id, "operator", "late", require_open=True)
    cs.add_message(conv.id, "system", "archiving", require_open=False)                           # the system may still note it
    assert cs.set_state(conv.id, "open") is False and cs.set_state(conv.id, "archived") is True
    done = cs.get(conv.id)
    assert done.state == "archived" and done.archived_at is not None and done.uid is None
    assert cs.set_state(conv.id, "archiving") is False                                            # nothing reopens


def test_turns_are_numbered_when_they_start_and_usage_is_charged_when_they_finish_and_never_negative(tmp_path):
    _, _, _, cs, conv = rig(tmp_path)
    assert cs.begin_turn(conv.id) == 1 and cs.begin_turn(conv.id) == 2          # a turn that crashes still used its number
    assert cs.add_usage(conv.id, 4000) == 4000 and cs.add_usage(conv.id, -50) == 4000
    got = cs.get(conv.id)
    assert got.turns == 2 and got.tokens_used == 4000 and got.tokens_left == 146_000
    with pytest.raises(ConversationNotFound):
        cs.begin_turn("nope")
    with pytest.raises(ConversationNotFound):
        cs.add_usage("nope", 1)


def test_the_agent_session_id_is_recorded_every_turn(tmp_path):
    _, _, _, cs, conv = rig(tmp_path)
    cs.set_agent_session(conv.id, "11111111-2222-3333-4444-555555555555")
    cs.set_agent_session(conv.id, "99999999-2222-3333-4444-555555555555")          # an interrupt changes it
    assert cs.get(conv.id).agent_session_id.startswith("99999999")


def test_listing_counting_and_idle_candidates(tmp_path):
    store, clock, mission, cs, conv = rig(tmp_path)
    ms = MissionStore(store)
    other = ms.create_mission(title="t2", origin="operator", grant=Grant.model_validate({"tier": "observe"}), goal="g",
                              success_criteria="s", runtime="console")
    clock.t += 100
    second = cs.create(other.id, title="b", origin="incident", token_budget=10)
    assert cs.count_open() == 2 and [c.id for c in cs.list()] == [second.id, conv.id]
    clock.t += 1000
    cs.add_message(second.id, "operator", "still here")
    assert [c.id for c in cs.idle_since(int(clock.t) - 500)] == [conv.id]          # only the one nobody touched
    cs.set_state(conv.id, "archiving")
    assert cs.count_open() == 1 and cs.idle_since(int(clock.t) + 1) == [cs.get(second.id)]


def test_the_tables_appear_on_an_existing_database_without_a_schema_bump(tmp_path):
    """An upgraded host has a v6 database from before the Console: opening it adds the tables and changes nothing else."""
    store, _ = make_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        for table in ("console_messages", "console_conversations", "reserved_uids"):
            conn.execute(f"DROP TABLE {table}")
        before = conn.execute("PRAGMA user_version").fetchone()[0]
    Store(store.path, clock=store._clock).init()
    with sqlite3.connect(store.path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"console_messages", "console_conversations", "reserved_uids"} <= names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == before == 6


def test_a_newer_plan_supersedes_the_pending_one_and_a_final_status_is_final(tmp_path):
    _, _, _, cs, conv = rig(tmp_path)
    assert cs.current_plan(conv.id) is None
    first = cs.add_plan(conv.id, 1, {"known": []}, "a" * 64)
    second = cs.add_plan(conv.id, 2, {"known": ["x"]}, "b" * 64)
    assert [p["status"] for p in cs.plans(conv.id)] == ["superseded", "pending"] and cs.current_plan(conv.id)["id"] == second
    assert cs.set_plan_status(first, "scrapped") is False                         # already superseded: stays superseded
    assert cs.set_plan_status(second, "scrapped") is True and cs.current_plan(conv.id) is None
    assert cs.set_plan_status(second, "confirmed") is False                       # a final status never changes
    with pytest.raises(ValueError):
        cs.set_plan_status(second, "superseded")
    with pytest.raises(ConversationNotFound):
        cs.add_plan("nope", 1, {}, "c" * 64)
