"""The Console on Telegram: the same API as the page, as plain-text commands, and a push of what a followed conversation produced. Telegram
never widens what the operator may do: no elevated approval, no drift override, and authorising needs the hash that was shown."""
import pytest
from test_console_confirm import Rig as ConfirmRig, plan_doc

from planet_express.console.telegram import ConsoleTelegram, render_plan

OP = "tg.42"


class Rig(ConfirmRig):
    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        self.sent = []
        self.fail = False

        def notify(text):
            if self.fail:
                raise RuntimeError("telegram down")
            self.sent.append(text)
        self.tg = ConsoleTelegram(self.api, self.console, notify)
        self.short = self.conv.id[:8]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_ask_starts_a_conversation_followed_on_telegram_and_threads_lists_it(rig):
    out = rig.tg.command("/ask why is media slow", OP)
    assert out.startswith("Started ") and "/reply" in out
    new_id = out.split()[1].rstrip(".")
    assert any(c.id.startswith(new_id) for c in rig.console.conversations(states=("open",)))
    assert new_id in rig.tg.command("/threads", OP)
    assert {r["conversation_id"][:8] for r in rig.console.tg_followed()} == {new_id}
    assert rig.tg.command("/ask", OP).startswith("Usage")


def test_reply_goes_to_the_named_conversation_or_the_latest_and_nothing_else(rig):
    assert rig.tg.command(f"/reply {rig.short} and the router?", OP) == f"{rig.short}: sent."
    assert any(m.role == "operator" and m.text == "and the router?" and m.actor == "operator:tg.42" for m in rig.console.messages(rig.conv.id))
    assert rig.tg.command("/reply no id here", OP).endswith("sent.")                                        # the latest conversation
    assert "No open conversation deadbeef" in rig.tg.command("/reply deadbeef hi", OP)
    assert rig.tg.command("/reply", OP).startswith("Usage")


def test_the_push_sends_new_agent_and_system_lines_once_and_never_replays_history(rig):
    rig.console.add_message(rig.conv.id, "agent", "old answer")
    rig.tg.command(f"/reply {rig.short} hello", OP)                                                       # follows from here
    rig.tg.push()
    assert rig.sent == []                                                                                 # what existed is not replayed
    rig.console.add_message(rig.conv.id, "agent", "It is the gateway.", turn=1)
    rig.console.add_message(rig.conv.id, "system", "PE took a checkpoint and is carrying out the plan.")
    rig.tg.push()
    assert len(rig.sent) == 1 and "agent: It is the gateway." in rig.sent[0] and "PE: PE took a checkpoint" in rig.sent[0] and rig.short in rig.sent[0]
    rig.tg.push()
    assert len(rig.sent) == 1                                                                             # once


def test_a_failed_send_is_retried_and_a_long_answer_is_cut(rig):
    rig.tg.command(f"/reply {rig.short} hi", OP)
    rig.console.add_message(rig.conv.id, "agent", "x" * 5000)
    rig.fail = True
    rig.tg.push()
    rig.fail = False
    assert rig.sent == []
    rig.tg.push()
    assert len(rig.sent) == 1 and "open the Console for the rest" in rig.sent[0] and len(rig.sent[0]) < 2000


def test_a_new_plan_is_pushed_as_a_card_with_the_checks_in_words(rig):
    rig.tg.command(f"/reply {rig.short} hi", OP)
    rig.add_plan()
    rig.tg.push()
    assert len(rig.sent) == 1 and "Option A (recommended): Restart media" in rig.sent[0] and "answers" in rig.sent[0]
    assert f"/confirm {rig.short} <A|B|C>" in rig.sent[0]
    assert "Option A" in rig.tg.command(f"/plan {rig.short}", OP)
    assert "Option B" in render_plan(rig.conv.id, rig.api.feed(rig.conv.id)["plan"])


def test_confirm_then_authorise_needs_the_hash_that_was_shown_and_the_repair_runs(rig):
    rig.add_plan()
    card = rig.tg.command(f"/confirm {rig.short} A", OP)
    assert "scope: media (stack)" in card and "PE will check" in card
    fp = card.split(f"/authorise {rig.short} ")[1].split()[0]
    assert len(fp) == 8
    assert "not this confirmation's grant hash" in rig.tg.command(f"/authorise {rig.short} deadbeef", OP)
    assert rig.console.executions(rig.conv.id)[0]["status"] == "proposed"
    assert "authorised" in rig.tg.command(f"/authorise {rig.short} {fp}", OP)
    execution = rig.console.executions(rig.conv.id)[0]
    assert execution["status"] == "authorised" and execution["authorised_by"] == "operator:tg.42"
    assert rig.st.drive(execution["mission_id"]) == "completed"


def test_a_confirmation_that_needs_elevation_is_withdrawn_and_sent_to_the_dashboard(tmp_path):
    rig = Rig(tmp_path, covered=False)
    try:
        rig.flow.validator = lambda grant, plan: [] if grant.backup.covered or grant.backup.acknowledged_no_backup else [
            "backup floor: the scope is not covered by a recent backup and no-backup is not acknowledged"]
        rig.add_plan()
        out = rig.tg.command(f"/confirm {rig.short} A", OP)
        assert "no recent verified backup" in out                                                         # Telegram cannot accept that risk
        assert rig.console.executions(rig.conv.id) == []
    finally:
        rig.close()


def test_withdraw_and_run_controls_and_close(rig):
    rig.add_plan()
    rig.tg.command(f"/confirm {rig.short} A", OP)
    assert "withdrawn" in rig.tg.command(f"/withdraw {rig.short}", OP)
    assert "nothing to withdraw" in rig.tg.command(f"/withdraw {rig.short}", OP)
    assert "no run to control" in rig.tg.command(f"/run {rig.short} pause", OP)
    assert rig.tg.command(f"/run {rig.short} fly", OP).startswith("Usage")
    assert "closing" in rig.tg.command(f"/close {rig.short}", OP)


def test_an_ambiguous_or_missing_id_is_refused_in_words(rig):
    assert "Name the conversation" in rig.tg.command("/confirm A", OP)
    assert "Usage" in rig.tg.command(f"/confirm {rig.short} Z", OP)
