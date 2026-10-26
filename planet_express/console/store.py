"""The conversation record: PE's own copy of every operator message and every agent reply, and the per-conversation counters.

The agent's own transcript (inside its state directory) is a convenience for `--resume`, never the record: if it is lost, the
thread is rebuilt from here. Everything stored is redacted first and bounded. Nothing in this module starts, stops or
authorises anything; the supervisor does that, through the mission the conversation is bound to.
"""

import secrets
from dataclasses import dataclass
from typing import Literal

from planet_express.core.redact import redact
from planet_express.core.store import Store

State = Literal["open", "archiving", "archived"]
Role = Literal["operator", "agent", "system"]
MAX_MESSAGE_CHARS = 32_000
MAX_TITLE_CHARS = 120
TRUNCATED = "\n[message truncated]"


class ConversationNotFound(LookupError):
    pass


class ConversationClosed(RuntimeError):
    """The conversation is not open: it takes no more messages."""


@dataclass(frozen=True)
class Conversation:
    id: str
    mission_id: str
    title: str
    origin: str
    state: str
    agent_session_id: str | None
    uid: int | None
    token_budget: int
    tokens_used: int
    turns: int
    created_at: int
    updated_at: int
    last_activity_at: int
    archived_at: int | None

    @property
    def state_dir(self) -> str:
        """The name of the conversation's state directory beside the session workspaces (the launcher's `C-<id>`)."""
        return f"C-{self.id}"

    @property
    def tokens_left(self) -> int:
        return max(self.token_budget - self.tokens_used, 0)


@dataclass(frozen=True)
class Message:
    seq: int
    conversation_id: str
    at: int
    role: str
    actor: str | None
    turn: int | None
    text: str
    consumed_turn: int | None


def clean_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Redacted, and cut to the limit on a character boundary with a visible marker."""
    text = redact(str(text))
    return text if len(text) <= limit else text[:limit - len(TRUNCATED)] + TRUNCATED


class ConsoleStore:
    def __init__(self, store: Store):
        self.store = store

    def _now(self) -> int:
        return int(self.store._clock())

    # ── conversations ──────────────────────────────────────────────────────────────────────
    def create(self, mission_id: str, *, title: str, origin: str, token_budget: int, uid: int | None = None) -> Conversation:
        if token_budget <= 0:
            raise ValueError("a conversation needs a positive token budget")
        now = self._now()
        conversation_id = secrets.token_hex(8)
        with self.store._write() as conn:
            conn.execute(
                "INSERT INTO console_conversations (id, mission_id, title, origin, state, uid, token_budget, created_at, "
                "updated_at, last_activity_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (conversation_id, mission_id, clean_text(title.strip().splitlines()[0] if title.strip() else "New conversation",
                                                         MAX_TITLE_CHARS), origin, uid, token_budget, now, now, now))
        return self.get(conversation_id)

    def get(self, conversation_id: str) -> Conversation:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise ConversationNotFound(conversation_id)
        return Conversation(**dict(row))

    def by_mission(self, mission_id: str) -> Conversation | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_conversations WHERE mission_id=?", (mission_id,)).fetchone()
        return Conversation(**dict(row)) if row else None

    def conversations(self, *, states: tuple[str, ...] = ("open", "archiving", "archived"), limit: int = 200) -> list[Conversation]:
        marks = ",".join("?" for _ in states)
        with self.store._connect() as conn:
            rows = conn.execute(f"SELECT * FROM console_conversations WHERE state IN ({marks}) "
                                "ORDER BY last_activity_at DESC, id LIMIT ?", (*states, limit)).fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def count_open(self) -> int:
        with self.store._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM console_conversations WHERE state='open'").fetchone()[0]

    def idle_since(self, cutoff: int) -> list[Conversation]:
        """Open conversations with no activity since `cutoff` (the archive policy's candidates)."""
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_conversations WHERE state='open' AND last_activity_at < ? "
                                "ORDER BY last_activity_at", (cutoff,)).fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def set_state(self, conversation_id: str, state: State) -> bool:
        """open -> archiving -> archived only; nothing reopens. Returns whether the row moved."""
        order = {"open": 0, "archiving": 1, "archived": 2}
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT state FROM console_conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise ConversationNotFound(conversation_id)
            if order[state] != order[row["state"]] + 1:
                return False
            conn.execute("UPDATE console_conversations SET state=?, updated_at=?, archived_at=? WHERE id=?",
                         (state, now, now if state == "archived" else None, conversation_id))
            if state == "archived":
                conn.execute("UPDATE console_conversations SET uid=NULL WHERE id=?", (conversation_id,))
        return True

    def set_agent_session(self, conversation_id: str, agent_session_id: str) -> None:
        """The id the agent's own stream reported for the turn that just ran. It can change between turns (an interrupt does),
        so it is recorded every turn and never assumed stable."""
        with self.store._write() as conn:
            conn.execute("UPDATE console_conversations SET agent_session_id=?, updated_at=? WHERE id=?",
                         (agent_session_id[:128], self._now(), conversation_id))

    def begin_turn(self, conversation_id: str) -> int:
        """Number the next turn (1, 2, ...). Counted when the turn STARTS, so a turn that crashes still has its number and the next
        one does not reuse it."""
        with self.store._write() as conn:
            conn.execute("UPDATE console_conversations SET turns=turns+1, updated_at=? WHERE id=?", (self._now(), conversation_id))
            row = conn.execute("SELECT turns FROM console_conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise ConversationNotFound(conversation_id)
            return int(row["turns"])

    def add_usage(self, conversation_id: str, tokens: int) -> int:
        """Charge a finished turn. Returns the tokens used in total."""
        with self.store._write() as conn:
            conn.execute("UPDATE console_conversations SET tokens_used=tokens_used+?, updated_at=? WHERE id=?",
                         (max(int(tokens), 0), self._now(), conversation_id))
            row = conn.execute("SELECT tokens_used FROM console_conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise ConversationNotFound(conversation_id)
            return int(row["tokens_used"])

    # ── messages ───────────────────────────────────────────────────────────────────────────
    def add_message(self, conversation_id: str, role: Role, text: str, *, turn: int | None = None,
                    require_open: bool = False, actor: str | None = None) -> int:
        now = self._now()
        with self.store._write() as conn:
            row = conn.execute("SELECT state FROM console_conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise ConversationNotFound(conversation_id)
            if require_open and row["state"] != "open":
                raise ConversationClosed("this conversation is closed: it takes no more messages")
            cur = conn.execute("INSERT INTO console_messages (conversation_id, at, role, actor, turn, text) VALUES (?, ?, ?, ?, ?, ?)",
                               (conversation_id, now, role, actor[:128] if actor else None, turn, clean_text(text)))
            if role == "operator":
                conn.execute("UPDATE console_conversations SET last_activity_at=?, updated_at=? WHERE id=?", (now, now, conversation_id))
            return int(cur.lastrowid)

    def messages(self, conversation_id: str, *, after: int = 0, limit: int = 500) -> list[Message]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_messages WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
                                (conversation_id, after, limit)).fetchall()
        return [Message(**dict(r)) for r in rows]

    def recent_messages(self, conversation_id: str, limit: int = 200) -> list[Message]:
        """The newest `limit` messages, oldest first (what a first load of a long conversation should show)."""
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
                                (conversation_id, limit)).fetchall()
        return [Message(**dict(r)) for r in reversed(rows)]

    def last_system_text(self, conversation_id: str) -> str | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT text FROM console_messages WHERE conversation_id=? AND role='system' ORDER BY seq DESC LIMIT 1",
                               (conversation_id,)).fetchone()
        return row[0] if row else None

    def pending(self, conversation_id: str) -> Message | None:
        """The oldest operator message no turn has taken yet."""
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_messages WHERE conversation_id=? AND role='operator' "
                               "AND consumed_turn IS NULL ORDER BY seq LIMIT 1", (conversation_id,)).fetchone()
        return Message(**dict(row)) if row else None

    def consume(self, seq: int, turn: int) -> bool:
        """A turn takes the message. Once only: a second turn can never take the same message."""
        with self.store._write() as conn:
            cur = conn.execute("UPDATE console_messages SET consumed_turn=? WHERE seq=? AND consumed_turn IS NULL", (turn, seq))
            return cur.rowcount == 1

    def has_pending(self) -> list[str]:
        """Open conversations with an operator message waiting for a turn."""
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.id FROM console_conversations c JOIN console_messages m ON m.conversation_id=c.id "
                "WHERE c.state='open' AND m.role='operator' AND m.consumed_turn IS NULL ORDER BY c.id").fetchall()
        return [r[0] for r in rows]


    # ── executions: a confirmed plan option and the Repair mission made from it ─────────────────────────
    def add_execution(self, conversation_id: str, plan_id: int, option_index: int, mission_id: str, grant_hash: str) -> int:
        with self.store._write() as conn:
            cur = conn.execute("INSERT INTO console_executions (conversation_id, plan_id, option_index, mission_id, grant_hash, created_at) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (conversation_id, plan_id, option_index, mission_id, grant_hash, self._now()))
            return int(cur.lastrowid)

    def execution(self, execution_id: int) -> dict | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_executions WHERE id=?", (execution_id,)).fetchone()
        return dict(row) if row else None

    def executions(self, conversation_id: str) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_executions WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
        return [dict(r) for r in rows]

    def live_executions(self) -> list[dict]:
        """Authorised executions whose progress is still being reported into their conversation."""
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_executions WHERE status='authorised' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def set_execution_status(self, execution_id: int, status: str, *, by: str | None = None) -> bool:
        """proposed -> authorised | withdrawn; authorised -> ended. Forward only."""
        allowed = {"authorised": ("proposed",), "withdrawn": ("proposed",), "ended": ("authorised",)}
        if status not in allowed:
            raise ValueError("an execution moves to authorised, withdrawn or ended")
        now = self._now()
        marks = ",".join("?" for _ in allowed[status])
        with self.store._write() as conn:
            if status == "authorised":
                cur = conn.execute(f"UPDATE console_executions SET status=?, authorised_at=?, authorised_by=? WHERE id=? AND status IN ({marks})",
                                   (status, now, by, execution_id, *allowed[status]))
            else:
                cur = conn.execute(f"UPDATE console_executions SET status=? WHERE id=? AND status IN ({marks})", (status, execution_id, *allowed[status]))
            return cur.rowcount == 1

    def was_reported(self, execution_id: int, key: str) -> bool:
        with self.store._connect() as conn:
            row = conn.execute("SELECT reported FROM console_executions WHERE id=?", (execution_id,)).fetchone()
        return row is not None and key in (row["reported"].split(",") if row["reported"] else [])

    def mark_reported(self, execution_id: int, key: str) -> bool:
        """Record that a progress line (`key`) was posted into the thread; False if it already had been (so it is posted once)."""
        with self.store._write() as conn:
            row = conn.execute("SELECT reported FROM console_executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                return False
            keys = row["reported"].split(",") if row["reported"] else []
            if key in keys:
                return False
            conn.execute("UPDATE console_executions SET reported=? WHERE id=?", (",".join([*keys, key]), execution_id))
            return True

    # ── lessons: the memory candidates a conversation produced (the cards themselves live in Ship's Memory) ──
    def memory_ready(self) -> bool:
        """Ship's Memory exists on this database (a SQLite build without FTS5 leaves its tables out)."""
        with self.store._connect() as conn:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_cards'").fetchone() is not None

    def link_lesson(self, conn, conversation_id: str, card_id: str) -> None:
        """Record, inside the caller's write, that a card came from this conversation."""
        conn.execute("INSERT OR IGNORE INTO console_lessons (conversation_id, card_id) VALUES (?, ?)", (conversation_id, card_id))

    def add_lesson(self, conversation_id: str, card_id: str) -> bool:
        with self.store._write() as conn:
            return conn.execute("INSERT OR IGNORE INTO console_lessons (conversation_id, card_id) VALUES (?, ?)", (conversation_id, card_id)).rowcount == 1

    def lesson_ids(self, conversation_id: str) -> list[str]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT card_id FROM console_lessons WHERE conversation_id=? ORDER BY rowid", (conversation_id,)).fetchall()
        return [r["card_id"] for r in rows]

    # ── Telegram: which conversations are followed there, and how far they have been sent ──
    def tg_follow(self, conversation_id: str) -> None:
        """Start sending this conversation's answers, plans and progress to Telegram, from now on (what already exists is not replayed)."""
        with self.store._write() as conn:
            conn.execute("INSERT OR IGNORE INTO console_telegram (conversation_id, last_seq, last_plan_id) VALUES (?, "
                         "(SELECT COALESCE(MAX(seq), 0) FROM console_messages WHERE conversation_id=?), "
                         "(SELECT COALESCE(MAX(id), 0) FROM console_plans WHERE conversation_id=?))", (conversation_id, conversation_id, conversation_id))

    def tg_followed(self) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_telegram").fetchall()
        return [dict(r) for r in rows]

    def tg_advance(self, conversation_id: str, last_seq: int, last_plan_id: int) -> None:
        with self.store._write() as conn:
            conn.execute("UPDATE console_telegram SET last_seq=?, last_plan_id=? WHERE conversation_id=?", (last_seq, last_plan_id, conversation_id))

    def tg_unfollow(self, conversation_id: str) -> None:
        with self.store._write() as conn:
            conn.execute("DELETE FROM console_telegram WHERE conversation_id=?", (conversation_id,))

    # ── plans ───────────────────────────────────────────────────────────────────────────────
    def add_plan(self, conversation_id: str, turn: int, plan: dict, plan_hash: str) -> int:
        """Record a validated plan (the caller passes `plan.canonical`, already redacted). A newer plan supersedes the pending one."""
        import json
        now = self._now()
        with self.store._write() as conn:
            if conn.execute("SELECT 1 FROM console_conversations WHERE id=?", (conversation_id,)).fetchone() is None:
                raise ConversationNotFound(conversation_id)
            conn.execute("UPDATE console_plans SET status='superseded' WHERE conversation_id=? AND status='pending'", (conversation_id,))
            cur = conn.execute("INSERT INTO console_plans (conversation_id, turn, plan_json, plan_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                               (conversation_id, turn, json.dumps(plan, sort_keys=True), plan_hash, now))
            return int(cur.lastrowid)

    def plans(self, conversation_id: str) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM console_plans WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
        return [dict(r) for r in rows]

    def current_plan(self, conversation_id: str) -> dict | None:
        """The pending plan, if any."""
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_plans WHERE conversation_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                               (conversation_id,)).fetchone()
        return dict(row) if row else None

    def plan(self, plan_id: int) -> dict | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_plans WHERE id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def latest_plan(self, conversation_id: str) -> dict | None:
        """The newest plan whatever its status (the card shows an expired or scrapped one as such)."""
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM console_plans WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
        return dict(row) if row else None

    def set_plan_status(self, plan_id: int, status: str) -> bool:
        """pending -> scrapped | expired | confirmed (P2 drives the last). A plan never leaves a final status."""
        if status not in ("scrapped", "expired", "confirmed"):
            raise ValueError("a plan moves from pending to scrapped, expired or confirmed")
        with self.store._write() as conn:
            return conn.execute("UPDATE console_plans SET status=? WHERE id=? AND status='pending'", (status, plan_id)).rowcount == 1


__all__ = ["ConsoleStore", "Conversation", "ConversationClosed", "ConversationNotFound", "Message", "clean_text"]
