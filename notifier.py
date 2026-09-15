"""
notifier.py — Outbound notifications + a channel-agnostic approve/cancel flow.

Covers: plain notifications, and the plan/diff approve-or-cancel request-response pattern
Farnsworth uses for every remediation and compose-diff decision. Deliberately does NOT cover
inbound remote-control command parsing (Telegram's /stacks, /up, /down, /mounts, /help) --
that's a fundamentally different interaction model (arbitrary command text vs. a structured
button tap) that stays directly on TelegramClient in casa_farnsworth.py's handle_message()
until/unless a second channel actually needs it generalized too.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from telegram_client import TelegramClient

log = logging.getLogger("planetexpress.notifier")


@dataclass
class Decision:
    request_id: str
    kind: str             # caller-defined tag, e.g. "plan" or "diff"
    approved: bool
    decided_by: str | None = None  # who tapped: "@username (id)" or "id" for Telegram
    _ref: Any = None      # opaque, implementation-owned handle resolve() needs (e.g.
                          # Telegram's message id) -- callers must never inspect this.


class Notifier(ABC):
    @abstractmethod
    def notify(self, text: str) -> None:
        """Send a plain informational message. No response expected."""

    @abstractmethod
    def request_approval(self, text: str, request_id: str, kind: str) -> int:
        """Send `text` with an approve/cancel affordance tagged to (kind, request_id).
        Returns an opaque message id for the caller's own informational state tracking."""

    @abstractmethod
    def interpret_decision(self, raw_event: dict) -> Decision | None:
        """None if raw_event isn't an approve/cancel tap this Notifier recognizes."""

    @abstractmethod
    def resolve(self, decision: Decision, ack_text: str, resolution_text: str) -> None:
        """ack_text is immediate tap feedback; resolution_text replaces the original
        message body to show the final outcome. Both are caller-supplied -- the Notifier
        doesn't invent copy, the caller's business logic still owns what each case says."""

    @abstractmethod
    def update_request(self, message_id: int | None, text: str) -> None:
        """Replace the body of a previously sent request message by its id, without a tap
        to answer: used when a decision arrives from somewhere else (the dashboard) or an
        execution finishes later. Best-effort, like resolve()."""

    @abstractmethod
    def acknowledge(self, decision: Decision, text: str) -> None:
        """Answer the tap without resolving the request: the card, its buttons and the
        pending request all stay exactly as they were (e.g. "busy, try again shortly")."""


class TelegramNotifier(Notifier):
    def __init__(self, client: TelegramClient):
        self._client = client

    def notify(self, text: str) -> None:
        self._client.send(text)

    def request_approval(self, text: str, request_id: str, kind: str) -> int:
        if kind == "diff":
            keyboard = TelegramClient.diff_approve_keyboard(request_id)
        elif kind == "action":
            keyboard = TelegramClient.act_keyboard(request_id)
        else:
            keyboard = TelegramClient.approve_keyboard(request_id)
        sent = self._client.send(text, reply_markup=keyboard)
        return sent.get("message_id")

    def interpret_decision(self, raw_event: dict) -> Decision | None:
        cb = raw_event.get("callback_query")
        if not cb:
            return None

        cb_id = cb.get("id", "")
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != self._client.chat_id:
            self._client.answer_callback(cb_id, "Not your bot.")
            return None

        data = cb.get("data", "")
        if not data:
            self._client.answer_callback(cb_id)
            return None

        action, _, request_id = data.partition(":")
        kind = {
            "approve": "plan", "cancel": "plan",
            "approve_diff": "diff", "cancel_diff": "diff",
            "act_ok": "action", "act_no": "action",
        }.get(action)
        if kind is None:
            return None

        approved = action in ("approve", "approve_diff", "act_ok")
        msg_id = cb.get("message", {}).get("message_id")
        sender = cb.get("from") or {}
        sender_id = sender.get("id")
        username = sender.get("username")
        if username:
            decided_by = f"@{username} ({sender_id})"
        else:
            decided_by = str(sender_id) if sender_id is not None else None
        return Decision(
            request_id=request_id, kind=kind, approved=approved, decided_by=decided_by,
            _ref={"cb_id": cb_id, "msg_id": msg_id},
        )

    def update_request(self, message_id: int | None, text: str) -> None:
        if message_id is None:
            return
        try:
            self._client.edit(message_id, text)
        except Exception:  # noqa: BLE001 -- best-effort, same as resolve(); never log the error text
            log.warning("Failed to update request message")

    def acknowledge(self, decision: Decision, text: str) -> None:
        ref = decision._ref or {}
        self._client.answer_callback(ref.get("cb_id", ""), text)

    def resolve(self, decision: Decision, ack_text: str, resolution_text: str) -> None:
        ref = decision._ref or {}
        self._client.answer_callback(ref.get("cb_id", ""), ack_text)
        try:
            self._client.edit(ref.get("msg_id"), resolution_text)
        except Exception:  # noqa: BLE001 -- best-effort UI cleanup, decision is already resolved
            # No exc_info/str(e) here: TelegramClient embeds the bot token in its
            # request URL, and requests exceptions often stringify that URL.
            log.warning("Failed to edit resolution message")


class FakeNotifier(Notifier):
    """In-memory test double. Records every call; interpret_decision() is driven by
    tests via queue_decision() rather than parsing any real event shape."""

    def __init__(self):
        self.notifications: list[str] = []
        self.approval_requests: list[tuple[str, str, str]] = []  # (text, request_id, kind)
        self.resolutions: list[tuple[Decision, str, str]] = []   # (decision, ack, resolution)
        self.acknowledgements: list[tuple[Decision, str]] = []    # (decision, text)
        self.request_updates: list[tuple[int | None, str]] = []   # (message_id, text)
        self._next_decision: Decision | None = None
        self._next_message_id = 1

    def notify(self, text: str) -> None:
        self.notifications.append(text)

    def request_approval(self, text: str, request_id: str, kind: str) -> int:
        self.approval_requests.append((text, request_id, kind))
        msg_id = self._next_message_id
        self._next_message_id += 1
        return msg_id

    def queue_decision(self, decision: Decision) -> None:
        self._next_decision = decision

    def interpret_decision(self, raw_event: dict) -> Decision | None:
        decision, self._next_decision = self._next_decision, None
        return decision

    def resolve(self, decision: Decision, ack_text: str, resolution_text: str) -> None:
        self.resolutions.append((decision, ack_text, resolution_text))

    def acknowledge(self, decision: Decision, text: str) -> None:
        self.acknowledgements.append((decision, text))

    def update_request(self, message_id: int | None, text: str) -> None:
        self.request_updates.append((message_id, text))
