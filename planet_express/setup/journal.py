"""The setup journal: what `apply` did, durably, in an order a reader can trust.

Append-only JSON lines, one file per plan id, each event fsync'd before the next side effect. It is the
only state `apply` keeps: the browser reads it to rebuild its view after a reload, a crashed run is
resumed from it, and undo takes its evidence from it. The screen and the truth cannot disagree because
there is one record.

A crash can tear the last line. That line is discarded when the journal is next opened. A bad line
anywhere else means the file was tampered with or the disk is failing, and is an error, not a guess.

**No secret reaches this file.** Events hold secret names, never values, and every string is passed
through `redact` as a last defence.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MASK = "••••"


class JournalCorrupt(Exception):
    """The journal has an unreadable line that is not the torn final one."""


def make_redactor(secrets: dict[str, str]) -> Callable[[str], str]:
    """Replace every secret value (longest first, so a prefix cannot expose a longer one) with a mask."""
    values = sorted({v for v in secrets.values() if v and len(v) >= 4}, key=len, reverse=True)

    def redact(text: str) -> str:
        for value in values:
            text = text.replace(value, MASK)
        return text
    return redact


def _scrub(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _scrub(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, redact) for v in value]
    return value


@dataclass
class StepRecord:
    status: str = "pending"          # pending | started | ok | failed
    effect: str | None = None        # applied | not_applied | unknown
    reason: str | None = None
    evidence: list[dict] = field(default_factory=list)
    satisfied: bool = False


class Journal:
    def __init__(self, directory: str | Path, plan_id: str, *, redact: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time):
        self.directory = Path(directory) / plan_id
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "events.jsonl"
        self._redact = redact or (lambda text: text)
        self._clock = clock
        self._repair_torn_tail()
        existing = self.events()
        self._seq = existing[-1]["seq"] if existing else 0

    # -- reading ------------------------------------------------------------------------------------
    def events(self, after: int = 0) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        events = []
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    break                                   # torn final line: a crash mid-write
                raise JournalCorrupt(f"{self.path}: line {index + 1} is not valid JSON") from None
            events.append(event)
        return [e for e in events if e["seq"] > after]

    def steps(self) -> dict[str, StepRecord]:
        """The state of every step the journal has seen, folded from its events."""
        records: dict[str, StepRecord] = {}
        for event in self.events():
            step = event.get("step")
            if step is None:
                continue
            record = records.setdefault(step, StepRecord())
            kind = event["type"]
            if kind == "step_started":
                record.status = "started"
            elif kind == "evidence":
                record.evidence.append(event.get("data", {}))
            elif kind == "step_ok":
                record.status, record.effect = "ok", event.get("effect")
                record.satisfied = bool(event.get("satisfied"))
            elif kind == "step_failed":
                record.status, record.effect, record.reason = "failed", event.get("effect"), event.get("reason")
        return records

    # -- writing ------------------------------------------------------------------------------------
    def append(self, type_: str, **data) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "ts": round(self._clock(), 3), "type": type_, **_scrub(data, self._redact)}
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        created = not self.path.exists()
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return event

    def save_plan(self, public: dict) -> None:
        """Keep the plan that was approved beside its events, once. Written atomically, 0600."""
        target = self.directory / "plan.json"
        if target.exists():
            return
        temporary = self.directory / ".plan.json.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, json.dumps(public, ensure_ascii=False, indent=2).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)

    def _repair_torn_tail(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        keep = data.rfind(b"\n") + 1                        # drop the partial line, keep everything before it
        descriptor = os.open(self.path, os.O_WRONLY)
        try:
            os.ftruncate(descriptor, keep)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
