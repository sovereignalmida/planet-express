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
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 2
EVENT_RETENTION_SECONDS = 90 * 24 * 3600
AUTH_MAX_FAILURES = 3
AUTH_LOCK_SECONDS = 900
DEFAULT_TTL_SECONDS = 3600  # design v20: an approval card expires after 60 minutes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT, client_ip TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS auth_totp_steps (
    operator TEXT PRIMARY KEY, last_step INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_device_epochs (
    operator TEXT PRIMARY KEY, epoch INTEGER NOT NULL
);
-- Per-key counter state. A lock carries its own deadline (deriving it from the rolling failure
-- count lifted it early), and reset_after_id restarts one key's count without deleting failure
-- rows that also count toward the other key (Codex reviews, T13a). It is a failure row id, not a
-- time, so failures in the same clock tick as a reset are never ambiguous.
CREATE TABLE IF NOT EXISTS auth_counters (
    kind TEXT NOT NULL CHECK (kind IN ('operator', 'client_ip')), value TEXT NOT NULL,
    reset_after_id INTEGER NOT NULL, locked_until REAL NOT NULL DEFAULT 0, PRIMARY KEY (kind, value)
);

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
-- (kind, ts) serves the daily chat ceiling's COUNT and kind lookups like auth_lock_notified (T22).
-- No SCHEMA_VERSION bump: an added IF NOT EXISTS index is backward compatible (an older core
-- opens this DB unchanged), and T23 makes user_version a compatibility gate, so it moves only
-- for changes an older core cannot read.
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);
"""

TERMINAL_EXECUTION_STATUSES = ("passed", "failed", "interrupted")
_EXECUTION_STATUSES = ("running", "verifying", *TERMINAL_EXECUTION_STATUSES)


def _new_id() -> str:
    # 12 hex chars: short enough for Telegram callback_data ("act_ok:<id>" is 19 bytes of
    # a 64-byte limit), random enough that ids never collide in practice.
    return secrets.token_hex(6)


class SchemaTooNewError(RuntimeError):
    """The database requires a newer core or restoration of an older snapshot."""


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

    def _check_schema_version(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"{self.path}: database schema is newer (database version {version}, "
                f"code version {SCHEMA_VERSION}); restore the pre-upgrade snapshot with "
                "scripts/state_snapshot.py or upgrade the code."
            )

    def init(self) -> None:
        # A read-only preflight also avoids checkpointing an existing WAL on refusal.
        if self.path.exists():
            with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
                self._check_schema_version(conn)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self._connect() as conn:
            self._check_schema_version(conn)
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

    def prune_events(self, max_age_seconds: float = EVENT_RETENTION_SECONDS) -> int:
        """Delete old unlinked events, preserving approval and execution audit trails."""
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM events WHERE ts < ? AND approval_id IS NULL AND execution_id IS NULL",
                (self._clock() - max_age_seconds,),
            )
            return cursor.rowcount

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

    def list_pending(self) -> list[dict]:
        """List pending approvals after applying the usual lazy expiry."""
        with self._write() as conn:
            self._expire_stale(conn, self._clock())
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at, id"
            ).fetchall()
        return [dict(row) for row in rows]

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

    def create_direct_execution(
        self, *, action: str, target_key: str, target: dict, risk: str,
        operator: str, arrived_at: float, deadline: float | None = None,
    ) -> dict | None:
        """Record an operator confirmation and its execution in one transaction.

        Returns None, writing nothing, when `deadline` has passed by the time the write lock is
        held: BEGIN IMMEDIATE can wait up to 5s behind another writer, and the caller's RPC deadline
        may already have expired (Codex review, T14)."""
        with self._write() as conn:
            now = self._clock()
            if deadline is not None and now > deadline:
                return None
            self._expire_stale(conn, arrived_at)
            pending = conn.execute(
                "SELECT * FROM approvals WHERE action = ? AND target_key = ? AND status = 'pending'",
                (action, target_key),
            ).fetchone()
            # Adopt a live card only when it names the target the operator just confirmed. A card
            # whose container has since changed would make the worker refuse the restart as
            # "container changed since approval" (Codex review, T14); that card stays pending and
            # keeps its own refusal if tapped.
            adopted = pending is not None and json.loads(pending["target_json"]) == target
            if adopted:
                approval_id = pending["id"]
                conn.execute(
                    "UPDATE approvals SET status = 'approved', decided_by = ?, decided_at = ? WHERE id = ?",
                    (operator, now, approval_id),
                )
            else:
                approval_id = _new_id()
                conn.execute(
                    "INSERT INTO approvals (id, action, target_key, target_json, risk, status, "
                    "requested_via, requested_by, created_at, expires_at, decided_by, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, 'approved', 'dashboard-direct', ?, ?, ?, ?, ?)",
                    (approval_id, action, target_key, json.dumps(target, sort_keys=True), risk,
                     operator, now, now, operator, now),
                )
                self._event(conn, "proposal.created", approval_id=approval_id, action=action,
                            target=target, risk=risk, requested_via="dashboard-direct", requested_by=operator)
            execution_id = _new_id()
            conn.execute(
                "INSERT INTO executions (id, approval_id, status, started_at) VALUES (?, ?, 'running', ?)",
                (execution_id, approval_id, now),
            )
            self._event(conn, "approval.approved", approval_id=approval_id, decided_by=operator)
            self._event(conn, "execution.running", approval_id=approval_id, execution_id=execution_id)
            approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            execution = conn.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        return {"approval": dict(approval), "execution": dict(execution), "adopted": adopted}

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

    # ── dashboard authentication ────────────────────────────────────────────
    #   3 failures for an operator or an IP within 15 min ──► lock that key until
    #   (3rd failure + 15 min) ──► after the deadline: 3 fresh attempts for that key.
    #   A failure recorded while locked never extends the lock; a success clears both keys.
    #   Failure rows are never deleted to reset a counter: one row counts toward BOTH an operator
    #   and an IP, so deleting it for one key reset the other too, letting an attacker rotate IPs
    #   past the per-operator limit (Codex review, T13a). Each key counts only failure rows after
    #   its own reset_after_id instead.
    #   Operator "?" means no passphrase matched: that failure counts toward the IP only. As an
    #   operator key it would be global, and one client could seal the airlock for every
    #   operator on every device (Codex review, T13b).
    _AUTH_KEYS = ("operator", "client_ip")

    def _auth_keys(self, operator, client_ip) -> list[tuple[str, str]]:
        keys = list(zip(self._AUTH_KEYS, (operator, client_ip)))
        return keys[1:] if operator == "?" else keys

    def _counter(self, conn, kind, value) -> tuple[float, float]:
        row = conn.execute("SELECT reset_after_id, locked_until FROM auth_counters WHERE kind = ? AND value = ?",
                           (kind, value)).fetchone()
        return (row[0], row[1]) if row else (0, 0.0)

    def _auth_status(self, conn, operator, client_ip, now) -> dict:
        counts, deadlines = [], []
        for kind, value in self._auth_keys(operator, client_ip):
            reset_after_id, locked_until = self._counter(conn, kind, value)
            if locked_until > now:
                deadlines.append(locked_until)
            counts.append(conn.execute(
                # at >= locked_until: attempts made while this key was locked never spend the
                # fresh budget it gets at the deadline (Codex review, T13a). They still count
                # toward the other key.
                f"SELECT COUNT(*) FROM auth_failures WHERE {kind} = ? AND at > ? AND id > ? AND at >= ?",
                (value, now - AUTH_LOCK_SECONDS, reset_after_id, locked_until),
            ).fetchone()[0])
        locked = bool(deadlines)
        return {"locked": locked, "locked_until": max(deadlines) if locked else None,
                "remaining_attempts": 0 if locked else max(0, AUTH_MAX_FAILURES - max(counts)),
                "_counts": counts}

    @staticmethod
    def _public(status: dict) -> dict:
        return {k: v for k, v in status.items() if not k.startswith("_")}

    def auth_status(self, operator, client_ip) -> dict:
        with self._connect() as conn:
            return self._public(self._auth_status(conn, operator, client_ip, self._clock()))

    def _reset_counter(self, conn, kind, value, locked_until) -> None:
        last_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM auth_failures").fetchone()[0]
        conn.execute(
            "INSERT INTO auth_counters (kind, value, reset_after_id, locked_until) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind, value) DO UPDATE SET reset_after_id = excluded.reset_after_id, "
            "locked_until = excluded.locked_until",
            (kind, value, last_id, locked_until),
        )

    def record_auth_failure(self, operator, client_ip) -> dict:
        with self._write() as conn:
            now = self._clock()
            before = self._auth_status(conn, operator, client_ip, now)
            conn.execute("INSERT INTO auth_failures (operator, client_ip, at) VALUES (?, ?, ?)",
                         (operator, client_ip, now))
            conn.execute("DELETE FROM auth_failures WHERE at <= ?", (now - AUTH_LOCK_SECONDS,))
            # A counter can be dropped once it filters nothing: its reset point precedes every
            # remaining failure row (AUTOINCREMENT ids are never reused) and its deadline is a full
            # window old, so no locked-period failure is still countable.
            conn.execute(
                "DELETE FROM auth_counters WHERE locked_until <= ? AND reset_after_id < "
                "COALESCE((SELECT MIN(id) FROM auth_failures), reset_after_id + 1)",
                (now - AUTH_LOCK_SECONDS,))
            counted = self._auth_status(conn, operator, client_ip, now)
            for (kind, value), count in zip(self._auth_keys(operator, client_ip), counted["_counts"]):
                if count >= AUTH_MAX_FAILURES and self._counter(conn, kind, value)[1] <= now:
                    self._reset_counter(conn, kind, value, now + AUTH_LOCK_SECONDS)
            status = self._public(self._auth_status(conn, operator, client_ip, now))
            just_locked = status["locked"] and not before["locked"]
            payload = {"operator": operator, "client_ip": client_ip, "locked_until": status["locked_until"]}
            self._event(conn, "auth.failure", **payload)
            if just_locked:
                self._event(conn, "auth.locked", **payload)
            return status | {"just_locked": just_locked}

    def record_auth_success(self, operator, client_ip) -> None:
        with self._write() as conn:
            for kind, value in self._auth_keys(operator, client_ip):
                self._reset_counter(conn, kind, value, 0)
            self._event(conn, "auth.success", operator=operator, client_ip=client_ip)

    def consume_totp_step(self, operator, step: int) -> bool:
        with self._write() as conn:
            result = conn.execute(
                "INSERT INTO auth_totp_steps (operator, last_step) VALUES (?, ?) "
                "ON CONFLICT(operator) DO UPDATE SET last_step = excluded.last_step "
                "WHERE excluded.last_step > auth_totp_steps.last_step", (operator, step),
            )
            return result.rowcount == 1

    def device_epoch(self, operator) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT epoch FROM auth_device_epochs WHERE operator = ?",
                               (operator,)).fetchone()
            return row[0] if row else 0

    def revoke_devices(self, operator) -> int:
        with self._write() as conn:
            row = conn.execute(
                "INSERT INTO auth_device_epochs (operator, epoch) VALUES (?, 1) "
                "ON CONFLICT(operator) DO UPDATE SET epoch = epoch + 1 RETURNING epoch",
                (operator,),
            ).fetchone()
            self._event(conn, "auth.devices_revoked", operator=operator, epoch=row[0])
            return row[0]

    def auth_lock_notified(self, operator, client_ip, locked_until) -> bool:
        payload = {"operator": operator, "client_ip": client_ip, "locked_until": locked_until}
        with self._write() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE kind = 'auth.lock_notified' AND payload = ? LIMIT 1",
                (json.dumps(payload, sort_keys=True),),
            ).fetchone()
            if row:
                return True
            self._event(conn, "auth.lock_notified", **payload)
            return False
