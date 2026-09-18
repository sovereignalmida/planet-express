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

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 4
EVENT_RETENTION_SECONDS = 90 * 24 * 3600
AUTH_MAX_FAILURES = 3
AUTH_LOCK_SECONDS = 900
DEFAULT_TTL_SECONDS = 3600  # design v20: an approval card expires after 60 minutes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_reconciliations (
    scan_id TEXT PRIMARY KEY,
    snapshot_timestamp TEXT NOT NULL,
    reconciled_at REAL NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0)
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    fingerprint_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    condition TEXT NOT NULL CHECK (condition IN ('failing', 'unknown')),
    severity TEXT CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL,
    last_observed_scan_id TEXT NOT NULL REFERENCES incident_reconciliations(scan_id),
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    resolved_at REAL,
    occurrences INTEGER NOT NULL CHECK (occurrences > 0)
);
CREATE INDEX IF NOT EXISTS incidents_status_last_seen ON incidents(status, last_seen DESC);

CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    scan_id TEXT NOT NULL REFERENCES incident_reconciliations(scan_id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('opened', 'observed', 'resolved', 'reopened')),
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS incident_events_incident ON incident_events(incident_id, id);
CREATE INDEX IF NOT EXISTS incident_reconciliations_time
    ON incident_reconciliations(reconciled_at DESC);

CREATE TABLE IF NOT EXISTS chat_tickets (
    id TEXT PRIMARY KEY, operator TEXT NOT NULL, submission_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','interrupted')),
    outcome TEXT CHECK (outcome IN ('answer','proposal','insufficient_evidence','unsupported_fix','quota_exhausted')),
    answer TEXT, evidence_json TEXT NOT NULL DEFAULT '[]', cited_json TEXT NOT NULL DEFAULT '[]',
    approval_id TEXT, error TEXT, llm_calls INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, finished_at REAL,
    UNIQUE (operator, submission_id)
);

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

CREATE INDEX IF NOT EXISTS approvals_action_target ON approvals (action, target_key);

CREATE TABLE IF NOT EXISTS incident_proposals (
    approval_id TEXT PRIMARY KEY REFERENCES approvals (id),
    incident_id TEXT NOT NULL REFERENCES incidents (id),
    scan_id     TEXT NOT NULL REFERENCES incident_reconciliations (scan_id)
);
CREATE INDEX IF NOT EXISTS incident_proposals_incident
    ON incident_proposals (incident_id, approval_id);

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


class IncidentSourceError(RuntimeError):
    """An incident proposal has no current reconciled source."""


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock

    # ── connections ─────────────────────────────────────────────────────────
    @contextmanager
    def _connect(self, timeout: float = 5) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self, timeout: float = 5) -> Iterator[sqlite3.Connection]:
        with self._connect(timeout=timeout) as conn:
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
            try:
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{_SCHEMA}\n"
                    f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

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
        self, kind: str, *, approval_id: str | None = None, execution_id: str | None = None,
        timeout: float = 5, **payload,
    ) -> None:
        with self._write(timeout=timeout) as conn:
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

    # ── incidents ──────────────────────────────────────────────────────────
    @staticmethod
    def _incident_row(row):
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    @staticmethod
    def _validate_observations(observations):
        valid_conditions = {"healthy", "failing", "unknown"}
        valid_severities = {None, "CRITICAL", "HIGH", "MEDIUM", "LOW"}
        normalized = []
        fingerprints = set()
        for observation in observations:
            if not isinstance(observation, dict):
                raise TypeError("incident observations must be dictionaries")
            required = {"fingerprint", "fingerprint_version", "kind", "resource", "condition",
                        "severity", "summary", "details"}
            if set(observation) != required:
                raise ValueError("incident observation has unexpected fields")
            fingerprint = observation["fingerprint"]
            if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
                raise ValueError("invalid incident fingerprint")
            if fingerprint in fingerprints:
                raise ValueError("duplicate incident fingerprint")
            fingerprints.add(fingerprint)
            if observation["fingerprint_version"] != 1:
                raise ValueError("unsupported incident fingerprint version")
            if (not isinstance(observation["kind"], str) or not observation["kind"]
                    or len(observation["kind"]) > 64):
                raise ValueError("invalid incident kind")
            if (not isinstance(observation["resource"], str) or not observation["resource"]
                    or len(observation["resource"]) > 512):
                raise ValueError("invalid incident resource")
            identity = json.dumps(
                {
                    "version": observation["fingerprint_version"],
                    "kind": observation["kind"],
                    "resource": observation["resource"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if hashlib.sha256(identity).hexdigest() != fingerprint:
                raise ValueError("incident fingerprint does not match its identity")
            if observation["condition"] not in valid_conditions:
                raise ValueError("invalid incident condition")
            if observation["severity"] not in valid_severities:
                raise ValueError("invalid incident severity")
            if not isinstance(observation["summary"], str) or len(observation["summary"]) > 1024:
                raise ValueError("invalid incident summary")
            if not isinstance(observation["details"], dict):
                raise TypeError("invalid incident details")
            details_json = json.dumps(observation["details"], sort_keys=True, separators=(",", ":"))
            if len(details_json) > 16 * 1024:
                raise ValueError("incident details are too large")
            normalized.append(observation | {"details_json": details_json})
        return normalized

    def reconcile_incidents(self, scan_id: str, snapshot_timestamp: str, observations) -> list[dict]:
        if not isinstance(scan_id, str) or re.fullmatch(r"[0-9a-f]{64}", scan_id) is None:
            raise ValueError("invalid incident scan id")
        if (
            not isinstance(snapshot_timestamp, str)
            or not snapshot_timestamp
            or len(snapshot_timestamp) > 128
        ):
            raise ValueError("invalid snapshot timestamp")
        observations = self._validate_observations(observations)
        now = self._clock()
        changed = []
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM incident_reconciliations WHERE scan_id=?", (scan_id,)).fetchone():
                return []
            conn.execute(
                "INSERT INTO incident_reconciliations "
                "(scan_id, snapshot_timestamp, reconciled_at, observation_count) VALUES (?, ?, ?, ?)",
                (scan_id, snapshot_timestamp, now, len(observations)),
            )
            for item in observations:
                row = conn.execute("SELECT * FROM incidents WHERE fingerprint=?",
                                   (item["fingerprint"],)).fetchone()
                condition, severity = item["condition"], item["severity"]
                if condition == "healthy":
                    if row is None:
                        continue
                    if row["status"] == "open":
                        conn.execute(
                            "UPDATE incidents SET status='resolved', last_observed_scan_id=?, "
                            "last_seen=?, resolved_at=? WHERE id=?",
                            (scan_id, now, now, row["id"]),
                        )
                        conn.execute(
                            "INSERT INTO incident_events (incident_id, scan_id, ts, kind, payload) "
                            "VALUES (?, ?, ?, 'resolved', '{}')", (row["id"], scan_id, now),
                        )
                        changed.append(row["id"])
                    else:
                        conn.execute(
                            "UPDATE incidents SET last_observed_scan_id=?, last_seen=? WHERE id=?",
                            (scan_id, now, row["id"]),
                        )
                    continue
                if condition == "unknown" and severity is None and (
                    row is None or row["status"] == "resolved"
                ):
                    continue
                payload = json.dumps({"condition": condition, "severity": severity,
                                      "summary": item["summary"]}, sort_keys=True)
                if row is None:
                    incident_id = _new_id()
                    conn.execute(
                        "INSERT INTO incidents (id, fingerprint, fingerprint_version, kind, resource, "
                        "status, condition, severity, summary, details_json, last_observed_scan_id, "
                        "first_seen, last_seen, occurrences) "
                        "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, 1)",
                        (incident_id, item["fingerprint"], item["fingerprint_version"], item["kind"],
                         item["resource"], condition, severity, item["summary"], item["details_json"],
                         scan_id, now, now),
                    )
                    event_kind = "opened"
                else:
                    incident_id = row["id"]
                    event_kind = "reopened" if row["status"] == "resolved" else "observed"
                    conn.execute(
                        "UPDATE incidents SET status='open', condition=?, severity=?, summary=?, "
                        "details_json=?, last_observed_scan_id=?, last_seen=?, resolved_at=NULL, "
                        "occurrences=occurrences+1 WHERE id=?",
                        (condition, severity, item["summary"], item["details_json"], scan_id, now,
                         incident_id),
                    )
                conn.execute(
                    "INSERT INTO incident_events (incident_id, scan_id, ts, kind, payload) "
                    "VALUES (?, ?, ?, ?, ?)", (incident_id, scan_id, now, event_kind, payload),
                )
                changed.append(incident_id)
            return [self._incident_row(conn.execute("SELECT * FROM incidents WHERE id=?", (item,)).fetchone())
                    for item in changed]

    def latest_incident_reconciliation(self, *, timeout: float = 5) -> dict | None:
        with self._connect(timeout=timeout) as conn:
            row = conn.execute(
                "SELECT * FROM incident_reconciliations "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def get_incident(self, incident_id: str, *, timeout: float = 5) -> dict | None:
        with self._connect(timeout=timeout) as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return self._incident_row(row)

    def list_incidents(
        self, status: str | None = None, limit: int = 20, *, timeout: float = 5
    ) -> list[dict]:
        if status not in (None, "open", "resolved"):
            raise ValueError("invalid incident status")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("incident limit must be an integer from 1 to 100")
        with self._connect(timeout=timeout) as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM incidents ORDER BY last_seen DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE status=? ORDER BY last_seen DESC, id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
        return [self._incident_row(row) for row in rows]

    def list_incident_events(self, incident_id: str, *, timeout: float = 5) -> list[dict]:
        with self._connect(timeout=timeout) as conn:
            rows = conn.execute(
                "SELECT * FROM incident_events WHERE incident_id=? ORDER BY id", (incident_id,)
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def get_incident_proposal(self, approval_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM incident_proposals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_incident_proposals(self, incident_id: str) -> list[dict]:
        return self.list_incident_proposals_batch([incident_id]).get(incident_id, [])

    def list_incident_proposals_batch(
        self, incident_ids: list[str], *, timeout: float = 5
    ) -> dict[str, list[dict]]:
        """Return linked approvals with one bounded transaction and lazy expiry."""
        if (not isinstance(incident_ids, list) or len(incident_ids) > 100
                or any(not isinstance(item, str) or not item for item in incident_ids)):
            raise ValueError("invalid incident IDs")
        unique_ids = list(dict.fromkeys(incident_ids))
        result = {incident_id: [] for incident_id in unique_ids}
        if not unique_ids:
            return result
        placeholders = ",".join("?" for _ in unique_ids)
        with self._write(timeout=timeout) as conn:
            self._expire_stale(conn, self._clock())
            rows = conn.execute(
                "SELECT ip.incident_id, ip.scan_id, a.* FROM incident_proposals ip "
                f"JOIN approvals a ON a.id=ip.approval_id WHERE ip.incident_id IN ({placeholders}) "
                "ORDER BY a.created_at DESC, a.id DESC", unique_ids,
            ).fetchall()
        for row in rows:
            result[row["incident_id"]].append(dict(row))
        return result

    # Chat quota reservations and ticket transitions share the normal write transaction.
    @staticmethod
    def _chat_row(row):
        if row is None:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        result["cited"] = json.loads(result.pop("cited_json"))
        return result

    def create_chat_ticket(self, *, operator, submission_id, question) -> tuple[dict, bool]:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM chat_tickets WHERE operator=? AND submission_id=?",
                               (operator, submission_id)).fetchone()
            if row is not None:
                return self._chat_row(row), False
            ticket_id = _new_id()
            conn.execute("INSERT INTO chat_tickets (id, operator, submission_id, question, status, created_at) "
                         "VALUES (?, ?, ?, ?, 'queued', ?)",
                         (ticket_id, operator, submission_id, question, self._clock()))
            self._event(conn, "chat.queued", ticket_id=ticket_id)
            return self._chat_row(conn.execute("SELECT * FROM chat_tickets WHERE id=?",
                                               (ticket_id,)).fetchone()), True

    def get_chat_ticket(self, ticket_id) -> dict | None:
        with self._connect() as conn:
            return self._chat_row(conn.execute("SELECT * FROM chat_tickets WHERE id=?",
                                               (ticket_id,)).fetchone())

    def set_chat_ticket_running(self, ticket_id):
        with self._write() as conn:
            conn.execute("UPDATE chat_tickets SET status='running' WHERE id=? AND status='queued'",
                         (ticket_id,))

    def finish_chat_ticket(self, ticket_id, *, status, outcome=None, answer=None,
                           evidence=None, cited=None, approval_id=None, error=None):
        if status not in ('done', 'failed', 'interrupted'):
            raise ValueError("Invalid terminal chat status")
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE chat_tickets SET status=?, outcome=?, answer=?, evidence_json=?, cited_json=?, "
                "approval_id=?, error=?, finished_at=? WHERE id=? AND status IN ('queued','running')",
                (status, outcome, answer, json.dumps(evidence or []), json.dumps(cited or []),
                 approval_id, error, self._clock(), ticket_id),
            ).rowcount
            if changed:
                self._event(conn, "chat.finished", ticket_id=ticket_id, approval_id=approval_id, status=status)

    def reserve_llm_call(self, *, ticket_id, limit, day_start) -> bool:
        with self._write() as conn:
            used = conn.execute("SELECT COUNT(*) FROM events WHERE kind='chat.llm_call' AND ts>=?",
                                (day_start,)).fetchone()[0]
            if used >= limit:
                return False
            changed = conn.execute("UPDATE chat_tickets SET llm_calls=llm_calls+1 WHERE id=?",
                                   (ticket_id,)).rowcount
            if not changed:
                raise ValueError("Unknown chat ticket")
            self._event(conn, "chat.llm_call", ticket_id=ticket_id)
            return True

    def chat_quota(self, *, limit, day_start) -> dict:
        with self._connect() as conn:
            used = conn.execute("SELECT COUNT(*) FROM events WHERE kind='chat.llm_call' AND ts>=?",
                                (day_start,)).fetchone()[0]
            return {"used": used, "limit": limit}

    def interrupt_chat_tickets(self, reason) -> int:
        with self._write() as conn:
            return conn.execute("UPDATE chat_tickets SET status='interrupted', error=?, finished_at=? "
                                "WHERE status IN ('queued','running')", (reason, self._clock())).rowcount

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
        incident_id: str | None = None,
        incident_scan_id: str | None = None,
        timeout: float = 5,
    ) -> tuple[dict, bool]:
        """Return (approval row, created). A live proposal for the same (action, target)
        is returned as-is instead of creating a second one. Lazy expiry, lookup and
        insert happen in one BEGIN IMMEDIATE transaction."""
        now = self._clock()
        if (incident_id is None) != (incident_scan_id is None):
            raise ValueError("incident_id and incident_scan_id must be supplied together")
        with self._write(timeout=timeout) as conn:
            self._expire_stale(conn, now)
            if incident_id is not None:
                latest = conn.execute(
                    "SELECT scan_id FROM incident_reconciliations ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                incident = conn.execute(
                    "SELECT status, condition, last_observed_scan_id FROM incidents WHERE id=?",
                    (incident_id,),
                ).fetchone()
                if (
                    latest is None
                    or latest["scan_id"] != incident_scan_id
                    or incident is None
                    or incident["status"] != "open"
                    or incident["condition"] != "failing"
                    or incident["last_observed_scan_id"] != incident_scan_id
                ):
                    raise IncidentSourceError("incident source is no longer current")
            row = conn.execute(
                "SELECT * FROM approvals WHERE action = ? AND target_key = ? AND status = 'pending'",
                (action, target_key),
            ).fetchone()
            if row is not None:
                if incident_id is not None:
                    linked = conn.execute(
                        "SELECT incident_id, scan_id FROM incident_proposals WHERE approval_id=?",
                        (row["id"],),
                    ).fetchone()
                    if linked is None or (linked["incident_id"], linked["scan_id"]) != (
                        incident_id, incident_scan_id
                    ):
                        raise IncidentSourceError(
                            "another pending proposal already exists for this target"
                        )
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
            if incident_id is not None:
                conn.execute(
                    "INSERT INTO incident_proposals (approval_id, incident_id, scan_id) "
                    "VALUES (?, ?, ?)", (approval_id, incident_id, incident_scan_id),
                )
            created = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return dict(created), True

    def refuse_stale_incident_approval(
        self, approval_id: str, *, attempted_by: str, arrived_at: float, reason: str
    ) -> bool:
        """Deny a still-pending incident approval and record the refusal atomically."""
        with self._write() as conn:
            linked = conn.execute(
                "SELECT 1 FROM incident_proposals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if linked is None:
                return False
            cur = conn.execute(
                "UPDATE approvals SET status='denied', decided_by='policy', decided_at=? "
                "WHERE id=? AND status='pending' AND expires_at>?",
                (self._clock(), approval_id, arrived_at),
            )
            if cur.rowcount != 1:
                return False
            self._event(conn, "approval.denied", approval_id=approval_id, decided_by="policy")
            self._event(
                conn, "approval.refused_stale_incident", approval_id=approval_id,
                reason=reason, attempted_by=attempted_by,
            )
            return True

    def get_approval(self, approval_id: str, as_of: float | None = None, *, timeout: float = 5) -> dict | None:
        """Look up an approval, marking it expired first if its TTL has passed as of
        `as_of` (the request's arrival time), defaulting to now."""
        with self._write(timeout=timeout) as conn:
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

    def list_recent_approvals(self, limit: int, *, timeout: float = 5) -> list[dict]:
        """Resolved approvals after lazy expiry, with their latest execution."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 to 20")
        with self._write(timeout=timeout) as conn:
            self._expire_stale(conn, self._clock())
            rows = conn.execute(
                "SELECT a.*, e.id AS execution_id, e.status AS execution_status, "
                "e.started_at, e.finished_at, e.reason FROM approvals a "
                "LEFT JOIN executions e ON e.id = (SELECT id FROM executions "
                "WHERE approval_id = a.id ORDER BY started_at DESC, id DESC LIMIT 1) "
                "WHERE a.status != 'pending' "
                "ORDER BY COALESCE(a.decided_at, a.expires_at, a.created_at) DESC, a.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            execution = {"id": item.pop("execution_id"), "status": item.pop("execution_status"),
                         **{key: item.pop(key) for key in ("started_at", "finished_at", "reason")}}
            item["execution"] = execution if execution["id"] is not None else None
            result.append(item)
        return result

    def list_executions(self, approval_id: str, *, timeout: float = 5) -> list[dict]:
        with self._connect(timeout=timeout) as conn:
            rows = conn.execute(
                "SELECT id, status, started_at, finished_at, reason FROM executions "
                "WHERE approval_id = ? ORDER BY started_at DESC, id DESC", (approval_id,),
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
                "SELECT a.*, ip.approval_id AS incident_approval_id FROM approvals a "
                "LEFT JOIN incident_proposals ip ON ip.approval_id=a.id "
                "WHERE a.action = ? AND a.target_key = ? AND a.status = 'pending'",
                (action, target_key),
            ).fetchone()
            # Adopt a live card only when it names the target the operator just confirmed. A card
            # whose container has since changed would make the worker refuse the restart as
            # "container changed since approval" (Codex review, T14); that card stays pending and
            # keeps its own refusal if tapped.
            adopted = (
                pending is not None and pending["incident_approval_id"] is None
                and json.loads(pending["target_json"]) == target
            )
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
    def recent_attempts(
        self, action: str, target_key: str, since: float, *, timeout: float = 5
    ) -> list[float]:
        """Execution starts for this action and target, including operator executions."""
        with self._connect(timeout=timeout) as conn:
            rows = conn.execute(
                "SELECT e.started_at FROM executions e "
                "JOIN approvals a ON a.id = e.approval_id "
                "WHERE a.action = ? AND a.target_key = ? AND e.started_at >= ? "
                "ORDER BY e.started_at",
                (action, target_key, since),
            ).fetchall()
        return [row["started_at"] for row in rows]

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
