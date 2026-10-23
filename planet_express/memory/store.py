"""Ship's Memory store: the status machine, who may move a card through it, and the audit trail.

Rules enforced here (contracts §13, mission-control §8.2):
  - Everything enters as `candidate`. Nothing a runtime or importer writes is ever trusted on arrival.
  - Only an operator confirms a procedure or constraint. `system` may confirm facts and decisions
    (discovery candidates); `runtime` and `importer` never confirm anything.
  - A confirmed card is never overwritten by an upsert. Callers get CardExistsError and decide
    (the importer creates a conflict candidate).
  - A candidate cannot retire anything: supersession only takes effect once the superseding card is
    confirmed, so a quarantined proposal cannot hide a confirmed constraint.
  - Every transition appends a memory_history row naming the actor. The table is append-only in SQL.

Functions never commit on their own beyond their own SAVEPOINT; if you need several calls to be
one unit, `BEGIN` first and commit or roll back yourself.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from planet_express.memory.models import Card, HistoryEntry, Step, parse_actor

DEFAULT_REVIEW_DAYS = 180

_EXTRA_SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.")),
)


class MemoryStoreError(Exception):
    pass


class UnknownCard(MemoryStoreError):
    pass


class CardExistsError(MemoryStoreError):
    def __init__(self, card_id: str, status: str):
        super().__init__(f"card {card_id!r} already exists as {status}; it is not overwritten")
        self.card_id, self.status = card_id, status


class ActorNotPermitted(MemoryStoreError):
    pass


class InvalidTransition(MemoryStoreError):
    pass


class SecretShapedContent(MemoryStoreError):
    """Never carries the offending text, only where it is."""

    def __init__(self, card_id: str, where: str, why: str):
        super().__init__(f"card {card_id!r} looks like it contains a secret ({why}) at {where}; refusing to store it")
        self.card_id, self.where = card_id, where


_redact_fn = None
_redact_fallback = False


def redact_is_shape_only() -> bool:
    """True when core.redact had to be loaded without a PE config: configured (site-specific) secrets
    are then unknown and screening is by shape only."""
    return _redact_fallback


def _redact(text: str) -> str:
    """core.redact is imported lazily: it needs a PE config file at import time, and a user with only
    Markdown notes has none. Without one we import it against the repository's example config, which
    only means no site-specific credentials are known; the shape-based detectors are unaffected.
    CASA_CONFIG is set only for that one import and put back exactly as it was."""
    global _redact_fn, _redact_fallback
    if _redact_fn is None:
        import os
        from pathlib import Path
        try:
            from planet_express.core.redact import redact
        except (Exception, SystemExit):
            example = Path(__file__).resolve().parents[2] / "config.example.yaml"
            if "CASA_CONFIG" in os.environ or not example.exists():
                raise
            os.environ["CASA_CONFIG"] = str(example)
            try:
                from planet_express.core.redact import redact
            finally:
                del os.environ["CASA_CONFIG"]
            _redact_fallback = True
        _redact_fn = redact
    return _redact_fn(text)


def _card_text_fields(card: Card):
    yield "title", card.title
    yield "statement", card.statement
    for i, step in enumerate(card.steps, 1):
        yield f"steps[{i}].text", step.text
        yield f"steps[{i}].check", step.check
    for i, ref in enumerate(card.evidence, 1):
        yield f"evidence[{i}]", ref
    for i, tag in enumerate(card.scope, 1):
        yield f"scope[{i}]", tag


def check_text_no_secrets(card_id: str, name: str, text: str) -> None:
    """Raise SecretShapedContent (naming `name` and the line, never the text) if any line looks secret."""
    for line_no, line in enumerate(text.split("\n"), 1):
        if _redact(line) != line:
            raise SecretShapedContent(card_id, f"{name} line {line_no}", "credential assignment or token")
        for why, pattern in _EXTRA_SECRET_PATTERNS:
            if pattern.search(line):
                raise SecretShapedContent(card_id, f"{name} line {line_no}", why)


def check_no_secrets(card: Card) -> None:
    for name, text in _card_text_fields(card):
        check_text_no_secrets(card.id, name, text)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """One atomic unit. Outside a transaction it takes the write lock up front (BEGIN IMMEDIATE), so a
    status read inside the block cannot go stale before the write; inside one it nests as a SAVEPOINT
    and the caller owns commit and locking."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT memory_op")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO memory_op")
        conn.execute("RELEASE memory_op")
        if own:
            conn.execute("ROLLBACK")
        raise
    conn.execute("RELEASE memory_op")
    if own:
        conn.execute("COMMIT")


def _cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def _history(conn, card_id, actor, action, now, from_status=None, to_status=None, detail=""):
    conn.execute(
        "INSERT INTO memory_history (card_id, at, actor, action, from_status, to_status, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (card_id, _iso(now), actor, action, from_status, to_status, detail))


def _row(conn, card_id: str):
    return _cursor(conn).execute("SELECT * FROM memory_cards WHERE id = ?", (card_id,)).fetchone()


def _require(conn, card_id: str):
    row = _row(conn, card_id)
    if row is None:
        raise UnknownCard(f"no card {card_id!r}")
    return row


def _edges(conn, card_id: str) -> tuple[list[str], list[str]]:
    cur = _cursor(conn)
    supersedes = [r["dst"] for r in cur.execute(
        "SELECT dst FROM memory_edges WHERE src = ? AND kind = 'supersedes' ORDER BY dst", (card_id,))]
    conflicts = [r[0] for r in conn.execute(
        "SELECT dst FROM memory_edges WHERE src = ? AND kind = 'conflicts_with'"
        " UNION SELECT src FROM memory_edges WHERE dst = ? AND kind = 'conflicts_with' ORDER BY 1",
        (card_id, card_id))]
    return supersedes, conflicts


def _to_card(conn, row) -> Card:
    supersedes, conflicts = _edges(conn, row["id"])
    return Card(
        id=row["id"], title=row["title"], type=row["type"], scope=json.loads(row["scope_json"]),
        statement=row["statement"], steps=[Step(**s) for s in json.loads(row["steps_json"])],
        evidence=json.loads(row["evidence_json"]), source=row["source"], status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_verified_at=datetime.fromisoformat(row["last_verified_at"]) if row["last_verified_at"] else None,
        review_after=row["review_after"] and datetime.fromisoformat(row["review_after"]).date(),
        supersedes=supersedes, conflicts_with=conflicts)


def get_card(conn: sqlite3.Connection, card_id: str) -> Card | None:
    row = _row(conn, card_id)
    return None if row is None else _to_card(conn, row)


def list_cards(conn: sqlite3.Connection, status: str | None = None) -> list[Card]:
    cur = _cursor(conn)
    if status is None:
        rows = cur.execute("SELECT * FROM memory_cards ORDER BY id").fetchall()
    else:
        rows = cur.execute("SELECT * FROM memory_cards WHERE status = ? ORDER BY id", (status,)).fetchall()
    return [_to_card(conn, r) for r in rows]


def stored_hash(conn: sqlite3.Connection, card_id: str) -> str | None:
    """content_hash of what was last written for this card; None if absent."""
    row = conn.execute("SELECT content_hash FROM memory_cards WHERE id = ?", (card_id,)).fetchone()
    return None if row is None else row[0]


def history(conn: sqlite3.Connection, card_id: str) -> list[HistoryEntry]:
    rows = _cursor(conn).execute("SELECT * FROM memory_history WHERE card_id = ? ORDER BY seq", (card_id,))
    return [HistoryEntry(
        seq=r["seq"], card_id=r["card_id"], at=datetime.fromisoformat(r["at"]), actor=r["actor"],
        action=r["action"], from_status=r["from_status"], to_status=r["to_status"], detail=r["detail"])
        for r in rows]


def _add_edge(conn, src, dst, kind, now):
    conn.execute("INSERT OR IGNORE INTO memory_edges (src, dst, kind, created_at) VALUES (?, ?, ?, ?)",
                 (src, dst, kind, _iso(now)))


def upsert_candidate(conn: sqlite3.Connection, card: Card, actor: str, *, now: datetime | None = None,
                     detail: str = "") -> str:
    """Create or update a CANDIDATE. Returns 'created', 'updated' or 'unchanged'.

    The card's own `status` is ignored: this function cannot produce anything but a candidate.
    """
    kind, _ = parse_actor(actor)
    now = _now(now)
    if kind in ("runtime", "system") and card.source == "operator-stated":
        raise ActorNotPermitted(f"{kind} actors cannot attribute a card to the operator")
    card = card.model_copy(update={"status": "candidate"})
    check_no_secrets(card)
    check_text_no_secrets(card.id, "history detail", detail)  # detail is stored forever
    values = (
        card.title, card.type, json.dumps(card.scope), " ".join(card.scope), card.statement,
        json.dumps([s.model_dump() for s in card.steps]), json.dumps(card.evidence), card.source,
        _iso(card.last_verified_at), card.review_after.isoformat() if card.review_after else None,
        card.content_hash())
    try:
        with _atomic(conn):
            row = _row(conn, card.id)  # read under the write lock; the UPDATE is guarded as well
            if row is not None and row["status"] != "candidate":
                raise CardExistsError(card.id, row["status"])
            if row is None:
                conn.execute(
                    "INSERT INTO memory_cards (title, type, scope_json, tags, statement, steps_json,"
                    " evidence_json, source, last_verified_at, review_after, content_hash, id, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)",
                    (*values, card.id, _iso(card.created_at)))
                outcome, from_status = "created", None
            else:
                if row["content_hash"] == card.content_hash():
                    return "unchanged"
                changed = conn.execute(
                    "UPDATE memory_cards SET title=?, type=?, scope_json=?, tags=?, statement=?, steps_json=?,"
                    " evidence_json=?, source=?, last_verified_at=?, review_after=?, content_hash=?"
                    " WHERE id = ? AND status = 'candidate'", (*values, card.id)).rowcount
                if changed != 1:
                    raise CardExistsError(card.id, "no longer a candidate")
                outcome, from_status = "updated", "candidate"
            conn.execute("DELETE FROM memory_edges WHERE src = ? AND kind = 'supersedes'", (card.id,))
            for dst in card.supersedes:
                _add_edge(conn, card.id, dst, "supersedes", now)
            for other in card.conflicts_with:
                _add_edge(conn, card.id, other, "conflicts_with", now)
            _history(conn, card.id, actor, "create" if outcome == "created" else "update", now,
                     from_status, "candidate", detail)
    except sqlite3.IntegrityError as exc:
        # Someone else created the id between our read and our insert (only possible inside a
        # caller-owned transaction): the same answer as finding it already there.
        current = _row(conn, card.id)
        raise CardExistsError(card.id, current["status"] if current else "existing") from exc
    return outcome


def _deprecate_row(conn, card_id, actor, action, now, detail):
    row = _row(conn, card_id)
    if row is None or row["status"] == "deprecated":
        return
    conn.execute("UPDATE memory_cards SET status = 'deprecated' WHERE id = ?", (card_id,))
    _history(conn, card_id, actor, action, now, row["status"], "deprecated", detail)


def _apply_supersession(conn, new_id, actor, now):
    for old in [r[0] for r in conn.execute(
            "SELECT dst FROM memory_edges WHERE src = ? AND kind = 'supersedes'", (new_id,))]:
        _deprecate_row(conn, old, actor, "supersede", now, f"superseded by {new_id}")


def confirm(conn: sqlite3.Connection, card_id: str, operator: str, *, now: datetime | None = None) -> Card:
    kind, _ = parse_actor(operator)
    now = _now(now)
    with _atomic(conn):
        row = _require(conn, card_id)
        allowed = kind == "operator" or (kind == "system" and row["type"] in ("fact", "decision"))
        if not allowed:
            raise ActorNotPermitted(f"a {kind} actor cannot confirm a {row['type']} card; an operator must")
        if row["status"] != "candidate":
            raise InvalidTransition(f"card {card_id!r} is {row['status']}, only a candidate can be confirmed")
        conn.execute(
            "UPDATE memory_cards SET status = 'confirmed', confirmed_at = ?, last_verified_at = ? WHERE id = ? "
            "AND status = 'candidate'",
            (_iso(now), _iso(now), card_id))
        _history(conn, card_id, operator, "confirm", now, "candidate", "confirmed")
        _apply_supersession(conn, card_id, operator, now)
    return get_card(conn, card_id)


def deprecate(conn: sqlite3.Connection, card_id: str, actor: str, reason: str = "", *,
              now: datetime | None = None) -> Card:
    """Retire a card. Operator or system only: a runtime must not be able to delete a constraint."""
    kind, _ = parse_actor(actor)
    if kind not in ("operator", "system"):
        raise ActorNotPermitted(f"a {kind} actor cannot deprecate a card")
    row = _require(conn, card_id)
    if row["status"] == "deprecated":
        raise InvalidTransition(f"card {card_id!r} is already deprecated")
    with _atomic(conn):
        _deprecate_row(conn, card_id, actor, "deprecate", _now(now), reason)
    return get_card(conn, card_id)


def supersede(conn: sqlite3.Connection, new_id: str, old_id: str, actor: str, *,
              now: datetime | None = None) -> None:
    """Record that `new_id` replaces `old_id`. Inert until `new_id` is confirmed."""
    kind, _ = parse_actor(actor)
    now = _now(now)
    if new_id == old_id:
        raise InvalidTransition("a card cannot supersede itself")
    new_row, _old = _require(conn, new_id), _require(conn, old_id)
    if new_row["status"] == "confirmed" and kind not in ("operator", "system"):
        raise ActorNotPermitted(f"a {kind} actor cannot retire a card")
    with _atomic(conn):
        _add_edge(conn, new_id, old_id, "supersedes", now)
        _history(conn, new_id, actor, "link-supersedes", now, detail=old_id)
        if new_row["status"] == "confirmed":
            _deprecate_row(conn, old_id, actor, "supersede", now, f"superseded by {new_id}")


def mark_conflict(conn: sqlite3.Connection, a: str, b: str, actor: str, *, now: datetime | None = None) -> None:
    """Flag two cards as disagreeing. Symmetric; changes no status."""
    parse_actor(actor)
    now = _now(now)
    _require(conn, a)
    _require(conn, b)
    if a == b:
        raise InvalidTransition("a card cannot conflict with itself")
    with _atomic(conn):
        _add_edge(conn, a, b, "conflicts_with", now)
        for card_id, other in ((a, b), (b, a)):
            _history(conn, card_id, actor, "conflict", now, detail=other)


def reverify(conn: sqlite3.Connection, card_id: str, operator: str, *, now: datetime | None = None,
             review_after=None) -> Card:
    kind, _ = parse_actor(operator)
    if kind != "operator":
        raise ActorNotPermitted("only an operator can re-verify a card")
    now = _now(now)
    row = _require(conn, card_id)
    if row["status"] != "confirmed":
        raise InvalidTransition(f"card {card_id!r} is {row['status']}, only a confirmed card is re-verified")
    with _atomic(conn):
        conn.execute("UPDATE memory_cards SET last_verified_at = ?, review_after = ? WHERE id = ?",
                     (_iso(now), review_after.isoformat() if review_after else None, card_id))
        _history(conn, card_id, operator, "reverify", now, "confirmed", "confirmed")
    return get_card(conn, card_id)


def review_due(conn: sqlite3.Connection, now: datetime | None = None) -> list[Card]:
    """Confirmed cards whose review_after has passed, or (with none set) that have not been verified for
    DEFAULT_REVIEW_DAYS. Stale cards become review items, never silent folklore."""
    now = _now(now)
    cutoff = _iso(now - timedelta(days=DEFAULT_REVIEW_DAYS))
    rows = _cursor(conn).execute(
        "SELECT * FROM memory_cards WHERE status = 'confirmed' AND ("
        " (review_after IS NOT NULL AND review_after <= ?)"
        " OR (review_after IS NULL AND COALESCE(last_verified_at, confirmed_at, created_at) <= ?))"
        " ORDER BY COALESCE(review_after, last_verified_at), id",
        (now.date().isoformat(), cutoff)).fetchall()
    return [_to_card(conn, r) for r in rows]
