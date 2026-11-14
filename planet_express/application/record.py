"""The record (4.1, docs/designs/missions-memory.md §4): everything PE changed or looked into, one row per piece of work, newest
first, whatever started it. A view over the tables that exist; no schema change.

Rows:
- a Console conversation, its runs nested under it (a run is a Repair mission started from the conversation's plan);
- a mission no conversation owns (a pre-Console mission, a Work-log repair);
- a v3 approval: a runbook or direct action approved, denied or expired, with its execution (canary updates and safe prunes
  are automatic approvals), a canary update carrying its line from Zoidberg's update history;
- an update-history entry no approval accounts for (written before approvals recorded canary updates).

Read-only. Every value that reaches the page is bounded here; the page escapes it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Iterable

OUTCOMES = ("open", "held", "completed", "rolled_back", "failed", "cancelled", "declined")
NATURES = ("question", "change")
ORIGINS = ("console", "telegram", "dashboard", "incident", "scan", "automatic", "other")

# How long after an approval's run a Zoidberg history line may land and still be that run's (a canary watches for minutes).
_HISTORY_WINDOW_S = 1800
_TEXT = 300


def _text(value: Any, limit: int = _TEXT) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def mission_outcome(status: str) -> str:
    if status in ("completed", "rolled_back", "failed", "cancelled"):
        return status
    if status in ("needs_operator", "recovery_required", "paused_tripwire", "blocked", "rollback_pending"):
        return "held"
    return "open"


def execution_outcome(status: str | None, rolled_back: bool) -> str:
    if rolled_back or status == "rolled_back":
        return "rolled_back"
    return {"passed": "completed", "failed": "failed", "rollback_failed": "failed", "interrupted": "failed",
            "aborted": "cancelled"}.get(status or "", "open")


def approval_origin(origin: str | None, via: str | None) -> str:
    o = (origin or via or "").lower()
    if o in ("zoidberg", "system"):
        return "automatic"
    if o.startswith("telegram"):
        return "telegram"
    if o.startswith("dashboard"):
        return "dashboard"
    if o == "incident":
        return "incident"
    if o in ("planner", "amy"):
        return "scan"
    return "other"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _spend(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict]:
    """Per mission: model calls and the vendor's price where the reply carried one (`MissionStore.model_spend`, grouped)."""
    ids = sorted(set(ids))
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT mission_id, COUNT(*) AS calls, SUM(json_extract(body_json, '$.cost_usd')) AS cost, "
                "COUNT(json_extract(body_json, '$.cost_usd')) AS priced "
                f"FROM mission_events WHERE mission_id IN ({marks}) AND kind='tool' "
                "AND json_extract(body_json, '$.event')='model.request' GROUP BY mission_id", chunk):
            out[r[0]] = {"calls": int(r[1]), "cost": float(r[2]) if r[3] else 0.0, "priced": int(r[3])}
    return out


def _add_spend(row: dict, spend: dict[str, dict], ids: Iterable[str]) -> None:
    calls = priced = 0
    cost = 0.0
    for mid in ids:
        s = spend.get(mid)
        if s:
            calls += s["calls"]
            priced += s["priced"]
            cost += s["cost"]
    row["calls"] = calls
    row["cost_usd"] = round(cost, 4) if priced else None
    row["unpriced"] = calls - priced                            # calls without a price: the cost shown is only part of it


def _mission_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row                             # a caller's own connection may not set it
    missions = {r["id"]: r for r in conn.execute(
        "SELECT id, title, status, tier, origin, runtime, created_at, updated_at, closed_at, outcome FROM missions")}
    convs = list(conn.execute("SELECT id, mission_id, title, origin, state, created_at, last_activity_at FROM console_conversations"))
    runs: dict[str, list] = {}
    for r in conn.execute("SELECT conversation_id, mission_id, status, created_at FROM console_executions ORDER BY id"):
        runs.setdefault(r["conversation_id"], []).append(r)
    spend = _spend(conn, missions)
    owned: set[str] = set()
    rows: list[dict] = []
    for c in convs:
        own = missions.get(c["mission_id"])
        if own is None:
            continue
        owned.add(own["id"])
        nested = []
        for e in runs.get(c["id"], []):
            m = missions.get(e["mission_id"])
            if m is None:
                continue
            owned.add(m["id"])
            if e["status"] not in ("authorised", "ended"):
                continue                                       # proposed or withdrawn: nothing was run
            nested.append({"id": m["id"], "title": _text(m["title"]), "outcome": mission_outcome(m["status"]),
                           "status": m["status"], "at": m["updated_at"], "href": f"/missions/{m['id']}"})
        at = max([own["updated_at"] or 0, c["last_activity_at"] or 0] + [n["at"] or 0 for n in nested])
        # A later turn that did not finish (the conversation's own mission held) names it, as the Console rail does; otherwise
        # the latest run's outcome is the conversation's, and without runs its own (answered and still open, or closed).
        # A turn running now does too (Working in the Console). An answered conversation (awaiting_decision) keeps its run's.
        own_outcome = mission_outcome(own["status"])
        busy = own_outcome == "held" or own["status"] in ("investigating", "queued", "draft")
        outcome = own_outcome if busy or not nested else nested[-1]["outcome"]
        row = {"kind": "conversation", "id": c["id"], "title": _text(c["title"]), "at": at, "started_at": c["created_at"],
               "nature": "change" if nested else "question",
               # A conversation records only "operator" or "incident" (one started from Telegram is "operator" too).
               "origin": "incident" if c["origin"] == "incident" else "console",
               "outcome": outcome, "detail": "", "runs": nested,
               "href": f"/console/{c['id']}", "mission_id": own["id"]}
        _add_spend(row, spend, [own["id"], *(n["id"] for n in nested)])
        rows.append(row)
    for mid, m in missions.items():
        if mid in owned:
            continue
        row = {"kind": "mission", "id": mid, "title": _text(m["title"]), "at": m["updated_at"], "started_at": m["created_at"],
               "nature": "change" if m["tier"] not in ("observe", "propose") else "question",
               "origin": "incident" if m["origin"] == "incident" else "dashboard",
               "outcome": mission_outcome(m["status"]), "detail": _text(m["outcome"]), "runs": [],
               "href": f"/missions/{mid}", "mission_id": mid}
        _add_spend(row, spend, [mid])
        rows.append(row)
    return rows


def _iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def _canary_key(plan: dict) -> tuple[str, str] | None:
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and step.get("type") == "update.canary":
            p = step.get("params") or {}
            if isinstance(p.get("stack"), str) and isinstance(p.get("service"), str):
                return p["stack"], p["service"]
    return None


def _approval_title(action: str, target: dict) -> str:
    if isinstance(target.get("title"), str) and target["title"]:
        return target["title"]
    what = target.get("service") or target.get("container") or target.get("stack") or ""
    label = action.replace(".", " ").replace("_", " ")
    return f"{label} {what}".strip()


def _json(text: str | None) -> dict:
    try:
        v = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


# Zoidberg's update-history statuses (casa_zoidberg.py).
_HISTORY_OUTCOME = {"updated": "completed", "no_change": "completed", "skipped": "cancelled", "rolled_back": "rolled_back"}
_NEVER_RAN_S = 3600


def _approval_rows(conn: sqlite3.Connection, history: list[dict], now: float) -> list[dict]:
    conn.row_factory = sqlite3.Row
    execs: dict[str, dict] = {}
    rollbacks: dict[str, sqlite3.Row] = {}                     # each run's latest rollback
    for e in conn.execute("SELECT id, approval_id, kind, parent_execution_id, status, started_at, finished_at, reason FROM executions "
                          "ORDER BY started_at, id"):
        if e["kind"] == "run":
            execs[e["approval_id"]] = e                        # the latest run of this approval
        elif e["parent_execution_id"]:
            rollbacks[e["parent_execution_id"]] = e
    # A canary step that deployed and was put back reads as a failed step with effect not_applied and outputs (Zoidberg's own
    # `_classify`): a rollback, decided from the engine's rows, so it outlives Zoidberg's 200-line history file.
    canary_steps = {r["execution_id"]: r for r in conn.execute(
        "SELECT execution_id, status, effect, output_json, reason FROM execution_steps WHERE type='update.canary'")} \
        if "execution_steps" in _tables(conn) else {}
    unmatched = list(history)
    rows = []
    canaries: list[tuple] = []
    # A mission's own typed steps are approvals too (origin "mission"): they are that mission's work, shown with the mission.
    for a in conn.execute("SELECT id, action, target_json, status, requested_via, origin, created_at, decided_by, decided_at, plan_json "
                          "FROM approvals WHERE status != 'pending' AND COALESCE(origin, '') != 'mission' "
                          "AND requested_via != 'mission'"):
        target, plan = _json(a["target_json"]), _json(a["plan_json"])
        run = execs.get(a["id"])
        never_ran = False
        if a["status"] != "approved":
            outcome = "declined"
        elif run is None:
            # Approved and nothing ran: still starting, or (an hour on) it never will.
            never_ran = (now - (a["decided_at"] or a["created_at"] or 0)) > _NEVER_RAN_S
            outcome = "cancelled" if never_ran else "open"
        else:
            back = rollbacks.get(run["id"])
            outcome = execution_outcome(run["status"], back is not None and back["status"] == "passed")
            if back is not None and back["status"] != "passed":
                # A rollback running, failed or cut off by a restart: the host is not what the run left, say so.
                outcome = "open" if back["status"] in ("running", "verifying") else "failed"
        back = rollbacks.get(run["id"]) if run else None
        last = back if back is not None else run
        at = (last["finished_at"] or last["started_at"]) if last else (a["decided_at"] or a["created_at"])
        detail = (_text(f"rollback {back['status']}: {back['reason'] or ''}".rstrip(": ")) if back is not None and back["status"] != "passed" else
                  "expired before anyone decided" if a["status"] == "expired" else
                  _text(f"denied by {a['decided_by'] or 'the operator'}", 120) if a["status"] == "denied" else
                  "approved, but it never ran" if never_ran else
                  _text(run["reason"]) if run and run["reason"] else "")
        row = {"kind": "execution", "id": a["id"], "title": _text(_approval_title(a["action"], target)), "at": at,
                     "started_at": a["created_at"], "nature": "change",
                     "origin": approval_origin(a["origin"], a["requested_via"]), "outcome": outcome, "detail": detail,
                     "runs": [], "href": f"/executions/{run['id']}" if run else None, "mission_id": None,
                     "calls": 0, "cost_usd": None}
        rows.append(row)
        key = _canary_key(plan)
        step = canary_steps.get(run["id"]) if run else None
        if step is not None and step["status"] == "failed" and step["effect"] == "not_applied" and _json(step["output_json"]):
            row["outcome"] = "rolled_back"
        if step is not None and not row["detail"] and step["reason"]:
            row["detail"] = _text(step["reason"])
        if key and run:
            start = run["started_at"] or a["created_at"]
            end = run["finished_at"] or start
            canaries.append((key, start - 60, end + _HISTORY_WINDOW_S, end, row))
    # Each history line goes to the closest update of that service that could have written it, closest pairs first: a no-change
    # check writes no line, so a later update's line must not be taken by an earlier run of the same service.
    pairs = []
    for hi_, h in enumerate(unmatched):
        t = _iso(h.get("ts"))
        if t is None:
            continue
        for ci, (key, lo, hi, end, _) in enumerate(canaries):
            if (h.get("stack"), h.get("service")) == key and lo <= t <= hi:
                pairs.append((abs(t - end), hi_, ci))
    used_h: set[int] = set()
    used_c: set[int] = set()
    for _, hi_, ci in sorted(pairs):
        if hi_ in used_h or ci in used_c:
            continue
        used_h.add(hi_)
        used_c.add(ci)
        h, row = unmatched[hi_], canaries[ci][4]
        row["detail"] = _text(h.get("reason") or h.get("status"))
        if h.get("status") == "rolled_back":
            row["outcome"] = "rolled_back"                     # Zoidberg put the old image back: say so, not "failed"
    unmatched = [h for i, h in enumerate(unmatched) if i not in used_h]
    for h in unmatched:
        t = _iso(h.get("ts"))
        if t is None:
            continue
        rows.append({"kind": "update", "id": "", "title": _text(f"Canary update {h.get('stack')}/{h.get('service')}"), "at": t,
                     "started_at": t, "nature": "change", "origin": "automatic",
                     "outcome": _HISTORY_OUTCOME.get(h.get("status"), "failed"),
                     "detail": _text(h.get("reason") or h.get("status")), "runs": [], "href": None, "mission_id": None,
                     "calls": 0, "cost_usd": None})
    return rows


# Canary updates closer together than this are one Zoidberg run (a nightly pass updates dozens of services, minutes apart).
_UPDATE_RUN_GAP_S = 1800
_FOLD_MAX = 120
# What survives the cap first: a failure, then a rollback, then anything unfinished; a quiet success last.
_FOLD_RANK = {"failed": 0, "rolled_back": 1, "open": 2, "held": 2, "cancelled": 3, "completed": 4}
_PAGE_BUDGET = 600_000


def _is_canary(row: dict) -> bool:
    return row["origin"] == "automatic" and row["kind"] in ("execution", "update") and row["title"].startswith("Canary update ")


def _fold_canary_runs(rows: list[dict]) -> list[dict]:
    """One row per Zoidberg run instead of one per service (rows newest first). A lone update stays a row of its own."""
    out: list[dict] = []
    group: list[dict] = []

    def flush():
        if len(group) == 1:
            out.append(group[0])
        elif group:
            n = {o: sum(1 for r in group if r["outcome"] == o) for o in ("failed", "rolled_back", "open", "cancelled")}
            # The worst outcome in the run names it; a run where nothing ran at all is cancelled, not "completed".
            outcome = ("failed" if n["failed"] else "rolled_back" if n["rolled_back"] else "open" if n["open"]
                       else "cancelled" if n["cancelled"] == len(group) else "completed")
            words = (("open", "still running"), ("failed", "failed"), ("rolled_back", "rolled back"), ("cancelled", "did not run"))
            out.append({"kind": "updates", "id": "", "title": f"Canary updates: {len(group)} services", "at": group[0]["at"],
                        "started_at": group[-1]["started_at"], "nature": "change", "origin": "automatic", "outcome": outcome,
                        "detail": ", ".join(f"{n[k]} {w}" for k, w in words if n[k]) or "all checked",
                        # Bounded: a run of a few hundred services must not outgrow the RPC frame (failures first, so none is cut).
                        "runs": [{"id": "", "title": _text(f"{r['title'].removeprefix('Canary update ')}: {r['detail']}" if r["detail"]
                                                          else r["title"].removeprefix("Canary update "), 200),
                                  "outcome": r["outcome"], "status": r["outcome"], "at": r["at"], "href": r["href"]}
                                 for r in sorted(group, key=lambda r: _FOLD_RANK.get(r["outcome"], 2))[:_FOLD_MAX]],
                        "href": None, "mission_id": None, "calls": 0, "cost_usd": None})
        group.clear()

    for row in rows:
        if _is_canary(row):
            if group and (group[-1]["at"] or 0) - (row["at"] or 0) > _UPDATE_RUN_GAP_S:
                flush()
            group.append(row)
        else:
            flush()
            out.append(row)
    flush()
    return out


def build_record(conn: sqlite3.Connection, *, missions: bool, history: list[dict], now: float | None = None) -> list[dict]:
    """Every row, newest first. `missions`: whether the mission tables are read (missions on)."""
    names = _tables(conn)
    now = time.time() if now is None else now
    rows = _approval_rows(conn, history, now) if {"approvals", "executions"} <= names else []
    if missions and {"missions", "console_conversations", "console_executions", "mission_events"} <= names:
        rows += _mission_rows(conn)
    rows.sort(key=lambda r: (r["at"] or 0), reverse=True)
    return _fold_canary_runs(rows)


def select(rows: list[dict], *, outcome: str | None, nature: str | None, origin: str | None, limit: int, offset: int) -> dict:
    """One page of rows matching the filters, with counts over the rows the other filters leave (for the filter chips)."""
    def keep(r, skip=None):
        return ((skip == "outcome" or outcome is None or r["outcome"] == outcome)
                and (skip == "nature" or nature is None or r["nature"] == nature)
                and (skip == "origin" or origin is None or r["origin"] == origin))
    chosen = [r for r in rows if keep(r)]
    counts = {f: {} for f in ("outcome", "nature", "origin")}
    for f in counts:
        for r in rows:
            if keep(r, skip=f):
                counts[f][r[f]] = counts[f].get(r[f], 0) + 1
    page = chosen[offset:offset + limit]
    # The RPC frame is 1 MiB: a page of very large update runs keeps its rows and sheds nested lines (failures were sorted first).
    while len(json.dumps(page, default=str)) > _PAGE_BUDGET and any(len(r["runs"]) > 1 for r in page):
        page = [{**r, "runs": r["runs"][:max(1, len(r["runs"]) // 2)]} if len(r["runs"]) > 1 else r for r in page]
    return {"rows": page, "total": len(chosen), "limit": limit, "offset": offset, "counts": counts}


class RecordCache:
    """The record is rebuilt at most once per `ttl_s` (every page load and filter click reads it; building it reads every approval
    and the model-call events). Filters and pages are cut from the cached rows."""

    def __init__(self, ttl_s: float = 15.0, clock=time.monotonic):
        self.ttl_s, self.clock = ttl_s, clock
        self._lock = threading.Lock()
        self._rows: list[dict] | None = None
        self._key = None
        self._at = 0.0

    def rows(self, key, build) -> list[dict]:
        with self._lock:
            if self._rows is None or key != self._key or self.clock() - self._at > self.ttl_s:
                self._rows, self._key, self._at = build(), key, self.clock()
            return self._rows
