"""Redaction of a container's log stream on its way to a session.

A session's model is a vendor, so nothing a log carries may reach it unredacted (the egress rule, v4 design). Docker sends a log as
multiplexed frames (an 8-byte header: stream, three zero bytes, a big-endian size, then that many bytes) unless the container has a
TTY, when it is plain text. A secret can be split across frames, so frames are not redacted one by one: each stream's bytes are
reassembled into logical lines, a line is redacted whole, and only complete lines go on, re-framed. A partial line waits for its end
(or for `finish` when the daemon closes). A line or frame that is too long ends the stream: it is never passed through unredacted
to make it fit."""
from __future__ import annotations

from planet_express.core.redact import redact

MAX_FRAME = 1 << 20
MAX_LINE = 1 << 16


def _clean(raw: bytes) -> bytes:
    return redact(raw.decode("utf-8", "replace")).encode("utf-8")


class LogRedactor:
    """Stateful: `feed` takes the daemon's bytes as they arrive and returns what may be sent on; `finish` returns the rest at the end."""

    def __init__(self) -> None:
        self._buf = b""
        self._mode: str | None = None
        self._lines: dict[int, bytes] = {}           # mux: each stream's incomplete line
        self._text = b""                             # text: the incomplete line

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        if self._mode is None:
            if len(self._buf) < 8:
                return b""
            self._mode = "mux" if self._buf[0] in (0, 1, 2) and self._buf[1:4] == b"\0\0\0" else "text"
        return self._mux() if self._mode == "mux" else self._plain()

    def finish(self) -> bytes:
        """The daemon closed: whatever is held is a final line without a newline. Redacted like any other, then sent."""
        if self._mode is None:
            self._mode = "text"
        if self._mode == "text":
            rest, self._text = self._text + self._buf, b""
            self._buf = b""
            return _clean(rest) if rest else b""
        out = [self._frame(stream, _clean(line)) for stream, line in sorted(self._lines.items()) if line]
        self._lines.clear()
        return b"".join(out)

    def fit(self, out: bytes, room: int) -> bytes:
        """The longest prefix of `out` (what `feed` or `finish` returned) that fits in `room` and ends on a whole frame or line."""
        if len(out) <= room:
            return out
        if self._mode == "mux":
            end = 0
            while end + 8 <= len(out):
                size = int.from_bytes(out[end + 4:end + 8], "big")
                if end + 8 + size > room:
                    break
                end += 8 + size
            return out[:end]
        cut = out.rfind(b"\n", 0, room)
        return out[:cut + 1] if cut >= 0 else b""

    @staticmethod
    def _frame(stream: int, body: bytes) -> bytes:
        return bytes([stream, 0, 0, 0]) + len(body).to_bytes(4, "big") + body

    def _mux(self) -> bytes:
        out = []
        while len(self._buf) >= 8:
            stream, size = self._buf[0], int.from_bytes(self._buf[4:8], "big")
            if stream not in (0, 1, 2) or self._buf[1:4] != b"\0\0\0" or size > MAX_FRAME:
                raise OSError("a log frame is malformed or too large")
            if len(self._buf) < 8 + size:
                break
            payload, self._buf = self._buf[8:8 + size], self._buf[8 + size:]
            held = self._lines.get(stream, b"") + payload
            if b"\n" in held:
                head, _, held = held.rpartition(b"\n")
                out.append(self._frame(stream, _clean(head + b"\n")))
            if len(held) > MAX_LINE:
                raise OSError("a log line is too long")
            self._lines[stream] = held
        return b"".join(out)

    def _plain(self) -> bytes:
        self._text += self._buf
        self._buf = b""
        if b"\n" not in self._text:
            if len(self._text) > MAX_LINE:
                raise OSError("a log line is too long")
            return b""
        head, _, self._text = self._text.rpartition(b"\n")
        if len(self._text) > MAX_LINE:
            raise OSError("a log line is too long")
        return _clean(head + b"\n")
