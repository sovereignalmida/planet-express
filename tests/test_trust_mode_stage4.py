"""Trust mode, stages 4 and 5 (docs/designs/v4-trust-mode.md): the strict model's grant fields, the Docker proxy, the side-door
gate, the protected set and the backup floor are gone. What this checks beyond the modules' own tests:

  - a mission row written before the update still loads, and an approval made before it is refused in words, never run;
  - a session's environment carries no DOCKER_HOST and no model key;
  - a mission waiting on another's lease says which mission holds what, and what to do;
  - a plan may still ask PE to check that a restart really happened."""
import json

import pytest
from missions_helpers import DEFAULT_PLAN
from repair_fakes import build, update

from planet_express.console import execution as ex_mod, plan as plan_mod
from planet_express.missions import approvals, leases
from planet_express.missions.approval_flow import FlowError
from planet_express.missions.canonical import canonical_json, sha256_hex
from planet_express.missions.models import LEGACY_GRANT_TEXT, Grant, is_legacy_grant
from planet_express.missions.runner import SessionSockets, without_broker_key


def legacy_grant_doc(**over) -> dict:
    """A repair grant exactly as rc13 stored it: every field the old model dumped, defaults included."""
    doc = {"v": 1, "tier": "repair",
           "scope": {"stacks": ["media"], "services": [], "paths": [], "mounts": [], "hosts": ["local"]},
           "protected": {"resources": ["host:local/service:media/traefik", "host:local/path:/etc/shadow"]},
           "exclusions": ["service:media/gluetun"], "window": {"deny": [], "allow": None},
           "sudo": {"mode": "scoped", "rules": []}, "credentials": [],
           "budgets": {"wall_s": 1800, "tool_calls": 200, "output_bytes": 4194304, "retries": 2},
           "effects": {"max_unexpected_in_scope": 3, "per_resource": {}}, "on_verify_fail": "needs_operator",
           "docker": "lifecycle",
           "verification": {"checks": ["default"], "agreed_at": 0, "plan_sha256": DEFAULT_PLAN.hash()},
           "backup": {"covered": False, "recent_s": 86400, "acknowledged_no_backup": False}}
    doc.update(over)
    return doc


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


def make_legacy(st, *, approved: bool):
    """A Repair waiting in awaiting_decision whose stored grant is from before trust mode, optionally with its (old) approval row."""
    mid = st.propose()["mission_id"]
    doc = legacy_grant_doc()
    old_hash = sha256_hex(doc)
    with st.store._write() as conn:
        conn.execute("UPDATE missions SET grant_json=?, grant_hash=? WHERE id=?", (canonical_json(doc), old_hash, mid))
        if approved:
            conn.execute("INSERT INTO mission_approvals (id, mission_id, phase, bound_hash, decision, operator, elevated, decided_at) "
                         "VALUES ('A-old', ?, 'grant', ?, 'approve', 'operator:me', 0, 1)", (mid, old_hash))
    return mid, old_hash


# ── old rows ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_grant_stored_before_trust_mode_still_loads_without_its_removed_fields():
    text = canonical_json(legacy_grant_doc())
    grant = Grant.load(text)
    assert grant.tier == "repair" and grant.scope.stacks == ["media"] and grant.verification.plan_sha256 == DEFAULT_PLAN.hash()
    assert not {"docker", "sudo", "protected", "backup", "exclusions", "effects"} & set(grant.model_dump())
    assert is_legacy_grant(text) and not is_legacy_grant(grant.canonical())
    assert grant.hash() != sha256_hex(legacy_grant_doc()), "the loaded grant is not the one that was approved"


def test_a_new_grant_is_still_strict():
    with pytest.raises(ValueError):
        Grant.model_validate({"tier": "repair", "docker": "lifecycle"})
    with pytest.raises(ValueError):
        Grant.load(json.dumps({"tier": "repair", "surprise": 1}))          # only the removed fields are let through


def test_every_reader_of_a_mission_takes_an_old_row(stack):
    st = stack()
    mid, _ = make_legacy(st, approved=False)
    mission = st.ms.get_mission(mid)
    assert mission.grant().scope.stacks == ["media"]
    card = st.flow.card(mid)                                                   # the approval card draws it
    assert card["warnings"][0]["kind"] == "legacy" and LEGACY_GRANT_TEXT in card["warnings"][0]["text"]


# ── a pre-upgrade approval is refused in words ───────────────────────────────────────────────────────

def test_an_approval_made_before_the_upgrade_never_starts_a_run_and_says_why(stack):
    st = stack()
    mid, _ = make_legacy(st, approved=True)
    verdict = approvals.execution_predicate(st.store, mid)
    assert not verdict.ok and verdict.reasons == (LEGACY_GRANT_TEXT,)
    for _ in range(3):
        st.sup.tick()
    assert st.status(mid) == "awaiting_decision" and st.runtimes == []
    waits = [json.loads(e.body_json)["start_waiting"] for e in st.ms.events(mid) if "start_waiting" in e.body_json]
    assert waits == [LEGACY_GRANT_TEXT], "said once, in words"
    assert ex_mod.start_waiting(st.ms, st.ms.get_mission(mid)) == LEGACY_GRANT_TEXT          # what the Console shows


def test_an_old_mission_cannot_be_approved_after_the_upgrade(stack):
    st = stack()
    mid, old_hash = make_legacy(st, approved=False)
    with pytest.raises(FlowError) as err:
        st.flow.approve("operator:me", mid, "grant", old_hash)
    assert err.value.code == "conflict" and "confirm the plan again" in str(err.value)
    st.sup.tick()
    assert not [e for e in st.ms.events(mid) if "start_waiting" in e.body_json], "an unapproved old mission is not announced"


def test_an_old_mission_can_still_be_withdrawn(stack):
    st = stack()
    mid, old_hash = make_legacy(st, approved=False)
    assert st.flow.deny("operator:me", mid, old_hash)["status"] == "cancelled"


# ── the session's environment ─────────────────────────────────────────────────────────────────────────

class _Broker:
    key_env = "ANTHROPIC_API_KEY"

    def secret_values(self):
        return ["sk-real-model-key-0123456789"]

    def env(self, cs):
        return {"ANTHROPIC_BASE_URL": "http://127.0.0.1:41000", "ANTHROPIC_AUTH_TOKEN": "per-session-code"}

    def start(self, cs):
        return None


def test_the_session_environment_has_no_docker_host_and_no_model_key(tmp_path):
    from planet_express.missions.grants import make_env_policy
    policy = without_broker_key(make_env_policy(stacks_root=str(tmp_path), known_tools=frozenset()), _Broker())
    base = {"PATH": "/bin", "DOCKER_HOST": "unix:///run/planetexpress-sessions/S-1/proxy.sock",
            "ANTHROPIC_API_KEY": "sk-real-model-key-0123456789", "SNEAKY": "x-sk-real-model-key-0123456789"}
    env = policy(Grant.model_validate({"tier": "repair"}), "M-1", str(tmp_path), base)
    assert "DOCKER_HOST" not in env and "ANTHROPIC_API_KEY" not in env and "SNEAKY" not in env
    assert "sk-real-model-key-0123456789" not in json.dumps(env)
    sockets = SessionSockets(str(tmp_path), backend=None, authenticator=None, model_broker=_Broker())
    assert set(sockets.env(None)) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}, "the broker's code, never a Docker socket"


# ── waiting on a lease ────────────────────────────────────────────────────────────────────────────────

def test_a_run_waiting_on_another_missions_lease_names_it_and_says_what_to_do(stack):
    st = stack(behaviours=[{"script": [update()] * 30}, {"script": [update("finished")]}])
    first = st.propose()["mission_id"]
    st.approve_all(first)
    st.sup.tick()
    assert st.status(first) == "executing"
    second = st.propose()["mission_id"]
    st.approve_all(second)
    st.sup.tick()
    assert st.status(second) == "awaiting_decision"
    want = f"{first} is holding the media stack — cancel or roll it back to continue"
    assert ex_mod.start_waiting(st.ms, st.ms.get_mission(second)) == want


def test_the_conflict_words_prefer_the_stack_to_its_directory():
    exc = leases.LeaseConflict("host:local/path:/srv/stacks/media", "host:local/stack:media", "M-13")
    assert leases.conflict_words(exc) == "M-13 is holding the media stack — cancel or roll it back to continue"
    exc = leases.LeaseConflict("host:local/service:media/sonarr", "host:local/path:/srv/stacks/media", "M-2")
    assert leases.conflict_words(exc).startswith("M-2 is holding the media/sonarr service")


# ── plans: the restart check stays available; restart-only is gone ───────────────────────────────────

def option(**over):
    base = {"name": "Restart prowlarr", "recommended": True, "summary": "s", "changes": ["media/prowlarr"],
            "will_not_touch": ["the gateway"], "risk": {"level": "low", "text": "t"}, "rolls_back_to": "the checkpoint",
            "effort": "1 min", "scope": {"stacks": [], "services": ["media/prowlarr"]},
            "checks": [{"id": "up", "check": "container_state",
                        "params": {"container": "prowlarr", "started_during_mission": True}}]}
    base.update(over)
    return base


def test_a_plan_may_ask_pe_to_check_that_a_restart_really_happened():
    plan = plan_mod.parse({"known": [], "unsure": [], "options": [option()]})
    assert plan.options[0].checks[0].params["started_during_mission"] is True
    assert "restarted by this run" in plan_mod.check_phrase(plan.options[0].checks[0])
    text = plan_mod.instructions()
    assert "started_during_mission: true" in text and "restart_only" not in text and "full access" in text


def test_restart_only_is_no_longer_part_of_a_plan():
    with pytest.raises(plan_mod.PlanError):
        plan_mod.parse({"known": [], "unsure": [], "options": [option(restart_only=True)]})
    assert "restart_only" not in json.dumps(plan_mod.json_schema())
    stored = json.dumps({"known": [], "unsure": [], "options": [option(restart_only=False)]})
    assert plan_mod.stored_before_trust_mode(stored)
    assert not plan_mod.stored_before_trust_mode(json.dumps({"known": [], "unsure": [], "options": [option()]}))
