"""Shared helpers for tests/test_memory_*.py. Standalone sqlite only: no PE state, no network."""

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CASA_CONFIG", str(ROOT / "config.example.yaml"))

from planet_express.memory import schema, store
from planet_express.memory.models import Card

FIXTURES = ROOT / "tests" / "fixtures" / "v4" / "memory"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
OPERATOR = "operator:alice"
RUNTIME = "runtime:session-1"
SYSTEM = "system:scan"


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    schema.init_schema(conn)
    return conn


def make_card(card_id="card-one", **overrides) -> Card:
    data = {
        "id": card_id,
        "title": "A title",
        "type": "fact",
        "scope": ["stack:media"],
        "statement": "A short statement about the media stack.",
        "source": "operator-stated",
        "created_at": NOW,
    }
    if data.get("type") == "procedure" or overrides.get("type") == "procedure":
        data["steps"] = [{"text": "Do the thing.", "check": "The thing is done."}]
    data.update(overrides)
    return Card(**data)


def propose(conn, card_id="card-one", actor=OPERATOR, **overrides):
    card = make_card(card_id, **overrides)
    store.upsert_candidate(conn, card, actor, now=NOW)
    return card


def confirmed(conn, card_id="card-one", **overrides):
    propose(conn, card_id, **overrides)
    return store.confirm(conn, card_id, OPERATOR, now=NOW)
