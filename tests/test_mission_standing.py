"""Standing grants (slice 7a): scoped, expiring, listed, revocable, versioned, incident-backed only; an unattended start is
decided solely by `approvals.evaluate`, with an injected clock for expiry and windows."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions_helpers import add_incident
from repair_fakes import SCOPE, build

from planet_express.missions import approvals, window
from planet_express.missions.approval_flow import FlowError

# The fake clock starts at epoch 1_000_000 = Monday 1970-01-12 13:46:40 UTC.
MONDAY_1346 = 1_000_000


@pytest.fixture
def stack(tmp_path):
    made = []

    def make(**kw):
        st = build(tmp_path, **kw)
        st.flow.named_windows = lambda: {"backup": ["13:00-14:00"]}
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


def make_standing(st, *, ttl=3600, scope=None, window_spec=None, tier="repair", elevated=True):
    return st.flow.standing_create("operator:alice", scope=scope or SCOPE, ttl_s=ttl, window_spec=window_spec, tier=tier,
                                   elevated=elevated)


def unattended(st, standing_id, n=1, **kw):
    spec = {"title": "restart media", "goal": "an incident is open", "scope": SCOPE,
            "checks": [{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}],
            "incident_id": add_incident(st.store, f"{n:012x}")}
    spec.update(kw)
    return st.flow.start_under_standing(standing_id, **spec)


def ready(st, mid):
    return approvals.execution_predicate(st.store, mid).ok


# ── creating, listing, revoking, versions ────────────────────────────────────────────────────────────

def test_create_list_revoke(stack):
    st = stack(start_monitor=False)
    made = make_standing(st, window_spec={"deny": ["backup"]})
    assert made["id"] == "SG-1-v1" and made["state"] == "active" and made["created_by"] == "operator:alice"
    assert made["window"] == {"deny": ["13:00-14:00"], "allow": None}               # the name was resolved when it was created
    assert [g["id"] for g in st.flow.standing_list()["standing_grants"]] == ["SG-1-v1"]
    revoked = st.flow.standing_revoke("operator:alice", "SG-1-v1")
    assert revoked["state"] == "revoked"
    assert st.flow.standing_list()["standing_grants"] == []
    assert [g["state"] for g in st.flow.standing_list(include_inactive=True)["standing_grants"]] == ["revoked"]
    with pytest.raises(FlowError) as exc:
        st.flow.standing_revoke("operator:alice", "SG-9-v1")
    assert exc.value.code == "not_found"


def test_creating_one_needs_an_elevated_session_and_limits_apply(stack):
    st = stack(start_monitor=False)
    with pytest.raises(FlowError) as exc:
        make_standing(st, elevated=False)
    assert exc.value.code == "elevation_required"
    for tier in ("change", "critical", "propose"):
        with pytest.raises(FlowError) as exc:
            make_standing(st, tier=tier)
        assert exc.value.code == "forbidden"
    for ttl in (0, 59, 30 * 86400 + 1, True, "3600"):
        with pytest.raises(FlowError):
            make_standing(st, ttl=ttl)
    with pytest.raises(FlowError):
        make_standing(st, scope={"stacks": [], "services": []})                     # an empty scope covers nothing and is refused
    with pytest.raises(FlowError, match="not times"):
        make_standing(st, window_spec={"deny": ["lunchtime"]})                       # an unknown name never becomes a window
    assert st.flow.standing_list(include_inactive=True)["standing_grants"] == []


def test_a_new_version_replaces_the_old_one_and_the_old_one_stops_starting_missions(stack):
    st = stack(start_monitor=False)
    make_standing(st)
    v2 = st.flow.standing_supersede("operator:alice", "SG-1-v1", scope=SCOPE, ttl_s=7200, elevated=True)
    assert v2["id"] == "SG-1-v2" and v2["version"] == 2
    states = {g["id"]: g["state"] for g in st.flow.standing_list(include_inactive=True)["standing_grants"]}
    assert states == {"SG-1-v1": "revoked", "SG-1-v2": "active"}
    assert not unattended(st, "SG-1-v1")["unattended"]
    assert unattended(st, "SG-1-v2", n=2)["unattended"]
    with pytest.raises(FlowError) as exc:
        st.flow.standing_supersede("operator:alice", "SG-1-v1", scope=SCOPE, ttl_s=60, elevated=True)
    assert exc.value.code == "conflict"


# ── the unattended start ─────────────────────────────────────────────────────────────────────────────

def test_an_in_scope_incident_backed_mission_starts_unattended_and_is_recorded(stack):
    st = stack()
    make_standing(st)
    made = unattended(st, "SG-1-v1")
    mid = made["mission_id"]
    assert made["unattended"] and made["standing_refused"] == []
    mission = st.ms.get_mission(mid)
    assert mission.standing_grant_id == "SG-1-v1" and mission.origin == "incident" and mission.incident_id
    assert ready(st, mid)                                                          # no approval row exists, and none was needed
    assert st.flow.card(mid)["ready"] and st.flow.card(mid)["standing_grant_id"] == "SG-1-v1"
    assert [b for k, a, b in map(lambda e: (e.kind, e.actor, json.loads(e.body_json)), st.ms.events(mid))
            if b.get("event") == "standing.accepted"]
    assert st.drive(mid) == "completed"                                            # and the supervisor ran it end to end
    assert st.ms.get_mission(mid).standing_grant_id == "SG-1-v1"


def test_the_standing_path_never_needs_or_creates_an_operator_row(stack):
    st = stack()
    make_standing(st)
    mid = unattended(st, "SG-1-v1")["mission_id"]
    st.drive(mid)
    import sqlite3
    with sqlite3.connect(st.store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mission_approvals").fetchone()[0] == 0


def test_an_unattended_start_needs_an_incident(stack):
    st = stack(start_monitor=False)
    make_standing(st)
    with pytest.raises(FlowError) as exc:
        st.flow.start_under_standing("SG-1-v1", title="t", goal="g", scope=SCOPE,
                                     checks=[{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}])
    assert exc.value.code == "forbidden"


def refused(st, made, *words):
    assert not made["unattended"] and made["standing_refused"], made
    mid = made["mission_id"]
    assert st.ms.get_mission(mid).standing_grant_id is None                        # an ordinary approval card instead
    assert st.ms.get_mission(mid).status == "awaiting_decision" and not ready(st, mid)
    assert any(w in " ".join(made["standing_refused"]) for w in words), made["standing_refused"]
    events = [json.loads(e.body_json) for e in st.ms.events(mid)]
    assert any(b.get("event") == "standing.refused" for b in events) and any(b.get("event") == "approval.requested" for b in events)
    st.approve_all(mid, elevated=True)                                             # and an operator CAN still approve it
    assert ready(st, mid)


def test_out_of_scope_refuses_and_raises_a_decision_card(stack):
    st = stack(start_monitor=False)
    make_standing(st, scope={"stacks": ["photos"], "hosts": ["local"]})
    refused(st, unattended(st, "SG-1-v1"), "scope exceeds")


def test_expired_refuses(stack):
    st = stack(start_monitor=False)
    make_standing(st, ttl=3600)
    st.clock.t += 3601
    refused(st, unattended(st, "SG-1-v1"), "expired")


def test_revoked_refuses(stack):
    st = stack(start_monitor=False)
    make_standing(st)
    st.flow.standing_revoke("operator:alice", "SG-1-v1")
    refused(st, unattended(st, "SG-1-v1"), "revoked")


def test_unknown_standing_grant_refuses(stack):
    st = stack(start_monitor=False)
    refused(st, unattended(st, "SG-7-v1"), "does not exist")


def test_a_denied_window_refuses_and_the_clock_decides(stack):
    st = stack(start_monitor=False)
    make_standing(st, ttl=86400, window_spec={"deny": ["backup"]})                  # 13:00-14:00 UTC
    refused(st, unattended(st, "SG-1-v1"), "denied window")
    st.clock.t += 3600                                                             # 14:46: outside it
    assert unattended(st, "SG-1-v1", n=2)["unattended"]


def test_broad_sudo_raw_docker_and_no_backup_refuse_and_raise_a_card(stack):
    st = stack(start_monitor=False, covered=False)
    make_standing(st)
    refused(st, unattended(st, "SG-1-v1", n=1, sudo={"mode": "broad"}, docker="raw"), "broad_sudo")
    refused(st, unattended(st, "SG-1-v1", n=2, acknowledged_no_backup=True), "no_backup")


def test_a_standing_grant_cannot_be_made_for_change_or_critical_and_a_change_mission_never_matches_one(stack):
    from missions_helpers import advance_to, new_mission
    from planet_express.missions.models import Grant
    st = stack(start_monitor=False)
    make_standing(st)
    incident = add_incident(st.store, "f" * 12)
    m = new_mission(st.ms, Grant.model_validate({"tier": "change", "scope": {"stacks": ["media"]}}),
                    standing_grant_id="SG-1-v1", origin="incident", incident_id=incident)
    advance_to(st.ms, m.id, "queued", "investigating", "awaiting_decision")
    verdict = approvals.execution_predicate(st.store, m.id)
    assert not verdict.ok and any("never cover change or critical" in r for r in verdict.reasons)


def test_expiry_and_revoke_stop_an_unattended_mission_that_has_not_started_yet(stack):
    st = stack()
    make_standing(st)
    mid = unattended(st, "SG-1-v1")["mission_id"]
    st.flow.standing_revoke("operator:alice", "SG-1-v1")                             # revoked between acceptance and the supervisor's tick
    st.sup.tick()
    st.sup.tick()
    assert st.ms.get_mission(mid).status == "awaiting_decision" and st.runtimes == []
    made = unattended(st, "SG-1-v1", n=2)                                            # (refused: revoked) ...
    assert not made["unattended"]
    make_standing(st, ttl=600)
    second = unattended(st, "SG-2-v1", n=3)["mission_id"]
    st.clock.t += 601
    st.sup.tick()
    assert st.ms.get_mission(second).status == "awaiting_decision" and st.runtimes == []


def test_the_standing_row_is_judged_again_on_every_transition_into_executing(stack):
    st = stack(start_monitor=False)
    make_standing(st)
    mid = unattended(st, "SG-1-v1")["mission_id"]
    st.flow.standing_revoke("operator:alice", "SG-1-v1")
    from planet_express.missions.state import IllegalTransition, transition
    with pytest.raises(IllegalTransition, match="standing grant is revoked"):
        transition(st.store, mid, "executing", "core")


# ── window semantics, with an injected clock ─────────────────────────────────────────────────────────

def at(day, hh, mm):
    """Epoch seconds for 1970-01-(5+day) hh:mm UTC; day 0 is a Monday."""
    return ((4 + day) * 24 + hh) * 3600 + mm * 60


def test_window_entries_and_the_deny_wins_rule():
    w = {"deny": ["02:00-03:30"], "allow": None}
    assert window.allowed(w, at(0, 1, 59))[0] and not window.allowed(w, at(0, 2, 0))[0]
    assert not window.allowed(w, at(0, 3, 29))[0] and window.allowed(w, at(0, 3, 30))[0]      # end is exclusive
    assert window.allowed(None, 5)[0] and window.allowed({"deny": [], "allow": None}, 5)[0]


def test_a_range_that_wraps_midnight_belongs_to_the_day_it_starts_on():
    w = {"deny": ["sat@23:00-01:00"], "allow": None}
    assert not window.allowed(w, at(5, 23, 30))[0]                                    # Saturday 23:30
    assert not window.allowed(w, at(6, 0, 30))[0]                                     # Sunday 00:30: the tail of Saturday's window
    assert window.allowed(w, at(6, 1, 0))[0] and window.allowed(w, at(4, 23, 30))[0]  # Friday 23:30 is not in it
    assert window.allowed(w, at(5, 0, 30))[0]                                         # Saturday 00:30 is the tail of FRIDAY's, not denied


def test_whole_days_and_day_lists():
    w = {"deny": ["sat,sun"], "allow": None}
    assert window.allowed(w, at(4, 12, 0))[0] and not window.allowed(w, at(5, 12, 0))[0] and not window.allowed(w, at(6, 12, 0))[0]


def test_an_allow_list_opens_only_inside_its_entries():
    w = {"deny": [], "allow": ["mon,tue@09:00-17:00"]}
    assert window.allowed(w, at(0, 10, 0))[0] and not window.allowed(w, at(0, 18, 0))[0] and not window.allowed(w, at(2, 10, 0))[0]
    assert not window.allowed({"deny": [], "allow": []}, at(0, 10, 0))[0]              # an empty allow list never opens


@pytest.mark.parametrize("entry", ["backup", "25:00-26:00", "09:00", "mon@", "funday", "09:00-9:30", "", "mon@09:00-10:00@x"])
def test_an_unreadable_deny_entry_denies_and_an_unreadable_allow_entry_matches_nothing(entry):
    assert not window.allowed({"deny": [entry], "allow": None}, at(0, 10, 0))[0]
    assert not window.allowed({"deny": [], "allow": [entry]}, at(0, 10, 0))[0]


def test_names_resolve_at_creation_and_an_unknown_name_is_reported():
    named = {"backup": ["01:00-02:00", "sun"]}
    assert window.resolve_names({"deny": ["backup", "12:00-13:00"], "allow": None}, named) == {
        "deny": ["01:00-02:00", "sun", "12:00-13:00"], "allow": None}
    assert window.unreadable(window.resolve_names({"deny": ["nap"], "allow": None}, named)) == ["nap"]


# ── Codex round 1: atomic supersede, id allocation, and a reachable standing path ─────────────────

CHECKS = [{"id": "default", "check": "http", "params": {"url": "http://localhost/"}}]


def test_a_failed_supersede_leaves_the_old_version_active_and_never_two_active(stack):
    import sqlite3
    st = stack(start_monitor=False)
    make_standing(st)
    with sqlite3.connect(st.store.path) as conn:                 # the new version's id is already taken: the insert fails
        conn.execute("INSERT INTO standing_grants (id, scope_json, tier, window_json, created_by, created_at, expires_at, version) "
                     "VALUES ('SG-1-v2', '{}', 'repair', NULL, 'x', 1, 99999999, 2)")
    with pytest.raises(FlowError) as exc:
        st.flow.standing_supersede("operator:alice", "SG-1-v1", scope=SCOPE, ttl_s=7200, elevated=True)
    assert exc.value.code == "conflict"
    assert st.flow._standing_row("SG-1-v1")["state"] == "active"            # not revoked: the replacement never happened


def test_standing_ids_are_allocated_with_a_retry_when_two_creates_race(stack, monkeypatch):
    st = stack(start_monitor=False)
    real = st.flow.ms.create_standing_grant
    seen = []

    def racing(**kw):
        if not seen:
            seen.append(kw["grant_id"])
            real(**{**kw, "created_by": "operator:other"})            # the other request wins this number
        return real(**kw)
    monkeypatch.setattr(st.flow.ms, "create_standing_grant", racing)
    made = make_standing(st)
    assert made["id"] != seen[0] and made["id"].startswith("SG-") and made["created_by"] == "operator:alice"
    assert len(st.flow.standing_list(include_inactive=True)["standing_grants"]) == 2


def test_an_operator_can_propose_a_repair_for_an_incident_under_a_standing_grant_and_core_decides_the_approval(stack):
    st = stack()
    make_standing(st)
    made = st.flow.propose_repair("operator:alice", title="t", goal="g", scope=SCOPE, standing_grant_id="SG-1-v1",
                                  checks=CHECKS, incident_id=add_incident(st.store, "a" * 12))
    assert made["unattended"] and st.ms.get_mission(made["mission_id"]).standing_grant_id == "SG-1-v1"
    assert st.drive(made["mission_id"]) == "completed"
    assert any(e.actor == "operator:alice" for e in st.ms.events(made["mission_id"]))      # the proposer is on the record
    with pytest.raises(FlowError) as exc:                                                    # an incident is required
        st.flow.propose_repair("operator:alice", title="t", goal="g", scope=SCOPE, standing_grant_id="SG-1-v1", checks=CHECKS)
    assert exc.value.code == "forbidden"


def test_the_rpc_takes_a_standing_id_but_never_an_actor_and_names_it_in_the_expected_shape(stack):
    from planet_express.application.mission_service import MissionService
    from planet_express.integrations.rpc import RpcError
    st = stack()
    make_standing(st)
    svc = MissionService(st.store, enabled=lambda: True)
    svc.flow = st.flow
    handler = svc.handlers()["mission.propose_repair"]
    base = {"operator": "alice", "title": "t", "goal": "g", "scope": SCOPE, "checks": CHECKS,
            "incident_id": add_incident(st.store, "d" * 12), "standing_grant_id": "SG-1-v1"}
    made = handler(base)
    assert made["unattended"] and st.ms.get_mission(made["mission_id"]).standing_grant_id == "SG-1-v1"
    for bad in ({"standing_grant_id": "SG-1"}, {"actor": "core"}, {"_internal": True}):
        with pytest.raises(RpcError) as exc:
            handler({**base, **bad})
        assert exc.value.code == "bad_request"


def test_nobody_can_pass_core_as_the_actor_to_skip_the_operator_requirement(stack):
    st = stack(start_monitor=False)
    make_standing(st)
    spec = {"title": "t", "goal": "g", "scope": SCOPE, "incident_id": add_incident(st.store, "b" * 12), "checks": CHECKS}
    for kw in ({}, {"standing_grant_id": "SG-1-v1"}):                                       # only start_under_standing may speak as core
        with pytest.raises(FlowError) as exc:
            st.flow.propose_repair("core", **spec, **kw)
        assert exc.value.code == "forbidden"
