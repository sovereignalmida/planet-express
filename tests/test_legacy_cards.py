"""What happens to work left over from before slice 5b-5: a pending shell plan, and the cards that
used to run one."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
import config
from notifier import Decision, FakeNotifier


class FakeTg:
    chat_id = "42"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "STATE_STATUS", tmp_path / "run_status.json")
    return tmp_path


@pytest.mark.parametrize("kind", ["plan", "diff"])
@pytest.mark.parametrize("approved", [True, False])
def test_a_pre_upgrade_card_says_it_can_no_longer_be_approved(kind, approved):
    notifier = FakeNotifier()
    notifier.queue_decision(Decision(request_id="p1", kind=kind, approved=approved))

    fw.handle_callback({}, FakeTg(), notifier, fw.PipelineState(), commands=None)

    assert len(notifier.resolutions) == 1
    text = " ".join(str(part) for part in notifier.resolutions[0])
    assert "no longer be approved" in text
    assert notifier.notifications == []


def test_a_pending_legacy_plan_is_announced_once_and_retired(state_dir):
    (state_dir / "pending_plan.json").write_text(json.dumps({
        "planned_at": "2026-09-01T00:00:00+00:00",
        "plans": [{"id": "p1", "title": "Repair the gluetun port forward"}],
    }))
    notifier = FakeNotifier()

    fw._retire_legacy_state(notifier)

    assert not (state_dir / "pending_plan.json").exists()
    assert (state_dir / "pending_plan.json.retired").exists()   # kept as the record of what it was
    assert any("Repair the gluetun port forward" in m for m in notifier.notifications)
    assert any("typed runbooks now" in m for m in notifier.notifications)

    notifier.notifications.clear()
    fw._retire_legacy_state(notifier)
    assert notifier.notifications == []      # ...and not again on the next start


def test_no_legacy_plan_file_means_no_message(state_dir):
    notifier = FakeNotifier()
    fw._retire_legacy_state(notifier)
    assert notifier.notifications == []


def test_an_unreadable_legacy_plan_file_is_still_retired(state_dir):
    (state_dir / "pending_plan.json").write_text("{ this is not json")
    notifier = FakeNotifier()

    fw._retire_legacy_state(notifier)

    assert (state_dir / "pending_plan.json.retired").exists()
    assert notifier.notifications == []      # nothing to name, so nothing to say


def test_a_pending_compose_diff_is_announced_and_retired(state_dir):
    """The live host still had one of these at upgrade time (Codex, T44)."""
    (state_dir / "pending_diffs.json").write_text(json.dumps({
        "diff-media-1757000000": {"stack": "media", "reason": "Amy's fix for sonarr",
                                  "new_content": "services:\n  sonarr:\n    image: x\n"},
    }))
    notifier = FakeNotifier()

    fw._retire_legacy_state(notifier)

    assert not (state_dir / "pending_diffs.json").exists()
    assert (state_dir / "pending_diffs.json.retired").exists()
    text = "\n".join(notifier.notifications)
    assert "compose diff" in text and "media" in text and "Amy's fix for sonarr" in text


def test_a_stale_awaiting_approval_status_is_normalised(state_dir):
    (state_dir / "run_status.json").write_text(json.dumps({
        "schema_version": 1, "state": "awaiting_approval", "pending_plan_id": "p1",
        "pending_msg_id": 397, "updated_at": "2026-09-01T00:00:00+00:00",
    }))

    fw._retire_legacy_state(FakeNotifier())

    status = json.loads((state_dir / "run_status.json").read_text())
    assert status["state"] == "idle"
    assert status["pending_plan_id"] is None and status["pending_msg_id"] is None


def test_a_healthy_status_file_is_left_alone(state_dir):
    original = {"schema_version": 1, "state": "running", "pending_plan_id": None,
                "pending_msg_id": None, "updated_at": "2026-09-24T00:00:00+00:00"}
    (state_dir / "run_status.json").write_text(json.dumps(original))

    fw._retire_legacy_state(FakeNotifier())

    assert json.loads((state_dir / "run_status.json").read_text()) == original


def test_skip_no_longer_touches_the_pipeline_state():
    """`/skip` forced IDLE to clear a pending plan. With no pending plans it could only lie about
    a running scan — and let a second scan or a mutation start alongside it (Codex, T44)."""
    notifier = FakeNotifier()
    state = fw.PipelineState()
    state.transition(fw.PipelineState.RUNNING)

    fw.handle_message({"message": {"text": "/skip p1", "chat": {"id": 42}, "from": {"id": 1}}},
                      FakeTg(), notifier, state, commands=None)

    assert state.state == fw.PipelineState.RUNNING       # the scan is still running, and says so
    text = "\n".join(notifier.notifications)
    assert "DENY" in text and "/abort" in text and "/rollback" in text


def test_a_notice_that_cannot_be_delivered_leaves_the_file_for_next_time(state_dir):
    """A repeated message is a nuisance; a dropped one means the operator never learns their
    pending work is gone (Codex, T44)."""
    (state_dir / "pending_plan.json").write_text(json.dumps({
        "planned_at": "2026-09-01T00:00:00+00:00", "plans": [{"id": "p1", "title": "Repair it"}]}))
    notifier = FakeNotifier()
    notifier.notify = lambda message: (_ for _ in ()).throw(RuntimeError("telegram is down"))

    with pytest.raises(RuntimeError):
        fw._retire_legacy_state(notifier)

    assert (state_dir / "pending_plan.json").exists()
    assert not (state_dir / "pending_plan.json.retired").exists()
