"""The agent CLI's `--output-format stream-json` stream -> PE events and one turn result.

Everything on this stream is untrusted agent output. This module is pure (bytes in, events and a result out): it never
touches the host, and nothing it returns is evidence of anything. Its rules, each tested:

  * every string that leaves it is redacted, and bounded (a 10 MB tool result becomes a short, marked excerpt);
  * a malformed, truncated, over-long or unknown line is skipped and counted, never raised;
  * partial reads are fine: feeding the stream one byte at a time gives the same events as feeding it whole;
  * the session id the stream reports is validated, and it is the LAST one seen (an interrupt changes it);
  * a tool's name and a short summary of its arguments are kept for the trail, not its full input.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from planet_express.core.redact import redact

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
MAX_LINE_BYTES = 256 * 1024          # a longer line is skipped whole (a tool result of that size is excerpted by the CLI's own cap)
MAX_EXCERPT = 2000                   # characters of a tool result kept for the trail
MAX_ARG = 300                        # characters of one tool argument kept for the trail
MAX_DELTA = 4000
MAX_REPLY = 32_000
MAX_EVENTS_PER_TURN = 2000
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead", "TodoRead"})
CHANGING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
# Which arguments of a tool call are worth showing. Anything else is dropped, not stored.
SHOWN_ARGS = ("command", "file_path", "path", "pattern", "description", "url", "query", "glob")


@dataclass(frozen=True)
class TurnEvent:
    """One thing for the activity trail. Always a `note` (a kind a runtime may write): the supervisor counts `tool` events against the
    grant's tool budget, and the installed hooks already post one per call, so a stream `tool` event would count every call twice. The
    trail tells a call from a result from text by the body (`call`, `result_of`, `delta`)."""
    kind: str
    body: dict[str, Any]


@dataclass
class TurnResult:
    reply: str = ""                    # the agent's final text for this turn (redacted, bounded)
    session_id: str | None = None      # the id the stream last reported
    ok: bool = False                   # the stream ended with a result that says success
    interrupted: bool = False          # the stream ended with an error result (an interrupt, a limit): partial work, not failure of PE
    subtype: str | None = None
    tokens: int = 0                    # what the stream says the turn used (informational: the broker's count is the budget)
    tool_calls: int = 0
    changed_calls: int = 0             # calls of a tool that writes files (Write, Edit): the activity trail marks them
    skipped_lines: int = 0
    truncated: bool = False
    events_dropped: int = 0


def _clip(text: str, limit: int) -> str:
    """Redacted, then cut. Only the head is redacted (the rest is thrown away, and redacting a megabyte is slow), with room past the cut
    so a secret that straddles it is still matched whole before the cut."""
    head = redact(text[:limit + 512])
    return head if len(head) <= limit and len(text) <= limit + 512 else head[:limit] + " [truncated]"


def _flatten(content: Any) -> str:
    """A tool result's content is a string or a list of blocks; take its text and nothing else."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _summary(name: str, tool_input: Any) -> dict[str, str]:
    if not isinstance(tool_input, dict):
        return {}
    return {k: _clip(str(v), MAX_ARG) for k, v in tool_input.items() if k in SHOWN_ARGS and isinstance(v, (str, int))}


@dataclass
class StreamParser:
    result: TurnResult = field(default_factory=TurnResult)
    _partial: bytes = b""
    _skipping: bool = False
    _last_text: str = ""
    _events: int = 0
    _tool_names: dict[str, str] = field(default_factory=dict)

    def feed(self, data: bytes, *, final: bool = False) -> list[TurnEvent]:
        """Take more bytes; return the events for the complete lines in them. `final` also takes an unterminated last line."""
        events: list[TurnEvent] = []
        deltas: list[str] = []
        *lines, buf = (self._partial + data).split(b"\n")           # one split: the last piece is a line not yet finished
        self._partial = b""
        for line in lines:
            if self._skipping:
                self._skipping = False
                continue
            self._line(line, events, deltas)
        if self._skipping:
            buf = b""
        elif len(buf) > MAX_LINE_BYTES:
            self.result.skipped_lines += 1
            self.result.truncated = True
            self._skipping, buf = True, b""
        if final and buf and not self._skipping:
            self._line(buf, events, deltas)
            buf = b""
        self._partial = buf
        if deltas:
            events.insert(0, TurnEvent("note", {"delta": _clip("".join(deltas), MAX_DELTA)}))     # one note per feed, not one per token
        return self._bounded(events)

    def finish(self) -> TurnResult:
        """The turn's result. Call after the process is provably gone and the stream has been fed to its end."""
        self.feed(b"", final=True)
        res = self.result
        if res.subtype is None:
            res.interrupted = True              # the stream ended with no result line: stopped part-way, what it said is still kept
        if not res.reply:
            res.reply = _clip(self._last_text, MAX_REPLY)
        return res

    # ── internals ─────────────────────────────────────────────────────────────────────────
    def _bounded(self, events: list[TurnEvent]) -> list[TurnEvent]:
        room = MAX_EVENTS_PER_TURN - self._events
        if len(events) > room:
            self.result.events_dropped += len(events) - max(room, 0)
            events = events[:max(room, 0)]
        self._events += len(events)
        return events

    def _line(self, raw: bytes, events: list[TurnEvent], deltas: list[str]) -> None:
        if not raw.strip():
            return
        if len(raw) > MAX_LINE_BYTES:
            self.result.skipped_lines += 1
            self.result.truncated = True
            return
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            self.result.skipped_lines += 1
            return
        if not isinstance(obj, dict):
            self.result.skipped_lines += 1
            return
        kind = obj.get("type")
        sid = obj.get("session_id")
        if isinstance(sid, str) and SESSION_ID.fullmatch(sid):
            self.result.session_id = sid
        if kind == "stream_event":
            event = obj.get("event")
            delta = event.get("delta") if isinstance(event, dict) else None
            if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                deltas.append(delta["text"])
        elif kind == "assistant":
            self._assistant(obj.get("message"), events)
        elif kind == "user":
            self._tool_results(obj.get("message"), events)
        elif kind == "result":
            self._final(obj)

    def _assistant(self, message: Any, events: list[TurnEvent]) -> None:
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                name = _clip(block["name"], 64)
                if isinstance(block.get("id"), str):
                    self._tool_names[block["id"][:128]] = name
                self.result.tool_calls += 1
                changes = block["name"] in CHANGING_TOOLS
                self.result.changed_calls += int(changes)
                events.append(TurnEvent("note", {"call": name, "args": _summary(block["name"], block.get("input")),
                                                 "read_only": block["name"] in READ_ONLY_TOOLS,
                                                 **({"changes_files": True} if changes else {})}))
        if texts:
            self._last_text = "\n".join(texts)

    def _tool_results(self, message: Any, events: list[TurnEvent]) -> None:
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _flatten(block.get("content"))
                clipped = len(text) > MAX_EXCERPT
                tool_id = block.get("tool_use_id")
                events.append(TurnEvent("note", {                    # a note, not a tool event: the supervisor counts tool events as calls
                    "result_of": self._tool_names.get(tool_id[:128] if isinstance(tool_id, str) else "", "?"),
                    "excerpt": _clip(text[:MAX_EXCERPT + 1], MAX_EXCERPT), "bytes": len(text),
                    "is_error": bool(block.get("is_error")), **({"clipped": True} if clipped else {})}))

    def _final(self, obj: dict) -> None:
        res = self.result
        res.subtype = obj.get("subtype") if isinstance(obj.get("subtype"), str) else None
        failed = bool(obj.get("is_error")) or res.subtype != "success"
        res.ok = not failed
        res.interrupted = failed
        text = obj.get("result")
        if isinstance(text, str):
            res.reply = _clip(text, MAX_REPLY)
        usage = obj.get("usage")
        if isinstance(usage, dict):
            res.tokens = sum(v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")
                             and isinstance(v, int) and not isinstance(v, bool) and v > 0)


__all__ = ["MAX_EXCERPT", "READ_ONLY_TOOLS", "StreamParser", "TurnEvent", "TurnResult"]
