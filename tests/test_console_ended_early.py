"""rc14.4: a Console turn whose CLI exited without its `result` line (live M-26). It left a plan and streamed its reply: that is not a
crash. The turn is kept as ended early, the plan offered and the conversation left waiting for the operator. Why any session died is
kept as `session-tail.txt`. Finished conversations no longer count against `max_open`."""
import json

import pytest
from test_console_runtime import GOOD_PLAN
from test_console_service import Rig, _Inv

from planet_express.console.stream import TAIL_LINES, StreamParser
from planet_express.missions.state import transition

SID = "aaaaaaaa-1111-2222-3333-444444444444"
SECRET = "supersecretvalue123"


def _line(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def _delta(text) -> bytes:
    return _line({"type": "stream_event", "session_id": SID, "event": {"type": "content_block_delta", "index": 0,
                                                                         "delta": {"type": "text_delta", "text": text}}})


def m26_stream(*, final_words=True) -> bytes:
    """An early sentence, a tool call, then the final reply streamed as deltas, a line on stderr, and no result line."""
    out = _line({"type": "system", "subtype": "init", "session_id": SID})
    out += _line({"type": "assistant", "session_id": SID, "message": {"content": [{"type": "text", "text": "Let me look at the gateway."}]}})
    out += _line({"type": "assistant", "session_id": SID, "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"file_path": "plan.json"}}]}})
    out += _line({"type": "user", "session_id": SID, "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}})
    if final_words:
        out += _line({"type": "stream_event", "session_id": SID, "event": {"type": "message_start", "message": {}}})
        out += _delta("The gateway is fine. ") + _delta("qBittorrent lost its VPN route; ") + _delta("I wrote a plan to restart it.")
    out += f"Error: socket hang up API_KEY={SECRET}\n".encode()
    return out


def _texts(rig, conv, role):
    return [m.text for m in rig.console.messages(conv.id) if m.role == role]


def _artifacts(rig, mid, name):
    import sqlite3
    with sqlite3.connect(rig.store.path) as db:
        return [r[0] for r in db.execute("SELECT path FROM mission_artifacts WHERE mission_id=? AND name=?", (mid, name))]


# ── the stream parser ────────────────────────────────────────────────────────────────────────
def test_text_streamed_after_the_last_complete_message_is_the_reply_when_no_result_line_came():
    p = StreamParser()
    p.feed(m26_stream())
    res = p.finish()
    assert res.interrupted and res.from_deltas
    assert res.reply == "The gateway is fine. qBittorrent lost its VPN route; I wrote a plan to restart it."


def test_a_complete_message_supersedes_the_deltas_that_carried_it_and_a_new_message_starts_afresh():
    p = StreamParser()
    p.feed(_delta("half") + _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "whole sentence"}]}}))
    assert p.finish().reply == "whole sentence" and not p.result.from_deltas
    p = StreamParser()
    p.feed(_delta("an abandoned draft") + _line({"type": "stream_event", "event": {"type": "message_start"}}) + _delta("the new one"))
    assert p.finish().reply == "the new one"


def test_non_json_lines_are_kept_redacted_and_only_the_last_ones():
    p = StreamParser()
    p.feed(b"".join(f"line {i}\n".encode() for i in range(TAIL_LINES + 10)) + f"token API_KEY={SECRET}\n".encode()
           + _line({"type": "system", "session_id": SID}) + b"x" * 2000 + b"\n")
    tail = p.tail()
    assert len(tail) == TAIL_LINES and tail[0] == "line 12" and SECRET not in "\n".join(tail)
    assert tail[-1].endswith("[truncated]") and len(tail[-1]) < 600
    assert not any("system" in line for line in tail)                    # JSON lines are the stream, never the tail


# ── a turn end to end over the real supervisor ───────────────────────────────────────────────
@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_a_turn_that_exits_without_a_result_line_but_left_a_plan_is_ended_early_not_a_crash(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.runtime.inventory = lambda: _Inv
    run = next(iter(rig.runtime._runs.values()))
    (run.workspace / "plan.json").write_text(json.dumps(GOOD_PLAN))
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, m26_stream())
    rig.cont.crash(sid, exit_status=1)
    rig.sup.poll(conv.mission_id)
    assert rig.status(conv) == "awaiting_decision"
    assert _texts(rig, conv, "agent")[-1] == "The gateway is fine. qBittorrent lost its VPN route; I wrote a plan to restart it."
    system = _texts(rig, conv, "system")
    assert "The agent's session ended before it signed off; its plan and reply are shown." in system
    assert not any("stopped unexpectedly" in t or "stopped before it finished" in t for t in system)
    assert rig.console.current_plan(conv.id) is not None                  # the plan is offered
    tails = _artifacts(rig, conv.mission_id, "session-tail.txt")
    assert len(tails) == 1
    text = open(tails[0]).read()
    assert "exit status: 1" in text and "Error: socket hang up" in text and SECRET not in text
    notes = [json.loads(e.body_json) for e in rig.ms.events(conv.mission_id) if e.kind == "note" and "session_tail" in e.body_json]
    assert notes and notes[0]["exit_status"] == 1 and notes[0]["lines"] == 1
    rig.service.post(conv.id, "chris", "go on")                          # the conversation simply goes on
    rig.sup.tick()
    assert rig.status(conv) == "investigating"


def test_a_turn_that_ended_early_with_only_its_reply_says_so(rig):
    conv = rig.open()
    rig.sup.tick()
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, m26_stream())
    rig.cont.crash(sid)
    rig.sup.poll(conv.mission_id)
    assert rig.status(conv) == "awaiting_decision" and rig.console.current_plan(conv.id) is None
    assert "The agent's session ended before it signed off; its reply is shown." in _texts(rig, conv, "system")
    assert "exit status: unknown" in open(_artifacts(rig, conv.mission_id, "session-tail.txt")[0]).read()


def test_a_refused_plan_from_a_turn_that_ended_early_still_goes_back_to_the_planner(rig):
    from test_console_service import BAD_PLAN

    from planet_express.missions.state import CONSOLE_PLAN_RETRY
    conv = rig.open()
    rig.sup.tick()
    rig.runtime.inventory = lambda: _Inv
    run = next(iter(rig.runtime._runs.values()))
    (run.workspace / "plan.json").write_text(json.dumps(BAD_PLAN))
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, m26_stream(final_words=False))
    rig.cont.crash(sid)
    rig.sup.poll(conv.mission_id)
    assert rig.status(conv) == "awaiting_decision"
    system = _texts(rig, conv, "system")
    assert any(t.startswith("PE refused the plan") for t in system)
    assert "The agent's session ended before it signed off; its reply is shown." in system      # the early sentence, no plan
    rig.sup.tick()
    moves = [e.actor for e in rig.ms.events(conv.mission_id) if e.kind == "state" and '"to": "investigating"' in e.body_json]
    assert moves[-1] == CONSOLE_PLAN_RETRY


def test_a_turn_that_left_nothing_usable_is_still_a_crash_and_the_crash_note_points_at_the_tail(rig):
    conv = rig.open()
    rig.sup.tick()
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, _line({"type": "system", "subtype": "init", "session_id": SID}) + b"FATAL: out of file descriptors\n")
    rig.cont.crash(sid, exit_status=-9)
    rig.sup.poll(conv.mission_id)
    assert rig.status(conv) == "needs_operator"
    assert any("stopped unexpectedly" in t and "session-tail.txt" in t for t in _texts(rig, conv, "system"))
    text = open(_artifacts(rig, conv.mission_id, "session-tail.txt")[0]).read()
    assert "killed by signal 9" in text and "FATAL: out of file descriptors" in text and "(crash)" in text
    assert rig.sup.last_session_tail == {}                                # taken by the Console's note


def test_an_interrupted_turn_is_never_ended_early_and_keeps_no_tail(rig):
    conv = rig.open()
    rig.sup.tick()
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, m26_stream())
    assert rig.service.interrupt(conv.id, "chris") is True
    system = _texts(rig, conv, "system")
    assert "This turn was stopped before it finished." in system and not any("signed off" in t for t in system)
    assert _artifacts(rig, conv.mission_id, "session-tail.txt") == []


# ── max_open counts only conversations that can still take a turn ────────────────────────────
def test_conversations_whose_mission_has_finished_do_not_count_against_max_open(tmp_path):
    from planet_express.console.service import ConsoleFull
    rig = Rig(tmp_path, max_open=2)
    one, _two = rig.open("a"), rig.open("b")
    with pytest.raises(ConsoleFull, match="2 conversations are open"):
        rig.open("c")
    transition(rig.store, one.mission_id, "cancelled", "operator:chris")         # closed from the Work log
    assert rig.console.count_open() == 1 and rig.console.get(one.id).state == "open"
    three = rig.open("c")                                                          # a place was freed
    assert rig.console.get(three.id).state == "open" and rig.console.count_open() == 2
    with pytest.raises(ConsoleFull):
        rig.open("d")


# ── review fixes ─────────────────────────────────────────────────────────────────────────────
def test_a_complete_answer_followed_by_a_cut_off_fragment_keeps_the_answer():
    p = StreamParser()
    p.feed(_line({"type": "assistant", "message": {"content": [{"type": "text", "text": "The full answer."}]}})
           + _line({"type": "stream_event", "event": {"type": "message_start"}}) + _delta("I'll al"))
    assert p.finish().reply == "The full answer.\n\nI'll al"


def test_a_sentence_before_a_tool_call_that_never_came_back_is_not_a_closing_reply_so_the_turn_is_a_crash(rig):
    conv = rig.open()
    rig.sup.tick()
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, _line({"type": "assistant", "session_id": SID, "message": {"content": [
        {"type": "text", "text": "Let me check the logs."}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}})
        + b"panic: stack overflow\n")
    rig.cont.crash(sid, exit_status=1)
    rig.sup.poll(conv.mission_id)
    assert rig.status(conv) == "needs_operator"
    assert any("stopped unexpectedly" in t for t in _texts(rig, conv, "system"))


def test_a_turn_killed_by_a_signal_is_a_crash_even_with_a_plan_and_a_reply(rig):
    conv = rig.open()
    rig.sup.tick()
    rig.runtime.inventory = lambda: _Inv
    run = next(iter(rig.runtime._runs.values()))
    (run.workspace / "plan.json").write_text(json.dumps(GOOD_PLAN))
    sid = rig.live_session(conv)
    rig.cont.write_output(sid, m26_stream())
    rig.cont.crash(sid, exit_status=-9)                                    # the OOM killer, say
    rig.sup.poll(conv.mission_id)
    assert rig.status(conv) == "needs_operator" and rig.console.current_plan(conv.id) is None
    assert "killed by signal 9" in open(_artifacts(rig, conv.mission_id, "session-tail.txt")[0]).read()
