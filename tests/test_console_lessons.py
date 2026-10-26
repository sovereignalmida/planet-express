"""What a conversation learned: the agent's lessons.json becomes memory CANDIDATES that stay quarantined until the operator keeps them (with or
without their own wording) or discards them. The agent cannot keep, and a candidate is never retrievable by any session."""
import json

import pytest
from test_console_confirm import Rig as ConfirmRig
from test_console_runtime import Inv, Rig as RuntimeRig, Broker

from planet_express.console import lessons as lessons_mod
from planet_express.console.api import ConsoleApiError
from planet_express.memory import retrieval, store as memory_store

LESSON = {"title": "qbittorrent needs gluetun first", "statement": "qbittorrent shares gluetun's network, so it must start after gluetun is healthy.",
          "stacks": ["media"], "services": ["media/qbittorrent"]}


def doc(*lessons):
    return {"lessons": list(lessons or [LESSON])}


def test_the_lessons_file_is_strict_bounded_and_scope_checked_against_the_inventory():
    assert lessons_mod.parse(doc(), Inv).lessons[0].title.startswith("qbittorrent")
    for bad in ({"lessons": []}, {"lessons": [LESSON] * 6}, {"lessons": [LESSON], "extra": 1}, {"lessons": [{**LESSON, "statement": "x" * 601}]},
                {"lessons": [{**LESSON, "kind": "constraint"}]}, {"lessons": [{**LESSON, "services": ["../etc"]}]}):
        with pytest.raises(lessons_mod.LessonError):
            lessons_mod.parse(bad)
    with pytest.raises(lessons_mod.LessonError, match="does not have"):
        lessons_mod.parse(doc({**LESSON, "stacks": ["ghost"]}), Inv)


def run_turn(tmp_path, content, **kw):
    rig = RuntimeRig(tmp_path, broker=Broker(tokens=1), **kw)
    handle, workspace, cs = rig.start()
    (workspace / "lessons.json").write_text(content if isinstance(content, str) else json.dumps(content))
    rig.finish(handle, cs)
    return rig, workspace


def candidates(rig):
    with rig.store._connect() as conn:
        return memory_store.list_cards(conn, "candidate")


def test_a_lesson_becomes_a_quarantined_candidate_and_the_operator_is_told(tmp_path):
    rig, _ = run_turn(tmp_path, doc())
    cards = candidates(rig)
    assert len(cards) == 1 and cards[0].status == "candidate" and cards[0].source == "inferred" and cards[0].scope == ["stack:media", "service:media/qbittorrent"]
    assert cards[0].evidence == [f"conversation:{rig.conv.id}"] and rig.console.lesson_ids(rig.conv.id) == [cards[0].id]
    assert any("noted 1 thing worth remembering" in m.text and "nothing is remembered until you keep it" in m.text
               for m in rig.console.messages(rig.conv.id) if m.role == "system")
    with rig.store._connect() as conn:
        assert retrieval.retrieve(conn, ["service:media/qbittorrent"], "gluetun").cards == []                 # quarantined: no session is shown it
        assert not [c for c in memory_store.list_cards(conn, "confirmed")]                      # nothing the agent writes is confirmed


def test_the_same_lesson_written_again_is_not_duplicated(tmp_path):
    rig, workspace = run_turn(tmp_path, doc())
    rig.console.add_message(rig.conv.id, "operator", "and again")
    handle, workspace2, cs = rig.start()
    (workspace2 / "lessons.json").write_text(json.dumps(doc()))
    rig.finish(handle, cs)
    assert len(candidates(rig)) == 1 and len(rig.console.lesson_ids(rig.conv.id)) == 1


def test_a_refused_file_a_secret_and_an_interrupted_turn_keep_nothing_and_never_echo_the_agents_text(tmp_path):
    rig, _ = run_turn(tmp_path, "{not json")
    assert candidates(rig) == [] and any("lessons were refused" in m.text for m in rig.console.messages(rig.conv.id))
    secret = {**LESSON, "statement": "the registry token is ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"}
    rig, _ = run_turn(tmp_path / "b", doc(secret))
    assert candidates(rig) == []
    said = " ".join(m.text for m in rig.console.messages(rig.conv.id))
    assert "not kept for review" in said and "ghp_" not in said


def test_at_most_twenty_lessons_a_conversation(tmp_path):
    rig = RuntimeRig(tmp_path, broker=Broker(tokens=1))
    for n in range(5):
        if n:
            rig.console.add_message(rig.conv.id, "operator", f"turn {n}")
        handle, workspace, cs = rig.start()
        (workspace / "lessons.json").write_text(json.dumps(doc(*({**LESSON, "statement": f"fact {n}-{i}"} for i in range(5)))))
        rig.finish(handle, cs)
    assert len(rig.console.lesson_ids(rig.conv.id)) == 20


@pytest.fixture
def rig(tmp_path):
    r = ConfirmRig(tmp_path)
    yield r
    r.close()


def offer(rig, conv_id=None, **over):
    lesson = lessons_mod.Lesson.model_validate({**LESSON, **over})
    card = lessons_mod.to_card(conv_id or rig.conv.id, lesson, __import__("datetime").datetime.now(__import__("datetime").UTC))
    with rig.st.store._write() as conn:
        memory_store.upsert_candidate(conn, card, lessons_mod.actor(conv_id or rig.conv.id))
    rig.console.add_lesson(conv_id or rig.conv.id, card.id)
    return card.id


def stored(rig, card_id):
    with rig.st.store._connect() as conn:
        return memory_store.get_card(conn, card_id)


def test_the_feed_lists_lessons_with_where_each_stands_and_keeping_confirms_as_the_operator(rig):
    card_id = offer(rig)
    shown = rig.api.feed(rig.conv.id)["lessons"]
    assert shown == [{"id": card_id, "title": LESSON["title"], "statement": LESSON["statement"], "scope": ["stack:media", "service:media/qbittorrent"], "state": "waiting"}]
    assert rig.api.lesson_decide(rig.conv.id, card_id, "keep", "chris") == {"decided": "keep"}
    card = stored(rig, card_id)
    assert card.status == "confirmed" and rig.api.feed(rig.conv.id)["lessons"][0]["state"] == "kept"
    with rig.st.store._connect() as conn:
        history = memory_store.history(conn, card_id)
    assert any(h.action == "confirm" and h.actor == "operator:chris" for h in history)
    with pytest.raises(ConsoleApiError) as err:
        rig.api.lesson_decide(rig.conv.id, card_id, "discard", "chris")
    assert err.value.code == "conflict"


def test_keeping_with_the_operators_own_words_stores_those_words_as_operator_stated(rig):
    card_id = offer(rig)
    rig.api.lesson_decide(rig.conv.id, card_id, "keep", "chris", statement="Start gluetun first; qbittorrent follows its network.")
    card = stored(rig, card_id)
    assert card.status == "confirmed" and card.statement == "Start gluetun first; qbittorrent follows its network." and card.source == "operator-stated"


def test_discarding_deprecates_and_an_edit_that_holds_a_secret_is_refused_and_leaves_the_candidate(rig):
    a, b = offer(rig), offer(rig, statement="a second, different fact")
    rig.api.lesson_decide(rig.conv.id, a, "discard", "chris")
    assert stored(rig, a).status == "deprecated" and rig.api.feed(rig.conv.id)["lessons"][0]["state"] == "discarded"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.lesson_decide(rig.conv.id, b, "keep", "chris", statement="token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8")
    assert err.value.code == "bad_request" and stored(rig, b).status == "candidate"
    with pytest.raises(ConsoleApiError) as err:
        rig.api.lesson_decide(rig.conv.id, b, "keep", "chris", statement="x" * 601)
    assert err.value.code == "bad_request"


def test_a_lesson_can_only_be_decided_in_the_conversation_that_offered_it_and_by_a_named_operator(rig):
    card_id = offer(rig)
    other = rig.service.create("chris", "another question")
    with pytest.raises(ConsoleApiError) as err:
        rig.api.lesson_decide(other.id, card_id, "keep", "chris")
    assert err.value.code == "not_found" and stored(rig, card_id).status == "candidate"
    for bad in ("", "has space", "x" * 65):
        with pytest.raises(ConsoleApiError) as err:
            rig.api.lesson_decide(rig.conv.id, card_id, "keep", bad)
        assert err.value.code == "forbidden"
    with pytest.raises(ConsoleApiError):
        rig.api.lesson_decide(rig.conv.id, card_id, "maybe", "chris")
    assert stored(rig, card_id).status == "candidate"


def test_a_candidate_is_never_retrieved_until_it_is_kept(rig):
    card_id = offer(rig)
    with rig.st.store._connect() as conn:
        assert retrieval.retrieve(conn, ["service:media/qbittorrent"], "gluetun").cards == []
    rig.api.lesson_decide(rig.conv.id, card_id, "keep", "chris")
    with rig.st.store._connect() as conn:
        found = retrieval.retrieve(conn, ["service:media/qbittorrent"], "gluetun")
    assert [c.id for c in found.cards] == [card_id]
