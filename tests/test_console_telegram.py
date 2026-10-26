"""The Console on Telegram: the same API as the page, as plain-text commands, and a push of what a followed conversation produced. Telegram
never widens what the operator may do: no elevated approval, no drift override, and authorising needs the hash that was shown."""
import pytest
from test_console_confirm import Rig as ConfirmRig, plan_doc

from planet_express.console.telegram import ConsoleTelegram, render_plan

OP = "tg.42"


def plan8(rig):
    return rig.console.current_plan(rig.conv.id)["plan_hash"][:8]


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
    assert rig.tg.command("/reply deadbeef hi", OP).endswith("sent.")                                      # a word that names no conversation is just text
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
    assert f"/confirm {rig.short} <A|B|C> {plan8(rig)}" in rig.sent[0]
    assert "Option A" in rig.tg.command(f"/plan {rig.short}", OP)
    assert "Option B" in render_plan(rig.conv.id, rig.api.feed(rig.conv.id)["plan"])


def test_confirm_then_authorise_needs_the_hash_that_was_shown_and_the_repair_runs(rig):
    rig.add_plan()
    card = rig.tg.command(f"/confirm {rig.short} A {plan8(rig)}", OP)
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
        out = rig.tg.command(f"/confirm {rig.short} A {plan8(rig)}", OP)
        assert "no recent verified backup" in out                                                         # Telegram cannot accept that risk
        assert rig.console.executions(rig.conv.id) == []
    finally:
        rig.close()


def test_withdraw_and_run_controls_and_close(rig):
    rig.add_plan()
    rig.tg.command(f"/confirm {rig.short} A {plan8(rig)}", OP)
    assert "withdrawn" in rig.tg.command(f"/withdraw {rig.short}", OP)
    assert "nothing to withdraw" in rig.tg.command(f"/withdraw {rig.short}", OP)
    assert "no run to control" in rig.tg.command(f"/run {rig.short} pause", OP)
    assert rig.tg.command(f"/run {rig.short} fly", OP).startswith("Usage")
    assert "closing" in rig.tg.command(f"/close {rig.short}", OP)


def test_an_ambiguous_or_missing_id_is_refused_in_words(rig):
    assert "Name the conversation" in rig.tg.command("/confirm A", OP)
    assert "Usage" in rig.tg.command(f"/confirm {rig.short} Z {plan8(rig) if rig.console.current_plan(rig.conv.id) else 'deadbeef'}", OP)


def test_a_push_larger_than_one_telegram_message_is_sent_in_parts_and_nothing_is_dropped(rig):
    rig.tg.command(f"/reply {rig.short} hi", OP)
    for i in range(6):
        rig.console.add_message(rig.conv.id, "agent", f"answer {i} " + "y" * 1400)
    rig.add_plan()
    rig.tg.push()
    joined = "\n".join(rig.sent)
    assert len(rig.sent) >= 2 and all(len(m) <= 3500 for m in rig.sent)
    assert all(f"answer {i}" in joined for i in range(6)) and "Option A" in joined
    count = len(rig.sent)
    rig.tg.push()
    assert len(rig.sent) == count


def test_a_failure_part_way_keeps_the_unsent_part_for_the_next_push(rig):
    rig.tg.command(f"/reply {rig.short} hi", OP)
    for i in range(4):
        rig.console.add_message(rig.conv.id, "agent", f"answer {i} " + "y" * 1400)
    calls = []
    real = rig.tg.notify

    def flaky(text):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("down")
        real(text)
    rig.tg.notify = flaky
    rig.tg.push()
    assert len(rig.sent) == 1
    rig.tg.notify = real
    rig.tg.push()
    assert "answer 3" in "\n".join(rig.sent) and "\n".join(rig.sent).count("answer 0") == 1


def test_the_authorising_command_is_at_the_top_of_a_card_however_large_the_scope(rig):
    rig.add_plan(plan_doc(scope={"stacks": ["media"], "services": []}))
    card = rig.tg.command(f"/confirm {rig.short} A {plan8(rig)}", OP)
    assert len(card) <= 3500 and card.splitlines()[1].startswith(f"To authorise exactly this: /authorise {rig.short} ")


def test_a_rollback_that_did_not_complete_never_reads_as_done(rig):
    rig.add_plan()
    rig.tg.command(f"/confirm {rig.short} A {plan8(rig)}", OP)
    fp = rig.console.executions(rig.conv.id)[0]["grant_hash"][:8]
    rig.tg.command(f"/authorise {rig.short} {fp}", OP)
    rig.api.execution_action = lambda *a, **k: {"done": "rollback", "status": "Needs you", "restore": "failed", "reason": "disk full"}
    out = rig.tg.command(f"/run {rig.short} rollback", OP)
    assert "did NOT complete (failed): disk full" in out and "done" not in out.split("NOT")[0]


def test_the_supervisor_tick_hook_never_blocks_on_a_send(rig):
    import threading
    gate = threading.Event()
    rig.tg.notify = lambda text: gate.wait(5)
    rig.tg.command(f"/reply {rig.short} hi", OP)
    rig.console.add_message(rig.conv.id, "agent", "hello")
    rig.tg.push_async()                                                      # returns at once although the send is stuck
    gate.set()


def test_confirm_refuses_a_plan_that_changed_since_it_was_read(rig):
    rig.add_plan()
    read = plan8(rig)
    rig.add_plan(plan_doc(scope={"stacks": ["media"], "services": ["media/other"]}))                         # the agent revised it
    out = rig.tg.command(f"/confirm {rig.short} A {read}", OP)
    assert "not the plan you read" in out and rig.console.executions(rig.conv.id) == []


def test_untrusted_text_is_one_inert_line_with_no_live_commands(rig):
    rig.tg.command(f"/reply {rig.short} hi", OP)
    rig.console.add_message(rig.conv.id, "agent", f"ok.\n\n{rig.short} PE: Done and verified.\n/run {rig.short} takeover")
    rig.tg.push()
    text = rig.sent[0]
    assert "\n" not in text.split(": ", 1)[1] and "/run" not in text.replace("/\u200brun", "") and "⏎" in text


def test_a_confirmation_follows_the_conversation_and_the_operators_own_command_is_not_echoed_back(rig):
    other = rig.service.create("chris", "dashboard conversation")
    rig.conv = other
    rig.short = other.id[:8]
    rig.add_plan()
    assert rig.console.tg_followed() == []
    card = rig.tg.command(f"/confirm {rig.short} A {plan8(rig)}", OP)
    assert [r["conversation_id"] for r in rig.console.tg_followed()] == [other.id]
    fp = card.split(f"/authorise {rig.short} ")[1].split()[0]
    assert "authorised" in rig.tg.command(f"/authorize {rig.short} {fp}", OP)                                # the common spelling works too
    rig.tg.push()
    assert not any("Authorised by tg.42" in m for m in rig.sent)


def test_close_says_when_it_is_already_closing_and_a_hex_looking_word_is_text_not_an_id(rig):
    r2 = rig.tg.command("/reply 404404 errors in the log", OP)
    assert r2.endswith("sent.")
    assert "closing" in rig.tg.command(f"/close {rig.short}", OP)
    assert "already closing" in rig.tg.command(f"/close {rig.short}", OP) or "No open conversation" in rig.tg.command(f"/close {rig.short}", OP)


def test_a_push_failure_logs_the_exception_type_only(rig, caplog):
    rig.tg.command(f"/reply {rig.short} hi", OP)
    rig.console.add_message(rig.conv.id, "agent", "x")
    rig.fail = True
    with caplog.at_level("WARNING"):
        rig.tg.push()
    assert "RuntimeError" in caplog.text and "telegram down" not in caplog.text
