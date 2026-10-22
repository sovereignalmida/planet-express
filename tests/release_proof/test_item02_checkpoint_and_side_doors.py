"""Release proof item 2:

    Checkpoint restores in-scope files and state exactly; refusal when backups are stale. The worker cannot reach Docker or
    root by any path (group, TCP socket, unmanaged sudo rule); the 6.8 preflight fails closed on each.

Fakes only (a tmp stacks tree, a scripted Docker CLI, a temp passwd/group/sudoers tree). The privileged half runs through
scripts/run_privileged_tests.sh and is recorded in docs/handoff/v4/release-proof.md."""
import json

import pytest
from checkpoint_fakes import COMPOSE, make_env, tree
from test_launcher_side_doors import CORE, Host

from planet_express.missions.checkpoint import BackupFloorError, CheckpointScope
from planet_express.missions.launcher.side_doors import (
    repair_gate_report,
    side_door_steps,
)
from release_proof.support import ROOT  # noqa: F401  (sys.path)

# ── checkpoint: exact restore, stale backups ────────────────────────────────────────────────────────

DATA = "/mnt/data/appdata"


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


@pytest.mark.parametrize("label,kwargs,state", [
    ("no backup source configured", {}, "unverifiable"),
    ("the source does not cover the data path", {"sources": [("/elsewhere", "/m")], "mtime": lambda p: 1.0}, "absent"),
    ("the covering backup is older than the freshness window",
     {"sources": [(DATA, "/m")], "mtime": lambda p: 1_000_000 - 90_000}, "stale"),
])
def test_a_checkpoint_of_data_is_refused_when_backups_are_stale_absent_or_unverifiable(tmp_path, label, kwargs, state):
    env = make_env(tmp_path, **kwargs)
    with pytest.raises(BackupFloorError) as exc:
        env.svc.create(env.mission.id, CheckpointScope(stacks=("media",), data_paths=(DATA,)))
    assert exc.value.state == state, label
    assert not env.root.exists() or list(env.root.iterdir()) == []         # nothing half-made is left behind


def test_a_fresh_backup_lets_the_checkpoint_through(tmp_path):
    env = make_env(tmp_path, sources=[("/mnt/data", "/m")], mtime=lambda p: 1_000_000 - 60)
    cid = env.svc.create(env.mission.id, CheckpointScope(stacks=("media",), data_paths=(DATA,)))
    assert json.loads(json.dumps(env.svc.store.read_manifest(cid)))["no_backup"] is False


# ── side doors: each fails closed ──────────────────────────────────────────────────────────────────

@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def gate(host):
    return repair_gate_report(side_door_steps(host.paths()))


def failed(report):
    return {c.name for c in report.failures}


def test_a_clean_host_passes_every_side_door(host):
    report = gate(host)
    assert report.ok and failed(report) == set()


def test_a_session_account_in_the_docker_group_fails_the_gate(host):
    host.passwd.write_text(host.passwd.read_text() + "pe-s1:x:61001:61001::/nonexistent:/bin/false\n")
    host.group.write_text(host.group.read_text().replace("docker:x:998:chris,planetexpress", "docker:x:998:chris,pe-s1"))
    assert "worker-identity" in failed(gate(host))


def test_core_inside_the_session_uid_range_fails_the_gate(host):
    assert "worker-identity" in failed(repair_gate_report(side_door_steps(host.paths(uid_min=1000, uid_max=1100))))


def test_an_unauthenticated_docker_tcp_socket_fails_the_gate(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["unix:///var/run/docker.sock", "tcp://0.0.0.0:2375"]}\n')
    assert "docker-tcp-exposure" in failed(gate(host))


def test_a_live_listener_on_the_docker_port_fails_the_gate_even_when_the_config_says_nothing(host):
    host.ss = "tcp LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:*\n"
    assert "docker-tcp-exposure" in failed(gate(host))


def test_an_unmanaged_wildcard_sudo_rule_for_core_fails_the_gate(host):
    (host.etc / "sudoers.d" / "legacy").write_text(f"{CORE} ALL=(ALL) NOPASSWD: /usr/bin/docker *\n")
    assert "sudoers-managed" in failed(gate(host))


def test_a_managed_sudoers_file_edited_by_hand_fails_the_gate(host):
    path = host.etc / "sudoers.d" / "planetexpress"
    path.write_text(path.read_text() + f"{CORE} ALL=(root) NOPASSWD: ALL\n")
    assert "sudoers-managed" in failed(gate(host))


@pytest.mark.parametrize("unreadable", ["passwd", "group", "daemon", "sudoers"])
def test_each_side_door_fails_closed_when_its_input_cannot_be_read_or_parsed(host, unreadable):
    target = {"passwd": host.passwd, "group": host.group, "daemon": host.etc / "docker" / "daemon.json",
              "sudoers": host.etc / "sudoers"}[unreadable]
    if unreadable == "daemon":                       # an absent daemon.json is the default; a garbled one is unknown
        target.write_text("{not json")
    else:
        target.unlink()
    assert not gate(host).ok


def test_a_step_that_crashes_is_a_failed_check_not_a_pass(host):
    steps = side_door_steps(host.paths())

    def boom():
        raise RuntimeError("unexpected")
    steps[1].probe = boom
    assert not repair_gate_report(steps).ok
