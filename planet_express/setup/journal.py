"""The setup journal: what `apply` did, durably, in an order a reader can trust.

Append-only JSON lines, one file per plan id, each event fsync'd before the next side effect. It is the
only state `apply` keeps: the browser reads it to rebuild its view after a reload, a crashed run is
resumed from it, and undo takes its evidence from it. The screen and the truth cannot disagree because
there is one record.

A crash can tear the last line. That line is discarded when the journal is next opened. A bad line
anywhere else means the file was tampered with or the disk is failing, and is an error, not a guess.

**The directory is trusted before it is used.** The journal runs as root, so it is created and opened
relative to a descriptor obtained after validating the whole path (no ancestor writable by group or other,
every one owned by a trusted uid), never following a symlink; the files inside are opened `O_NOFOLLOW` and
must be regular files owned by a trusted uid. A pre-planted directory or symlink cannot redirect a write.

**No secret reaches this file.** Events hold secret names, never values, and every string is passed
through `redact` as a last defence.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from planet_express.setup.host import HostError, RealHost

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


_PLAN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FLAGS = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class Journal:
    def __init__(self, directory: str | Path, plan_id: str, *, redact: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time, trusted_uids: frozenset[int] | set[int] = frozenset({0})):
        if not _PLAN_ID.fullmatch(plan_id):
            raise HostError(f"not a usable plan id: {plan_id!r}")
        host = RealHost(trusted_uids)
        self._trusted = host.trusted_uids
        root = str(Path(directory))
        os.close(host.open_private_dir(root, create=True))             # the root exists and is trustworthy
        self.directory = Path(root) / plan_id
        self._fd = host.open_private_dir(str(self.directory), create=True)
        self.path = self.directory / "events.jsonl"
        self._redact = redact or (lambda text: text)
        self._clock = clock
        self._repair_torn_tail()
        existing = self.events()
        self._seq = existing[-1]["seq"] if existing else 0

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    def __del__(self):
        try:
            self.close()
        except OSError:
            pass

    # -- files, always relative to the validated directory --------------------------------------------------
    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        descriptor = os.open(name, flags | _FLAGS, mode, dir_fd=self._fd)
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in self._trusted:
            os.close(descriptor)
            raise HostError(f"{self.directory}/{name} is not a regular file owned by a trusted account")
        return descriptor

    def _read(self, name: str) -> bytes | None:
        try:
            descriptor = self._open(name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HostError(f"cannot read {self.directory}/{name}: {exc.strerror}") from exc
        try:
            chunks = []
            while chunk := os.read(descriptor, 1 << 16):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    # -- reading ------------------------------------------------------------------------------------
    def events(self, after: int = 0) -> list[dict]:
        data = self._read("events.jsonl")
        if data is None:
            return []
        lines = data.decode("utf-8").split("\n")
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
        created = not self._exists("events.jsonl")
        descriptor = self._open("events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            os.fsync(self._fd)
        return event

    def _exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def save_plan(self, public: dict) -> None:
        """Keep the plan that was approved beside its events, once. Written atomically, 0600."""
        if self._exists("plan.json"):
            return
        temporary = ".plan.json.tmp"
        try:
            os.unlink(temporary, dir_fd=self._fd)
        except FileNotFoundError:
            pass
        descriptor = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(descriptor, json.dumps(public, ensure_ascii=False, indent=2).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, "plan.json", src_dir_fd=self._fd, dst_dir_fd=self._fd)
        os.fsync(self._fd)

    def _repair_torn_tail(self) -> None:
        data = self._read("events.jsonl")
        if not data or data.endswith(b"\n"):
            return
        keep = data.rfind(b"\n") + 1                        # drop the partial line, keep everything before it
        descriptor = self._open("events.jsonl", os.O_RDWR)
        try:
            os.ftruncate(descriptor, keep)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
