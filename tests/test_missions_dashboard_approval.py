"""The dashboard side of the approval flow (slice 7a): the card, approve/deny/resume/rollback/take over, and standing grants.
The acting operator is the signed-in session's, `elevated` is true only when this request holds a live elevated session, and
a form can supply neither."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest
from test_missions_dashboard import detail, mission, token, web  # noqa: F401  (the fixture and its helpers)
from test_scruffy_routes import PASSPHRASE

H = "a" * 64


def card(**over):
    base = {"mission_id": "M-2", "title": "restart media", "goal": "g", "status": "awaiting_decision", "tier": "repair",
            "grant_hash": H, "plan_hash": None, "standing_grant_id": None,
            "warnings": [{"kind": "full_access", "text": "FULL ACCESS: the session runs with your access to this host."}],
            "scope": {"stacks": ["media"], "hosts": ["local"]},
            "budgets": {"wall_s": 900, "tool_calls": 100, "output_bytes": 1000, "retries": 1},
            "verification": {"plan_sha256": H, "checks": [{"id": "route", "check": "http", "params": {"url": "http://x/"}}],
                             "on_verify_fail": "rollback"},
            "credentials": [], "window": {"deny": [], "allow": None},
            "required": [{"phase": "grant", "bound": H, "elevated": False, "state": "pending"},
                         {"phase": "critical:0", "bound": H, "elevated": True, "state": "pending"}],
            "ready": False}
    base.update(over)
    return base


def repair(web, status="awaiting_decision", **card_over):
    client, core, now = web
    core.results["mission.get"] = detail(mission("M-2", "restart media", status=status, tier="repair"))
    core.results["mission.approval_card"] = card(status=status, **card_over)
    return client, core, now


def elevate(client):
    assert client.post("/api/elevate", data={"csrf_token": token(client), "passphrase": PASSPHRASE}).status_code == 200


def test_the_card_is_rendered_with_the_elevated_risks_before_the_scope(web):
    client, core, _ = repair(web)
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert ("mission.approval_card", {"mission_id": "M-2"}) in core.calls
    assert page.index("FULL ACCESS") < page.index("SCOPE") and "BACKUP" not in page and "SUDO" not in page
    assert 'action="/missions/M-2/approve"' in page and 'name="phase" value="critical:0"' in page
    assert 'name="grant_hash" value="' + H + '"' in page and "needs elevation" in page
    assert 'action="/missions/M-2/deny"' in page


def test_no_card_for_an_observe_mission_or_when_the_card_is_unavailable(web):
    client, core, _ = web
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert "mission.approval_card" not in core.methods() and "APPROVAL CARD" not in page
    client, core, _ = repair(web)
    core.errors["mission.approval_card"] = "unavailable"
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert page and "APPROVE" not in page                              # controls are hidden, the mission is still readable


@pytest.mark.parametrize("status", ["needs_operator", "recovery_required", "rollback_pending"])
def test_a_mission_that_needs_the_operator_offers_rollback_and_cancel_and_nothing_else(web, status):
    client, core, _ = repair(web, status=status, warnings=[], required=[])
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert 'action="/missions/M-2/rollback"' in page and 'name="ack_drift"' in page and 'action="/missions/M-2/cancel"' in page
    assert "/resume" not in page and "/takeover" not in page and "TRIPWIRE" not in page


def test_the_resume_and_takeover_routes_are_gone(web):
    client, core, _ = web
    for action in ("resume", "takeover"):
        assert client.post(f"/missions/M-2/{action}", data={"csrf_token": token(client)}).status_code == 404
    assert not {"mission.resume", "mission.takeover"} & set(core.methods())


@pytest.mark.parametrize("action", ["approve", "deny", "rollback"])
def test_every_action_needs_csrf_and_a_signed_in_session(web, action):
    client, core, now = web
    assert client.post(f"/missions/M-2/{action}").status_code == 400
    assert client.post(f"/missions/M-2/{action}", data={"csrf_token": "wrong"}).status_code == 400
    assert core.methods() == []
    assert client.post(f"/missions/x/{action}", data={"csrf_token": token(client)}).status_code == 404


def test_approve_uses_the_session_operator_and_the_form_cannot_name_one_or_elevate(web):
    client, core, _ = repair(web)
    core.results["mission.approve"] = card()
    core.results["mission.deny"] = {"mission_id": "M-2", "status": "cancelled"}
    r = client.post("/missions/M-2/approve", data={"csrf_token": token(client), "phase": "grant", "grant_hash": H,
                                                   "operator": "bob", "actor": "operator:root", "elevated": "true"})
    assert r.status_code == 302 and r.headers["Location"].endswith("notice=approved")
    assert ("mission.approve", {"mission_id": "M-2", "operator": "alice", "phase": "grant", "grant_hash": H,
                                "elevated": False}) in core.calls
    assert not any(p.get("operator") == "bob" or "actor" in p for _, p in core.calls)


def test_an_elevated_phase_carries_elevated_true_only_from_a_live_elevated_session(web):
    client, core, _ = repair(web)
    core.results["mission.approve"] = card()
    client.post("/missions/M-2/approve", data={"csrf_token": token(client), "phase": "broad_sudo", "grant_hash": H})
    assert core.calls[-1][1]["elevated"] is False                      # core will answer elevation_required
    elevate(client)
    client.post("/missions/M-2/approve", data={"csrf_token": token(client), "phase": "broad_sudo", "grant_hash": H})
    assert core.calls[-1][1]["elevated"] is True


@pytest.mark.parametrize("code, notice", [("stale", "stale"), ("elevation_required", "elevation_required"),
                                          ("conflict", "refused"), ("invalid_grant", "invalid_grant"),
                                          ("forbidden", "refused"), ("unavailable", "unavailable")])
def test_refusals_become_notices_and_are_not_raised(web, code, notice):
    client, core, _ = repair(web)
    core.errors["mission.approve"] = code
    r = client.post("/missions/M-2/approve", data={"csrf_token": token(client), "phase": "grant", "grant_hash": H})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"notice={notice}")


def test_a_malformed_value_is_a_400_not_a_call_through(web):
    client, core, _ = repair(web)
    core.errors["mission.approve"] = "bad_request"
    r = client.post("/missions/M-2/approve", data={"csrf_token": token(client), "phase": "grant", "grant_hash": "zz"})
    assert r.status_code == 400


def test_deny_and_rollback_pass_only_the_session_operator(web):
    client, core, _ = repair(web)
    core.results.update({"mission.deny": {}, "mission.rollback": {}})
    t = token(client)
    client.post("/missions/M-2/deny", data={"csrf_token": t, "grant_hash": H, "operator": "bob"})
    client.post("/missions/M-2/rollback", data={"csrf_token": t, "ack_drift": "1", "operator": "bob"})
    sent = {m: p for m, p in core.calls if m in ("mission.deny", "mission.rollback")}
    assert sent["mission.deny"] == {"mission_id": "M-2", "operator": "alice", "grant_hash": H}
    assert sent["mission.rollback"] == {"mission_id": "M-2", "operator": "alice", "ack_drift": True}


# ── standing grants ──────────────────────────────────────────────────────────────────────────────────

GRANT = {"id": "SG-1-v1", "version": 1, "tier": "repair", "state": "active", "scope": {"stacks": ["media"], "hosts": ["local"]},
         "window": {"deny": ["13:00-14:00"], "allow": None}, "created_by": "operator:alice", "created_at": 1,
         "expires_at": 9999, "revoked_at": None}


def test_a_completed_repair_with_a_checkpoint_offers_undo_and_one_without_does_not(web):
    client, core, _ = web
    core.results["mission.get"] = detail({**mission("M-2", "restart media", status="completed", tier="repair"), "has_checkpoint": True})
    core.results["mission.approval_card"] = card(status="completed")
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert 'action="/missions/M-2/rollback"' in page and "UNDO THIS CHANGE (ROLL BACK)" in page and 'name="ack_drift"' in page
    core.results["mission.get"] = detail({**mission("M-2", "restart media", status="completed", tier="repair"), "has_checkpoint": False})
    page = client.get("/missions/M-2").get_data(as_text=True)
    assert 'action="/missions/M-2/rollback"' not in page


def test_the_standing_grants_page_is_gone(web):  # noqa: F811
    """4.2 removed standing grants (they never had an effect: nothing attached one to a mission)."""
    client, core, _ = web
    assert client.get("/missions/standing").status_code == 404
    assert "STANDING GRANTS" not in client.get("/missions").get_data(as_text=True)
