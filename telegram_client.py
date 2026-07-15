"""
telegram_client.py — Planet Express Telegram API wrapper
Lightweight raw-requests implementation. No framework dependency.
Uses HTML parse_mode — content escaping is just &, <, > which is far more
predictable than Markdown v1/v2 where (, ), -, ., # etc. cause parse errors.
"""

import logging
from typing import Optional
import requests

log = logging.getLogger("planetexpress.telegram")

PLAN_EXPIRY_HOURS = 24


class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"
        self._offset = 0

    # ── Core API call ─────────────────────────────────────────────────────────
    def _call(self, method: str, req_timeout: int = 35, **kwargs) -> dict:
        try:
            r = requests.post(
                f"{self.base}/{method}", json=kwargs, timeout=req_timeout
            )
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(
                    f"Telegram error [{method}]: {data.get('description')}"
                )
            return data["result"]
        except requests.RequestException as e:
            raise RuntimeError(f"Telegram request failed [{method}]: {e}") from e

    # ── Messaging ─────────────────────────────────────────────────────────────
    def send(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> dict:
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            return self._call("sendMessage", **payload)
        except RuntimeError as e:
            if "parse" in str(e).lower() or "entities" in str(e).lower():
                # HTML parse failure — retry as plain text so the pipeline
                # never crashes on a formatting edge case
                log.warning(f"HTML parse failed, retrying as plain text: {e}")
                plain: dict = {"chat_id": self.chat_id, "text": text[:4096]}
                if reply_markup:
                    plain["reply_markup"] = reply_markup
                return self._call("sendMessage", **plain)
            raise

    def edit(
        self,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> dict:
        payload: dict = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("editMessageText", **payload)

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self._call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
        )

    # ── Long polling ──────────────────────────────────────────────────────────
    def poll_updates(self, timeout: int = 30) -> list[dict]:
        try:
            updates = self._call(
                "getUpdates",
                req_timeout=timeout + 5,
                offset=self._offset,
                timeout=timeout,
                allowed_updates=["message", "callback_query"],
            )
            for u in updates:
                self._offset = max(self._offset, u["update_id"] + 1)
            return updates
        except Exception as e:
            log.warning(f"Poll error (will retry): {e}")
            return []

    # ── Keyboard helpers ──────────────────────────────────────────────────────
    @staticmethod
    def approve_keyboard(plan_id: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{plan_id}"},
                {"text": "❌ Cancel",  "callback_data": f"cancel:{plan_id}"},
            ]]
        }

    @staticmethod
    def diff_approve_keyboard(diff_id: str) -> dict:
        """Deliberately a separate callback prefix from approve_keyboard — a compose
        diff and a shell-command plan are different kinds of approval and must never
        be foldable into the same tap."""
        return {
            "inline_keyboard": [[
                {"text": "✅ Apply diff", "callback_data": f"approve_diff:{diff_id}"},
                {"text": "❌ Discard",    "callback_data": f"cancel_diff:{diff_id}"},
            ]]
        }

    # ── HTML content escaping ─────────────────────────────────────────────────
    @staticmethod
    def s(text: str) -> str:
        """Escape dynamic content for Telegram HTML parse mode.
        Only &, <, > need escaping — everything else (parens, dashes, dots,
        underscores, asterisks) is safe in HTML text nodes."""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    # ── Formatted message builders ────────────────────────────────────────────
    @staticmethod
    def fmt_report(date: str, findings: list[dict]) -> str:
        if not findings:
            body = "<i>Captain's log: all systems nominal. No findings filed.</i>"
        else:
            lines = []
            for f in findings:
                icon = {
                    "CRITICAL": "🚨", "HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"
                }.get(f["severity"], "⚪")
                lines.append(
                    f"{icon} <b>{f['severity']}</b> — "
                    f"<code>{TelegramClient.s(f['resource'])}</code>: "
                    f"{TelegramClient.s(f['description'])}"
                )
            body = "\n".join(lines)
        return f"🔍 <b>CasaSysAdmin Report</b>\nDate: {date}\n\n{body}"

    @staticmethod
    def fmt_plan(plan: dict) -> str:
        s = TelegramClient.s
        steps = "\n".join(
            f"{step['n']}. {s(step['description'])}" for step in plan["steps"]
        )
        rollback = s(plan["rollback"][0]["description"]) if plan["rollback"] else "none"
        downtime = s(plan.get("estimated_downtime", "unknown"))
        return (
            f"📋 <b>Action Plan #{s(plan['id'])}</b>\n"
            f"Priority: <code>{s(plan['priority'].upper())}</code>\n"
            f"{s(plan['title'])}\n\n"
            f"<b>Steps:</b>\n{steps}\n\n"
            f"<b>Rollback:</b> {rollback}\n"
            f"<b>Est. downtime:</b> {downtime}\n\n"
            f"Good news, everyone! Reply ✅ to approve or ❌ to cancel."
        )

    @staticmethod
    def fmt_step_status(
        plan_id: str, n: int, total: int, description: str, ok: bool, error: str = ""
    ) -> str:
        s = TelegramClient.s
        status = "✅ ok" if ok else f"❌ error: <code>{s(error)}</code>"
        return (
            f"⚙️ <b>Executing Plan #{s(plan_id)}</b>\n"
            f"Step {n}/{total}: {s(description)}\n"
            f"Status: {status}"
        )

    @staticmethod
    def fmt_complete(plan_id: str, steps_done: int, errors: list[str]) -> str:
        s = TelegramClient.s
        err_str = s(", ".join(errors)) if errors else "none"
        return (
            f"✅ <b>Plan #{s(plan_id)} Complete</b>\n"
            f"{steps_done} step(s) executed. You're welcome.\n"
            f"Errors: {err_str}"
        )

    @staticmethod
    def fmt_failed(plan_id: str, step_n: int, description: str, error: str) -> str:
        s = TelegramClient.s
        return (
            f"🛑 <b>Plan #{s(plan_id)} — Step {step_n} Failed</b>\n"
            f"{s(description)}\n"
            f"Error: <code>{s(error)}</code>\n\n"
            f"Reply /rollback {s(plan_id)} to roll back or /skip {s(plan_id)} to continue anyway."
        )

    @staticmethod
    def fmt_diagnosis(stack: str, service: str, diagnosis: dict) -> str:
        """Amy's diagnosis — deliberately a different message shape from fmt_plan,
        since this is a research-backed judgment call, not a routine action plan."""
        s = TelegramClient.s
        remediation = diagnosis.get("proposed_remediation", {})
        steps = remediation.get("steps", [])
        steps_text = "\n".join(
            f"{step.get('n', i+1)}. {s(step.get('description', ''))}"
            for i, step in enumerate(steps)
        ) or "(no concrete steps proposed)"
        sources = diagnosis.get("sources", [])
        sources_text = "\n".join(f"• {s(src)}" for src in sources) if sources else "none"
        compose_note = (
            "\n⚠️ <b>Requires a compose-file edit</b> — proposed separately, see next message.\n"
            if remediation.get("requires_compose_edit")
            else ""
        )
        return (
            f"🩺 <b>Amy's diagnosis: {s(stack)}/{s(service)}</b>\n"
            f"Category: <code>{s(diagnosis.get('category', 'unknown'))}</code> "
            f"(confidence: {s(diagnosis.get('confidence', 'unknown'))})\n\n"
            f"{s(diagnosis.get('diagnosis', ''))}\n\n"
            f"<b>Proposed: {s(remediation.get('summary', '(none)'))}</b>\n{steps_text}\n"
            f"{compose_note}\n"
            f"<b>Sources:</b>\n{sources_text}\n\n"
            f"This is research, not an executed action — nothing has run yet."
        )

    @staticmethod
    def fmt_diff(stack: str, reason: str, diff_text: str) -> str:
        """Telegram messages cap at 4096 chars — truncate long diffs rather than
        fail to send; the full diff is always still on disk in pending_diffs.json."""
        s = TelegramClient.s
        max_diff_chars = 3200
        truncated = len(diff_text) > max_diff_chars
        shown = diff_text[:max_diff_chars]
        note = "\n... (truncated, full diff on disk)" if truncated else ""
        return (
            f"📝 <b>Proposed compose-file edit: {s(stack)}</b>\n"
            f"Reason: {s(reason)}\n\n"
            f"<pre>{s(shown)}{note}</pre>\n\n"
            f"This only writes the file (with a backup taken first) — it does not restart "
            f"anything. Reply ✅ to apply or ❌ to discard."
        )
