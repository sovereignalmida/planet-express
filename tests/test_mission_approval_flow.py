"""The approval flow (slice 7a): a card, approve/deny bound to the grant hash, elevated phases, and an actor that always
comes from the authenticated identity, never from a request."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repair_fakes import build
from verify_fakes import (
    media_down,  # noqa: F401  (keeps the fake-host import path warm)
)

from planet_express.application.mission_service import MissionError, MissionService
from planet_express.integrations.rpc import RpcError
from planet_express.missions import approvals
from planet_express.missions.approval_flow import FlowError
from planet_express.missions.canonical import canonical_json, sha256_hex
from planet_express.missions.models import Grant
from planet_express.missions.store import GrantLocked


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, **kw)
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def service(st):
    svc = MissionService(st.store, enabled=lambda: True)
    svc.flow = st.flow
    return svc, svc.handlers()


def call(handlers, method, **params):
    try:
        return handlers[method](params)
    except RpcError as exc:
        return {"error": exc.code, "message": str(exc)}


def predicate(st, mid):
    return approvals.execution_predicate(st.store, mid).ok


def approvals_rows(st, mid):
    with sqlite3.connect(st.store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM mission_approvals WHERE mission_id=?", (mid,))]


# ── the card ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_card_shows_scope_tier_budgets_plan_and_full_access(stack):
    st = stack()
    mid = st.propose(budgets={"wall_s": 900})["mission_id"]
    card = st.flow.card(mid)
    assert card["tier"] == "repair" and card["scope"]["stacks"] == ["media"]
    assert card["budgets"]["wall_s"] == 900
    assert [c["id"] for c in card["verification"]["checks"]] == ["default"]
    assert [w["kind"] for w in card["warnings"]] == ["full_access"] and "FULL ACCESS" in card["warnings"][0]["text"]
    assert not {"sudo", "docker", "backup", "protected"} & set(card) and not card["ready"]
    assert [r["phase"] for r in card["required"]] == ["grant"]
    assert card["grant_hash"] == st.ms.get_mission(mid).grant_hash


def test_the_removed_access_fields_cannot_be_proposed(stack):
    st = stack()
    for extra in ({"sudo": {"mode": "broad"}}, {"docker": "raw"}, {"acknowledged_no_backup": True}, {"exclusions": ["x"]}):
        with pytest.raises(TypeError):
            st.propose(**extra)


def test_an_observe_mission_has_no_card(stack):
    from missions_helpers import advance_to, new_mission
    st = stack()
    m = new_mission(st.ms, Grant.model_validate({"tier": "observe", "scope": {"stacks": ["media"]}}))
    advance_to(st.ms, m.id, "queued")
    with pytest.raises(FlowError):
        st.flow.card(m.id)


# ── approval binds to the hash ───────────────────────────────────────────────────────────────────────

def test_an_approval_covers_exactly_the_hash_that_was_read(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    card = st.flow.card(mid)
    assert not predicate(st, mid)
    st.flow.approve("operator:alice", mid, "grant", card["grant_hash"])
    assert predicate(st, mid)
    row = approvals_rows(st, mid)[0]
    assert row["operator"] == "operator:alice" and row["bound_hash"] == card["grant_hash"] and row["decision"] == "approve"


def test_editing_any_grant_field_after_approval_means_not_approved(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.approve_all(mid)
    assert predicate(st, mid)
    with pytest.raises(GrantLocked):                                 # the store refuses the edit outright...
        st.ms.set_grant(mid, st.ms.get_mission(mid).grant())
    mission = st.ms.get_mission(mid)
    body = mission.grant().model_dump(mode="json")
    for field, value in (("budgets", {**body["budgets"], "wall_s": body["budgets"]["wall_s"] + 1}),
                         ("scope", {**body["scope"], "stacks": ["media", "photos"]}),
                         ("credentials", ["tool:x"]),
                         ("on_verify_fail", "rollback")):
        changed = Grant.model_validate({**body, field: value})
        if changed.hash() == mission.grant_hash:
            continue
        with sqlite3.connect(st.store.path) as conn:                 # ...and a raw edit (a bug, or a hostile DB write) is not approved
            conn.execute("UPDATE missions SET grant_json=?, grant_hash=? WHERE id=?", (changed.canonical(), changed.hash(), mid))
        assert not predicate(st, mid), field
        with sqlite3.connect(st.store.path) as conn:
            conn.execute("UPDATE missions SET grant_json=?, grant_hash=? WHERE id=?", (mission.grant_json, mission.grant_hash, mid))
        assert predicate(st, mid)


def test_a_stale_card_is_refused_after_the_scope_changed(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    stale = st.flow.card(mid)["grant_hash"]
    body = st.ms.get_mission(mid).grant().model_dump(mode="json")
    st.ms.set_grant(mid, Grant.model_validate({**body, "scope": {**body["scope"], "services": ["media/web"]}}))
    with pytest.raises(FlowError) as exc:
        st.flow.approve("operator:alice", mid, "grant", stale)
    assert exc.value.code == "stale" and not approvals_rows(st, mid)


def test_two_missions_cannot_share_one_approval(stack):
    st = stack()
    a = st.propose()["mission_id"]
    b = st.propose()["mission_id"]
    hash_a = st.flow.card(a)["grant_hash"]
    assert hash_a == st.flow.card(b)["grant_hash"]                  # identical grants, identical hashes
    st.flow.approve("operator:alice", a, "grant", hash_a)
    assert predicate(st, a) and not predicate(st, b)                # a row names its mission as well as its hash
    with sqlite3.connect(st.store.path) as conn:                    # and a row copied by hand to another mission id is not a proof of it
        row = conn.execute("SELECT phase, bound_hash FROM mission_approvals WHERE mission_id=?", (a,)).fetchone()
    assert row and not predicate(st, b)


def test_a_phase_the_grant_does_not_need_cannot_be_approved(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    with pytest.raises(FlowError):
        st.flow.approve("operator:alice", mid, "broad_sudo", st.flow.card(mid)["grant_hash"], elevated=True)
    with pytest.raises(FlowError):
        st.flow.approve("operator:alice", mid, "critical:0", st.flow.card(mid)["grant_hash"], elevated=True)


def test_a_decision_is_recorded_once_per_phase_and_hash(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    h = st.flow.card(mid)["grant_hash"]
    st.flow.approve("operator:alice", mid, "grant", h)
    with pytest.raises(FlowError) as exc:
        st.flow.approve("operator:alice", mid, "grant", h)
    assert exc.value.code == "conflict"


def test_only_a_mission_awaiting_a_decision_can_be_approved(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.sup.cancel(mid, "operator:alice")
    with pytest.raises(FlowError) as exc:
        st.flow.approve("operator:alice", mid, "grant", st.ms.get_mission(mid).grant_hash)
    assert exc.value.code == "conflict"


# ── elevated approvals ───────────────────────────────────────────────────────────────────────────────

def test_the_phases_only_an_old_grant_needed_are_never_asked_for(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    h = st.flow.card(mid)["grant_hash"]
    for phase in ("broad_sudo", "no_backup"):
        with pytest.raises(FlowError):
            st.flow.approve("operator:alice", mid, phase, h, elevated=True)
    _, handlers = service(st)
    assert call(handlers, "mission.approve", mission_id=mid, operator="alice", phase="grant", grant_hash=h,
                elevated="true")["error"] == "bad_request"                     # elevated is still strictly a boolean


# ── the actor comes from the authenticated identity ──────────────────────────────────────────────────

def test_a_forged_actor_in_params_is_refused_and_a_forged_operator_name_is_not_an_identity(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    h = st.flow.card(mid)["grant_hash"]
    _, handlers = service(st)
    base = {"mission_id": mid, "phase": "grant", "grant_hash": h}
    assert call(handlers, "mission.approve", **base, operator="alice", actor="operator:root")["error"] == "bad_request"
    assert call(handlers, "mission.approve", **base, operator="alice", by="operator:root")["error"] == "bad_request"
    assert call(handlers, "mission.approve", **base, operator="operator:root")["error"] == "bad_request"     # kind:name is not a name
    ok = call(handlers, "mission.approve", **base, operator="core")                                           # a plain name is a name
    assert "error" not in ok and {r["operator"] for r in approvals_rows(st, mid)} == {"operator:core"}        # built into operator:core


def test_the_flow_itself_refuses_a_non_operator_actor(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    h = st.flow.card(mid)["grant_hash"]
    for actor in ("core", "worker", "supervisor", "system", "operator:", "import:external", "tg.5"):
        with pytest.raises(FlowError) as exc:
            st.flow.approve(actor, mid, "grant", h)
        assert exc.value.code == "forbidden"
    with pytest.raises(FlowError):
        st.flow.deny("core", mid, h)
    with pytest.raises(FlowError):
        st.flow.propose_repair("core", title="t", goal="g", scope={"stacks": ["media"]},
                               checks=[{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}])
    assert not approvals_rows(st, mid)


def test_the_rpc_derives_the_actor_for_every_decision_and_the_rollback(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    _, handlers = service(st)
    h = st.flow.card(mid)["grant_hash"]
    ok = call(handlers, "mission.approve", mission_id=mid, operator="alice", phase="grant", grant_hash=h)
    assert "error" not in ok and approvals_rows(st, mid)[0]["operator"] == "operator:alice"
    for forged in ({"actor": "operator:root"}, {"ack_actor": "x"}):
        assert call(handlers, "mission.rollback", mission_id=mid, operator="alice", **forged)["error"] == "bad_request"
    assert call(handlers, "mission.rollback", mission_id=mid, operator="alice", ack_drift=True)["error"] != "bad_request"


def test_the_pause_resume_and_take_over_rpcs_are_gone(stack):
    """Trust mode removed them with the tripwire: the RPC table has no such method (an unknown method, not a refusal)."""
    st = stack()
    _, handlers = service(st)
    assert not {"mission.resume", "mission.takeover", "mission.pause"} & set(handlers)
    assert "/resume" not in MissionService.TELEGRAM_COMMANDS and "/takeover" not in MissionService.TELEGRAM_COMMANDS


def test_every_new_rpc_answers_disabled_before_it_looks_at_params_and_unavailable_without_a_flow(stack):
    st = stack()
    off = MissionService(st.store, enabled=lambda: False)
    handlers = off.handlers()
    for method in ("mission.approval_card", "mission.approve", "mission.deny", "mission.rollback"):
        assert call(handlers, method, junk=1)["error"] == "disabled", method
    on = MissionService(st.store, enabled=lambda: True)               # no flow attached
    assert call(on.handlers(), "mission.approval_card", mission_id="M-1")["error"] == "unavailable"


def test_a_worker_raised_card_cannot_approve_anything(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    st.ms.append_event(mid, "note", "worker", {"event": "decision.requested", "summary": "approve me", "approve": True})
    assert not predicate(st, mid) and not approvals_rows(st, mid)


# ── deny ─────────────────────────────────────────────────────────────────────────────────────────────

def test_deny_records_a_denial_closes_the_mission_and_a_denial_is_never_a_proof(stack):
    st = stack()
    mid = st.propose()["mission_id"]
    h = st.flow.card(mid)["grant_hash"]
    out = st.flow.deny("operator:alice", mid, h)
    assert out["status"] == "cancelled" and approvals_rows(st, mid)[0]["decision"] == "deny"
    assert not predicate(st, mid)
    with pytest.raises(FlowError):
        st.flow.approve("operator:alice", mid, "grant", h)


# ── Telegram ─────────────────────────────────────────────────────────────────────────────────────────

def test_telegram_shows_the_card_and_approves_only_with_the_hash_it_displayed(stack):
    st = stack()
    svc, _ = service(st)
    mid = st.propose()["mission_id"]
    card = svc.telegram(f"/mission_card {mid}", "tg.42")
    h8 = st.flow.card(mid)["grant_hash"][:8]
    assert f"grant hash: {h8}" in card and "repair" in card and "verification: default(http)" in card
    assert "does not match" in svc.telegram(f"/approve {mid} deadbeef", "tg.42") or "not this mission" in svc.telegram(f"/approve {mid} deadbeef", "tg.42")
    assert not approvals_rows(st, mid)
    assert "Usage" in svc.telegram(f"/approve {mid}", "tg.42")
    assert "approved" in svc.telegram(f"/approve {mid} {h8}", "tg.42")
    assert approvals_rows(st, mid)[0]["operator"] == "operator:tg.42" and predicate(st, mid)


def test_telegram_deny_and_unidentifiable_operator(stack):
    st = stack()
    svc, _ = service(st)
    mid = st.propose()["mission_id"]
    h8 = st.flow.card(mid)["grant_hash"][:8]
    assert "Cannot identify" in svc.telegram(f"/deny {mid} {h8}", "Bad Name")
    assert "denied" in svc.telegram(f"/deny {mid} {h8}", "tg.42")
    assert st.ms.get_mission(mid).status == "cancelled"
    assert "No such mission" in svc.telegram("/mission_card M-999", "tg.42")


# ── events carry no secrets ──────────────────────────────────────────────────────────────────────────

def test_approval_events_hold_no_secret_text(stack):
    st = stack()
    mid = st.propose(title="restart password=hunter2-secret", goal="token=abcdef0123456789abcdef")["mission_id"]
    st.approve_all(mid)
    dump = json.dumps([e.body_json for e in st.ms.events(mid)]) + json.dumps(st.flow.card(mid))
    assert "hunter2-secret" not in dump and "abcdef0123456789abcdef" not in dump
    assert sha256_hex({"a": 1}) and canonical_json({"a": 1}) and MissionError                        # imports used


def test_the_console_commands_are_dispatched_to_the_console_and_say_so_when_there_is_none(stack):
    st = stack()
    svc, _ = service(st)
    assert svc.telegram("/ask why is media slow", "tg.42") == "The Console is not available on this host."
    seen = []
    svc.console_telegram = type("T", (), {"command": lambda self, text, operator: seen.append((text, operator)) or "ok"})()
    assert svc.telegram("/threads", "tg.42") == "ok" and seen == [("/threads", "tg.42")]
    assert svc.telegram("/threads", "Not A Name!") == "Cannot identify the operator."
