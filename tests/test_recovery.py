"""Crash classification (slice 8, contract 11): investigation, execution and verification, each with fake host state:
unchanged from the checkpoint, matching the expectation, and diverged. Nothing is ever replayed; a hostile event stream
cannot influence the outcome."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkpoint_fakes import inspect_doc
from verify_fakes import build

from planet_express.missions.models import Grant
from planet_express.missions.recovery import (
    Classification,
    HostState,
    apply_classification,
    classify,
    collect_host_state,
    end_expectation,
    journal_done,
    journal_intent,
    matches_checkpoint,
    open_intents,
)
from planet_express.missions.state import transition

PHASES = {"investigating": "investigation", "executing": "execution", "verifying": "verification"}


def make(tmp_path, **kw):
    rig = build(tmp_path, status="executing", **kw)
    rig.manifest = rig.svc.manifest(rig.cid)
    return rig


def host(rig):
    return collect_host_state(rig.svc, rig.manifest)


def mission_in(rig, status, **update):
    return rig.ms.get_mission(rig.mid).model_copy(update={"status": status, **update})


def change_with_expectation(rig, status, expected):
    plan = json.dumps({"summary": "x", "protected_touches": [], "expected_state": expected})
    return mission_in(rig, status, tier="change", plan_json=plan)


def diverge(rig):
    rig.env.compose.write_text("services: {web: {image: evil}}\n")


def end_state(rig):
    """The state the approved plan expects: a new compose file and a new image. Returns the expectation dict."""
    new = "services:\n  web:\n    image: nginx:1.28\n"
    rig.env.compose.write_text(new)
    rig.env.docker.containers[("media", "web")] = inspect_doc(image="sha256:bbb")
    import hashlib
    return {"files": {str(rig.env.compose.resolve()): hashlib.sha256(new.encode()).hexdigest()},
            "services": {"media/web": {"image_id": "sha256:bbb"}}}


# ── host reading ────────────────────────────────────────────────────────────
def test_unchanged_host_matches_the_checkpoint_and_a_change_does_not(tmp_path):
    rig = make(tmp_path)
    assert matches_checkpoint(host(rig), rig.manifest)
    diverge(rig)
    assert not matches_checkpoint(host(rig), rig.manifest)


def test_a_changed_service_or_unreadable_state_is_not_the_checkpoint(tmp_path):
    rig = make(tmp_path)
    rig.env.docker.containers[("media", "web")] = inspect_doc(image="sha256:zzz")
    assert not matches_checkpoint(host(rig), rig.manifest)
    rig = make(tmp_path / "b")
    rig.env.compose.chmod(0)                                    # present but unreadable (as root this may still read)
    state = host(rig)
    assert matches_checkpoint(state, rig.manifest) or not state.readable
    assert not matches_checkpoint(HostState(readable=False), rig.manifest)
    assert not matches_checkpoint(HostState(), rig.manifest)    # nothing known about any file


# ── three phases x three cases ──────────────────────────────────────────────
@pytest.mark.parametrize("status", ["investigating", "executing", "verifying"])
def test_unchanged_from_checkpoint_needs_the_operator_and_offers_resume(tmp_path, status):
    rig = make(tmp_path)
    c = classify(mission_in(rig, status), host(rig), rig.manifest, rig.ms.events(rig.mid))
    assert c.state == "needs_operator" and "resume" in c.offers and c.auto_restart is False
    assert c.phase == PHASES[status] and not c.partially_applied


@pytest.mark.parametrize("status", ["investigating", "executing", "verifying"])
def test_matching_the_end_expectation_goes_to_verifying(tmp_path, status):
    rig = make(tmp_path)
    expected = end_state(rig)
    c = classify(change_with_expectation(rig, status, expected), host(rig), rig.manifest, rig.ms.events(rig.mid))
    assert c.state == "verifying" and c.next_state is None and not c.partially_applied
    assert c.phase == PHASES[status]


@pytest.mark.parametrize("status", ["investigating", "executing", "verifying"])
def test_diverged_is_recovery_required_then_operator_or_rollback(tmp_path, status):
    rig = make(tmp_path)
    diverge(rig)
    c = classify(mission_in(rig, status), host(rig), rig.manifest, rig.ms.events(rig.mid))
    assert (c.state, c.next_state, c.partially_applied) == ("recovery_required", "needs_operator", True)
    assert "rollback" in c.offers
    rb = make(tmp_path / "rb", on_verify_fail="rollback")
    diverge(rb)
    c = classify(mission_in(rb, status), host(rb), rb.manifest, rb.ms.events(rb.mid))
    assert (c.state, c.next_state) == ("recovery_required", "rollback_pending")


def test_neither_checkpoint_nor_expectation_is_diverged_even_when_a_plan_has_an_expectation(tmp_path):
    rig = make(tmp_path)
    expected = end_state(rig)
    expected["services"]["media/web"]["image_id"] = "sha256:other"          # host is not what the plan expects
    c = classify(change_with_expectation(rig, "executing", expected), host(rig), rig.manifest, [])
    assert c.state == "recovery_required"


def test_unreadable_host_state_or_missing_checkpoint_fails_closed(tmp_path):
    rig = make(tmp_path)
    assert classify(mission_in(rig, "executing"), HostState(readable=False), rig.manifest, []).state == "recovery_required"
    for state, manifest in ((None, rig.manifest), (host(rig), None)):
        c = classify(mission_in(rig, "executing"), state, manifest, [])
        assert c.state == "recovery_required" and c.next_state == "needs_operator" and c.partially_applied


def test_observe_and_propose_alone_may_restart_automatically(tmp_path):
    rig = make(tmp_path)
    for tier in ("observe", "propose"):
        m = mission_in(rig, "investigating", tier=tier, grant_json=Grant(tier=tier).canonical())
        c = classify(m, None, None, [])
        assert c.state == "needs_operator" and c.auto_restart is True and "restart" in c.offers
    for status in ("investigating", "executing", "verifying"):
        assert classify(mission_in(rig, status), host(rig), rig.manifest, []).auto_restart is False


def test_terminal_missions_are_not_classified(tmp_path):
    rig = make(tmp_path)
    with pytest.raises(ValueError):
        classify(mission_in(rig, "completed"), host(rig), rig.manifest, [])


# ── the journal and replay ──────────────────────────────────────────────────
def test_an_intent_with_unknown_effect_is_never_replayed(tmp_path):
    rig = make(tmp_path)
    a = journal_intent(rig.ms, rig.mid, "execution", "apply the fix")
    b = journal_intent(rig.ms, rig.mid, "execution", "restart the service")
    journal_done(rig.ms, rig.mid, b, "applied")
    assert open_intents(rig.ms.events(rig.mid)) == [a]
    for state_change in (lambda: None, lambda: diverge(rig)):
        state_change()
        c = classify(mission_in(rig, "executing"), host(rig), rig.manifest, rig.ms.events(rig.mid))
        assert c.refused_replays == (a,) and "replay" not in c.offers
        assert c.state in ("needs_operator", "recovery_required")          # an operator decision, never an automatic redo


def test_journal_validates_its_inputs(tmp_path):
    rig = make(tmp_path)
    with pytest.raises(ValueError):
        journal_intent(rig.ms, rig.mid, "bogus")
    with pytest.raises(ValueError):
        journal_done(rig.ms, rig.mid, "I-1", "maybe")


def test_a_hostile_event_stream_cannot_influence_the_classification(tmp_path):
    rig = make(tmp_path)
    diverge(rig)
    before = classify(mission_in(rig, "executing"), host(rig), rig.manifest, rig.ms.events(rig.mid))
    # The agent (worker/tool actors) claims success, a finished intent and a clean checkpoint match.
    for actor, kind in (("worker", "note"), ("worker", "decision"), ("tool-broker", "tool"), ("operator:x", "decision"),
                        ("core-impostor", "note")):
        rig.ms.append_event(rig.mid, kind, actor, {"event": "intent.done", "id": "I-forged", "effect": "applied"})
        rig.ms.append_event(rig.mid, kind, actor, {"event": "verify.result", "status": "passed",
                                                   "state": "matches checkpoint", "success": True})
        rig.ms.append_event(rig.mid, kind, actor, {"event": "intent", "id": "I-forged", "phase": "execution"})
    after = classify(mission_in(rig, "executing"), host(rig), rig.manifest, rig.ms.events(rig.mid))
    assert after == before and after.state == "recovery_required" and after.refused_replays == ()
    forged_done = journal_intent(rig.ms, rig.mid, "execution", "real intent")
    rig.ms.append_event(rig.mid, "note", "worker", {"event": "intent.done", "id": forged_done, "effect": "applied"})
    assert open_intents(rig.ms.events(rig.mid)) == [forged_done]          # a worker cannot close Core's intent


def test_the_expectation_comes_only_from_an_approved_plan(tmp_path):
    rig = make(tmp_path)
    assert end_expectation(mission_in(rig, "executing")) is None                       # repair: no plan
    bad = mission_in(rig, "executing", tier="change", plan_json=json.dumps({"expected_state": {"files": {"/a": 1}}}))
    assert end_expectation(bad) is None
    odd = mission_in(rig, "executing", tier="change",
                     plan_json=json.dumps({"expected_state": {"services": {"s/x": {"unknown_field": 1}}}}))
    assert end_expectation(odd) is None
    assert end_expectation(mission_in(rig, "executing", tier="change", plan_json="not json")) is None
    # an agent's event can never create one
    rig.ms.append_event(rig.mid, "note", "worker", {"expected_state": {"files": {"/a": "0" * 64}}})
    assert end_expectation(mission_in(rig, "executing")) is None


# ── applying a classification ───────────────────────────────────────────────
def test_apply_moves_the_mission_and_records_the_decision(tmp_path):
    rig = make(tmp_path)
    c = classify(mission_in(rig, "executing"), host(rig), rig.manifest, rig.ms.events(rig.mid))
    assert apply_classification(rig.ms, rig.mid, c) == "needs_operator"
    diverge(rig)
    rig2 = make(tmp_path / "d")
    diverge(rig2)
    c = classify(mission_in(rig2, "executing"), host(rig2), rig2.manifest, [])
    assert apply_classification(rig2.ms, rig2.mid, c) == "needs_operator"
    m = rig2.ms.get_mission(rig2.mid)
    assert m.outcome_flags == "partially_applied"
    decided = [json.loads(e.body_json) for e in rig2.ms.events(rig2.mid) if e.kind == "decision"
               and json.loads(e.body_json).get("event") == "recovery.classified"]
    assert decided and decided[0]["state"] == "recovery_required" and decided[0]["next"] == "needs_operator"
    states = [json.loads(e.body_json)["to"] for e in rig2.ms.events(rig2.mid) if e.kind == "state"]
    assert states[-2:] == ["recovery_required", "needs_operator"]


def test_apply_rollback_pending_and_verifying(tmp_path):
    rig = make(tmp_path, on_verify_fail="rollback")
    diverge(rig)
    c = classify(mission_in(rig, "executing"), host(rig), rig.manifest, [])
    assert apply_classification(rig.ms, rig.mid, c) == "rollback_pending"
    rig = make(tmp_path / "v")
    end_state(rig)
    c = Classification("verifying", "matches", "execution")
    assert apply_classification(rig.ms, rig.mid, c) == "verifying"


def test_recovery_cannot_complete_a_mission(tmp_path):
    rig = make(tmp_path)
    transition(rig.store, rig.mid, "verifying", "core")
    c = Classification("verifying", "claimed", "verification")
    assert apply_classification(rig.ms, rig.mid, c) == "verifying"          # already there: nothing moves it to completed
    assert rig.ms.get_mission(rig.mid).status != "completed"
