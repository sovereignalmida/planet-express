"""A container's log never reaches a session unredacted: framed (no TTY) and plain (TTY) streams, however the daemon's bytes are split."""
import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)
import pytest

from planet_express.missions.dockerproxy.logredact import MAX_FRAME, MAX_LINE, LogRedactor

SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef0123456789abcdef"


def frame(stream: int, text: str) -> bytes:
    body = text.encode()
    return bytes([stream, 0, 0, 0]) + len(body).to_bytes(4, "big") + body


def unframe(data: bytes) -> list[tuple[int, str]]:
    out = []
    while data:
        size = int.from_bytes(data[4:8], "big")
        out.append((data[0], data[8:8 + size].decode()))
        data = data[8 + size:]
    return out


def test_framed_logs_are_redacted_and_keep_their_stream_and_a_correct_size():
    r = LogRedactor()
    raw = frame(1, f"starting with api_key={SECRET}\n") + frame(2, "error: Authorization: Bearer abcdef0123456789abcdef\n") + frame(1, "all fine\n")
    frames = unframe(r.feed(raw))
    assert [s for s, _ in frames] == [1, 2, 1]
    joined = "".join(t for _, t in frames)
    assert SECRET not in joined and "abcdef0123456789abcdef" not in joined and "all fine" in joined


def test_frames_split_across_reads_come_out_whole_and_nothing_is_sent_early():
    raw = frame(1, f"password={SECRET}\n") + frame(1, "next line\n")
    r, out = LogRedactor(), b""
    for i in range(len(raw)):
        out += r.feed(raw[i:i + 1])                              # one byte at a time, the worst case
    assert [t for _, t in unframe(out)][1] == "next line\n" and SECRET not in out.decode()
    assert LogRedactor().feed(raw[:7]) == b""                    # not even a header yet: nothing goes out


def test_a_tty_log_is_plain_text_redacted_a_line_at_a_time_and_a_partial_line_waits():
    r = LogRedactor()
    out = r.feed(f"token={SECRET}\nplain line\nhalf".encode())
    assert SECRET not in out.decode() and b"plain line\n" in out and b"half" not in out
    assert r.feed(b" done\n") == b"half done\n"


@pytest.mark.parametrize("bad", [bytes([7, 0, 0, 0]) + (5).to_bytes(4, "big") + b"hello", bytes([1, 0, 0, 0]) + (MAX_FRAME + 1).to_bytes(4, "big")])
def test_a_malformed_or_oversized_frame_ends_the_stream_instead_of_passing_through(bad):
    r = LogRedactor()
    r.feed(frame(1, "ok\n"))                                      # fixes the mode as framed
    with pytest.raises(OSError):
        r.feed(bad)


def test_a_text_line_that_never_ends_is_refused_not_passed_on():
    r = LogRedactor()
    with pytest.raises(OSError):
        r.feed(b"x" * (MAX_LINE + 10))


def test_a_secret_split_across_frames_is_redacted_as_one_line():
    r = LogRedactor()
    out = r.feed(frame(1, "Authorization: Bear") + frame(1, f"er {SECRET[:20]}") + frame(1, f"{SECRET[20:]}\nnext\n"))
    text = "".join(t for _, t in unframe(out))
    assert SECRET not in text and SECRET[:20] not in text and "next" in text


def test_streams_do_not_mix_their_partial_lines():
    r = LogRedactor()
    out = r.feed(frame(1, "out-a ") + frame(2, "err-a ") + frame(1, "out-b\n") + frame(2, "err-b\n"))
    assert unframe(out) == [(1, "out-a out-b\n"), (2, "err-a err-b\n")]


def test_finish_sends_the_held_final_line_redacted_and_a_tiny_text_log_is_not_lost():
    r = LogRedactor()
    assert r.feed(f"last words password={SECRET}".encode()) == b""
    tail = r.finish()
    assert SECRET.encode() not in tail and b"last words" in tail
    short = LogRedactor()
    assert short.feed(b"ok\n") == b"" and short.finish() == b"ok\n"
    framed = LogRedactor()
    framed.feed(frame(1, f"unterminated key={SECRET}"))
    assert [s for s, _ in unframe(framed.finish())] == [1] and SECRET not in framed.finish().decode()


def test_fit_cuts_at_a_whole_frame_or_line_and_never_inside_one():
    framed = LogRedactor()
    out = framed.feed(frame(1, "aaaa\n") + frame(2, "bbbb\n"))
    assert unframe(framed.fit(out, 13)) == [(1, "aaaa\n")] and framed.fit(out, 7) == b"" and framed.fit(out, len(out)) == out
    text = LogRedactor()
    out = text.feed(b"one\ntwo\nthree\n")
    assert text.fit(out, 9) == b"one\ntwo\n" and text.fit(out, 2) == b""
