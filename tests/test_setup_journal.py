import json
import os
import stat

import pytest

from planet_express.setup.journal import MASK, Journal, JournalCorrupt, make_redactor


def test_events_are_numbered_in_order_and_survive_a_reopen_with_numbering_continued(tmp_path):
    first = Journal(tmp_path, "plan1")
    first.append("apply_started")
    first.append("step_started", step="s01")
    reopened = Journal(tmp_path, "plan1")
    event = reopened.append("step_ok", step="s01", effect="applied")
    assert [e["seq"] for e in reopened.events()] == [1, 2, 3] and event["seq"] == 3
    assert [e["type"] for e in reopened.events(after=1)] == ["step_started", "step_ok"]


def test_the_journal_is_private_and_written_with_mode_0600(tmp_path):
    journal = Journal(tmp_path, "p")
    journal.append("apply_started")
    assert stat.S_IMODE(os.stat(journal.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(journal.path).st_mode) == 0o600


def test_a_torn_final_line_is_discarded_and_appending_continues_cleanly(tmp_path):
    journal = Journal(tmp_path, "p")
    journal.append("apply_started")
    journal.append("step_started", step="s01")
    with open(journal.path, "ab") as handle:
        handle.write(b'{"seq": 3, "type": "step_ok", "ste')      # power lost mid-write
    reopened = Journal(tmp_path, "p")
    assert [e["seq"] for e in reopened.events()] == [1, 2]
    reopened.append("step_failed", step="s01", effect="unknown", reason="crashed")
    assert [e["seq"] for e in reopened.events()] == [1, 2, 3]
    assert reopened.path.read_bytes().endswith(b"\n")
    for line in reopened.path.read_text().splitlines():
        json.loads(line)                                        # every remaining line is whole


def test_a_bad_line_that_is_not_the_last_is_corruption_not_a_crash(tmp_path):
    journal = Journal(tmp_path, "p")
    journal.append("apply_started")
    journal.append("step_started", step="s01")
    lines = journal.path.read_text().splitlines()
    journal.path.write_text(lines[0] + "\nNOT JSON\n" + lines[1] + "\n")
    with pytest.raises(JournalCorrupt, match="line 2"):
        Journal(tmp_path, "p").events()


def test_step_state_is_folded_from_the_events(tmp_path):
    journal = Journal(tmp_path, "p")
    journal.append("step_started", step="s01")
    journal.append("evidence", step="s01", data={"created_file": True, "ino": 5})
    journal.append("step_ok", step="s01", effect="applied")
    journal.append("step_started", step="s02")
    journal.append("step_started", step="s03")
    journal.append("step_failed", step="s03", effect="unknown", reason="boom")
    journal.append("step_started", step="s04")
    journal.append("step_ok", step="s04", effect="not_applied", satisfied=True)
    steps = journal.steps()
    assert (steps["s01"].status, steps["s01"].effect, steps["s01"].evidence) == ("ok", "applied", [{"created_file": True, "ino": 5}])
    assert steps["s02"].status == "started"                      # started, no result: needs reconciling
    assert (steps["s03"].status, steps["s03"].reason) == ("failed", "boom")
    assert steps["s04"].satisfied is True


def test_secret_values_are_masked_everywhere_in_an_event_including_nested_data(tmp_path):
    redact = make_redactor({"telegram_token": "123456:SECRET-TOKEN", "short": "ab"})
    journal = Journal(tmp_path, "p", redact=redact)
    journal.append("log", step="s01", line="wrote TG_BOT_TOKEN=123456:SECRET-TOKEN to the env file",
                   data={"nested": ["x 123456:SECRET-TOKEN y"], "n": 3})
    text = journal.path.read_text()
    assert "SECRET-TOKEN" not in text and text.count(MASK) == 2
    assert "ab" in make_redactor({"short": "ab"})("ab")           # values under 4 characters are not masked


def test_a_longer_secret_is_masked_whole_even_when_a_shorter_one_is_its_prefix():
    redact = make_redactor({"a": "secret", "b": "secret-and-more"})
    assert redact("x secret-and-more y") == f"x {MASK} y"


def test_different_plans_have_separate_journals(tmp_path):
    Journal(tmp_path, "one").append("apply_started")
    assert Journal(tmp_path, "two").events() == []


def test_a_step_that_failed_and_then_succeeded_no_longer_carries_the_failure_reason(tmp_path):
    journal = Journal(tmp_path / "j", "abc")
    journal.append("step_started", step="s01")
    journal.append("step_failed", step="s01", effect="not_applied", reason="boom")
    journal.append("step_started", step="s01")
    journal.append("step_ok", step="s01", effect="applied")
    record = journal.steps()["s01"]
    assert record.status == "ok" and record.reason is None
