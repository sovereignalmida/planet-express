"""Undo covers every file the plan changes (rc14.5, from live M-30).

M-30, a Repair confirmed in the Console, moved Soulsync's YouTube binds to the NFS share root. It edited the stack's compose file and a
helper script outside the stack (`/home/casaroot/casafast/soulsync-test/scripts/yt_film.py`). The checkpoint held only the compose
file and `.env`, so an Undo would have put the compose file back and left the script pointing at a path that no longer existed, with
nothing saying so.

The rule now: the confirmed option lists every file it changes (`files`); that list is hash-bound in the grant; the checkpoint captures
each one wherever it is on the host, and Undo puts it back. A file the checkpoint cannot cover (it does not exist yet, PE cannot write
its directory, it is PE's own) does not stop the run: the result says it is not covered. After the run, every file the hook saw the
session write that the checkpoint does not hold is named on the result card too.
"""
import dataclasses
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkpoint_fakes import make_env
from test_checkpoint_rollback_paths import DOTENV, media_env

from planet_express.console import execution as execution_mod
from planet_express.console import plan as plan_mod
from planet_express.missions import hooks
from planet_express.missions.canonical import sha256_hex
from planet_express.missions.checkpoint import CheckpointScope
from planet_express.missions.checkpoint.service import NOT_COVERED_ABSENT, NOT_COVERED_PE, _Hooks
from planet_express.missions.grants import Inventory, SiteLimits, validate_grant
from planet_express.missions.models import Grant, GrantScope
from planet_express.missions.supervisor import NOT_COVERED_GONE, NOT_COVERED_WRITTEN, Supervisor

SCRIPT = 'ROOT = "/media/yt-films"\n'


def outside(tmp_path: Path) -> Path:
    """A helper script outside the stacks root, as yt_film.py was."""
    d = tmp_path / "casafast" / "scripts"
    d.mkdir(parents=True)
    f = d / "yt_film.py"
    f.write_text(SCRIPT)
    f.chmod(0o664)
    return f


def with_files(env, *paths) -> CheckpointScope:
    return dataclasses.replace(env.scope, plan_files=tuple(str(p) for p in paths))


def notes(env, name):
    return [json.loads(e.body_json) for e in env.ms.events(env.mission.id) if json.loads(e.body_json).get("event") == name]


# ── the checkpoint ───────────────────────────────────────────────────────────────────────────────

def test_m30_undo_puts_back_the_script_outside_the_stack_as_well_as_the_compose_file(tmp_path):
    env = media_env(tmp_path)
    script = outside(tmp_path)
    cid = env.svc.create(env.mission.id, with_files(env, script))
    manifest = env.svc.manifest(cid)
    entry = next(f for f in manifest["files"] if f["path"] == str(script.resolve()))
    assert entry["kind"] == "plan_file" and entry["root"] == "/" and "not_covered" not in manifest
    assert manifest["scope"]["files"] == [str(script)]
    # what the session did: both files edited
    env.dotenv.write_text(DOTENV + "\n# m30\n")
    script.write_text('ROOT = "/casamedia_nfs/media/yt-films"\n')
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok, (report.reason, report.unrestored)
    assert sorted(report.written) == sorted([str(env.dotenv.resolve()), str(script.resolve())])
    assert script.read_text() == SCRIPT and env.dotenv.read_text() == DOTENV
    assert os.stat(script).st_mode & 0o777 == 0o664


def test_a_plan_file_that_does_not_exist_yet_is_not_covered_and_does_not_stop_the_run(tmp_path):
    env = make_env(tmp_path)
    missing = tmp_path / "casafast" / "new.conf"
    cid = env.svc.create(env.mission.id, with_files(env, missing))
    assert env.svc.not_covered(cid) == [{"path": str(missing), "why": NOT_COVERED_ABSENT}]
    created = notes(env, "checkpoint.created")[-1]
    assert created["not_covered"] == [{"path": str(missing), "why": NOT_COVERED_ABSENT}]


def test_pe_own_files_are_never_covered(tmp_path):
    env = make_env(tmp_path)
    own = tmp_path / "pe" / "config.yaml"
    own.parent.mkdir()
    own.write_text("x: 1\n")
    env.svc.pe_paths = (str(own.parent),)
    cid = env.svc.create(env.mission.id, with_files(env, own))
    assert env.svc.not_covered(cid) == [{"path": str(own), "why": NOT_COVERED_PE}]
    assert str(own) not in env.svc.covered_paths(cid)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write any directory")
def test_a_plan_file_in_a_directory_pe_cannot_write_is_not_covered(tmp_path):
    env = make_env(tmp_path)
    script = outside(tmp_path)
    script.parent.chmod(0o555)
    try:
        cid = env.svc.create(env.mission.id, with_files(env, script))
    finally:
        script.parent.chmod(0o755)
    [item] = env.svc.not_covered(cid)
    assert item["path"] == str(script) and "not writable" in item["why"]


def test_a_plan_file_that_vanishes_before_capture_is_not_covered_and_the_rest_is_captured(tmp_path):
    script_holder = {}

    class Vanish(_Hooks):
        def before_capture(self, planned):
            script_holder["p"].unlink()

    env = make_env(tmp_path, hooks=Vanish())
    script_holder["p"] = outside(tmp_path)
    cid = env.svc.create(env.mission.id, with_files(env, script_holder["p"]))
    [item] = env.svc.not_covered(cid)
    assert item["path"] == str(script_holder["p"]) and item["why"].startswith("cannot be snapshotted")
    assert str(env.compose.resolve()) in env.svc.covered_paths(cid)


def test_a_plan_file_that_is_also_a_stack_file_is_captured_once(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, with_files(env, env.dotenv))
    paths = [f["path"] for f in env.svc.manifest(cid)["files"]]
    assert paths.count(str(env.dotenv.resolve())) == 1 and env.svc.not_covered(cid) == []


# ── the grant ────────────────────────────────────────────────────────────────────────────────────

def test_a_grant_without_files_hashes_exactly_as_before():
    grant = Grant(tier="repair", scope=GrantScope(stacks=["media"]))
    doc = grant.model_dump(mode="json")
    assert "files" not in doc["scope"]
    legacy = {**doc, "scope": {"stacks": ["media"], "services": [], "hosts": ["local"]}}
    assert grant.hash() == sha256_hex(legacy)


def test_files_are_bound_into_the_grant_hash_and_survive_a_reload():
    a = Grant(tier="repair", scope=GrantScope(stacks=["media"], files=["/srv/a.conf"]))
    b = Grant(tier="repair", scope=GrantScope(stacks=["media"], files=["/srv/b.conf"]))
    assert a.hash() != b.hash() and Grant.load(a.canonical()).hash() == a.hash()


@pytest.mark.parametrize("bad", ["relative/x", "/a/../etc/shadow", "/a/./b", "/a//b", "/a/", "/"])
def test_validate_grant_refuses_a_file_that_is_not_a_plain_absolute_path(bad):
    grant = Grant(tier="repair", scope=GrantScope(stacks=["media"], files=[bad]))
    reasons = validate_grant(grant, inventory=Inventory(stacks=("media",)), known_checks=set(), known_tools=set(),
                             limits=SiteLimits(3600, 1000, 1 << 30, 3))
    assert any(r.startswith("scope file 1 ") for r in reasons)


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────

def _plan(files=None):
    option = {"name": "Move binds", "recommended": True, "summary": "s", "changes": ["compose binds"], "will_not_touch": [],
              "risk": {"level": "low", "text": "t"}, "rolls_back_to": "the checkpoint", "effort": "small",
              "scope": {"stacks": ["soulsync"], "services": []},
              "checks": [{"id": "up", "check": "project_complete", "params": {"stack": "soulsync"}}]}
    if files is not None:
        option["files"] = files
    return {"known": [], "unsure": [], "options": [option]}


def test_a_plan_lists_its_files_and_the_goal_names_them():
    plan = plan_mod.parse(_plan(["/home/casaroot/casafast/soulsync-test/scripts/yt_film.py"]))
    assert plan.options[0].files == ["/home/casaroot/casafast/soulsync-test/scripts/yt_film.py"]
    assert "/home/casaroot/casafast/soulsync-test/scripts/yt_film.py" in plan_mod.goal_text(plan.options[0])


def test_a_plan_with_a_bad_file_is_refused_in_words():
    with pytest.raises(plan_mod.PlanError) as exc:
        plan_mod.parse(_plan(["scripts/yt_film.py"]))
    assert "option 1: `files` item 1 is not an absolute path" in str(exc.value)


def test_a_plan_without_files_hashes_as_before():
    plan = plan_mod.parse(_plan())
    assert "files" not in plan_mod.canonical(plan)["options"][0]
    assert plan_mod.plan_hash(plan_mod.parse(_plan([]))) == plan_mod.plan_hash(plan)


# ── what the run wrote ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("tool", "args", "want"), [
    ("Edit", {"file_path": "/b/yt_film.py"}, (["/b/yt_film.py"], False)),
    ("Read", {"file_path": "/b/yt_film.py"}, ([], False)),
    ("Bash", {"command": "cd /srv/x && sudo cat a.yml && ls -la"}, ([], False)),
    ("Bash", {"command": "sudo sed -i 's|/media/yt|/casamedia_nfs/media/yt|' /b/yt_film.py"}, (["/b/yt_film.py"], False)),
    ("Bash", {"command": "sudo cp -a /a/c.yml /a/c.yml.bak; echo x > /etc/a.conf 2>&1"}, (["/etc/a.conf", "/a/c.yml.bak"], False)),
    ("Bash", {"command": "sudo docker compose -f /a/c.yml up -d soulsync"}, ([], False)),
    ("Bash", {"command": "S=/a/c.yml; sudo cp -a $S $S.bak"}, ([], True)),
    ("Bash", {"command": "sudo python3 - <<'EOF'\nopen('/a/c.yml','w').write('x')\nEOF"}, ([], True)),
    ("Bash", {"command": "source /srv/fix.sh; . ./env.sh"}, ([], True)),
    ("Bash", {"command": "sudo ./scripts/fix-binds.sh /srv/app"}, ([], True)),
    ("Bash", {"command": "echo x > /etc/a 2>&1; ls >&2"}, (["/etc/a"], False)),
    ("Bash", {"command": "echo hi>/srv/x.txt"}, (["/srv/x.txt"], False)),
    ("Bash", {"command": "cd /srv/stacks/media && echo X >> .env"}, (["/srv/stacks/media/.env"], False)),
    ("Bash", {"command": "sudo docker exec app sh -c \"echo a > /in/container\""}, ([], False)),
    ("Bash", {"command": "curl -o /etc/x https://example.org/b"}, (["/etc/x"], False)),
    ("Bash", {"command": "bash -c 'echo x > /etc/b'"}, (["/etc/b"], False)),
])
def test_writes_of_names_what_a_call_writes_and_admits_what_it_cannot_read(tool, args, want):
    paths, unsure = hooks.writes_of(tool, args, "/home/u")
    assert (sorted(paths), unsure) == (sorted(want[0]), want[1])


def test_an_allowed_call_records_its_writes_and_a_blocked_one_does_not():
    seen = []
    runner = hooks.HookRunner(post=lambda kind, body: seen.append(body))
    runner.pre({"tool_name": "Edit", "tool_input": {"file_path": "/srv/app/x.conf"}})
    runner.pre({"tool_name": "Edit", "tool_input": {"file_path": "/etc/planetexpress/config.yaml"}})
    assert seen[0]["writes"] == ["/srv/app/x.conf"] and seen[0]["verdict"] == "allow"
    assert seen[1]["verdict"] == "block" and "writes" not in seen[1]


def supervisor_for(env):
    return SimpleNamespace(checkpoint=env.svc, ms=env.ms, workspace_dir=env.tmp / "state" / "mission-workspaces", store=env.store,
                           _note=lambda mid, body: env.ms.append_event(mid, "note", "supervisor", body))


def _hook_event(env, body):
    env.ms.append_event(env.mission.id, "tool", "worker", {"via": "hook", "hook": "pre", "verdict": "allow", **body})


def test_the_result_names_files_written_outside_the_checkpoint(tmp_path):
    env = make_env(tmp_path)
    missing = tmp_path / "casafast" / "new.conf"
    cid = env.svc.create(env.mission.id, with_files(env, missing))
    mission = env.ms.get_mission(env.mission.id)
    _hook_event(env, {"tool": "Edit", "writes": ["/srv/app/x.conf"]})                     # not covered, not there now
    _hook_event(env, {"tool": "Bash", "writes": [str(env.compose.resolve()), "/tmp/scratch.txt"]})   # covered / scratch
    _hook_event(env, {"tool": "Bash", "writes_unsure": True})
    env.ms.append_event(env.mission.id, "tool", "worker", {"hook": "pre", "verdict": "block", "writes": ["/srv/blocked"]})
    sup = supervisor_for(env)
    Supervisor._record_not_covered(sup, mission)
    [note] = notes(env, "checkpoint.not_covered")
    assert note["checkpoint"] == cid and note["unread_calls"] == 1
    assert note["files"] == [{"path": str(missing), "why": NOT_COVERED_ABSENT},
                             {"path": "/srv/app/x.conf", "why": NOT_COVERED_GONE}]
    view = execution_mod.not_covered_view(env.ms, mission)
    assert view == {"files": note["files"], "unread_calls": 1}
    assert Supervisor._wrote_outside_checkpoint(sup, mission) is True


def test_a_file_the_run_wrote_that_is_still_there_is_named_as_written(tmp_path, monkeypatch):
    # tmp_path is under /tmp, which the supervisor treats as scratch space; here it stands in for a host directory
    monkeypatch.setattr("planet_express.missions.supervisor._SCRATCH_PREFIXES", ("/nonexistent/",))
    env = make_env(tmp_path)
    env.svc.create(env.mission.id, env.scope)
    mission = env.ms.get_mission(env.mission.id)
    target = tmp_path / "kept.conf"
    target.write_text("x")
    (tmp_path / "a-dir").mkdir()
    sup = supervisor_for(env)
    _hook_event(env, {"tool": "Bash", "writes": [str(target), str(tmp_path / "a-dir")]})        # a directory (mkdir) is not a file
    Supervisor._record_not_covered(sup, mission)
    assert notes(env, "checkpoint.not_covered")[-1]["files"] == [{"path": str(target), "why": NOT_COVERED_WRITTEN}]


def test_nothing_to_say_records_nothing_and_shows_nothing(tmp_path):
    env = make_env(tmp_path)
    env.svc.create(env.mission.id, env.scope)
    mission = env.ms.get_mission(env.mission.id)
    sup = supervisor_for(env)
    Supervisor._record_not_covered(sup, mission)
    assert notes(env, "checkpoint.not_covered") == [] and execution_mod.not_covered_view(env.ms, mission) is None
    assert Supervisor._wrote_outside_checkpoint(sup, mission) is False


def test_a_plan_file_the_checkpoint_could_not_take_means_the_run_may_have_changed_something(tmp_path):
    env = make_env(tmp_path)
    env.svc.create(env.mission.id, with_files(env, tmp_path / "casafast" / "new.conf"))
    mission = env.ms.get_mission(env.mission.id)
    sup = supervisor_for(env)
    Supervisor._record_not_covered(sup, mission)
    assert Supervisor._wrote_outside_checkpoint(sup, mission) is True


def test_a_note_the_worker_forged_does_not_count(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    mission = env.ms.get_mission(env.mission.id)
    env.ms.append_event(mission.id, "note", "worker", {"event": "checkpoint.not_covered", "checkpoint": cid,
                                                       "files": [{"path": "/srv/x", "why": NOT_COVERED_WRITTEN}]})
    assert Supervisor._wrote_outside_checkpoint(supervisor_for(env), mission) is False
    assert execution_mod.not_covered_view(env.ms, mission) is None


def test_writes_in_the_session_workspace_and_state_directory_are_not_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("planet_express.missions.supervisor._SCRATCH_PREFIXES", ("/nonexistent/",))
    env = make_env(tmp_path)
    env.svc.create(env.mission.id, env.scope)
    mission = env.ms.get_mission(env.mission.id)
    _hook_event(env, {"tool": "Write", "writes": [str(env.tmp / "state" / "mission-workspaces" / "S-1" / "plan.json"),
                                                   str(env.store.path.parent / "notes.txt")]})
    Supervisor._record_not_covered(supervisor_for(env), mission)
    assert notes(env, "checkpoint.not_covered") == []


def test_a_write_through_a_symlinked_spelling_of_a_covered_file_is_covered(tmp_path, monkeypatch):
    monkeypatch.setattr("planet_express.missions.supervisor._SCRATCH_PREFIXES", ("/nonexistent/",))
    env = make_env(tmp_path)
    env.svc.create(env.mission.id, env.scope)
    mission = env.ms.get_mission(env.mission.id)
    (tmp_path / "link").symlink_to(env.stacks / "media")
    _hook_event(env, {"tool": "Edit", "writes": [str(tmp_path / "link" / ".env")]})
    Supervisor._record_not_covered(supervisor_for(env), mission)
    assert notes(env, "checkpoint.not_covered") == []


def test_a_long_write_list_never_costs_the_call_its_record():
    seen = []
    runner = hooks.HookRunner(post=lambda kind, body: seen.append(body))
    files = " ".join(f"/srv/{'d' * 200}/{n}.yml" for n in range(12))
    runner.pre({"tool_name": "Bash", "tool_input": {"command": f"sudo sed -i s/a/b/ {files}"}})
    line = json.dumps({"kind": "tool", "data": seen[0]}, separators=(",", ":"))
    assert len(line) <= hooks.MAX_LOG_LINE and seen[0]["verdict"] == "allow" and seen[0]["writes_unsure"] is True


@pytest.mark.parametrize("bad", ["/a\n/../etc/shadow", "/a\n/./b", "/a/b\x00c", "/a/b\x1b"])
def test_a_path_with_a_control_character_is_refused(bad):
    from planet_express.missions.models import scope_file_problem
    assert scope_file_problem(bad) is not None


def test_plan_files_are_leased():
    from planet_express.missions.store import grant_resources
    grant = Grant(tier="repair", scope=GrantScope(stacks=["media"], files=["/srv/app/x.conf"]))
    assert "host:local/path:/srv/app/x.conf" in grant_resources(grant)


def test_the_telegram_card_keeps_its_file_list_short():
    from planet_express.application.mission_service import _render_card
    card = {"mission_id": "M-1", "status": "awaiting_decision", "tier": "repair", "title": "t", "warnings": [],
            "scope": {"stacks": ["media"], "services": [], "hosts": ["local"], "files": [f"/srv/{'x' * 500}/{n}" for n in range(32)]},
            "budgets": {"wall_s": 1, "tool_calls": 1, "output_bytes": 1, "retries": 0},
            "verification": {"checks": [], "on_verify_fail": "needs_operator"}, "required": [], "grant_hash": "a" * 64}
    text = _render_card(card)
    files_line = next(line for line in text.splitlines() if line.startswith("files Undo covers"))
    assert len(files_line) < 900 and files_line.endswith("and 26 more")


def test_a_symlinked_plan_file_leases_its_real_path_too(tmp_path):
    from planet_express.missions.store import grant_resources
    real = tmp_path / "real.conf"
    real.write_text("x")
    (tmp_path / "alias.conf").symlink_to(real)
    grant = Grant(tier="repair", scope=GrantScope(stacks=["media"], files=[str(tmp_path / "alias.conf")]))
    assert {f"host:local/path:{tmp_path / 'alias.conf'}", f"host:local/path:{real}"} <= set(grant_resources(grant))


def test_the_longest_file_a_plan_may_list_still_makes_a_valid_lease():
    from planet_express.missions.models import MAX_SCOPE_FILE_CHARS, scope_file_problem
    from planet_express.missions.resources import canon
    longest = "/" + "a" * (MAX_SCOPE_FILE_CHARS - 1)
    assert scope_file_problem(longest) is None and canon(f"host:local/path:{longest}")
    assert scope_file_problem(longest + "b") is not None


# ── compose references outside the stacks root (rc14.6.1, live M-33) ───────────────────────────────

def _compose_with_env_file(env, path: Path, required: bool = True):
    entry = f"      - {path}\n" if required else f"      - path: {path}\n        required: false\n"
    env.compose.write_text("services:\n  web:\n    image: nginx:1.27\n    env_file:\n" + entry)


def test_m33_an_env_file_outside_the_stacks_root_is_captured_and_undo_puts_it_back(tmp_path):
    env = make_env(tmp_path)
    homelink = tmp_path / "apps" / "services" / "homelink" / ".env"
    homelink.parent.mkdir(parents=True)
    homelink.write_text("A=1\n")
    _compose_with_env_file(env, homelink)
    cid = env.svc.create(env.mission.id, env.scope)                      # refused before rc14.6.1: "resolves outside the stacks root"
    entry = next(f for f in env.svc.manifest(cid)["files"] if f["path"] == str(homelink.resolve()))
    assert entry["kind"] == "env_file" and entry["root"] == "/" and env.svc.not_covered(cid) == []
    homelink.write_text("A=2\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done", report.reason
    assert str(homelink.resolve()) in report.written and homelink.read_text() == "A=1\n"


def test_a_missing_required_env_file_outside_the_root_is_not_covered_and_the_run_is_not_refused(tmp_path):
    env = make_env(tmp_path)
    missing = tmp_path / "apps" / "gone" / ".env"
    _compose_with_env_file(env, missing)
    cid = env.svc.create(env.mission.id, env.scope)
    assert env.svc.not_covered(cid) == [{"path": str(missing), "why": "does not exist"}]


def test_an_optional_env_file_outside_the_root_that_is_absent_is_simply_skipped(tmp_path):
    env = make_env(tmp_path)
    _compose_with_env_file(env, tmp_path / "apps" / "gone" / ".env", required=False)
    cid = env.svc.create(env.mission.id, env.scope)
    assert env.svc.not_covered(cid) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write any directory")
def test_an_env_file_outside_the_root_in_a_directory_pe_cannot_write_is_not_covered(tmp_path):
    env = make_env(tmp_path)
    locked = tmp_path / "apps" / "locked" / ".env"
    locked.parent.mkdir(parents=True)
    locked.write_text("A=1\n")
    _compose_with_env_file(env, locked)
    locked.parent.chmod(0o555)
    try:
        cid = env.svc.create(env.mission.id, env.scope)
    finally:
        locked.parent.chmod(0o755)
    [item] = env.svc.not_covered(cid)
    assert item["path"] == str(locked) and "not writable" in item["why"]


def test_an_env_file_that_is_pe_s_own_is_not_covered(tmp_path):
    env = make_env(tmp_path)
    own = tmp_path / "pe" / "planetexpress.env"
    own.parent.mkdir()
    own.write_text("X=1\n")
    env.svc.pe_paths = (str(own.parent),)
    _compose_with_env_file(env, own)
    cid = env.svc.create(env.mission.id, env.scope)
    assert env.svc.not_covered(cid) == [{"path": str(own), "why": NOT_COVERED_PE}]


def test_an_outside_env_file_that_changes_before_capture_is_not_covered_not_refused(tmp_path):
    holder = {}

    class Swap(_Hooks):
        def before_capture(self, planned):
            holder["p"].unlink()
            holder["p"].write_text("B=2\n")                  # a different file (new inode) by the time it is captured

    env = make_env(tmp_path, hooks=Swap())
    holder["p"] = tmp_path / "apps" / "homelink" / ".env"
    holder["p"].parent.mkdir(parents=True)
    holder["p"].write_text("A=1\n")
    _compose_with_env_file(env, holder["p"])
    cid = env.svc.create(env.mission.id, env.scope)
    [item] = env.svc.not_covered(cid)
    assert item["path"] == str(holder["p"]) and item["why"].startswith("cannot be snapshotted")


def test_a_reference_written_inside_the_root_that_a_symlink_leads_out_of_is_still_refused(tmp_path):
    from planet_express.missions.checkpoint.service import ScopeRefused
    env = make_env(tmp_path)
    target = tmp_path / "elsewhere" / "s.env"
    target.parent.mkdir()
    target.write_text("S=1\n")
    (env.stacks / "media" / "secrets.env").symlink_to(target)
    env.compose.write_text("services:\n  web:\n    image: nginx:1.27\n    env_file: secrets.env\n")
    with pytest.raises(ScopeRefused, match="outside the stacks root"):
        env.svc.create(env.mission.id, env.scope)


def test_a_relative_reference_that_leaves_the_root_is_captured_where_it_is(tmp_path):
    env = make_env(tmp_path)
    shared = tmp_path / "shared" / ".env"
    shared.parent.mkdir()
    shared.write_text("A=1\n")
    env.compose.write_text("services:\n  web:\n    image: nginx:1.27\n    env_file: ../../shared/.env\n")
    cid = env.svc.create(env.mission.id, env.scope)
    assert str(shared.resolve()) in env.svc.covered_paths(cid) and env.svc.not_covered(cid) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory")
def test_an_optional_reference_in_a_directory_pe_cannot_search_is_not_covered_not_skipped(tmp_path):
    env = make_env(tmp_path)
    private = tmp_path / "private"
    (private / "app").mkdir(parents=True)
    (private / "app" / ".env").write_text("A=1\n")
    _compose_with_env_file(env, private / "app" / ".env", required=False)
    private.chmod(0o000)
    try:
        cid = env.svc.create(env.mission.id, env.scope)
    finally:
        private.chmod(0o755)
    [item] = env.svc.not_covered(cid)
    assert item["path"] == str(private / "app" / ".env") and item["why"].startswith("cannot be snapshotted")


def test_a_file_named_by_the_compose_file_and_the_plan_is_reported_once(tmp_path):
    env = make_env(tmp_path)
    missing = tmp_path / "apps" / "gone" / ".env"
    _compose_with_env_file(env, missing)
    cid = env.svc.create(env.mission.id, with_files(env, missing))
    assert [i["path"] for i in env.svc.not_covered(cid)] == [str(missing)]


def test_an_outside_env_file_two_stacks_share_is_restored_for_both(tmp_path):
    env = make_env(tmp_path)
    shared = tmp_path / "opt" / "common.env"
    shared.parent.mkdir()
    shared.write_text("A=1\n")
    (env.stacks / "arr").mkdir()
    (env.stacks / "arr" / "docker-compose.yml").write_text(f"services:\n  app:\n    image: x:1\n    env_file: {shared}\n")
    _compose_with_env_file(env, shared)
    cid = env.svc.create(env.mission.id, dataclasses.replace(env.scope, stacks=("arr", "media")))
    entry = next(f for f in env.svc.manifest(cid)["files"] if f["path"] == str(shared.resolve()))
    assert {entry["stack"], *entry.get("also_stacks", [])} == {"arr", "media"}
    shared.write_text("A=2\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert {"arr", "media"} <= report.written_stacks and shared.read_text() == "A=1\n"


def test_an_in_root_file_also_named_through_an_outside_symlink_can_still_be_undone(tmp_path):
    env = make_env(tmp_path)
    link = tmp_path / "opt" / "media.env"
    link.parent.mkdir()
    link.symlink_to(env.dotenv)
    cid = env.svc.create(env.mission.id, with_files(env, link))          # the plan names the stack's .env through the outside link
    entry = next(f for f in env.svc.manifest(cid)["files"] if f["path"] == str(env.dotenv.resolve()))
    assert str(link) in entry["aliases"]
    env.dotenv.write_text("CHANGED=1\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done", report.reason
