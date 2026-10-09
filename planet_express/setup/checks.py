"""The outbound checks the wizard makes on a person's behalf: Telegram and the LLM provider.

Fixed endpoints only (no URL ever comes from a browser), short timeouts, and every error text is scrubbed of the
credential before it leaves this module. Standard library only: setup runs before any virtualenv has `requests`.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

TELEGRAM = "https://api.telegram.org"
OPENAI_MODELS = "https://api.openai.com/v1/models"
ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models"
TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


@dataclass
class Found:
    ok: bool
    message: str
    chat_id: str | None = None
    bot: str | None = None


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "••••")
    return text[:200]


def _fetch(request: urllib.request.Request, *, opener=urllib.request.urlopen, timeout: float = 10.0):
    """(status, parsed JSON or {}), never raising for an HTTP error status."""
    try:
        with opener(request, timeout=timeout) as response:
            try:
                body = json.loads(response.read() or b"{}")
            except ValueError:
                return 502, {}                       # a success that is not JSON (a captive portal, a proxy page)
            return (response.status, body) if isinstance(body, dict) else (502, {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            return exc.code, {}


def telegram_find_chat(token: str, *, opener=urllib.request.urlopen) -> Found:
    """Prove the bot token works and find the chat that last wrote to the bot."""
    if not TOKEN_SHAPE.fullmatch(token):
        return Found(False, "That does not look like a bot token (it is digits, a colon, then letters and digits).")
    try:
        status, me = _fetch(urllib.request.Request(f"{TELEGRAM}/bot{token}/getMe"), opener=opener)
        if status != 200 or not me.get("ok"):
            return Found(False, "Telegram rejected this token. Copy it again from @BotFather.")
        bot = (me.get("result") or {}).get("username")
        status, updates = _fetch(urllib.request.Request(f"{TELEGRAM}/bot{token}/getUpdates?limit=20&timeout=0"), opener=opener)
        if status == 409:
            return Found(False, "This bot already has a webhook or another program is reading its messages, so setup "
                                "cannot see them. Stop that program briefly, or type the chat id yourself.", bot=bot)
        if status != 200:
            return Found(False, "Telegram did not answer the message check. Try again.", bot=bot)
        for update in reversed(updates.get("result") or []):
            chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat") or {}
            if "id" in chat:
                return Found(True, "Found the chat.", chat_id=str(chat["id"]), bot=bot)
        return Found(False, f"No messages yet. Open @{bot} in Telegram, send it any message, then press the button again.",
                     bot=bot)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return Found(False, "Could not reach Telegram: " + _scrub(str(getattr(exc, "reason", exc)), token))


def telegram_send_test(token: str, chat_id: str, *, opener=urllib.request.urlopen) -> tuple[bool, str]:
    body = json.dumps({"chat_id": chat_id, "text": "Planet Express setup: this chat is connected. No action needed."}).encode()
    request = urllib.request.Request(f"{TELEGRAM}/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"})
    try:
        status, reply = _fetch(request, opener=opener)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, "Could not reach Telegram: " + _scrub(str(getattr(exc, "reason", exc)), token)
    if status == 200 and reply.get("ok"):
        return True, "Test message sent. Check your phone."
    if status in (400, 403):
        return False, "Telegram would not deliver to that chat. Send the bot a message first, then find the chat again."
    return False, "Telegram did not accept the message."


def check_llm_key(provider: str, key: str, *, opener=urllib.request.urlopen) -> tuple[bool, str]:
    """List the provider's models: proves the key is valid without spending a token."""
    if provider == "openai":
        request = urllib.request.Request(OPENAI_MODELS, headers={"Authorization": f"Bearer {key}"})
    elif provider == "anthropic":
        request = urllib.request.Request(ANTHROPIC_MODELS, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        return False, "Unknown provider."
    try:
        status, _ = _fetch(request, opener=opener)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, "Could not reach the provider: " + _scrub(str(getattr(exc, "reason", exc)), key)
    if status == 200:
        return True, "The key works."
    if status in (401, 403):
        return False, "The provider rejected this key."
    return False, f"The provider answered with an error ({status}). Try again."
