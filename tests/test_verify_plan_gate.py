"""Verification plan, grant binding and the completion gate (slice 8, contract 10 and 3)."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions_helpers import make_store, new_mission, repair_grant
from verify_fakes import HTTP, build, plan_of

from planet_express.missions.models import Grant, VerificationPlan
from planet_express.missions.state import IllegalTransition, transition
from planet_express.missions.store import MissionStore
from planet_express.missions.verify.plan import (
    PlanInvalid,
    VerificationResult,
    binding_problems,
    parse_plan,
    parse_result,
    plan_hash,
)


# ── the plan ────────────────────────────────────────────────────────────────
def test_a_valid_plan_parses_and_hashes_canonically():
    plan = parse_plan({"catalogue_v": 1, "checks": [HTTP]})
    assert plan_hash(plan) == plan.hash() and len(plan.hash()) == 64
    reordered = parse_plan('{"checks":[{"params":{"consecutive":1,"url":"http://app.example/health"},'
                           '"check":"http","id":"route"}],"catalogue_v":1}')
    assert plan_hash(reordered) == plan_hash(plan)


@pytest.mark.parametrize("bad", [
    {"catalogue_v": 1, "checks": [{"id": "a", "check": "shell", "params": {}}]},                         # unknown kind
    {"catalogue_v": 1, "checks": [{"id": "a", "check": "http",
                                   "params": {"url": "http://x/", "stray": 1}}]},                         # extra param
    {"catalogue_v": 1, "checks": [{"id": "a", "check": "http", "params": {"url": "http://x/", "timeout_s": 61}}]},
    {"catalogue_v": 1, "checks": [{"id": "a", "check": "http", "params": {"url": "file:///etc/passwd"}}]},
    {"catalogue_v": 1, "checks": [{"id": "a", "check": "http", "params": {}}]},                           # missing url
    {"catalogue_v": 2, "checks": [HTTP]},                                                                 # unsupported catalogue
    {"catalogue_v": 1, "checks": []},                                                                     # empty
    {"catalogue_v": 1, "checks": [HTTP, HTTP]},                                                           # duplicate ids
    {"catalogue_v": 1, "checks": [HTTP], "extra": 1},
])
def test_unknown_kinds_extra_params_and_bad_values_refuse(bad):
    with pytest.raises(PlanInvalid):
        parse_plan(bad)


def test_duplicate_keys_and_floats_refuse():
    with pytest.raises(PlanInvalid):
        parse_plan('{"catalogue_v":1,"catalogue_v":1,"checks":[]}')
    with pytest.raises(PlanInvalid):
        parse_plan('{"catalogue_v":1,"checks":[{"id":"a","check":"http","params":{"url":"http://x/","consecutive":1.0}}]}')


def test_a_changed_plan_changes_the_hash():
    a = plan_of(HTTP)
    b = plan_of({**HTTP, "params": {**HTTP["params"], "expect_status": 204}})
    assert a.hash() != b.hash()


# ── grant binding ───────────────────────────────────────────────────────────
def grant_for(plan, **over):
    return repair_grant(verification={"checks": [c.id for c in plan.checks], "plan_sha256": plan.hash()}, **over)


def test_binding_requires_a_matching_plan_for_every_mutating_tier():
    plan = plan_of(HTTP)
    g = grant_for(plan)
    assert binding_problems(g, plan) == []
    assert binding_problems(g, None)                                    # plan missing
    assert binding_problems(repair_grant(), plan)                       # grant carries no hash
    other = plan_of({**HTTP, "id": "other"})
    assert binding_problems(g, other)                                   # hash and ids differ
    tampered = plan_of({**HTTP, "params": {**HTTP["params"], "expect_status": 500}})
    assert any("plan_sha256" in p for p in binding_problems(g, tampered))
    for tier in ("change", "critical"):
        assert binding_problems(grant_for(plan, tier=tier), None)
    for tier in ("observe", "propose"):
        assert binding_problems(Grant.model_validate({"tier": tier}), None) == []


def test_a_changed_plan_changes_the_grant_hash_and_invalidates_approval(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    plan = plan_of(HTTP)
    m = new_mission(ms, grant_for(plan))
    ms.set_verification_plan(m.id, plan)
    new_plan = plan_of({**HTTP, "params": {**HTTP["params"], "expect_status": 204}})
    # the old grant cannot carry the new plan ...
    with pytest.raises(ValueError, match="plan_sha256"):
        ms.set_verification_plan(m.id, new_plan)
    # ... and a grant that names it is a different grant: a different hash, so approvals stop matching.
    assert grant_for(new_plan).hash() != grant_for(plan).hash()


def test_the_store_refuses_a_plan_the_grant_does_not_name(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, grant_for(plan_of(HTTP)))
    with pytest.raises(ValueError):
        ms.set_verification_plan(m.id, plan_of({**HTTP, "id": "x"}))
    assert ms.get_mission(m.id).verification_plan_json is None


# ── the completion gate ─────────────────────────────────────────────────────
def passing_result(rig, **over):
    mission = rig.ms.get_mission(rig.mid)
    body = dict(status="passed", grant_hash=mission.grant_hash, checkpoint_id=mission.checkpoint_id,
                plan_sha256=rig.plan.hash(), at=1)
    body.update(over)
    return VerificationResult(**body)


def test_completion_without_a_result_fails_at_the_state_layer(tmp_path):
    rig = build(tmp_path)
    with pytest.raises(IllegalTransition, match="completion refused"):
        transition(rig.store, rig.mid, "completed", "operator:me")
    assert rig.ms.get_mission(rig.mid).status == "verifying"


def test_a_passed_result_for_the_current_grant_completes_and_frees_the_leases(tmp_path):
    rig = build(tmp_path)
    rig.ms.set_verification_result(rig.mid, passing_result(rig))
    transition(rig.store, rig.mid, "completed", "core")
    m = rig.ms.get_mission(rig.mid)
    assert m.status == "completed" and m.closed_at is not None


def test_a_failed_result_never_completes(tmp_path):
    rig = build(tmp_path)
    rig.ms.set_verification_result(rig.mid, passing_result(rig, status="failed", reason="no"))
    with pytest.raises(IllegalTransition, match="not passed"):
        transition(rig.store, rig.mid, "completed", "core")


def test_a_result_for_an_older_grant_hash_cannot_be_stored_or_used(tmp_path):
    rig = build(tmp_path)
    with pytest.raises(ValueError, match="current grant hash"):
        rig.ms.set_verification_result(rig.mid, passing_result(rig, grant_hash="0" * 64))
    # planted straight into the table (a store-level bypass): the gate still refuses it
    with sqlite3.connect(rig.store.path) as conn:
        conn.execute("UPDATE missions SET verification_result_json=? WHERE id=?",
                     (passing_result(rig, grant_hash="1" * 64).canonical(), rig.mid))
    with pytest.raises(IllegalTransition, match="different grant hash"):
        transition(rig.store, rig.mid, "completed", "core")


def test_a_result_for_another_checkpoint_or_plan_is_refused(tmp_path):
    rig = build(tmp_path)
    for over, text in (({"checkpoint_id": "C-" + "0" * 16}, "checkpoint"), ({"plan_sha256": "2" * 64}, "plan")):
        with sqlite3.connect(rig.store.path) as conn:
            conn.execute("UPDATE missions SET verification_result_json=? WHERE id=?",
                         (passing_result(rig, **over).canonical(), rig.mid))
        with pytest.raises(IllegalTransition, match=text):
            transition(rig.store, rig.mid, "completed", "core")


def test_garbage_in_the_result_column_fails_closed(tmp_path):
    rig = build(tmp_path)
    for junk in ("{}", "not json", '{"status":"passed"}', '{"v":1,"status":"passed","extra":1}'):
        with sqlite3.connect(rig.store.path) as conn:
            conn.execute("UPDATE missions SET verification_result_json=? WHERE id=?", (junk, rig.mid))
        with pytest.raises(IllegalTransition):
            transition(rig.store, rig.mid, "completed", "core")
    assert parse_result("nope") is None


def test_agent_asserted_success_is_not_a_result(tmp_path):
    rig = build(tmp_path)
    rig.ms.append_event(rig.mid, "note", "worker", {"event": "verify.result", "status": "passed"})
    rig.ms.append_event(rig.mid, "decision", "worker", {"verification_result": "passed"})
    with pytest.raises(IllegalTransition, match="no readable verification result"):
        transition(rig.store, rig.mid, "completed", "core")


def test_completion_only_from_verifying_even_with_a_stored_result(tmp_path):
    rig = build(tmp_path)
    rig.ms.set_verification_result(rig.mid, passing_result(rig))
    with sqlite3.connect(rig.store.path) as conn:
        conn.execute("UPDATE missions SET status='awaiting_decision' WHERE id=?", (rig.mid,))
    with pytest.raises(IllegalTransition, match="only from verifying"):
        transition(rig.store, rig.mid, "completed", "operator:me")


def test_a_result_is_voided_when_the_mission_leaves_and_re_enters_verifying(tmp_path):
    rig = build(tmp_path)
    rig.ms.set_verification_result(rig.mid, passing_result(rig))
    transition(rig.store, rig.mid, "needs_operator", "core")
    transition(rig.store, rig.mid, "executing", "operator:me")
    assert rig.ms.get_mission(rig.mid).verification_result_json is None
    transition(rig.store, rig.mid, "verifying", "core")
    with pytest.raises(IllegalTransition):
        transition(rig.store, rig.mid, "completed", "core")


def test_a_result_is_stored_only_while_verifying(tmp_path):
    rig = build(tmp_path, status="executing")
    with pytest.raises(ValueError, match="only while verifying"):
        rig.ms.set_verification_result(rig.mid, passing_result(rig))


def test_observe_and_propose_missions_are_not_gated(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    for tier in ("observe", "propose"):
        m = new_mission(ms, Grant.model_validate({"tier": tier}))
        for state in ("queued", "investigating", "completed"):
            transition(store, m.id, state, "operator:me")
        assert ms.get_mission(m.id).status == "completed"


def test_verification_plan_model_is_the_hashed_one():
    assert VerificationPlan is not None and plan_of(HTTP).hash() == parse_plan({"catalogue_v": 1, "checks": [HTTP]}).hash()
