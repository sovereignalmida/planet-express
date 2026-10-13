import json
import sqlite3

import pytest
from missions_helpers import make_store, new_mission, repair_grant

from planet_express.missions.approvals import (
    ApprovalConflict,
    execution_predicate,
    record_approval,
)
from planet_express.missions.canonical import canonical_json, sha256_hex
from planet_express.missions.models import Grant
from planet_express.missions.store import GrantLocked, MissionStore


def setup(tmp_path, grant=None, **kw):
    store, clock = make_store(tmp_path)
    ms = MissionStore(store)
    return store, clock, ms, new_mission(ms, grant, **kw)


def ok(store, m):
    return execution_predicate(store, m.id).ok


def test_approve_row_satisfies_deny_row_does_not(tmp_path):
    store, _, ms, m = setup(tmp_path)
    assert not ok(store, m)
    record_approval(store, m.id, "grant", m.grant_hash, "deny", "operator:me")
    assert not ok(store, m)
    store2, _, ms2, m2 = setup(tmp_path / "b")
    record_approval(store2, m2.id, "grant", m2.grant_hash, "approve", "operator:me")
    assert ok(store2, m2)


def test_duplicate_decision_refused(tmp_path):
    store, _, _, m = setup(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "deny", "operator:me")
    with pytest.raises(ApprovalConflict):
        record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert not ok(store, m)


def test_wrong_phase_or_hash_does_not_satisfy(tmp_path):
    store, _, _, m = setup(tmp_path)
    record_approval(store, m.id, "plan", m.grant_hash, "approve", "operator:me")
    record_approval(store, m.id, "grant", "0" * 64, "approve", "operator:me")
    record_approval(store, m.id, "broad_sudo", m.grant_hash, "approve", "operator:me", elevated=True)
    assert not ok(store, m)
    with pytest.raises(ValueError):
        record_approval(store, m.id, "whatever", m.grant_hash, "approve", "operator:me")


def test_changed_grant_invalidates_old_rows(tmp_path):
    store, _, ms, m = setup(tmp_path)
    old_hash = m.grant_hash
    ms.set_grant(m.id, repair_grant(scope={"stacks": ["media", "extra"]}))
    record_approval(store, m.id, "grant", old_hash, "approve", "operator:me")  # approval of the OLD grant
    assert not ok(store, m)
    new = ms.get_mission(m.id)
    assert new.grant_hash != old_hash
    record_approval(store, m.id, "grant", new.grant_hash, "approve", "operator:me")
    assert ok(store, m)


def test_approved_grant_is_immutable(tmp_path):
    store, _, ms, m = setup(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    with pytest.raises(GrantLocked):
        ms.set_grant(m.id, repair_grant(scope={"stacks": ["x"]}))
    assert ms.get_mission(m.id).grant_hash == m.grant_hash


def test_tampered_grant_json_fails_closed(tmp_path):
    store, _, _, m = setup(tmp_path)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert ok(store, m)
    wider = repair_grant(scope={"stacks": ["everything"]}).canonical()
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE missions SET grant_json=? WHERE id=?", (wider, m.id))
    assert not ok(store, m)


def test_observe_and_propose_never_satisfy(tmp_path):
    for tier in ("observe", "propose"):
        store, _, _, m = setup(tmp_path / tier, Grant.model_validate({"tier": tier}))
        record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
        assert not ok(store, m)


def test_broad_sudo_needs_elevated_row(tmp_path):
    grant = repair_grant(sudo={"mode": "broad"})
    store, _, _, m = setup(tmp_path, grant)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert not ok(store, m)
    record_approval(store, m.id, "broad_sudo", m.grant_hash, "approve", "operator:me", elevated=False)
    assert not ok(store, m)  # non-elevated broad_sudo does not count

    store, _, _, m = setup(tmp_path / "e", grant)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    record_approval(store, m.id, "broad_sudo", m.grant_hash, "approve", "operator:me", elevated=True)
    assert ok(store, m)


def test_no_backup_needs_elevated_row(tmp_path):
    grant = repair_grant(backup={"acknowledged_no_backup": True})
    store, _, _, m = setup(tmp_path, grant)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert not ok(store, m)
    record_approval(store, m.id, "no_backup", m.grant_hash, "approve", "operator:me", elevated=True)
    assert ok(store, m)


# ── change/critical plan rows ───────────────────────────────────────────────
def plan_mission(tmp_path, touches):
    grant = Grant.model_validate({"tier": "change"})
    store, _, ms, m = setup(tmp_path, grant)
    plan = {"v": 1, "protected_touches": touches}
    m = ms.set_plan(m.id, plan)
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    return store, ms, m


def test_change_needs_plan_row_bound_to_plan_hash(tmp_path):
    store, ms, m = plan_mission(tmp_path, [])
    assert m.plan_hash == sha256_hex(json.loads(m.plan_json))
    assert not ok(store, m)
    record_approval(store, m.id, "plan", m.grant_hash, "approve", "operator:me")  # wrong hash
    assert not ok(store, m)
    record_approval(store, m.id, "plan", m.plan_hash, "approve", "operator:me")
    assert ok(store, m)
    with pytest.raises(GrantLocked):
        ms.set_plan(m.id, {"v": 2})


def test_change_without_plan_never_satisfied(tmp_path):
    store, _, ms, m = setup(tmp_path, Grant.model_validate({"tier": "change"}))
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert not ok(store, m)


def test_every_critical_index_required_and_none_substitutes(tmp_path):
    store, ms, m = plan_mission(tmp_path, ["dns", "traefik"])
    for i in (0, 1):  # critical rows alone do not stand in for the plan row
        record_approval(store, m.id, f"critical:{i}", m.plan_hash, "approve", "operator:me", elevated=True)
    assert not ok(store, m)
    record_approval(store, m.id, "plan", m.plan_hash, "approve", "operator:me")
    assert ok(store, m)

    store, ms, m = plan_mission(tmp_path / "b", ["dns", "traefik"])
    record_approval(store, m.id, "plan", m.plan_hash, "approve", "operator:me")
    record_approval(store, m.id, "critical:0", m.plan_hash, "approve", "operator:me", elevated=True)
    assert not ok(store, m)  # critical:1 missing
    record_approval(store, m.id, "critical:1", m.plan_hash, "approve", "operator:me", elevated=False)
    assert not ok(store, m)  # critical needs elevation
    store, ms, m = plan_mission(tmp_path / "c", ["dns"])
    record_approval(store, m.id, "plan", m.plan_hash, "approve", "operator:me")
    record_approval(store, m.id, "critical:0", m.plan_hash, "deny", "operator:me", elevated=True)
    assert not ok(store, m)


def test_plan_hash_mismatch_fails_closed(tmp_path):
    store, ms, m = plan_mission(tmp_path, [])
    record_approval(store, m.id, "plan", m.plan_hash, "approve", "operator:me")
    assert ok(store, m)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE missions SET plan_json=? WHERE id=?",
                     (canonical_json({"v": 1, "protected_touches": []}) + " ", m.id))
    assert ok(store, m)  # whitespace only: canonical hash unchanged
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE missions SET plan_json=? WHERE id=?", ('{"v":9}', m.id))
    assert not ok(store, m)


# ── standing grants ─────────────────────────────────────────────────────────
def standing(ms, now_plus=3600, tier="repair", scope=None):
    ms.create_standing_grant(grant_id="SG-1", scope=scope or {"stacks": ["media"], "hosts": ["local"]},
                             tier=tier, created_by="operator:me", expires_at=int(ms._now()) + now_plus)


def test_standing_grant_satisfies_without_approval_row(tmp_path):
    store, clock, ms, _ = setup(tmp_path)
    standing(ms)
    m = new_mission(ms, standing_grant_id="SG-1")
    assert ok(store, m)
    clock.t += 7200
    assert not ok(store, m)  # expired


def test_standing_grant_revoked_or_out_of_scope(tmp_path):
    store, _, ms, _ = setup(tmp_path)
    standing(ms)
    m = new_mission(ms, standing_grant_id="SG-1")
    wide = new_mission(ms, repair_grant(scope={"stacks": ["media", "secrets"]}), standing_grant_id="SG-1")
    assert ok(store, m) and not ok(store, wide)
    ms.revoke_standing_grant("SG-1")
    assert not ok(store, m)
    missing = new_mission(ms, standing_grant_id="SG-nope")
    assert not ok(store, missing)


def test_standing_grant_refuses_broad_no_backup_and_change(tmp_path):
    store, _, ms, _ = setup(tmp_path)
    standing(ms)
    for grant in (repair_grant(sudo={"mode": "broad"}), repair_grant(backup={"acknowledged_no_backup": True}),
                  repair_grant(sudo={"mode": "broad"}, docker="raw")):
        m = new_mission(ms, grant, standing_grant_id="SG-1")
        # even with every elevated row on file, the standing path refuses
        record_approval(store, m.id, "broad_sudo", m.grant_hash, "approve", "operator:me", elevated=True)
        record_approval(store, m.id, "no_backup", m.grant_hash, "approve", "operator:me", elevated=True)
        assert not ok(store, m)
    m = new_mission(ms, Grant.model_validate({"tier": "change", "scope": {"stacks": ["media"]}}),
                    standing_grant_id="SG-1")
    assert not ok(store, m)
