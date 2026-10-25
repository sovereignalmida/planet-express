"""The stream-json parser: real CLI output in, redacted bounded events and one turn result out, whatever the bytes."""
import json
from pathlib import Path

import pytest

from planet_express.console.stream import (
    MAX_EVENTS_PER_TURN,
    MAX_EXCERPT,
    MAX_REPLY,
    StreamParser,
)

FIXTURE = Path(__file__).parent / "fixtures" / "console_stream_tool_turn.jsonl"
SECRET = "sk-or-v1-" + "a" * 48


def parse(data: bytes):
    parser = StreamParser()
    events = parser.feed(data, final=True)
    return events, parser.finish()


def test_a_real_turn_with_a_tool_call_becomes_a_trail_and_a_result():
    events, result = parse(FIXTURE.read_bytes())
    tools = [e for e in events if e.kind == "tool"]
    assert [t.body.get("call") for t in tools if "call" in t.body] == ["Bash"]
    call = next(t.body for t in tools if "call" in t.body)
    assert call["args"] == {"command": "echo hi", "description": "t"} and call["read_only"] is False
    seen = next(t.body for t in tools if "result_of" in t.body)
    assert seen["result_of"] == "Bash" and seen["excerpt"] == "hi" and seen["is_error"] is False
    assert result.ok and not result.interrupted and result.subtype == "success"
    assert result.reply == "all done here" and result.tool_calls == 1 and result.changed_calls == 0
    assert result.session_id == "2dc11a5c-9cef-4ce6-98ca-232a0476de7a" and result.tokens == 30


def test_the_text_deltas_become_one_note_per_feed():
    events, _ = parse(FIXTURE.read_bytes())
    notes = [e for e in events if e.kind == "note"]
    assert len(notes) == 1 and "all done here" in notes[0].body["delta"]


def test_feeding_one_byte_at_a_time_gives_the_same_events_and_result_as_feeding_it_whole():
    whole_events, whole = parse(FIXTURE.read_bytes())
    parser = StreamParser()
    events = []
    for byte in FIXTURE.read_bytes():
        events += parser.feed(bytes([byte]))
    result = parser.finish()
    strip = lambda evs: [e for e in evs if e.kind == "tool"]
    assert strip(events) == strip(whole_events)
    assert (result.reply, result.session_id, result.ok, result.tokens) == (whole.reply, whole.session_id, whole.ok, whole.tokens)


def test_a_planted_secret_never_leaves_the_parser():
    line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": f"KEY={SECRET} done"}]}})
    call = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                                      "input": {"command": f"curl -H 'Authorization: Bearer {SECRET}' x"}}]}})
    final = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": f"the key is OPENROUTER_API_KEY={SECRET}"})
    events, result = parse("\n".join((call, line, final)).encode())
    blob = json.dumps([e.body for e in events]) + result.reply
    assert SECRET not in blob


def test_a_ten_megabyte_tool_result_is_an_excerpt_not_stored_whole():
    huge = "x" * (10 * 1024 * 1024)
    line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": huge}]}})
    events, result = parse(line.encode())
    assert events == [] and result.skipped_lines == 1 and result.truncated        # over the line cap: skipped whole, marked
    medium = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "y" * 100_000}]}})
    events, result = parse(medium.encode())
    body = events[0].body
    assert len(body["excerpt"]) <= MAX_EXCERPT + len(" [truncated]") and body["clipped"] is True and body["bytes"] == 100_000


@pytest.mark.parametrize("junk", [b"not json at all\n", b"{\"type\": \n", b"[1,2,3]\n", b"\xff\xfe\x00garbage\n", b"{}\n", b"\n\n",
                                  b"{\"type\":\"assistant\",\"message\":5}\n", b"{\"type\":\"user\",\"message\":{\"content\":\"text\"}}\n",
                                  b"{\"type\":\"stream_event\",\"event\":null}\n", b"{\"type\":\"result\",\"usage\":\"x\"}\n"])
def test_a_malformed_or_odd_line_is_skipped_or_ignored_never_raised(junk):
    parser = StreamParser()
    parser.feed(junk + FIXTURE.read_bytes(), final=True)                  # and the real stream after it still parses
    assert parser.finish().session_id == "2dc11a5c-9cef-4ce6-98ca-232a0476de7a"


def test_a_truncated_last_line_is_skipped_unless_complete_at_the_end():
    data = FIXTURE.read_bytes()
    cut = data[: data.rindex(b'"result"') + 10]                             # the final result line is cut mid-way
    _, result = parse(cut)
    assert not result.ok and result.skipped_lines >= 1 and result.reply == "all done here"      # falls back to the last assistant text


def test_the_session_id_is_the_last_valid_one_and_a_bad_one_is_ignored():
    lines = [{"type": "system", "subtype": "init", "session_id": "aaaaaaaa-1111"}, {"type": "assistant", "session_id": "../etc/passwd"},
             {"type": "result", "subtype": "success", "session_id": "bbbbbbbb-2222", "result": "ok", "usage": {}}]
    _, result = parse("\n".join(json.dumps(x) for x in lines).encode())
    assert result.session_id == "bbbbbbbb-2222"


def test_an_error_result_is_an_interrupted_turn_not_a_failure_to_parse():
    final = {"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "cccccccc-3333", "usage": {"input_tokens": 5}}
    _, result = parse(json.dumps(final).encode())
    assert result.interrupted and not result.ok and result.subtype == "error_during_execution" and result.tokens == 5


def test_a_file_writing_tool_is_marked_in_the_trail_and_counted():
    block = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w", "name": "Write",
                                                           "input": {"file_path": "/work/plan.json", "content": "{...}"}},
                                                          {"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/work/a"}}]}}
    events, result = parse(json.dumps(block).encode())
    write, read = (e.body for e in events)
    assert write["changes_files"] is True and write["args"] == {"file_path": "/work/plan.json"} and "content" not in json.dumps(write)
    assert read["read_only"] is True and "changes_files" not in read and result.changed_calls == 1


def test_the_reply_and_the_event_count_are_bounded():
    big = {"type": "result", "subtype": "success", "result": "z" * (MAX_REPLY * 3), "usage": {}}
    _, result = parse(json.dumps(big).encode())
    assert len(result.reply) <= MAX_REPLY + len(" [truncated]")
    many = "\n".join(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": str(i), "name": "Read",
                                                                               "input": {"file_path": "/a"}}]}}) for i in range(MAX_EVENTS_PER_TURN + 50))
    events, result = parse(many.encode())
    assert len(events) == MAX_EVENTS_PER_TURN and result.events_dropped == 50 and result.tool_calls == MAX_EVENTS_PER_TURN + 50


def test_an_overlong_unterminated_line_is_dropped_to_its_newline_and_the_stream_recovers():
    parser = StreamParser()
    parser.feed(b"{" + b"a" * (300 * 1024))                                  # no newline yet, already over the cap
    parser.feed(b"b" * 100 + b"\n")                                         # the rest of that line
    parser.feed(FIXTURE.read_bytes(), final=True)
    result = parser.finish()
    assert result.truncated and result.skipped_lines == 1 and result.session_id == "2dc11a5c-9cef-4ce6-98ca-232a0476de7a"
