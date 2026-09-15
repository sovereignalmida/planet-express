"""
planet_express/core/store.py — durable state for typed actions (landing 1b): proposals and
their approvals, executions, and an append-only event log, in SQLite.

Threading (eng review issue 2): a fresh connection per operation, WAL journal, a 5s busy
timeout, and every write inside BEGIN IMMEDIATE. No connection object is ever shared
between threads. The database lives in a core-only directory (mode 0700); the dashboard
never opens it (landing 1c reads through RPC).

  approval:   pending ──consume(approved)──► approved ──► one execution row
                 │  └───consume(denied)────► denied
                 └──(expires_at passed; marked lazily on lookup)──► expired

  execution:  running ──► verifying ──► passed | failed
                 └────────────┴──(core restarted)──► interrupted

Times are UNIX epoch seconds from an injectable clock, so expiry is plain arithmetic and
tests control time. The server clock is the only clock: a decision counts if the request
ARRIVED before expires_at (the design pauses its countdown during submission), but a
client-supplied timestamp is never trusted.
"""

import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 3600  # design v20: an approval card expires after 60 minutes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id            TEXT PRIMARY KEY,
    action        TEXT NOT NULL,
    target_key    TEXT NOT NULL,
    target_json   TEXT NOT NULL,
    risk          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    requested_via TEXT NOT NULL,
    requested_by  TEXT,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    decided_by    TEXT,
    decided_at    REAL,
    message_id    INTEGER
);
-- One live proposal per (action, target): two front ends proposing at once can't both insert.
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending
    ON approvals (action, target_key) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS executions (
    id          TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL REFERENCES approvals (id),
    status      TEXT NOT NULL
                CHECK (status IN ('running', 'verifying', 'passed', 'failed', 'interrupted')),
    started_at  REAL NOT NULL,
    finished_at REAL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    approval_id  TEXT,
    execution_id TEXT,
    payload      TEXT NOT NULL DEFAULT '{}'
);
"""

TERMINAL_EXECUTION_STATUSES = ("passed", "failed", "interrupted")
_EXECUTION_STATUSES = ("running", "verifying", *TERMINAL_EXECUTION_STATUSES)


def _new_id() -> str:
    # 12 hex chars: short enough for Telegram callback_data ("act_ok:<id>" is 19 bytes of
    # a 64-byte limit), random enough that ids never collide in practice.
    return secrets.token_hex(6)


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock

    # ── connections ─────────────────────────────────────────────────────────
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def init(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ── events ──────────────────────────────────────────────────────────────
    def _event(
        self,
        conn: sqlite3.Connection,
        kind: str,
        *,
        approval_id: str | None = None,
        execution_id: str | None = None,
        **payload,
    ) -> None:
        conn.execute(
            "INSERT INTO events (ts, kind, approval_id, execution_id, payload) VALUES (?, ?, ?, ?, ?)",
            (self._clock(), kind, approval_id, execution_id, json.dumps(payload, sort_keys=True)),
        )

    def record_event(
        self, kind: str, *, approval_id: str | None = None, execution_id: str | None = None, **payload
    ) -> None:
        with self._write() as conn:
            self._event(conn, kind, approval_id=approval_id, execution_id=execution_id, **payload)

    def list_events(self, approval_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if approval_id is None:
                rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE approval_id = ? ORDER BY id", (approval_id,)
                ).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    # ── approvals ───────────────────────────────────────────────────────────
    def _expire_stale(self, conn: sqlite3.Connection, as_of: float) -> None:
        stale = conn.execute(
            "SELECT id FROM approvals WHERE status = 'pending' AND expires_at <= ?", (as_of,)
        ).fetchall()
        for row in stale:
            conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (row["id"],))
            self._event(conn, "approval.expired", approval_id=row["id"])

    def propose(
        self,
        *,
        action: str,
        target_key: str,
        target: dict,
        risk: str,
        requested_via: str,
        requested_by: str | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> tuple[dict, bool]:
        """Return (approval row, created). A live proposal for the same (action, target)
        is returned as-is instead of creating a second one. Lazy expiry, lookup and
        insert happen in one BEGIN IMMEDIATE transaction."""
        now = self._clock()
        with self._write() as conn:
            self._expire_stale(conn, now)
            row = conn.execute(
                "SELECT * FROM approvals WHERE action = ? AND target_key = ? AND status = 'pending'",
                (action, target_key),
            ).fetchone()
            if row is not None:
                return dict(row), False
            approval_id = _new_id()
            conn.execute(
                "INSERT INTO approvals (id, action, target_key, target_json, risk, status, "
                "requested_via, requested_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (approval_id, action, target_key, json.dumps(target, sort_keys=True), risk,
                 requested_via, requested_by, now, now + ttl_seconds),
            )
            self._event(conn, "proposal.created", approval_id=approval_id, action=action,
                        target=target, risk=risk, requested_via=requested_via,
                        requested_by=requested_by)
            created = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return dict(created), True

    def get_approval(self, approval_id: str, as_of: float | None = None) -> dict | None:
        """Look up an approval, marking it expired first if its TTL has passed as of
        `as_of` (the request's arrival time), defaulting to now."""
        with self._write() as conn:
            self._expire_stale(conn, self._clock() if as_of is None else as_of)
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return dict(row) if row is not None else None

    def set_message_id(self, approval_id: str, message_id: int | None) -> None:
        with self._write() as conn:
            conn.execute("UPDATE approvals SET message_id = ? WHERE id = ?", (message_id, approval_id))

    def consume(self, approval_id: str, *, decision: str, decided_by: str, arrived_at: float) -> bool:
        """Atomically move a pending approval to approved/denied. True for exactly one
        caller; False if it was already decided, expired, or unknown."""
        if decision not in ("approved", "denied"):
            raise ValueError(f"decision must be 'approved' or 'denied', not {decision!r}")
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending' AND expires_at > ?",
                (decision, decided_by, self._clock(), approval_id, arrived_at),
            )
            if cur.rowcount != 1:
                return False
            self._event(conn, f"approval.{decision}", approval_id=approval_id, decided_by=decided_by)
            return True

    def approve_and_create_execution(
        self, approval_id: str, *, decided_by: str, arrived_at: float
    ) -> dict | None:
        """Atomically consume an approval and create its running execution.

        Returning ``None`` means the approval lost the conditional-update race or was
        expired. Any insert/storage failure rolls the approval transition back as part of
        the same transaction, so an approval can never be stranded as approved without an
        execution record.
        """
        execution_id = _new_id()
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = 'approved', decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending' AND expires_at > ?",
                (decided_by, self._clock(), approval_id, arrived_at),
            )
            if cur.rowcount != 1:
                return None
            conn.execute(
                "INSERT INTO executions (id, approval_id, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (execution_id, approval_id, self._clock()),
            )
            self._event(conn, "approval.approved", approval_id=approval_id, decided_by=decided_by)
            self._event(
                conn,
                "execution.running",
                approval_id=approval_id,
                execution_id=execution_id,
            )
            row = conn.execute(
                "SELECT * FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
        return dict(row)

    # ── executions ──────────────────────────────────────────────────────────
    def create_execution(self, approval_id: str) -> dict:
        execution_id = _new_id()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO executions (id, approval_id, status, started_at) VALUES (?, ?, 'running', ?)",
                (execution_id, approval_id, self._clock()),
            )
            self._event(conn, "execution.running", approval_id=approval_id, execution_id=execution_id)
            row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        return dict(row)

    def set_execution_status(self, execution_id: str, status: str, reason: str | None = None) -> None:
        if status not in _EXECUTION_STATUSES:
            raise ValueError(f"unknown execution status {status!r}")
        finished = self._clock() if status in TERMINAL_EXECUTION_STATUSES else None
        with self._write() as conn:
            row = conn.execute("SELECT approval_id FROM executions WHERE id = ?", (execution_id,)).fetchone()
            if row is None:
                raise KeyError(execution_id)
            conn.execute(
                "UPDATE executions SET status = ?, reason = COALESCE(?, reason), finished_at = ? WHERE id = ?",
                (status, reason, finished, execution_id),
            )
            self._event(conn, f"execution.{status}", approval_id=row["approval_id"],
                        execution_id=execution_id, reason=reason)

    def get_execution(self, execution_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        return dict(row) if row is not None else None

    def interrupt_unfinished(self, reason: str) -> list[dict]:
        """Mark every running/verifying execution interrupted (startup reconciliation).
        Returns them joined with their approval's action, target and Telegram message id."""
        now = self._clock()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT e.id, e.approval_id, a.action, a.target_json, a.message_id "
                "FROM executions e JOIN approvals a ON a.id = e.approval_id "
                "WHERE e.status IN ('running', 'verifying') ORDER BY e.started_at"
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE executions SET status = 'interrupted', reason = ?, finished_at = ? WHERE id = ?",
                    (reason, now, r["id"]),
                )
                self._event(conn, "execution.interrupted", approval_id=r["approval_id"],
                            execution_id=r["id"], reason=reason)
        return [dict(r) | {"target": json.loads(r["target_json"]), "reason": reason} for r in rows]
