"""The only writer of mission_events. Insert-only: this module has no update or delete, and the
body is redacted before it reaches the table (contract §2, §14)."""

import json
import sqlite3
from typing import Any

from planet_express.core.redact import REDACTED, redact

MAX_BODY_BYTES = 64 * 1024


def _is_secret_key(key: str) -> bool:
    # redact() decides what a sensitive key looks like; ask it rather than keep a second list.
    probe = f"{key}=x"
    return redact(probe) != probe


def redact_value(value: Any) -> Any:
    """Redact every string leaf, and every value stored under a secret-looking key."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key = redact(str(key))
            out[key] = REDACTED if _is_secret_key(key) and item is not None else redact_value(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def insert_event(conn: sqlite3.Connection, mission_id: str, kind: str, actor: str,
                 body: dict[str, Any], now: int) -> int:
    text = json.dumps(redact_value(body), sort_keys=True, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        text = json.dumps({"truncated": True, "bytes": len(text)})
    cur = conn.execute(
        "INSERT INTO mission_events (mission_id, at, kind, actor, body_json, redaction_applied) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (mission_id, now, kind, actor, text),
    )
    return int(cur.lastrowid)
