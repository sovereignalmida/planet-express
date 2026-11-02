"""Restart-only repairs need no recent backup (rc13, operator decision after M-5: restarting Prowlarr asked to accept "no backup").

An option the agent marks `restart_only` mints a `docker: lifecycle` grant. What makes skipping the backup floor safe is that the
grant is ENFORCED to be restart-only, not described as one: the proxy serves reads and container start/stop/restart/kill only, the
typed-step gate runs restarts and checks only, and the grant carries no sudo rules, paths, mounts or brokered credentials."""
import os

import pytest
from test_console_confirm import Rig, plan_doc
from test_dockerproxy_policy import head, policy, root  # noqa: F401  (fixtures)
from test_mission_typed_steps import Rig as StepRig
from test_mission_typed_steps import book, restart

from planet_express.console import plan as plan_mod
from planet_express.missions.dockerproxy.httpwire import Refusal
from planet_express.missions.dockerproxy.policy import LIFECYCLE_MIN_GRACE_S, LIFECYCLE_WRITES, ROUTES, Policy
from planet_express.missions.grants import Inventory, SessionPrincipals, SiteLimits, lifecycle_write_reach, validate_grant
from planet_express.missions.models import Grant
from planet_express.missions.protected import Discovery, build_protected
from planet_express.missions.typed_steps import StepRefused

WHO = SessionPrincipals(60000, 60999)          # the launcher's default range: nobody on a test box
BASE = {"tier": "repair", "scope": {"stacks": ["media"], "hosts": ["local"]}, "verification": {"checks": ["http"]}}


def floor_reasons(grant: Grant) -> list[str]:
    prot = build_protected(site=[], discovery=Discovery(stacks_root="/srv/stacks"))
    inv = Inventory(hosts=("local",), stacks=("media",), services=(), mounts=(), path_roots=("/srv/stacks",))
    out = validate_grant(grant, inventory=inv, protected=prot, known_checks={"http"}, known_tools=set(),
                         limits=SiteLimits(3600, 500, 10_000_000, 3), stacks_root="/srv/stacks",
                         session_principals=WHO)
    return [r for r in out if r.startswith("backup floor")]


# ── the grant ────────────────────────────────────────────────────────────────────────────────────────

def test_a_lifecycle_grant_skips_the_backup_floor_and_a_proxy_grant_does_not():
    assert floor_reasons(Grant.model_validate({**BASE, "docker": "lifecycle"})) == []
    assert floor_reasons(Grant.model_validate(BASE)) != []


@pytest.mark.parametrize("over", [
    {"sudo": {"mode": "scoped", "rules": [{"unit": "casa-stacks.service", "actions": ["restart"]}]}},
    {"scope": {"stacks": ["media"], "paths": ["/srv/stacks/media/config"], "hosts": ["local"]}},
    {"scope": {"stacks": ["media"], "mounts": ["/mnt/data"], "hosts": ["local"]}},
    {"credentials": ["tool:arr.queue_item"]},
])
def test_a_lifecycle_grant_cannot_carry_anything_that_reaches_data(over):
    with pytest.raises(ValueError, match="lifecycle carries no"):
        Grant.model_validate({**BASE, **over, "docker": "lifecycle"})


# ── the proxy ────────────────────────────────────────────────────────────────────────────────────────

def test_the_lifecycle_proxy_serves_only_start_stop_restart_and_kill(policy):  # noqa: F811
    life = Policy(policy.scope, policy.inspector, lifecycle=True)
    assert life.match(head("POST", "/v1.43/containers/qbittorrent/restart"))[0].name == "container.restart"
    assert life.match(head("GET", "/v1.43/containers/json"))[0].name == "containers.list"
    for method, target in [("POST", "/v1.43/containers/create"), ("DELETE", "/v1.43/containers/qbittorrent"),
                           ("POST", "/v1.43/containers/qbittorrent/exec"), ("PUT", "/v1.43/containers/qbittorrent/archive?path=/"),
                           ("POST", "/v1.43/containers/qbittorrent/update"), ("POST", "/v1.43/containers/qbittorrent/rename?name=x"),
                           ("POST", "/v1.43/containers/qbittorrent/kill"), ("POST", "/v1.43/containers/qbittorrent/kill?signal=SIGKILL")]:
        with pytest.raises(Refusal, match="may only start, stop and restart"):
            life.match(head(method, target))
    assert {r.name for r in ROUTES if r.write} > LIFECYCLE_WRITES, "everything else that writes is refused"
    assert policy.match(head("POST", "/v1.43/containers/create"))[0].name == "container.create", "a proxy grant is unchanged"


# ── typed steps ──────────────────────────────────────────────────────────────────────────────────────

def test_a_lifecycle_mission_runs_restarts_but_not_stack_up(tmp_path):
    rig = StepRig(tmp_path, docker="lifecycle")
    assert rig.gate.authorize(rig.mission.id, book(restart()), session_id=rig.session)
    b = restart()["binding"]
    up = {"type": "stack.up", "params": {"stack": "media"},
          "binding": {"compose_path": b["compose_path"], "compose_sha256": b["compose_sha256"], "project": "media", "services": ["sonarr"]}}
    with pytest.raises(StepRefused, match="more than a restart-only repair may run"):
        rig.gate.authorize(rig.mission.id, book(up), session_id=rig.session)


# ── the Console: the option says so, the confirmation needs no backup ────────────────────────────────

def test_a_restart_only_option_is_confirmed_without_a_backup_and_says_why(tmp_path):
    rig = Rig(tmp_path, covered=False)
    try:
        rig.flow.validator = lambda grant, plan: floor_reasons(grant)          # the real floor rule, nothing else
        doc = plan_doc()
        doc["options"][0]["restart_only"] = True
        view = rig.api.confirm_prepare(rig.conv.id, rig.add_plan(doc), 0, "chris")
        grant = rig.st.ms.get_mission(view["mission_id"]).grant()
        assert grant.docker == "lifecycle" and grant.backup.acknowledged_no_backup is False
        assert view["backup"]["not_needed"] is True and "only start, stop and restart" in view["backup"]["text"]
        assert view["needs_elevation"] is False and view["phases"] == ["grant"], "no elevated no-backup approval"
        assert any("RESTART ONLY" in w for w in view["warnings"])
    finally:
        rig.close()


def test_an_ordinary_option_still_asks_about_the_backup(tmp_path):
    rig = Rig(tmp_path, covered=False)
    try:
        rig.flow.validator = lambda grant, plan: floor_reasons(grant)
        from planet_express.console.api import ConsoleApiError
        with pytest.raises(ConsoleApiError) as err:
            rig.api.confirm_prepare(rig.conv.id, rig.add_plan(), 0, "chris")
        assert err.value.code == "no_backup"
    finally:
        rig.close()


def test_the_agent_is_told_about_restart_only_and_the_schema_allows_it():
    assert "restart_only" in plan_mod.PLAN_INSTRUCTIONS
    assert plan_mod.json_schema()["properties"]["options"]["items"]["properties"]["restart_only"] == {"type": "boolean"}
    assert plan_mod.Plan.model_validate(plan_doc()).options[0].restart_only is False, "absent means an ordinary option"


def test_a_lifecycle_stop_or_restart_is_graceful_only(policy):  # noqa: F811
    """An abrupt stop can lose a stateful app's unflushed writes, and a lifecycle grant runs without a backup (Codex review, rc13)."""
    life = Policy(policy.scope, policy.inspector, lifecycle=True)
    for name in ("container.stop", "container.restart"):
        r = next(x for x in ROUTES if x.name == name)
        assert life.clean_query(r, {}) == {}
        assert life.clean_query(r, {"t": str(LIFECYCLE_MIN_GRACE_S)}) == {"t": str(LIFECYCLE_MIN_GRACE_S)}
        with pytest.raises(Refusal, match="own stop signal"):
            life.clean_query(r, {"signal": "SIGKILL"})
        for t in ("0", "1", str(LIFECYCLE_MIN_GRACE_S - 1)):
            with pytest.raises(Refusal, match="at least"):
                life.clean_query(r, {"t": t})
        assert policy.clean_query(r, {"t": "0", "signal": "SIGKILL"}), "a proxy grant is unchanged"


# ── the session's own shell: the waiver holds only if it cannot write in scope ───────────────────────

@pytest.fixture
def open_root():
    """A stacks root others can traverse (pytest's own tmp dirs are 0700, which would make every check pass vacuously)."""
    import shutil
    import tempfile
    base = tempfile.mkdtemp(dir="/tmp", prefix="pe-reach-")
    os.chmod(base, 0o755)
    os.makedirs(os.path.join(base, "media", "config"))
    for d in (os.path.join(base, "media"), os.path.join(base, "media", "config")):
        os.chmod(d, 0o755)
    with open(os.path.join(base, "media", "config", "app.db"), "w") as fh:
        fh.write("x")
    os.chmod(os.path.join(base, "media", "config", "app.db"), 0o644)
    yield base
    shutil.rmtree(base, ignore_errors=True)


LIFE = Grant.model_validate({**BASE, "docker": "lifecycle"})


def test_nothing_writable_by_others_waives_the_floor(open_root):
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None


def test_a_world_writable_file_or_directory_in_a_stack_keeps_the_floor(open_root):
    target = os.path.join(open_root, "media", "config", "app.db")
    os.chmod(target, 0o666)
    assert "can be written by a session" in lifecycle_write_reach(LIFE, open_root, WHO)
    os.chmod(target, 0o644)
    os.chmod(os.path.join(open_root, "media", "config"), 0o777)
    assert "config can be written by a session" in lifecycle_write_reach(LIFE, open_root, WHO)


def test_what_others_cannot_traverse_is_out_of_their_reach(open_root):
    os.chmod(os.path.join(open_root, "media", "config", "app.db"), 0o666)
    os.chmod(os.path.join(open_root, "media", "config"), 0o750)
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None
    os.chmod(os.path.join(open_root, "media", "config"), 0o755)
    os.chmod(open_root, 0o750)
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None, "an ancestor others cannot enter hides everything below"


def test_a_tree_too_large_to_check_keeps_the_floor(open_root):
    assert "too many to check" in lifecycle_write_reach(LIFE, open_root, WHO, limit=1)


def test_the_floor_names_why_a_restart_only_grant_still_needs_a_backup(open_root):
    os.chmod(os.path.join(open_root, "media", "config", "app.db"), 0o666)
    prot = build_protected(site=[], discovery=Discovery(stacks_root=open_root))
    inv = Inventory(hosts=("local",), stacks=("media",), services=(), mounts=(), path_roots=(open_root,))
    out = validate_grant(LIFE, inventory=inv, protected=prot, known_checks={"http"}, known_tools=set(),
                         limits=SiteLimits(3600, 500, 10_000_000, 3), stacks_root=open_root,
                         session_principals=WHO)
    floor = [r for r in out if r.startswith("backup floor")]
    assert floor and "restart-only repair skips the backup only when nothing in its stacks is writable" in floor[0]


def test_the_checkpoint_asks_again_at_start(open_root):
    """Proposed while nothing was writable, started after something became so: the floor applies at the checkpoint."""
    from types import SimpleNamespace

    from planet_express.missions.checkpoint.service import BackupFloorError, CheckpointService
    calls = []
    fake = SimpleNamespace(stacks_root=open_root, _now=lambda: 0, session_principals=WHO,
                           backup=SimpleNamespace(status=lambda path, now, recent_s: calls.append(path) or SimpleNamespace(state="stale")))
    scope = SimpleNamespace(data_paths=(os.path.join(open_root, "media"),))
    assert CheckpointService._backup_floor(fake, scope, LIFE) is False and calls == []
    os.chmod(os.path.join(open_root, "media", "config", "app.db"), 0o666)
    with pytest.raises(BackupFloorError):
        CheckpointService._backup_floor(fake, scope, LIFE)


def test_an_unattended_start_still_needs_a_covered_backup():
    import inspect

    from planet_express.missions import approvals
    assert "standing grants need a covered backup, restart-only included" in inspect.getsource(approvals)


def test_a_stack_a_session_uid_owns_keeps_the_floor_even_with_no_other_bits(open_root):
    """Codex review, rc13: looking at the 'other' bits alone would miss the owner class."""
    for d in (os.path.join(open_root, "media"), os.path.join(open_root, "media", "config")):
        os.chmod(d, 0o750)
    who = SessionPrincipals(os.getuid(), os.getuid())
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None, "others cannot even enter"
    assert "can be written by a session" in lifecycle_write_reach(LIFE, open_root, who)


def test_a_group_a_session_holds_counts_and_so_does_a_log_group(open_root):
    os.chmod(os.path.join(open_root, "media", "config", "app.db"), 0o664)
    gid = os.getgid()
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None
    assert "app.db can be written by a session" in lifecycle_write_reach(LIFE, open_root, SessionPrincipals(60000, 60999, frozenset({gid})))


def test_a_named_acl_entry_for_a_session_uid_counts_and_the_mask_still_applies(open_root):
    import shutil
    import subprocess
    if shutil.which("setfacl") is None:
        pytest.skip("setfacl is not installed")
    db = os.path.join(open_root, "media", "config", "app.db")
    if subprocess.run(["setfacl", "-m", "u:60001:rw", db], capture_output=True).returncode != 0:
        pytest.skip("this filesystem has no ACLs")
    assert "app.db can be written by a session" in lifecycle_write_reach(LIFE, open_root, WHO)
    subprocess.run(["setfacl", "-m", "m::r", db], check=True)
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None, "the mask takes the write away"
    subprocess.run(["setfacl", "-m", "u:60001:--x,m::rwx", os.path.join(open_root, "media")], check=True)
    os.chmod(os.path.join(open_root, "media"), 0o750)          # only the ACL lets a session in
    subprocess.run(["setfacl", "-m", "m::rwx", os.path.join(open_root, "media")], check=True)
    subprocess.run(["setfacl", "-m", "m::rw", db], check=True)
    assert "app.db can be written by a session" in lifecycle_write_reach(LIFE, open_root, WHO), "reached through the ACL"


def test_unknown_session_identities_waive_nothing(open_root):
    assert "not known" in lifecycle_write_reach(LIFE, open_root, None)


def test_a_directory_one_session_uid_owns_is_judged_for_the_other_session_uids_too(open_root):
    """Codex, rc13: a directory owned by session uid A with 0055 is closed to A but open to session uid B through the other bits.
    Judged by the owner bits alone it was skipped as unreachable, and the floor waived without looking inside."""
    config = os.path.join(open_root, "media", "config")
    os.chmod(config, 0o055)
    try:
        who = SessionPrincipals(os.getuid(), os.getuid() + 5)
        assert lifecycle_write_reach(LIFE, open_root, who) is not None, "entered, so checked (and here unreadable: the floor stays)"
    finally:
        os.chmod(config, 0o755)


def test_a_link_out_of_the_stack_keeps_the_floor_and_one_within_it_does_not(open_root):
    media = os.path.join(open_root, "media")
    os.symlink(os.path.join(media, "config", "app.db"), os.path.join(media, "inside"))
    assert lifecycle_write_reach(LIFE, open_root, WHO) is None
    os.symlink("/etc/hostname", os.path.join(media, "outside"))
    assert "link that leads outside" in lifecycle_write_reach(LIFE, open_root, WHO)


def test_a_dangling_link_and_a_linked_stack_directory_keep_the_floor(open_root):
    media = os.path.join(open_root, "media")
    os.symlink(os.path.join(media, "not-yet"), os.path.join(media, "dangling"))
    assert "link that leads outside" in lifecycle_write_reach(LIFE, open_root, WHO)
    os.remove(os.path.join(media, "dangling"))
    os.rename(media, os.path.join(open_root, "real-media"))
    os.symlink(os.path.join(open_root, "real-media"), media)
    assert "is a symbolic link" in lifecycle_write_reach(LIFE, open_root, WHO)
