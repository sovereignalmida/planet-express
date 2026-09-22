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

SCHEMA_VERSION = 5
EVENT_RETENTION_SECONDS = 90 * 24 * 3600
AUTH_MAX_FAILURES = 3
AUTH_LOCK_SECONDS = 900
DEFAULT_TTL_SECONDS = 3600  # design v20: an approval card expires after 60 minutes

# Kept as the v4 schema because databases at versions 0-3 are first brought to the
# old idempotent baseline and then migrated through the explicit v4 -> v5 step.
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

_EXECUTIONS_V5 = """
CREATE TABLE executions (
    id                  TEXT PRIMARY KEY,
    approval_id         TEXT NOT NULL REFERENCES approvals (id),
    kind                TEXT NOT NULL DEFAULT 'run' CHECK (kind IN ('run', 'rollback')),
    parent_execution_id TEXT REFERENCES executions (id),
    status              TEXT NOT NULL CHECK (status IN ('running', 'verifying', 'passed', 'failed',
                        'interrupted', 'aborted', 'rolled_back', 'rollback_failed')),
    started_at          REAL NOT NULL,
    finished_at         REAL,
    reason              TEXT,
    abort_requested_at  REAL,
    CHECK ((kind = 'run') = (parent_execution_id IS NULL))
);
CREATE UNIQUE INDEX executions_one_active_rollback ON executions (parent_execution_id)
    WHERE kind = 'rollback' AND status IN ('running', 'verifying');
"""

_RUNBOOK_TABLES_V5 = """
CREATE TABLE execution_steps (
    execution_id   TEXT NOT NULL REFERENCES executions (id),
    n              INTEGER NOT NULL CHECK (n >= 1),
    type           TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    binding_json   TEXT NOT NULL,
    pre_state_json TEXT,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'passed', 'failed',
                   'skipped', 'aborted')),
    effect         TEXT CHECK (effect IN ('applied', 'not_applied', 'unknown')),
    output_json    TEXT,
    reason         TEXT,
    started_at     REAL,
    finished_at    REAL,
    PRIMARY KEY (execution_id, n)
);

CREATE TABLE attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions (id),
    step_n       INTEGER NOT NULL,
    step_type    TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('reserved', 'consumed', 'released')),
    reserved_at  REAL NOT NULL,
    settled_at   REAL,
    UNIQUE (execution_id, step_n)
);
CREATE INDEX attempts_pair_time ON attempts (step_type, target_key, reserved_at);

CREATE TABLE rollback_candidates (
    execution_id    TEXT NOT NULL REFERENCES executions (id),
    step_n          INTEGER NOT NULL,
    stack           TEXT NOT NULL,
    service         TEXT NOT NULL,
    image_reference TEXT NOT NULL,
    old_image_id    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    closed_at       REAL,
    PRIMARY KEY (execution_id, step_n)
);
"""

_SCHEMA_V5 = (
    _SCHEMA.replace(
        "    message_id    INTEGER\n);",
        "    message_id    INTEGER,\n"
        "    plan_json     TEXT,\n"
        "    plan_sha256   TEXT,\n"
        "    origin        TEXT\n);",
    ).replace(
        "CREATE TABLE IF NOT EXISTS executions (\n"
        "    id          TEXT PRIMARY KEY,\n"
        "    approval_id TEXT NOT NULL REFERENCES approvals (id),\n"
        "    status      TEXT NOT NULL\n"
        "                CHECK (status IN ('running', 'verifying', 'passed', 'failed', 'interrupted')),\n"
        "    started_at  REAL NOT NULL,\n"
        "    finished_at REAL,\n"
        "    reason      TEXT\n"
        ");",
        _EXECUTIONS_V5.replace("CREATE TABLE executions", "CREATE TABLE IF NOT EXISTS executions")
        .replace("CREATE UNIQUE INDEX executions_one_active_rollback", "CREATE UNIQUE INDEX IF NOT EXISTS executions_one_active_rollback"),
    )
    + _RUNBOOK_TABLES_V5.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
    .replace("CREATE INDEX attempts_pair_time", "CREATE INDEX IF NOT EXISTS attempts_pair_time")
)

TERMINAL_EXECUTION_STATUSES = (
    "passed", "failed", "interrupted", "aborted", "rolled_back", "rollback_failed",
)
_EXECUTION_STATUSES = ("running", "verifying", *TERMINAL_EXECUTION_STATUSES)


def _new_id() -> str:
    # 12 hex chars: short enough for Telegram callback_data ("act_ok:<id>" is 19 bytes of
    # a 64-byte limit), random enough that ids never collide in practice.
    return secrets.token_hex(6)


def _execute_ddl(conn: sqlite3.Connection, script: str) -> None:
    """Execute this module's simple semicolon-delimited DDL without executescript.

    sqlite3.executescript() commits an already-open transaction, so the migration
    must issue each statement itself to remain one atomic BEGIN IMMEDIATE.
    """
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


class PendingPlanConflict(RuntimeError):
    """A pending approval for this (action, target) carries a different approved plan (its target
    drifted since it was proposed). Callers refuse with 'already pending' instead of crashing."""

    def __init__(self, approval_id: str):
        super().__init__(f"a different request is already pending for this target ({approval_id})")
        self.approval_id = approval_id


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

    def init(self) -> dict[str, list[dict]]:
        # A read-only preflight also avoids checkpointing an existing WAL on refusal.
        if self.path.exists():
            with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
                self._check_schema_version(conn)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self._connect() as conn:
            self._check_schema_version(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            if not tables:
                self._create_fresh_v5(conn)
                cutover = {"expired_approvals": [], "interrupted_executions": []}
            else:
                if version < 4:
                    self._create_v4_baseline(conn)
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                cutover = self._migrate_4_to_5(conn) if version == 4 else {
                    "expired_approvals": [], "interrupted_executions": [],
                }
                if cutover.pop("already", False):
                    version = SCHEMA_VERSION
                if version == 5:
                    # Preserve the old idempotent repair behaviour for a v5 database.
                    conn.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA_V5}\nCOMMIT;")
            conn.execute("PRAGMA journal_mode = WAL")
        return cutover

    @staticmethod
    def _create_v4_baseline(conn: sqlite3.Connection) -> None:
        """Bring a pre-v4 database to the v4 baseline. Re-reads the version under the write lock and
        never stamps it down: a concurrent initializer may already have migrated to v5 (Codex
        review, T38)."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] >= 4:
                conn.execute("ROLLBACK")
                return
            _execute_ddl(conn, _SCHEMA)
            conn.execute("PRAGMA user_version = 4")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _create_fresh_v5(conn: sqlite3.Connection) -> None:
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{_SCHEMA_V5}\n"
                f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _migrate_4_to_5(self, conn: sqlite3.Connection) -> dict[str, list[dict]]:
        """Transactional v4 -> v5 rebuild and live-row cutover."""
        conn.execute("PRAGMA foreign_keys = OFF")
        now = self._clock()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Re-read under the write lock: another process may have migrated between our first
            # read and acquiring it; it must then take the idempotent v5 path (Codex review, T38).
            if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                conn.execute("ROLLBACK")
                return {"expired_approvals": [], "interrupted_executions": [], "already": True}
            interrupted_rows = conn.execute(
                "SELECT e.id, e.approval_id, a.action, a.target_json, a.message_id "
                "FROM executions e JOIN approvals a ON a.id=e.approval_id "
                "WHERE e.status IN ('running','verifying') ORDER BY e.started_at, e.id"
            ).fetchall()
            expired_rows = conn.execute(
                "SELECT id, action, target_json, message_id FROM approvals "
                "WHERE status='pending' ORDER BY created_at, id"
            ).fetchall()

            interruption_reason = "interrupted by the v5 upgrade"
            expiry_reason = "superseded by the v5 upgrade; propose it again"
            for row in interrupted_rows:
                conn.execute(
                    "UPDATE executions SET status='interrupted', reason=?, finished_at=? WHERE id=?",
                    (interruption_reason, now, row["id"]),
                )
                self._event(
                    conn, "execution.interrupted", approval_id=row["approval_id"],
                    execution_id=row["id"], reason=interruption_reason,
                )
            for row in expired_rows:
                conn.execute(
                    "UPDATE approvals SET status='expired' WHERE id=?",
                    (row["id"],),
                )
                self._event(conn, "approval.expired", approval_id=row["id"], reason=expiry_reason)

            execution_table_ddl, execution_index_ddl = _EXECUTIONS_V5.split(
                "CREATE UNIQUE INDEX", 1
            )
            conn.execute(
                execution_table_ddl.replace("executions (", "executions_new (", 1).strip()
            )
            conn.execute(
                "INSERT INTO executions_new "
                "(id, approval_id, kind, parent_execution_id, status, started_at, finished_at, reason) "
                "SELECT id, approval_id, 'run', NULL, status, started_at, finished_at, reason "
                "FROM executions"
            )
            old_count = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            new_count = conn.execute("SELECT COUNT(*) FROM executions_new").fetchone()[0]
            if old_count != new_count:
                raise RuntimeError("v5 migration execution copy count mismatch")
            conn.execute("DROP TABLE executions")
            conn.execute("ALTER TABLE executions_new RENAME TO executions")
            conn.execute(("CREATE UNIQUE INDEX" + execution_index_ddl).strip())

            conn.execute("ALTER TABLE approvals ADD COLUMN plan_json TEXT")
            conn.execute("ALTER TABLE approvals ADD COLUMN plan_sha256 TEXT")
            conn.execute("ALTER TABLE approvals ADD COLUMN origin TEXT")
            _execute_ddl(conn, _RUNBOOK_TABLES_V5)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"foreign key check failed: {len(violations)} row(s)")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        return {
            "expired_approvals": [
                dict(row) | {"target": json.loads(row["target_json"]), "reason": expiry_reason}
                for row in expired_rows
            ],
            "interrupted_executions": [
                dict(row) | {
                    "target": json.loads(row["target_json"]), "reason": interruption_reason,
                }
                for row in interrupted_rows
            ],
        }

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
        plan_json: str | None = None,
        plan_sha256: str | None = None,
        origin: str | None = None,
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
                if (
                    (plan_json is None) != (row["plan_json"] is None)
                    or plan_json is not None and (
                        row["plan_json"] != plan_json
                        or row["plan_sha256"] != plan_sha256
                        or row["origin"] != origin
                    )
                ):
                    raise PendingPlanConflict(row["id"])
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
                "requested_via, requested_by, created_at, expires_at, plan_json, plan_sha256, origin) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, action, target_key, json.dumps(target, sort_keys=True), risk,
                 requested_via, requested_by, now, now + ttl_seconds,
                 plan_json, plan_sha256, origin),
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

    def propose_runbook(
        self, *, action: str, target_key: str, target: dict, risk: str,
        requested_via: str, plan_json: str, plan_sha256: str, origin: str,
        requested_by: str | None = None, ttl_seconds: float = DEFAULT_TTL_SECONDS,
        incident_id: str | None = None, incident_scan_id: str | None = None,
        timeout: float = 5,
    ) -> tuple[dict, bool]:
        if not all(isinstance(value, str) and value for value in (plan_json, plan_sha256, origin)):
            raise ValueError("runbook plan, hash and origin are required")
        return self.propose(
            action=action, target_key=target_key, target=target, risk=risk,
            requested_via=requested_via, requested_by=requested_by, ttl_seconds=ttl_seconds,
            incident_id=incident_id, incident_scan_id=incident_scan_id, timeout=timeout,
            plan_json=plan_json, plan_sha256=plan_sha256, origin=origin,
        )

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
        operator: str, arrived_at: float, origin: str = "dashboard-direct",
        deadline: float | None = None,
        plan_json: str | None = None, plan_sha256: str | None = None,
        runbook_origin: str | None = None,
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
                and (
                    plan_json is None and pending["plan_json"] is None
                    or plan_json is not None and (
                        # Same approved plan is what matters; the origin necessarily differs
                        # (a telegram card adopted by a *-direct request) (Codex review, T38).
                        pending["plan_json"] == plan_json
                        and pending["plan_sha256"] == plan_sha256
                    )
                )
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
                    "requested_via, requested_by, created_at, expires_at, decided_by, decided_at, "
                    "plan_json, plan_sha256, origin) "
                    "VALUES (?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (approval_id, action, target_key, json.dumps(target, sort_keys=True), risk,
                     origin, operator, now, now, operator, now,
                     plan_json, plan_sha256, runbook_origin),
                )
                self._event(conn, "proposal.created", approval_id=approval_id, action=action,
                            target=target, risk=risk, requested_via=origin, requested_by=operator)
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

    def create_direct_runbook_execution(
        self, *, action: str, target_key: str, target: dict, risk: str,
        operator: str, arrived_at: float, plan_json: str, plan_sha256: str,
        origin: str, deadline: float | None = None,
    ) -> dict | None:
        if not all(isinstance(value, str) and value for value in (plan_json, plan_sha256, origin)):
            raise ValueError("runbook plan, hash and origin are required")
        return self.create_direct_execution(
            action=action, target_key=target_key, target=target, risk=risk,
            operator=operator, arrived_at=arrived_at, origin=origin, deadline=deadline,
            plan_json=plan_json, plan_sha256=plan_sha256, runbook_origin=origin,
        )

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
                "SELECT e.id, e.approval_id, a.action, a.target_json, a.plan_json, a.message_id "
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

    # ── runbook execution state (schema v5; engine lands in T39) ───────────
    def create_steps(self, execution_id: str, steps) -> None:
        rows = []
        for n, step in enumerate(steps, start=1):
            if hasattr(step, "model_dump"):
                step = step.model_dump(mode="json")
            if not isinstance(step, dict) or set(step) != {"type", "params", "binding"}:
                raise ValueError("invalid execution step")
            rows.append((
                execution_id, n, step["type"],
                json.dumps(step["params"], sort_keys=True, separators=(",", ":")),
                json.dumps(step["binding"], sort_keys=True, separators=(",", ":")),
            ))
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO execution_steps "
                "(execution_id,n,type,params_json,binding_json,status) "
                "VALUES (?,?,?,?,?,'pending')",
                rows,
            )

    @staticmethod
    def _step_row(row) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        for source, target in (
            ("params_json", "params"), ("binding_json", "binding"),
            ("pre_state_json", "pre_state"), ("output_json", "output"),
        ):
            value = item.pop(source)
            item[target] = json.loads(value) if value is not None else None
        return item

    def unfinished_steps(self) -> list[dict]:
        """Pending/dispatched steps of executions that are still running or verifying (startup)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.* FROM execution_steps s JOIN executions e ON e.id = s.execution_id "
                "WHERE e.status IN ('running','verifying') AND s.status IN ('pending','dispatched') "
                "ORDER BY s.execution_id, s.n"
            ).fetchall()
        return [self._step_row(row) for row in rows]

    def list_steps(self, execution_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_steps WHERE execution_id=? ORDER BY n", (execution_id,)
            ).fetchall()
        return [self._step_row(row) for row in rows]

    def set_step_pre_state(self, execution_id: str, n: int, pre_state) -> None:
        encoded = json.dumps(pre_state, sort_keys=True, separators=(",", ":"))
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE execution_steps SET pre_state_json=? "
                "WHERE execution_id=? AND n=? AND status='pending' AND pre_state_json IS NULL",
                (encoded, execution_id, n),
            ).rowcount
            if changed != 1:
                raise ValueError("step is not pending or pre-state is already set")

    def mark_step_dispatched(self, execution_id: str, n: int) -> None:
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE execution_steps SET status='dispatched', started_at=? "
                "WHERE execution_id=? AND n=? AND status='pending'",
                (self._clock(), execution_id, n),
            ).rowcount
            if changed != 1:
                raise ValueError("step cannot transition to dispatched")

    def finish_step(
        self, execution_id: str, n: int, *, status: str, effect: str | None = None,
        output=None, reason: str | None = None,
    ) -> None:
        if status not in {"passed", "failed", "skipped", "aborted"}:
            raise ValueError("invalid terminal step status")
        if effect not in {None, "applied", "not_applied", "unknown"}:
            raise ValueError("invalid step effect")
        # A step refused before dispatch (binding drift, bad reference) is `failed` straight from
        # `pending`, and by construction changed nothing (own review, T38).
        if status == "failed" and effect == "not_applied":
            allowed_from = ("dispatched", "pending")
        else:
            allowed_from = {"passed": ("dispatched",), "failed": ("dispatched",),
                            "skipped": ("pending",), "aborted": ("pending",)}[status]
        encoded = None if output is None else json.dumps(
            output, sort_keys=True, separators=(",", ":")
        )
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE execution_steps SET status=?, effect=?, output_json=?, reason=?, "
                "finished_at=? WHERE execution_id=? AND n=? AND status IN "
                f"({','.join('?' for _ in allowed_from)})",
                (status, effect, encoded, reason, self._clock(), execution_id, n, *allowed_from),
            ).rowcount
            if changed != 1:
                raise ValueError(f"step cannot transition from its current state to {status}")

    def _normalize_attempt_pairs(self, conn, execution_id: str, pairs) -> list[tuple[int, str, str]]:
        supplied = list(pairs)
        if all(isinstance(pair, (tuple, list)) and len(pair) == 3 for pair in supplied):
            stored = {
                row["n"]: row["type"] for row in conn.execute(
                    "SELECT n,type FROM execution_steps WHERE execution_id=?", (execution_id,)
                )
            }
            result = []
            for n, step_type, target in supplied:
                # Explicit step numbers must name a stored step of that type (Codex review, T38).
                if type(n) is not int or stored.get(n) != step_type:
                    raise ValueError(f"attempt ({n}, {step_type!r}) does not match a stored step")
                result.append((n, str(step_type), str(target)))
            return result
        if not all(isinstance(pair, (tuple, list)) and len(pair) == 2 for pair in supplied):
            raise ValueError("attempt pairs must contain (type,target) or (n,type,target)")
        steps = conn.execute(
            "SELECT n,type FROM execution_steps WHERE execution_id=? ORDER BY n", (execution_id,)
        ).fetchall()
        result = []
        position = 0
        for step_type, target in supplied:
            while position < len(steps) and steps[position]["type"] != step_type:
                position += 1
            if position >= len(steps):
                # Never guess a step number: a wrong one breaks consume_attempt later or collides on
                # UNIQUE(execution_id, step_n) (own review, T38). Create the steps first.
                raise ValueError(f"no stored step of type {step_type!r} for this attempt pair")
            n = steps[position]["n"]
            position += 1
            result.append((n, str(step_type), str(target)))
        return result

    def reserve_runbook_attempts(
        self, execution_id: str, pairs, *, window_start: float, cooldown_start: float,
        max_per_day: int, now: float,
    ) -> str | None:
        """Reserve the full runbook multiplicity atomically, or insert nothing."""
        if type(max_per_day) is not int or max_per_day < 1:
            raise ValueError("max_per_day must be positive")
        with self._write() as conn:
            normalized = self._normalize_attempt_pairs(conn, execution_id, pairs)
            multiplicity: dict[tuple[str, str], int] = {}
            for _n, step_type, target in normalized:
                multiplicity[(step_type, target)] = multiplicity.get((step_type, target), 0) + 1
            for (step_type, target), requested in multiplicity.items():
                recent = conn.execute(
                    "SELECT reserved_at FROM attempts WHERE step_type=? AND target_key=? "
                    "AND state IN ('reserved','consumed') AND reserved_at>=?",
                    (step_type, target, min(window_start, cooldown_start)),
                ).fetchall()
                cooldown = [row[0] for row in recent if row[0] > cooldown_start]
                if cooldown:
                    minutes = int((now - max(cooldown)) // 60)
                    return f"cooling down: last attempt {minutes}m ago"
                daily = sum(row[0] >= window_start for row in recent)
                if daily + requested > max_per_day:
                    return (
                        f"attempt cap reached: {daily} in 24h plus {requested} requested "
                        f"(max {max_per_day})"
                    )
            conn.executemany(
                "INSERT INTO attempts "
                "(execution_id,step_n,step_type,target_key,state,reserved_at) "
                "VALUES (?,?,?,?,'reserved',?)",
                [(execution_id, n, step_type, target, now)
                 for n, step_type, target in normalized],
            )
        return None

    def consume_attempt(self, execution_id: str, step_n: int) -> None:
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE attempts SET state='consumed', settled_at=? "
                "WHERE execution_id=? AND step_n=? AND state='reserved'",
                (self._clock(), execution_id, step_n),
            ).rowcount
            if changed != 1:
                raise ValueError("attempt is not reserved")

    def release_attempts(self, execution_id: str, step_ns) -> int:
        step_ns = list(step_ns)
        if not step_ns:
            return 0
        placeholders = ",".join("?" for _ in step_ns)
        with self._write() as conn:
            changed = conn.execute(
                f"UPDATE attempts SET state='released', settled_at=? WHERE execution_id=? "
                f"AND step_n IN ({placeholders}) AND state='reserved'",
                (self._clock(), execution_id, *step_ns),
            ).rowcount
        return changed

    def reconcile_reserved_attempts(self) -> dict[str, int]:
        now = self._clock()
        with self._write() as conn:
            consumed = conn.execute(
                "UPDATE attempts SET state='consumed', settled_at=? WHERE state='reserved' AND "
                "EXISTS (SELECT 1 FROM execution_steps s WHERE s.execution_id=attempts.execution_id "
                "AND s.n=attempts.step_n AND (s.status='dispatched' OR s.effect='unknown'))",
                (now,),
            ).rowcount
            released = conn.execute(
                "UPDATE attempts SET state='released', settled_at=? WHERE state='reserved'",
                (now,),
            ).rowcount
        return {"consumed": consumed, "released": released}

    def open_rollback_candidate(
        self, execution_id: str, step_n: int, *, stack: str, service: str,
        image_reference: str, old_image_id: str, expires_at: float,
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO rollback_candidates "
                "(execution_id,step_n,stack,service,image_reference,old_image_id,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (execution_id, step_n, stack, service, image_reference, old_image_id,
                 self._clock(), expires_at),
            )

    def close_rollback_candidate(self, execution_id: str, step_n: int) -> bool:
        with self._write() as conn:
            return conn.execute(
                "UPDATE rollback_candidates SET closed_at=? "
                "WHERE execution_id=? AND step_n=? AND closed_at IS NULL",
                (self._clock(), execution_id, step_n),
            ).rowcount == 1

    def any_open_rollback_candidate(self, now: float) -> bool:
        # Deliberately do not catch sqlite/read errors: prune callers must fail closed.
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM rollback_candidates WHERE closed_at IS NULL AND expires_at>? LIMIT 1",
                (now,),
            ).fetchone() is not None

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
