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
from collections.abc import Callable

from planet_express.console import plan as plan_mod
from planet_express.console.api import ConsoleApi, ConsoleApiError
from planet_express.console.store import ConsoleStore, Conversation

log = logging.getLogger(__name__)

COMMANDS = frozenset({"/ask", "/reply", "/threads", "/plan", "/confirm", "/authorise", "/withdraw", "/run", "/close"})
ID_RE = re.compile(r"[0-9a-f]{6,16}")
MAX_PUSH = 1500                       # characters of one pushed message; the page has the rest
MAX_PUSH_TOTAL = 3500                 # of one Telegram message
ACTIONS = ("pause", "resume", "rollback", "takeover")
HELP = ("Console: /ask <question> · /reply [id] <text> · /threads · /plan [id] · /confirm <id> <A|B|C> · /authorise <id> <hash8> · "
        "/withdraw <id> · /run <id> pause|resume|rollback|takeover · /close <id>")


class ConsoleTelegram:
    def __init__(self, api: ConsoleApi, console: ConsoleStore, notify: Callable[[str], None]):
        self.api = api
        self.console = console
        self.notify = notify

    # ── commands ──────────────────────────────────────────────────────────────────────────
    def command(self, text: str, operator: str) -> str:
        parts = text.split(maxsplit=1)
        cmd, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
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
                self.api.archive(conv.id, operator)
                return f"{conv.id[:8]}: closing. Its workspace and identity are released."
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
        if first and ID_RE.fullmatch(first[0]):
            conv, text = self._conversation(rest, required=True)
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
        feed = self.api.feed(conv.id)
        plan = feed["plan"]
        if plan is None or plan["status"] != "pending" or plan["expired"]:
            return f"{conv.id[:8]}: no plan is waiting for you."
        return render_plan(conv.id, plan)

    def _confirm(self, conv: Conversation, tail: str, operator: str) -> str:
        letter = tail.strip().upper()
        if len(letter) != 1 or letter not in "ABC":
            raise _Usage("Usage: /confirm <id> <A|B|C>")
        plan = self.console.current_plan(conv.id)
        if plan is None:
            raise _Usage(f"{conv.id[:8]}: no plan is waiting for you.")
        view = self.api.confirm_prepare(conv.id, plan["id"], ord(letter) - 65, operator)
        if view["needs_elevation"]:
            self.api.confirm_withdraw(conv.id, view["execution_id"], operator)
            return f"{conv.id[:8]}: option {letter} needs an elevated dashboard session (Telegram cannot give one). Nothing was approved or changed."
        scope = ", ".join([*(f"{s} (stack)" for s in view["scope"]["stacks"]), *view["scope"]["services"]]) or "nothing"
        lines = [f"{conv.id[:8]}: authorise option {letter} · {view['option']}", *(f"!! {w}" for w in view["warnings"]),
                 f"scope: {scope}", f"backup: {view['backup']['text']}", f"will not touch: {view['protected']['count']} protected resources",
                 f"limits: {view['limits']['minutes']} min, {view['limits']['steps']} steps; PE pauses if anything outside the scope changes",
                 "PE will check: " + "; ".join(view["checks"]),
                 f"To authorise exactly this: /authorise {conv.id[:8]} {view['fingerprint']}   (or /withdraw {conv.id[:8]})"]
        return "\n".join(lines)

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
        if action == "rollback" and out.get("restore") == "refused":
            return (f"{conv.id[:8]}: PE did not restore: {out.get('reason') or 'files changed since the checkpoint'}. "
                    "Overwriting those changes is a deliberate choice you make on the dashboard, never from Telegram.")
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
    def push(self) -> None:
        for row in self.console.tg_followed():
            try:
                self._push_one(row)
            except Exception:                                          # noqa: BLE001 -- one conversation's trouble never stops the others
                log.exception("console telegram push for %s failed", row["conversation_id"])

    def _push_one(self, row: dict) -> None:
        conv_id = row["conversation_id"]
        conv = self.console.get(conv_id)
        short = conv_id[:8]
        sent: list[str] = []
        last_seq = row["last_seq"]
        for message in self.console.messages(conv_id, after=row["last_seq"], limit=50):
            last_seq = message.seq
            if message.role not in ("agent", "system"):
                continue
            body = message.text if len(message.text) <= MAX_PUSH else message.text[:MAX_PUSH] + "… (open the Console for the rest)"
            sent.append(f"{short} {'PE' if message.role == 'system' else 'agent'}: {body}")
        last_plan = row["last_plan_id"]
        plan = self.console.current_plan(conv_id)
        if plan is not None and plan["id"] > last_plan:
            last_plan = plan["id"]
            sent.append(render_plan(conv_id, self.api._plan(plan)))
        if sent:
            text = "\n\n".join(sent)
            self.notify(text[:MAX_PUSH_TOTAL])                         # raises on a send failure: the cursor stays, it is tried again
        if (last_seq, last_plan) != (row["last_seq"], row["last_plan_id"]):
            self.console.tg_advance(conv_id, last_seq, last_plan)
        if conv.state == "archived":
            self.console.tg_unfollow(conv_id)


class _Usage(Exception):
    """A usage message, not an error."""


def render_plan(conversation_id: str, plan: dict) -> str:
    """A pending plan card as plain text. Every word comes from the validated plan; checks are phrased from PE's own catalogue."""
    short = conversation_id[:8]
    lines = [f"{short}: the agent has a plan", ""]
    if plan["plan"]["unsure"]:
        lines.append("not sure about: " + "; ".join(plan["plan"]["unsure"][:3]))
    for i, option in enumerate(plan["plan"]["options"]):
        letter = chr(65 + i)
        lines += [f"Option {letter}{' (recommended)' if option['recommended'] else ''}: {option['name']}", f"  {option['summary']}",
                  f"  risk {option['risk']['level']}: {option['risk']['text']}", f"  rolls back to: {option['rolls_back_to']}",
                  "  PE will check: " + "; ".join(plan_mod.check_phrase(c) for c in option["checks"])]
    lines += ["", f"Nothing changes until you authorise it. /confirm {short} <A|B|C> shows exactly what you would be authorising."]
    return "\n".join(lines)
