"""Standing-grant windows (the `window_json` semantics deferred from slice 1).

A window is `{"deny": [entry, ...], "allow": [entry, ...] | null}` and each entry is one of:

    HH:MM-HH:MM                a daily range, in UTC; a range whose end is before its start wraps past midnight
    DAY[,DAY...]@HH:MM-HH:MM   the same, only on the named days (mon tue wed thu fri sat sun)
    DAY[,DAY...]               the whole of the named days

Evaluation at an instant (`allowed(window, now)`), with `now` an injected UTC epoch:

    1. any `deny` entry that contains the instant        -> refused (deny wins)
    2. any `deny` entry that cannot be parsed             -> refused (a rule Core cannot read denies)
    3. `allow` is null                                    -> allowed
    4. `allow` is a list                                  -> allowed only inside one of its entries; an entry that cannot be
       parsed matches nothing, so an unreadable allow list (or an empty one) never opens a window

A bare name such as `backup` is not a time. Names are resolved to ranges when the standing grant is CREATED (from
`missions.windows`), so the stored window is always concrete; a name that survives to evaluation is an unparseable deny
entry and therefore denies. The start boundary is inclusive and the end boundary exclusive.
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NamedTuple

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RANGE = re.compile(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})")


class Entry(NamedTuple):
    days: frozenset[int]                       # weekday numbers, Monday = 0
    start: int | None                          # minutes from midnight; None means the whole day
    end: int | None


def parse_entry(text: str) -> Entry | None:
    """The entry, or None when it is not one of the three forms."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    day_part, sep, time_part = text.partition("@")
    if sep and (not day_part or not time_part):
        return None
    if not sep:
        day_part, time_part = (text, "") if text.split(",")[0] in DAYS else ("", text)
    days = frozenset(range(7))
    if day_part:
        names = day_part.split(",")
        if any(n not in DAYS for n in names):
            return None
        days = frozenset(DAYS.index(n) for n in names)
    if not time_part:
        return Entry(days, None, None) if day_part else None
    m = _RANGE.fullmatch(time_part)
    if m is None:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    return Entry(days, h1 * 60 + m1, h2 * 60 + m2)


def contains(entry: Entry, now: int) -> bool:
    at = datetime.fromtimestamp(int(now), UTC)
    minute, today = at.hour * 60 + at.minute, at.weekday()
    if entry.start is None:
        return today in entry.days
    start, end = entry.start, entry.end
    assert end is not None
    if start == end:
        return False                           # an empty range matches nothing
    if start < end:
        return today in entry.days and start <= minute < end
    # wraps midnight: the part before midnight belongs to today, the part after to the day that follows
    if minute >= start:
        return today in entry.days
    return ((today - 1) % 7) in entry.days and minute < end


def allowed(window: dict | None, now: int) -> tuple[bool, str]:
    """(ok, reason). `window` is the parsed `window_json` (or None for no window)."""
    if not window:
        return True, ""
    for raw in window.get("deny") or []:
        entry = parse_entry(raw)
        if entry is None:
            return False, f"window.deny entry {str(raw)[:40]!r} cannot be read, so it denies"
        if contains(entry, now):
            return False, f"inside the denied window {raw[:40]!r}"
    allow = window.get("allow")
    if allow is None:
        return True, ""
    for raw in allow:
        entry = parse_entry(raw)
        if entry is not None and contains(entry, now):
            return True, ""
    return False, "outside every allowed window"


def resolve_names(window: dict | None, named: dict[str, Sequence[str]]) -> dict | None:
    """Replace each bare name in a window by the ranges `named` gives it (at creation time). An unknown name is kept as it
    is, which `allowed` then treats as unreadable (deny): creation callers refuse a window that still holds one."""
    if not window:
        return window

    def expand(items):
        out: list[str] = []
        for item in items:
            out += list(named[item]) if item in named else [item]
        return out
    return {"deny": expand(window.get("deny") or []),
            "allow": None if window.get("allow") is None else expand(window["allow"])}


def unreadable(window: dict | None) -> list[str]:
    if not window:
        return []
    return [str(x) for x in [*(window.get("deny") or []), *(window.get("allow") or [])] if parse_entry(x) is None]


__all__ = ["DAYS", "allowed", "contains", "parse_entry", "resolve_names", "unreadable"]
