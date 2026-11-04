"""Release proof item 2:

    Checkpoint restores in-scope files and state exactly. (Trust mode, rc14, removed the backup floor and the side-door gate this
    item also proved: a Repair session has full access to the host by design.)

Fakes only (a tmp stacks tree, a scripted Docker CLI). The privileged half runs through scripts/run_privileged_tests.sh and is
recorded in docs/handoff/v4/release-proof.md."""
import json

from checkpoint_fakes import COMPOSE, make_env, tree

from planet_express.missions.checkpoint import CheckpointScope
from release_proof.support import ROOT  # noqa: F401  (sys.path)

# ── checkpoint: exact restore ───────────────────────────────────────────────────────────────────────


def test_a_restore_returns_every_in_scope_file_byte_for_byte_with_its_mode_and_the_service_facts(tmp_path):
    env = make_env(tmp_path)
    extra = env.stacks / "media" / "docker-compose.override.yml"
    extra.write_text("services: {web: {}}\n")
    extra.chmod(0o640)
    before = tree(env.stacks)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("services: {web: {image: evil}}\n")
    env.dotenv.write_text("X=1\n")
    env.dotenv.chmod(0o644)
    extra.unlink()
    env.docker.containers[("media", "web")]["Image"] = "sha256:evil"           # container state drifts too
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok
    assert tree(env.stacks) == before and env.compose.read_text() == COMPOSE
    assert env.docker.recreated == ["media/web"]
    assert env.docker.containers[("media", "web")] == env.docker.baseline[("media", "web")]


def test_a_restore_refuses_and_writes_nothing_when_the_host_moved_on_after_the_session(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("session edit\n")
    env.svc.capture_post(cid)
    env.compose.write_text("someone else edited this later\n")
    snapshot = tree(env.stacks)
    report = env.svc.restore(cid)
    assert report.status == "refused" and tree(env.stacks) == snapshot
    assert env.ms.get_mission(env.mission.id).status == "needs_operator"


def test_a_restore_is_refused_until_the_session_end_state_is_captured(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("session edit\n")
    before = tree(env.stacks)
    report = env.svc.restore(cid)
    assert report.status == "refused" and report.reason == "needs_post_capture" and tree(env.stacks) == before


def test_a_repair_checkpoint_needs_no_backup(tmp_path):
    """Trust mode removed the backup floor: a checkpoint is taken whatever the backups say, and records no backup state."""
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, CheckpointScope.from_grant_scope(env.mission.grant().scope))
    assert "no_backup" not in json.loads(json.dumps(env.svc.store.read_manifest(cid)))
