"""Grant validation (contract 4) and the grant-to-environment builder (default deny)."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions_helpers import advance_to, make_store, new_mission, repair_grant

from planet_express.missions.approvals import execution_predicate, record_approval
from planet_express.missions.grants import (
    EnvLeak,
    Inventory,
    SiteLimits,
    build_session_env,
    grant_hash,
    scan_session_env,
    validate_grant,
    with_site_protected,
)
from planet_express.missions.models import Grant, VerificationPlan
from planet_express.missions.protected import Discovery, build_protected
from planet_express.missions.store import GrantLocked, MissionStore

STACKS = "/srv/stacks"
INV = Inventory(hosts=("local",), stacks=("media", "photos"), services=("media/qbit", "media/traefik"),
                mounts=("/mnt/data",), path_roots=(STACKS, "/mnt/data"))
LIMITS = SiteLimits(wall_s=3600, tool_calls=500, output_bytes=10_000_000, retries=3)
CHECKS = {"http", "container_state"}
TOOLS = {"arr.queue_item", "traefik.read"}


PLAN = VerificationPlan.model_validate(
    {"catalogue_v": 1, "checks": [{"id": "http", "check": "http", "params": {"url": "http://localhost/"}}]})


def protected(**kw):
    return build_protected(site=["service:media/traefik"], discovery=Discovery(stacks_root=STACKS, **kw))


def grant(**over):
    body = {"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["local"]},
            "verification": {"checks": ["http"], "plan_sha256": PLAN.hash()}, "backup": {"covered": True}}
    body.update(over)
    g = Grant.model_validate(body)
    return with_site_protected(g, protected())


def problems(g, **over):
    kw = dict(inventory=INV, protected=protected(), known_checks=CHECKS, known_tools=TOOLS, limits=LIMITS,
              stacks_root=STACKS)
    kw.setdefault("verification_plan", PLAN)
    kw.update(over)
    return validate_grant(g, **kw)


def test_a_good_grant_validates():
    assert problems(grant()) == []


def test_stack_containing_a_protected_service_is_allowed_with_carve_out():
    # the protected ingress container lives inside stack media: protected wins at match time, scope stays valid
    assert problems(grant()) == []


def test_scope_outside_the_inventory_is_refused():
    assert any("inventory" in p for p in problems(grant(scope={"stacks": ["nope"], "hosts": ["local"]})))
    assert any("inventory" in p for p in problems(grant(scope={"services": ["media/ghost"], "hosts": ["local"]})))
    assert any("inventory" in p for p in problems(grant(scope={"mounts": ["/mnt/other"], "hosts": ["local"]})))
    assert any("inventory" in p for p in problems(grant(scope={"paths": ["/etc"], "hosts": ["local"]})))
    assert any("inventory" in p for p in problems(grant(scope={"stacks": ["media"], "hosts": ["elsewhere"]})))


def test_a_scope_item_that_is_protected_is_refused_unless_critical():
    g = grant(scope={"services": ["media/traefik"], "hosts": ["local"]})
    assert any("protected" in p for p in problems(g))
    crit = grant(tier="critical", scope={"services": ["media/traefik"], "hosts": ["local"]})
    assert not any("protected" in p for p in problems(crit))


def test_a_scope_path_inside_a_protected_path_is_refused():
    prot = build_protected(site=[], discovery=Discovery(stacks_root=STACKS, storage_mounts=["/mnt/data"]))
    g = with_site_protected(Grant.model_validate(
        {"tier": "repair", "scope": {"paths": ["/mnt/data/photos"], "hosts": ["local"]},
         "verification": {"checks": ["http"]}, "backup": {"covered": True}}), prot)
    assert any("protected" in p for p in problems(g, protected=prot))


def test_grant_must_carry_the_site_protected_set():
    g = Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["local"]},
                              "verification": {"checks": ["http"]}, "backup": {"covered": True}})
    assert any("protected" in p for p in problems(g))


def test_unknown_check_and_missing_checks_are_refused():
    assert any("check" in p for p in problems(grant(verification={"checks": ["nope"]})))
    assert any("check" in p for p in problems(grant(verification={"checks": []})))


def test_budgets_over_the_site_maximum_are_refused():
    assert any("budget" in p for p in problems(grant(budgets={"wall_s": 99999})))
    assert any("budget" in p for p in problems(grant(budgets={"tool_calls": 501})))
    assert any("budget" in p for p in problems(grant(budgets={"output_bytes": 10_000_001})))
    assert any("budget" in p for p in problems(grant(budgets={"retries": 4})))


def test_backup_floor_needs_cover_or_an_acknowledgement():
    assert any("backup" in p for p in problems(grant(backup={"covered": False})))
    assert not any("backup" in p for p in problems(grant(backup={"covered": False, "acknowledged_no_backup": True})))


def test_credentials_must_be_registered_brokered_tools():
    assert problems(grant(credentials=["tool:arr.queue_item"])) == []
    assert any("credential" in p for p in problems(grant(credentials=["tool:ghost"])))
    assert any("credential" in p for p in problems(grant(credentials=["DNS_API_KEY"])))


def test_sudo_rule_on_a_protected_unit_is_refused():
    g = grant(sudo={"mode": "scoped", "rules": [{"unit": "reboot.target", "actions": ["start"]}]})
    assert any("protected" in p for p in problems(g))


def test_empty_scope_for_repair_is_refused():
    assert any("scope" in p for p in problems(grant(scope={"hosts": ["local"]})))


def test_observe_grant_skips_the_repair_only_rules():
    g = grant(tier="observe", verification={"checks": []}, backup={"covered": False})
    assert problems(g) == []


def test_protected_set_in_the_grant_is_hash_bound():
    a = grant()
    b = Grant.model_validate({**a.model_dump(mode="json"), "protected": {"resources": a.protected.resources[:-1]}})
    assert grant_hash(a) != grant_hash(b) and grant_hash(a) == a.hash()


# ── approval binding ────────────────────────────────────────────────────────────────────────────

def test_a_changed_grant_invalidates_the_approval(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, repair_grant())
    advance_to(ms, m.id, "queued", "investigating", "awaiting_decision")
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    assert execution_predicate(store, m.id).ok
    with pytest.raises(GrantLocked):
        ms.set_grant(m.id, repair_grant(credentials=["tool:x"]), actor="operator:me")
    # even a direct tamper with the stored grant is caught: the row binds to the hash of the grant it approved
    edited = repair_grant(credentials=["tool:x"])
    with store._write() as conn:
        conn.execute("UPDATE missions SET grant_json=?, grant_hash=? WHERE id=?",
                     (edited.canonical(), edited.hash(), m.id))
    assert not execution_predicate(store, m.id).ok
    assert "no approval for the current grant hash" in execution_predicate(store, m.id).reasons


# ── grant -> environment ────────────────────────────────────────────────────────────────────────

SECRETS = {"DNS_API_KEY": "dns-key-0123456789", "BACKUP_PASSWORD": "restic-pw-0123456789",
           "ROUTER_TOKEN": "router-tok-0123456789", "ARR_API_KEY": "arr-key-0123456789"}


def make_env(tmp_path, g=None, **over):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    base = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **SECRETS}
    kw = dict(mission_id="M-1", workspace=str(ws), worker_socket="/run/s/w.sock", protected=protected(),
              stacks_root=STACKS, base_env=base, known_tools=TOOLS, secret_values=list(SECRETS.values()))
    kw.update(over)
    return build_session_env(g or grant(), **kw)


def test_env_has_no_credentials_unless_named(tmp_path):
    se = make_env(tmp_path)
    assert set(se.env) == {"PATH", "LANG", "HOME", "PE_MISSION_ID", "PE_WORKER_SOCKET", "PE_TOOLS"}
    assert se.tools == ()
    assert se.env["PE_TOOLS"] == ""
    assert not any(s in v for v in se.env.values() for s in SECRETS.values())
    assert set(se.dropped) == set(SECRETS)


def test_named_tools_appear_by_name_only(tmp_path):
    se = make_env(tmp_path, grant(credentials=["tool:arr.queue_item"]))
    assert se.tools == ("arr.queue_item",)
    assert se.env["PE_TOOLS"] == "arr.queue_item"
    assert SECRETS["ARR_API_KEY"] not in "".join(se.env.values())


def test_value_that_is_a_secret_is_dropped_whatever_its_name(tmp_path):
    se = make_env(tmp_path, base_env={"PATH": "/bin", "INNOCENT": "xx" + SECRETS["DNS_API_KEY"]})
    assert "INNOCENT" not in se.env and se.dropped == ("INNOCENT",)


def test_unregistered_tool_is_a_hard_failure(tmp_path):
    with pytest.raises(EnvLeak):
        make_env(tmp_path, grant(credentials=["tool:ghost"]))


def test_pe_keys_in_the_base_environment_are_never_passed_on(tmp_path):
    se = make_env(tmp_path, base_env={"PATH": "/bin", "PE_SESSION_TOKEN": "x" * 40, "PE_MISSION_ID": "evil"})
    assert se.env["PE_MISSION_ID"] == "M-1" and "PE_SESSION_TOKEN" not in se.env


def test_readable_paths_exclude_protected_and_secret_files(tmp_path):
    secret_file = f"{STACKS}/media/core-secrets.yaml"
    se = make_env(tmp_path, secret_paths=[secret_file, "/etc/pe/config.yaml"])
    assert f"{STACKS}/media" in se.readable
    assert secret_file in se.denied and "/etc/pe/config.yaml" in se.denied
    assert str(tmp_path / "ws") in se.readable
    assert all(not r.startswith("/etc") for r in se.readable)


def test_a_secret_path_that_is_itself_readable_scope_is_removed(tmp_path):
    g = grant(scope={"paths": [f"{STACKS}/media/keys"], "hosts": ["local"]})
    se = make_env(tmp_path, g, secret_paths=[f"{STACKS}/media"])
    assert f"{STACKS}/media/keys" not in se.readable


def test_scan_finds_nothing_in_a_clean_session_and_finds_a_planted_secret(tmp_path):
    stacks = tmp_path / "stacks" / "media"
    stacks.mkdir(parents=True)
    (stacks / "compose.yml").write_text("services: {}\n")
    se = make_env(tmp_path, stacks_root=str(tmp_path / "stacks"), protected=build_protected(
        site=[], discovery=Discovery(stacks_root=str(tmp_path / "stacks"))), g=grant(scope={"stacks": ["media"], "hosts": ["local"]}))
    assert scan_session_env(se, list(SECRETS.values())) == []
    (stacks / "leak.txt").write_text("key=" + SECRETS["DNS_API_KEY"])
    hits = scan_session_env(se, list(SECRETS.values()))
    assert hits and "leak.txt" in hits[0]
    assert SECRETS["DNS_API_KEY"] not in " ".join(hits)       # a hit names where, never the value


def test_scan_flags_a_secret_in_the_env(tmp_path):
    se = make_env(tmp_path)
    object.__setattr__(se, "env", {**se.env, "X": SECRETS["ROUTER_TOKEN"]})
    assert any("env" in h for h in scan_session_env(se, list(SECRETS.values())))
    assert os.path.isdir(se.cwd)


def test_a_scope_path_that_is_not_normalised_is_refused_before_approval():
    # Leases refuse a non-normalised path, so such a grant would be approved and then never start.
    for path in (f"{STACKS}/media/", f"{STACKS}//media", f"{STACKS}/./media"):
        assert any("not normalised" in p for p in problems(grant(scope={"paths": [path], "hosts": ["local"]}))), path
    assert not any("not normalised" in p for p in problems(grant(scope={"paths": [f"{STACKS}/media"], "hosts": ["local"]})))
