"""Asynchronous, operator-owned chat tickets with bounded admission and a shared quota."""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

CHAT_DAILY_CALL_LIMIT = 100
MAX_QUESTION_CHARS = 2000
CHAT_WORKERS = 1
CHAT_QUEUE_LIMIT = 3
MAX_ANSWER_CHARS = 4000


def local_day_start(now: float) -> float:
    # Convert back to naive local time before resolving midnight's offset: today's
    # offset can differ from midnight's on a DST transition day.
    local = datetime.fromtimestamp(now).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None).astimezone().timestamp()


@dataclass
class ChatResult:
    status: str = "done"
    outcome: str | None = None
    answer: str | None = None
    evidence: list = field(default_factory=list)
    cited: list = field(default_factory=list)
    approval_id: str | None = None
    error: str | None = None


class ChatService:
    def __init__(self, store, commands, *, run_investigation, clock=time.time, executor=None):
        self._store = store
        self._commands = commands
        self._run = run_investigation
        self._clock = clock
        self._executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=CHAT_WORKERS, thread_name_prefix="chat")
        self._slots = threading.BoundedSemaphore(CHAT_WORKERS + CHAT_QUEUE_LIMIT)

    def ask(self, *, operator, question, submission_id) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
            raise ValueError("Invalid question")
        if not isinstance(submission_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,64}", submission_id) is None:
            raise ValueError("Invalid submission_id")
        row, created = self._store.create_chat_ticket(
            operator=operator, question=question.strip(), submission_id=submission_id)
        ticket_id = row["id"]
        if not created:
            return dict(row, ticket_id=ticket_id)
        if not self._slots.acquire(blocking=False):
            self._store.finish_chat_ticket(ticket_id, status="failed", error="chat is busy, try again shortly")
            return dict(self._store.get_chat_ticket(ticket_id), ticket_id=ticket_id)
        try:
            self._executor.submit(self._work, ticket_id, question.strip(), operator)
        except Exception:  # noqa: BLE001 -- provider/executor errors can expose credentials
            self._slots.release()
            self._store.finish_chat_ticket(ticket_id, status="failed", error="chat could not start")
            return dict(self._store.get_chat_ticket(ticket_id), ticket_id=ticket_id)
        return {"ticket_id": ticket_id, "status": "queued"}

    def _work(self, ticket_id, question, operator):
        try:
            self._store.set_chat_ticket_running(ticket_id)
            result = self._run(question, operator=operator, reserve=lambda: self._store.reserve_llm_call(
                ticket_id=ticket_id, limit=CHAT_DAILY_CALL_LIMIT, day_start=local_day_start(self._clock())))
            self._store.finish_chat_ticket(ticket_id, **asdict(result))
        except Exception:  # noqa: BLE001 -- provider/executor errors can expose credentials
            self._store.finish_chat_ticket(ticket_id, status="failed", error="chat investigation failed")
        finally:
            self._slots.release()

    def get(self, ticket_id, *, operator) -> dict | None:
        row = self._store.get_chat_ticket(ticket_id)
        return row if row is not None and row["operator"] == operator else None

    def quota(self) -> dict:
        start = local_day_start(self._clock())
        tomorrow = datetime.fromtimestamp(start).astimezone().replace(tzinfo=None) + timedelta(days=1)
        return self._store.chat_quota(limit=CHAT_DAILY_CALL_LIMIT, day_start=start) | {
            "resets_at": tomorrow.astimezone().timestamp()}

    def reconcile_on_startup(self) -> int:
        return self._store.interrupt_chat_tickets("core restarted")
