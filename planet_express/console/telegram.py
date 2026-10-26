"""The Console on Telegram: a miniature of the page. Plain-text commands over the same `ConsoleApi` the page uses (so every refusal, every
check and the grant-hash binding are the same ones), and a push of what a followed conversation produces: the agent's answers, a plan card,
and PE's progress lines.

Nothing here widens what Telegram may do. The operator is the Telegram sender, never a parameter. Telegram cannot give an elevated session, so
a confirmation that needs one (a scope with no backup) is withdrawn and sent to the dashboard; Telegram never overrides a refused restore
(`ack_drift` is dashboard-only); and authorising needs the first 8 characters of the grant hash the operator was shown, so a card that
changed after it was read cannot be approved.
"""
import logging
import re
import threading
from collections.abc import Callable

from planet_express.console import plan as plan_mod
from planet_express.console.api import ConsoleApi, ConsoleApiError
from planet_express.console.store import ConsoleStore, Conversation

log = logging.getLogger(__name__)

COMMANDS = frozenset({"/ask", "/reply", "/threads", "/plan", "/confirm", "/authorise", "/authorize", "/withdraw", "/run", "/close"})
ID_RE = re.compile(r"[0-9a-f]{6,16}")
MAX_PUSH = 1500                       # characters of one pushed message; the page has the rest
MAX_PUSH_TOTAL = 3500                 # of one Telegram message
MAX_CARD = 3500                       # of a confirmation or plan card (Telegram's own limit is 4096)
ACTIONS = ("pause", "resume", "rollback", "takeover")
HELP = ("Console: /ask <question> · /reply [id] <text> · /threads · /plan [id] · /confirm <id> <A|B|C> <plan8> · /authorise <id> <hash8> · "
        "/withdraw <id> · /run <id> pause|resume|rollback|takeover · /close <id>")


class ConsoleTelegram:
    def __init__(self, api: ConsoleApi, console: ConsoleStore, notify: Callable[[str], None]):
        self.api = api
        self.console = console
        self.notify = notify
        self._pushing = threading.Lock()

    # ── commands ──────────────────────────────────────────────────────────────────────────
    def command(self, text: str, operator: str) -> str:
        parts = text.split(maxsplit=1)
        cmd, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        cmd = "/authorise" if cmd == "/authorize" else cmd
        try:
            if cmd == "/ask":
                return self._ask(rest, operator)
            if cmd == "/reply":
                return self._reply(rest, operator)
            if cmd == "/threads":
                return self._threads()
            conv, tail = self._conversation(rest, required=cmd != "/plan")
            if cmd == "/plan":
                return self._plan(conv)
            if cmd == "/confirm":
                return self._confirm(conv, tail, operator)
            if cmd == "/authorise":
                return self._authorise(conv, tail, operator)
            if cmd == "/withdraw":
                return self._withdraw(conv, operator)
            if cmd == "/run":
                return self._run(conv, tail, operator)
            if cmd == "/close":
                started = self.api.archive(conv.id, operator)["archiving"]
                return (f"{conv.id[:8]}: closing. Its workspace and identity are released within a minute." if started
                        else f"{conv.id[:8]}: it is already closing or closed.")
        except ConsoleApiError as exc:
            return str(exc)
        except _Usage as exc:
            return str(exc)
        return HELP

    def _ask(self, text: str, operator: str) -> str:
        if not text:
            return "Usage: /ask <what you want investigated>"
        created = self.api.create(operator, text)
        self.console.tg_follow(created["id"])
        return f"Started {created['id'][:8]}. I'll message you with what it finds. Follow up with /reply {created['id'][:8]} <text>."

    def _reply(self, rest: str, operator: str) -> str:
        first = rest.split(maxsplit=1)
        if first and ID_RE.fullmatch(first[0].lower()) and any(c.id.startswith(first[0].lower()) for c in self.console.conversations(states=("open",), limit=200)):
            conv, text = self._conversation(rest, required=True)                           # a first word that names no conversation is just text
        else:
            conv, text = self._latest(), rest                          # no id: the conversation you last touched
        if not text:
            raise _Usage("Usage: /reply [id] <text>")
        if conv is None:
            raise _Usage("No open conversation. Start one with /ask <question>.")
        self.api.post(conv.id, operator, text)
        self.console.tg_follow(conv.id)
        return f"{conv.id[:8]}: sent."

    def _threads(self) -> str:
        rows = self.api.list()["conversations"]
        if not rows:
            return "No open conversations. /ask <question> starts one."
        return "\n".join(f"{r['id'][:8]} [{r['status']['text']}] {r['title'][:60]}" for r in rows[:12])

    def _plan(self, conv: Conversation | None) -> str:
        conv = conv or self._latest()
        if conv is None:
            raise _Usage("No open conversation.")
        plan = self.api.plan_card(self.console.current_plan(conv.id))
        if plan is None or plan["status"] != "pending" or plan["expired"]:
            return f"{conv.id[:8]}: no plan is waiting for you."
        return render_plan(conv.id, plan)

    def _confirm(self, conv: Conversation, tail: str, operator: str) -> str:
        words = tail.split()
        letter = words[0].upper() if words else ""
        if len(words) != 2 or len(letter) != 1 or letter not in "ABC" or re.fullmatch(r"[0-9a-f]{8}", words[1].lower()) is None:
            raise _Usage("Usage: /confirm <id> <A|B|C> <plan8>   (plan8 is the 8-character plan code on the plan you read)")
        plan = self.console.current_plan(conv.id)
        if plan is None:
            raise _Usage(f"{conv.id[:8]}: no plan is waiting for you.")
        if not plan["plan_hash"].startswith(words[1].lower()):
            # The agent may have replaced the plan since it was read: the option letter would then mean something else.
            raise _Usage(f"{conv.id[:8]}: that is not the plan you read (it has changed). Read it again: /plan {conv.id[:8]}")
        try:
            view = self.api.confirm_prepare(conv.id, plan["id"], ord(letter) - 65, operator)
        except ConsoleApiError as exc:
            if exc.code == "no_backup":
                return (f"{conv.id[:8]}: option {letter} has no recent verified backup. Running it anyway needs an elevated dashboard session, "
                        "which Telegram cannot give. Nothing was approved or changed.")
            raise
        self.console.tg_follow(conv.id)                                 # a run authorised here reports back here
        if view["needs_elevation"]:
            self.api.confirm_withdraw(conv.id, view["execution_id"], operator)
            return f"{conv.id[:8]}: option {letter} needs an elevated dashboard session (Telegram cannot give one). Nothing was approved or changed."
        scope = ", ".join([*(f"{s} (stack)" for s in view["scope"]["stacks"]), *view["scope"]["services"]]) or "nothing"
        # The command that authorises comes FIRST and the card is bounded: Telegram cuts a message at 4096 characters, and a hash that fell off the end
        # would leave the operator unable to authorise what they were shown.
        lines = [f"{conv.id[:8]}: authorise option {letter} · {_plain(view['option'], 80)}",
                 f"To authorise exactly this: /authorise {conv.id[:8]} {view['fingerprint']}   (or /withdraw {conv.id[:8]})",
                 f"what it does: {_plain(view['summary'], 300)}",
                 *(f"!! {_plain(w, 200)}" for w in view["warnings"][:4]),
                 f"scope: {_clip(scope, 400)}", f"backup: {view['backup']['text']}", f"will not touch: {view['protected']['count']} protected resources",
                 f"limits: {view['limits']['minutes']} min, {view['limits']['steps']} steps; PE pauses if anything outside the scope changes",
                 "PE will check: " + _clip("; ".join(view["checks"]), 800)]
        return _clip("\n".join(lines), MAX_CARD)

    def _authorise(self, conv: Conversation, tail: str, operator: str) -> str:
        fingerprint = tail.strip().lower()
        if re.fullmatch(r"[0-9a-f]{8}", fingerprint) is None:
            raise _Usage("Usage: /authorise <id> <first 8 characters of the grant hash on the card>")
        proposed = [e for e in self.console.executions(conv.id) if e["status"] == "proposed"]
        if not proposed:
            raise _Usage(f"{conv.id[:8]}: nothing is waiting to be authorised. Use /confirm first.")
        execution = proposed[-1]
        if not execution["grant_hash"].startswith(fingerprint):
            raise _Usage("That hash is not this confirmation's grant hash. Read it again: /confirm " + conv.id[:8] + " <A|B|C>")
        self.api.confirm_authorise(conv.id, execution["id"], execution["grant_hash"], operator, elevated=False)
        self.console.tg_follow(conv.id)
        return f"{conv.id[:8]}: authorised ({fingerprint}). PE takes a checkpoint, carries it out under supervision, then checks it worked."

    def _withdraw(self, conv: Conversation, operator: str) -> str:
        proposed = [e for e in self.console.executions(conv.id) if e["status"] == "proposed"]
        if not proposed:
            raise _Usage(f"{conv.id[:8]}: nothing to withdraw.")
        self.api.confirm_withdraw(conv.id, proposed[-1]["id"], operator)
        return f"{conv.id[:8]}: withdrawn. Nothing was changed."

    def _run(self, conv: Conversation, tail: str, operator: str) -> str:
        action = tail.strip().lower()
        if action not in ACTIONS:
            raise _Usage("Usage: /run <id> pause|resume|rollback|takeover")
        runs = [e for e in self.console.executions(conv.id) if e["status"] in ("authorised", "ended")]
        if not runs:
            raise _Usage(f"{conv.id[:8]}: no run to control.")
        out = self.api.execution_action(conv.id, runs[-1]["id"], action, operator)
        if action == "rollback" and out.get("restore") != "done":
            why = out.get("reason") or "the restore did not complete"
            if out.get("restore") == "refused":
                return (f"{conv.id[:8]}: PE did not restore: {why}. "
                        "Overwriting those changes is a deliberate choice you make on the dashboard, never from Telegram.")
            return f"{conv.id[:8]}: the rollback did NOT complete ({out.get('restore')}): {why}. Look at it on the dashboard before doing anything else."
        return f"{conv.id[:8]}: {action} done. Now: {out['status']}."

    # ── resolving a conversation ──────────────────────────────────────────────────────────
    def _latest(self) -> Conversation | None:
        open_ = self.console.conversations(states=("open",), limit=50)
        return max(open_, key=lambda c: c.last_activity_at, default=None)

    def _conversation(self, rest: str, *, required: bool) -> tuple[Conversation | None, str]:
        first = rest.split(maxsplit=1)
        if not first or not ID_RE.fullmatch(first[0].lower()):
            if required:
                raise _Usage("Name the conversation by its id (see /threads).")
            return None, rest
        prefix = first[0].lower()
        found = [c for c in self.console.conversations(states=("open",), limit=200) if c.id.startswith(prefix)]
        if not found:
            raise _Usage(f"No open conversation {prefix}. See /threads.")
        if len(found) > 1:
            raise _Usage(f"{prefix} matches more than one conversation. Use more characters.")
        return found[0], first[1].strip() if len(first) > 1 else ""

    # ── the push: what a followed conversation produced since last time ──────────────────
    def push_async(self) -> None:
        """Called from the supervisor tick: never blocks it. One sender at a time; a tick that finds one still sending does nothing."""
        if self._pushing.locked():
            return
        threading.Thread(target=self.push, daemon=True, name="console-telegram").start()

    def push(self) -> None:
        if not self._pushing.acquire(blocking=False):
            return
        try:
            for row in self.console.tg_followed():
                try:
                    self._push_one(row)
                except Exception as exc:                               # noqa: BLE001 -- one conversation's trouble never stops the others
                    # The type only: a Telegram client error carries the request URL, and the URL carries the bot token.
                    log.warning("console telegram push for %s failed: %s", row["conversation_id"], type(exc).__name__)
        finally:
            self._pushing.release()

    def _push_one(self, row: dict) -> None:
        conv_id = row["conversation_id"]
        conv = self.console.get(conv_id)
        short = conv_id[:8]
        # Items in order, each with the cursor position it moves to once it has been SENT. A batch is cut at the Telegram limit and the cursor
        # advances only through what went out, so nothing is dropped by truncation.
        items: list[tuple[str, int, int]] = []
        last_seq, last_plan = row["last_seq"], row["last_plan_id"]
        for message in self.console.messages(conv_id, after=row["last_seq"], limit=50):
            own_command = message.actor is not None and message.actor.startswith("operator:tg.")        # the command's own reply already said it
            if message.role in ("agent", "system") and not own_command:
                body = _plain(message.text, MAX_PUSH, rest="… (open the Console for the rest)")
                items.append((f"{short} {'PE' if message.role == 'system' else 'agent'}: {body}", message.seq, last_plan))
            else:
                items.append(("", message.seq, last_plan))             # moves the cursor only
        plan = self.console.current_plan(conv_id)
        if plan is not None and plan["id"] > row["last_plan_id"]:
            items.append((_clip(render_plan(conv_id, self.api.plan_card(plan)), MAX_CARD), items[-1][1] if items else last_seq, plan["id"]))
        batch: list[str] = []
        size = 0
        for text, seq, plan_id in items:
            if text and batch and size + len(text) + 2 > MAX_PUSH_TOTAL:
                self.notify("\n\n".join(batch))                       # raises on a send failure: the cursor stays where it was, it is tried again
                self.console.tg_advance(conv_id, last_seq, last_plan)
                batch, size = [], 0
            if text:
                batch.append(text)
                size += len(text) + 2
            last_seq, last_plan = seq, plan_id
        if batch:
            self.notify("\n\n".join(batch))
        if (last_seq, last_plan) != (row["last_seq"], row["last_plan_id"]):
            self.console.tg_advance(conv_id, last_seq, last_plan)
        if conv.state == "archived":
            self.console.tg_unfollow(conv_id)


class _Usage(Exception):
    """A usage message, not an error."""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _plain(text: str, limit: int, rest: str = "…") -> str:
    """Untrusted text (the agent's, or a name it chose) as one inert line: no line breaks to fake a PE line with, and no live /command to tap."""
    flat = " ⏎ ".join(part.strip() for part in str(text).splitlines() if part.strip())
    flat = re.sub(r"(?<![\w/])/(?=\w)", "/\u200b", flat)
    return flat if len(flat) <= limit else flat[:limit] + rest


def render_plan(conversation_id: str, plan: dict) -> str:
    """A pending plan card as plain text. Every word comes from the validated plan; checks are phrased from PE's own catalogue."""
    short = conversation_id[:8]
    intro = (f"{short}: the agent has a plan (plan {plan['hash']}). Nothing changes until you authorise it: "
             f"/confirm {short} <A|B|C> {plan['hash']} shows exactly what you would be authorising.")
    lines = [intro, ""]
    if plan["plan"]["unsure"]:
        lines.append("not sure about: " + _plain("; ".join(plan["plan"]["unsure"][:3]), 400))
    for i, option in enumerate(plan["plan"]["options"]):
        letter = chr(65 + i)
        lines += [f"Option {letter}{' (recommended)' if option['recommended'] else ''}: {_plain(option['name'], 80)}", f"  {_plain(option['summary'], 300)}",
                  f"  risk {option['risk']['level']}: {_plain(option['risk']['text'], 200)}", f"  rolls back to: {_plain(option['rolls_back_to'], 200)}",
                  "  PE will check: " + "; ".join(plan_mod.check_phrase(c) for c in option["checks"])]
    return "\n".join(lines)
