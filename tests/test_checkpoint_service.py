"""Checkpoint service (v4 slice 5): round trip, durability, crash safety, CAS, refusals, backup floor,
secrets, provider conformance, retention. Real files in a tmp_path; Docker is a scripted fake."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkpoint_fakes import COMPOSE, SECRET, make_env, tree
from missions_helpers import advance_to

from planet_express.missions.checkpoint import (
    BackupFloorError,
    CheckpointDisabled,
    CheckpointExists,
    CheckpointInUse,
    CheckpointNotFound,
    CheckpointScope,
    DiskGuardError,
    MosCheckpointProvider,
    ScopeChanged,
    ScopeRefused,
    SystemdCheckpointProvider,
)
from planet_express.missions.checkpoint.provider import ContainerFacts, ProviderError
from planet_express.missions.checkpoint.service import CheckpointSettings, _Hooks
from planet_express.missions.checkpoint.store import CheckpointStore, StoreError

ROOT = os.geteuid() == 0


def events(env, name=None):
    out = [e for e in env.ms.events(env.mission.id) if e.kind == "note" and "event" in json.loads(e.body_json)]
    return [e for e in out if name is None or json.loads(e.body_json)["event"] == name]


def status(env):
    return env.ms.get_mission(env.mission.id).status


# ── round trip ──────────────────────────────────────────────────────────────
def test_round_trip_bytes_mode_owner_and_env(tmp_path):
    env = make_env(tmp_path)
    extra = env.stacks / "media" / "docker-compose.override.yml"
    extra.write_text("services: {web: {}}\n")
    extra.chmod(0o755)
    before = tree(env.stacks)
    cid = env.svc.create(env.mission.id, env.scope)
    assert cid.no_backup is False
    env.compose.write_text("services: {web: {image: evil}}\n")
    env.dotenv.write_text("X=1\n")
    env.dotenv.chmod(0o644)
    extra.chmod(0o600)
    extra.unlink()
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok
    assert tree(env.stacks) == before         # bytes, mode, uid, gid (owner is our own uid unless root)
    assert {p.name for p in env.stacks.rglob("*") if p.is_file()} == {
        "docker-compose.yml", ".env", "docker-compose.override.yml"}


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="needs setfacl")
def test_acl_survives_round_trip(tmp_path):
    env = make_env(tmp_path)
    done = subprocess.run(["setfacl", "-m", "u:12345:r", str(env.compose)], capture_output=True, check=False)
    if done.returncode != 0:
        pytest.skip("filesystem has no ACL support")
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("changed\n")
    # a rewrite in place keeps the ACL; replace the inode so the restore must bring it back
    env.compose.unlink()
    env.compose.write_text("changed\n")
    env.svc.capture_post(cid)
    assert env.svc.restore(cid).status == "done"
    out = subprocess.run(["getfacl", "-c", str(env.compose)], capture_output=True, text=True, check=False).stdout
    assert "user:12345:r--" in out
    assert env.compose.read_text() == COMPOSE


# ── durability and crash safety ─────────────────────────────────────────────
def test_commit_order_payload_dirs_manifest_then_marker(tmp_path):
    ops: list[tuple[str, str]] = []
    env = make_env(tmp_path, trace=lambda op, path: ops.append((op, path)))
    cid = env.svc.create(env.mission.id, env.scope)
    base = str(env.root / cid)
    marker = ops.index(("write", f"{base}/COMMITTED"))
    before = ops[:marker]
    payloads = {p for op, p in ops if op == "write" and "/files/" in p}
    assert payloads and payloads <= {p for op, p in before if op == "fsync_file"}
    assert {f"{base}/files", base} <= {p for op, p in before if op == "fsync_dir"}
    assert ("write", f"{base}/manifest.json") in before and ("fsync_file", f"{base}/manifest.json") in before
    after = ops[marker:]
    assert after[1] == ("fsync_file", f"{base}/COMMITTED")
    assert [op for op, p in after[2:]] == ["fsync_dir", "fsync_dir"]
    assert [p for op, p in after[2:]] == [base, str(env.root)]
    # every payload fsync precedes the first directory fsync
    first_dir = next(i for i, (op, _) in enumerate(ops) if op == "fsync_dir")
    assert all(i < first_dir for i, (op, p) in enumerate(ops) if op == "fsync_file" and "/files/" in p)


def test_crash_before_marker_is_quarantined_and_never_restorable(tmp_path):
    env = make_env(tmp_path)
    store = CheckpointStore(env.root)
    cid = "C-" + "ab" * 8
    store.new_checkpoint(cid)
    store.write_payload(cid, 0, b"x")
    (env.root / cid / "manifest.json").write_text("{}")      # killed after the manifest, before the marker
    with pytest.raises(CheckpointNotFound):
        env.svc.restore(cid)
    scan = env.svc.startup_scan()
    assert scan["quarantined"] == [cid]
    assert not (env.root / cid).exists() and (env.root / ".quarantine" / cid).is_dir()
    with pytest.raises(CheckpointNotFound):
        env.svc.restore(cid)


def test_failed_capture_leaves_no_directory(tmp_path):
    def boom(op, path):
        if path.endswith("COMMITTED"):
            raise RuntimeError("disk gone")
    env = make_env(tmp_path, trace=boom)
    with pytest.raises(RuntimeError):
        env.svc.create(env.mission.id, env.scope)
    assert list(env.root.iterdir()) == []
    assert env.ms.get_mission(env.mission.id).checkpoint_id is None


class Crash(BaseException):
    pass


def test_mid_restore_crash_then_rerun_ends_identical(tmp_path):
    class Die(_Hooks):
        def after_file_restored(self, count):
            if count == 1:
                raise Crash()
    env = make_env(tmp_path, hooks=Die())
    before = tree(env.stacks)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("one\n")
    env.dotenv.write_text("two\n")
    env.svc.capture_post(cid)
    with pytest.raises(Crash):
        env.svc.restore(cid)
    assert tree(env.stacks) != before          # half done
    env.svc.hooks = _Hooks()
    report = env.svc.restore(cid)
    assert report.status == "done" and not report.drifted
    assert tree(env.stacks) == before
    again = env.svc.restore(cid)               # idempotent
    assert again.status == "done" and again.written == [] and tree(env.stacks) == before


# ── CAS ─────────────────────────────────────────────────────────────────────
def test_cas_refuses_drift_writes_nothing_and_needs_operator(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("session edit\n")
    env.dotenv.write_text("also changed by the session\n")
    env.svc.capture_post(cid)
    env.compose.write_text("someone else edit\n")
    snapshot = tree(env.stacks)
    report = env.svc.restore(cid)
    assert report.status == "refused"
    assert [d["path"] for d in report.drifted] == [os.path.realpath(env.compose)]
    assert tree(env.stacks) == snapshot
    assert status(env) == "needs_operator"
    assert events(env, "checkpoint.restore_refused")


def test_ack_drift_restores_and_keeps_drifted_bytes(tmp_path):
    env = make_env(tmp_path)
    original = tree(env.stacks)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("session edit\n")
    env.svc.capture_post(cid)
    env.compose.write_text("someone else edit\n")
    with pytest.raises(ValueError):
        env.svc.restore(cid, ack_drift=True)                 # no operator actor
    advance_to(env.ms, env.mission.id, "needs_operator")
    report = env.svc.restore(cid, ack_drift=True, actor="operator:chris")
    assert report.status == "done" and len(report.drift_saved) == 1
    assert Path(report.drift_saved[0]).read_bytes() == b"someone else edit\n"
    assert stat.S_IMODE(os.stat(report.drift_saved[0]).st_mode) == 0o600
    assert tree(env.stacks) == original


def test_restore_without_post_manifest_refuses_needs_post_capture(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("session edit\n")
    before = tree(env.stacks)
    report = env.svc.restore(cid)
    assert report.status == "refused" and report.reason == "needs_post_capture"
    assert tree(env.stacks) == before and status(env) == "draft"          # nothing written, no operator hand-off
    assert json.loads(events(env, "checkpoint.restore_refused")[-1].body_json)["reason"] == "needs_post_capture"
    env.svc.capture_post(cid)                       # the caller does this once the session is provably dead
    assert env.svc.restore(cid).status == "done"
    assert env.compose.read_text() == COMPOSE


# ── refusals ────────────────────────────────────────────────────────────────
def leftovers(env):
    return [p for p in env.root.iterdir()] if env.root.exists() else []


def test_symlink_leaving_root_refused(tmp_path):
    env = make_env(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    env.dotenv.unlink()
    env.dotenv.symlink_to(outside)
    with pytest.raises(ScopeRefused, match="outside the stacks root"):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []


def test_symlink_inside_root_is_followed_and_recorded(tmp_path):
    env = make_env(tmp_path)
    real = env.stacks / "media" / "real.env"
    real.write_text("A=1\n")
    env.dotenv.unlink()
    env.dotenv.symlink_to(real)
    cid = env.svc.create(env.mission.id, env.scope)
    manifest = env.svc.store.read_manifest(cid)
    entry = next(f for f in manifest["files"] if f["given_path"].endswith(".env"))
    assert entry["symlink"] is True and entry["path"] == os.path.realpath(real)


def test_toctou_symlink_swapped_after_validation(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    class Swap(_Hooks):
        def before_capture(self, planned):
            target = Path(next(p.real for p in planned if p.real.endswith(".env")))
            target.unlink()
            target.symlink_to(outside)
    env = make_env(tmp_path, hooks=Swap())
    with pytest.raises(ScopeRefused):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == [] and b"secret" not in b"".join(
        p.read_bytes() for p in env.root.rglob("*") if p.is_file())


def test_toctou_file_replaced_by_another_file(tmp_path):
    class Swap(_Hooks):
        def before_capture(self, planned):
            target = Path(next(p.real for p in planned if p.real.endswith(".env")))
            target.unlink()
            target.write_text("a different file\n")
    env = make_env(tmp_path, hooks=Swap())
    with pytest.raises(ScopeRefused, match="not the file that was validated"):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []


@pytest.mark.skipif(ROOT, reason="root can read anything")
def test_unreadable_file_refused(tmp_path):
    env = make_env(tmp_path)
    env.dotenv.chmod(0)
    try:
        with pytest.raises(ScopeRefused, match="permission denied"):
            env.svc.create(env.mission.id, env.scope)
    finally:
        env.dotenv.chmod(0o600)
    assert leftovers(env) == []


def test_fifo_refused_and_never_opened(tmp_path):
    env = make_env(tmp_path)
    fifo = env.stacks / "media" / "pipe"
    os.mkfifo(fifo)
    scope = CheckpointScope(stacks=("media",), files=(str(fifo),))
    with pytest.raises(ScopeRefused, match="FIFO"):
        env.svc.create(env.mission.id, scope)
    assert leftovers(env) == []


def test_oversize_file_refused(tmp_path):
    env = make_env(tmp_path, settings=CheckpointSettings(max_file_bytes=16))
    with pytest.raises(ScopeRefused, match="exceeds"):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []


def test_path_outside_root_and_dotdot_refused(tmp_path):
    env = make_env(tmp_path)
    other = tmp_path / "other.txt"
    other.write_text("x")
    for bad in (str(other), f"{env.stacks}/media/../media/.env", "relative/path", f"{env.stacks}"):
        with pytest.raises(ScopeRefused):
            env.svc.create(env.mission.id, CheckpointScope(stacks=("media",), files=(bad,)))
        assert leftovers(env) == []


def test_unknown_stack_and_missing_file_refused(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(ScopeRefused, match="no compose file"):
        env.svc.create(env.mission.id, CheckpointScope(stacks=("nope",)))
    with pytest.raises(ScopeRefused):
        env.svc.create(env.mission.id, CheckpointScope(stacks=("media",), files=(str(env.stacks / "media" / "gone"),)))


def test_one_checkpoint_per_mission(tmp_path):
    env = make_env(tmp_path)
    env.svc.create(env.mission.id, env.scope)
    with pytest.raises(CheckpointExists):
        env.svc.create(env.mission.id, env.scope)


# ── backup floor ────────────────────────────────────────────────────────────
DATA = "/mnt/data/appdata"


def data_scope():
    return CheckpointScope(stacks=("media",), data_paths=(DATA,))


def grant_with(**backup):
    from missions_helpers import repair_grant
    return repair_grant(backup=backup)


def test_backup_floor_refuses_unverifiable_absent_and_stale(tmp_path):
    env = make_env(tmp_path)                                         # no sources: unverifiable
    with pytest.raises(BackupFloorError) as exc:
        env.svc.create(env.mission.id, data_scope())
    assert exc.value.state == "unverifiable"
    env = make_env(tmp_path / "a", sources=[("/elsewhere", "/m")], mtime=lambda p: 1.0)      # configured, not covered
    with pytest.raises(BackupFloorError) as exc:
        env.svc.create(env.mission.id, data_scope())
    assert exc.value.state == "absent"
    env = make_env(tmp_path / "b", sources=[(DATA, "/m")], mtime=lambda p: 1_000_000 - 90_000)   # older than 86400 s
    with pytest.raises(BackupFloorError) as exc:
        env.svc.create(env.mission.id, data_scope())
    assert exc.value.state == "stale"
    assert leftovers(env) == []


def test_backup_floor_passes_with_fresh_backup_and_flag_stays_false(tmp_path):
    env = make_env(tmp_path, sources=[("/mnt/data", "/m")], mtime=lambda p: 1_000_000 - 60)
    cid = env.svc.create(env.mission.id, data_scope())
    assert cid.no_backup is False and env.svc.store.read_manifest(cid)["no_backup"] is False


def test_acknowledged_no_backup_lets_it_through_and_records_flag(tmp_path):
    env = make_env(tmp_path, grant=grant_with(acknowledged_no_backup=True))
    cid = env.svc.create(env.mission.id, data_scope())
    assert cid.no_backup is True and env.svc.store.read_manifest(cid)["no_backup"] is True
    assert env.ms.get_mission(env.mission.id).tier == "repair"        # this module never changes the tier


def test_no_backup_flag_defaults_false():
    from planet_express.missions.models import Backup
    assert Backup().acknowledged_no_backup is False


# ── secrets ─────────────────────────────────────────────────────────────────
def test_modes_and_no_content_in_events_or_errors(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.dotenv.write_text("DB_PASSWORD=changed-by-session\n")
    env.svc.capture_post(cid)
    env.dotenv.write_text("DB_PASSWORD=someone-else\n")
    env.svc.restore(cid)                                               # refused: drift
    env.svc.restore(cid, ack_drift=True, actor="operator:me")
    assert stat.S_IMODE(env.root.stat().st_mode) == 0o700
    for path in (env.root / cid).rglob("*"):
        want = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == want, path
    assert stat.S_IMODE((env.root / cid).stat().st_mode) == 0o700
    blob = json.dumps([e.body_json for e in env.ms.events(env.mission.id)])
    for secret in ("hunter2", "abcdef0123456789", "changed-by-session", "someone-else", "image: nginx"):
        assert secret not in blob
    try:
        env.svc.create(env.mission.id, env.scope)
    except CheckpointExists as exc:
        assert "hunter2" not in str(exc)


# ── provider seam: restore recreates and detects what it cannot fix ─────────
def test_restore_recreates_and_compares_service_facts(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("services: {web: {image: other}}\n")
    env.docker.containers[("media", "web")]["Config"]["Labels"]["a"] = "changed"
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok and report.unrestored == []
    assert env.docker.recreated == ["media/web"]


def test_restore_normalises_generated_labels_in_an_existing_v1_manifest(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.svc.capture_post(cid)
    path = env.root / cid / "manifest.json"
    manifest = json.loads(path.read_text())
    labels = manifest["facts"]["services"]["media/web"]["labels"]
    labels["com.docker.compose.replace"] = "legacy-generated-value"
    labels["io.planetexpress.created-by-proxy"] = "1"
    path.write_text(json.dumps(manifest))

    report = env.svc.restore(cid)

    assert report.status == "done" and report.ok and report.unrestored == []
    assert env.docker.recreated == []


def test_deliberately_drifted_label_and_env_are_unrestored(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("services: {web: {image: other}}\n")
    env.docker.fix_on_recreate = False
    env.docker.containers[("media", "web")]["Config"]["Labels"]["a"] = "changed"
    env.docker.containers[("media", "web")]["Config"]["Env"] = ["A=2"]
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and not report.ok
    assert report.unrestored == [{"service": "media/web", "fields": ["env_hash", "labels"]}]
    done = json.loads(events(env, "checkpoint.restore_done")[-1].body_json)
    assert done["unrestored"] == [{"service": "media/web", "fields": ["env_hash", "labels"]}]
    assert "A=2" not in json.dumps(done)


def test_recreate_failure_is_reported(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("x\n")
    env.docker.recreate_ok = False
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert any(u["fields"] == ["recreate"] for u in report.unrestored)


def test_observational_facts_are_reported_not_asserted(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.docker.ports = ["tcp 0.0.0.0:8080"]
    env.compose.write_text("x\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and report.ok
    assert report.observational["ports"] == {"added": ["tcp 0.0.0.0:8080"], "removed": ["tcp 0.0.0.0:80"]}


# ── retention, purge, disk guard, disabled ──────────────────────────────────
def test_purge_keeps_reference_and_refuses_for_live_mission(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    with pytest.raises(CheckpointInUse):
        env.svc.purge(cid)
    assert (env.root / cid).is_dir()
    advance_to(env.ms, env.mission.id, "cancelled")
    assert env.svc.purge(cid) is True
    assert not (env.root / cid).exists()
    assert env.ms.get_mission(env.mission.id).checkpoint_id == cid
    assert env.svc.purge(cid) is False
    with pytest.raises(CheckpointNotFound):
        env.svc.restore(cid)


def test_purge_expired_honours_retention(tmp_path):
    env = make_env(tmp_path, settings=CheckpointSettings(retention_days=90))
    cid = env.svc.create(env.mission.id, env.scope)
    advance_to(env.ms, env.mission.id, "cancelled")
    assert env.svc.purge_expired() == []
    env.clock.t += 90 * 86400 + 1
    assert env.svc.purge_expired() == [cid]


def test_disk_guard_per_checkpoint_and_store(tmp_path):
    env = make_env(tmp_path, settings=CheckpointSettings(max_total_bytes=10))
    with pytest.raises(DiskGuardError):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []
    env = make_env(tmp_path / "b", settings=CheckpointSettings(max_store_bytes=10))
    env.root.mkdir(parents=True)
    (env.root / "filler").write_bytes(b"x" * 11)
    with pytest.raises(DiskGuardError):
        env.svc.create(env.mission.id, env.scope)


def test_missions_disabled_runs_no_checkpoint_code(tmp_path):
    env = make_env(tmp_path, enabled=False)
    for call in (lambda: env.svc.create(env.mission.id, env.scope), lambda: env.svc.capture_post("C-" + "0" * 16),
                 lambda: env.svc.restore("C-" + "0" * 16), lambda: env.svc.purge("C-" + "0" * 16),
                 env.svc.startup_scan, env.svc.purge_expired):
        with pytest.raises(CheckpointDisabled):
            call()
    assert not env.root.exists() and env.docker.calls == []


def test_checkpoint_id_must_be_well_formed(tmp_path):
    env = make_env(tmp_path)
    for bad in ("../../etc", "C-xyz", "", "C-" + "0" * 15):
        with pytest.raises(StoreError):
            env.svc.purge(bad)


# ── conformance: both providers, same suite ─────────────────────────────────
@pytest.mark.parametrize("cls", [SystemdCheckpointProvider, MosCheckpointProvider])
def test_provider_conformance(tmp_path, cls):
    env = make_env(tmp_path, provider_cls=cls)
    p = env.provider
    assert p.version == 1
    names = [Path(c.path).name for c in p.discover_compose("media")]
    assert names == ["docker-compose.yml", ".env"]
    assert all(c.real_path == os.path.realpath(c.path) for c in p.discover_compose("media"))
    facts = p.inspect("media/web")
    assert facts.image_id == "sha256:aaa" and facts.command == ["nginx"] and facts.networks == ["media_default"]
    assert facts.labels == {"a": "b"} and facts.restart_policy == {"name": "unless-stopped", "max_retries": 0}
    assert "A=1" not in json.dumps(facts.to_dict())                   # env is a hash only
    assert p.inspect("media/missing") is None
    assert p.listening_ports() == ["tcp 0.0.0.0:80"]
    assert p.mount_table() == []
    assert p.unit_hashes(["x.service"]) == {"x.service": "absent"}
    assert p.recreate(["media/web"]).ok and env.docker.recreated == ["media/web"]
    prefix = ["docker", "compose"] if cls is SystemdCheckpointProvider else ["docker-compose"]
    assert env.docker.calls[-1][:len(prefix)] == prefix
    env.docker.recreate_ok = False
    assert not p.recreate(["media/web"]).ok
    assert not p.recreate(["bad name"]).ok
    with pytest.raises(ProviderError):
        p.discover_compose("../etc")
    assert all(isinstance(a, str) for call in env.docker.calls for a in call)     # argv lists only


def test_checkpoint_facts_exclude_only_proxy_and_per_recreate_labels():
    raw = {"Image": "sha256:x", "Config": {"Env": [], "Labels": {
        "io.planetexpress.created-by-proxy": "1", "com.docker.compose.replace": "abc", "com.docker.compose.container-number": "1",
        "com.docker.compose.config-hash": "changing", "com.docker.compose.project": "kept", "com.example.keep": "yes"}}, "HostConfig": {}, "Mounts": [],
           "NetworkSettings": {"Networks": {}}}
    assert ContainerFacts.from_inspect(raw).labels == {"com.docker.compose.project": "kept", "com.example.keep": "yes"}


def test_providers_read_unit_files_from_their_own_dirs(tmp_path):
    from types import SimpleNamespace
    texts = {"/etc/systemd/system/a.service": "unit", "/etc/init.d/a": "script"}

    def read(path):
        if path in texts:
            return texts[path]
        raise OSError(path)
    sysd = SystemdCheckpointProvider(SimpleNamespace(stacks_root=tmp_path), read_text=read)
    mos = MosCheckpointProvider(SimpleNamespace(stacks_root=tmp_path), read_text=read)
    assert sysd.unit_hashes(["a.service"])["a.service"] != "absent"
    assert mos.unit_hashes(["a.service"])["a.service"] == "absent" and mos.unit_hashes(["a"])["a"] != "absent"


# ── review round: provider project name and compose chain ───────────────────
def test_recreate_passes_every_compose_file_and_the_project_name(tmp_path):
    env = make_env(tmp_path)
    (env.stacks / "media" / "docker-compose.override.yml").write_text("services: {web: {}}\n")
    assert env.provider.recreate(["media/web"]).ok
    argv = env.docker.calls[-1]
    base, override = str(env.compose.resolve()), str((env.stacks / "media" / "docker-compose.override.yml").resolve())
    assert argv[:6] == ["docker", "compose", "-p", "media", "-f", base] and argv[6:8] == ["-f", override]


def test_project_name_comes_from_compose_config_not_a_guess(tmp_path):
    env = make_env(tmp_path)
    (env.stacks / "Media.App").mkdir()
    (env.stacks / "Media.App" / "docker-compose.yml").write_text("services: {web: {}}\n")
    # COMPOSE_PROJECT_NAME in .env, `name: ${VAR}` and an uppercase directory: only compose knows the answer
    env.docker.projects["Media.App"] = "from_dotenv"
    env.docker.containers[("from_dotenv", "web")] = env.docker.containers[("media", "web")]
    assert env.provider.project_name("Media.App") == "from_dotenv"
    assert env.provider.inspect("Media.App/web") is not None
    ps = next(c for c in env.docker.calls if c[:3] == ["docker", "ps", "-aq"])
    assert "label=com.docker.compose.project=from_dotenv" in ps
    assert env.provider.recreate(["Media.App/web"]).ok
    up = env.docker.calls[-1]
    assert up[up.index("-p") + 1] == "from_dotenv"
    config = next(c for c in env.docker.calls if c[-3:] == ["config", "--format", "json"])
    assert config[:2] == ["docker", "compose"] and "-f" in config


def test_unresolvable_project_omits_p_and_is_unverifiable(tmp_path):
    env = make_env(tmp_path)
    env.docker.config_ok = False
    assert env.provider.project_name("media") is None
    assert env.provider.inspect("media/web") is None          # no guess, so no container is matched
    assert not any(c[:3] == ["docker", "ps", "-aq"] for c in env.docker.calls)
    assert env.provider.recreate(["media/web"]).ok
    assert "-p" not in env.docker.calls[-1]
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("x\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert any(u["service"] == "media/web" and u["fields"] == ["unverifiable"] for u in report.unrestored)


def test_legacy_compose_file_precedence(tmp_path):
    env = make_env(tmp_path)
    media = env.stacks / "media"
    (media / "docker-compose.yaml").write_text("services: {}\n")
    (media / "docker-compose.override.yml").write_text("services: {}\n")
    (media / "docker-compose.override.yaml").write_text("services: {}\n")
    chain = [Path(c.path).name for c in env.provider.compose_chain("media")]
    assert chain == ["docker-compose.yaml", "docker-compose.override.yaml"]
    (media / "compose.yml").write_text("services: {}\n")
    assert Path(env.provider.compose_chain("media")[0].path).name == "compose.yml"
    (media / "compose.yaml").write_text("services: {}\n")
    assert Path(env.provider.compose_chain("media")[0].path).name == "compose.yaml"


def test_service_with_no_container_is_unverifiable_not_unchanged(tmp_path):
    env = make_env(tmp_path)
    env.compose.write_text(COMPOSE + "  db:\n    image: postgres\n")
    cid = env.svc.create(env.mission.id, env.scope)           # no container exists for db
    env.compose.write_text("x\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and not report.ok
    assert {"service": "media/db", "fields": ["unverifiable"],
            "reason": "no container facts were captured"} in report.unrestored


# ── review round: restore never escapes half-done ───────────────────────────
def mutate_and_post(env, cid):
    env.compose.write_text("session one\n")
    env.dotenv.write_text("session two\n")
    env.svc.capture_post(cid)


def test_write_failure_reports_partial_restore_and_hands_to_operator(tmp_path, monkeypatch):
    from planet_express.missions.checkpoint import safefs
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    mutate_and_post(env, cid)
    real, calls = safefs.write_atomic, []

    def flaky(*a, **kw):
        calls.append(a[1])
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(*a, **kw)
    monkeypatch.setattr(safefs, "write_atomic", flaky)
    report = env.svc.restore(cid)
    assert report.status == "failed" and report.partial and len(report.written) == 1
    assert "No space left" in report.reason
    ev = json.loads(events(env, "checkpoint.restore_refused")[-1].body_json)
    assert ev["partial"] is True and ev["written"] == report.written
    assert status(env) == "needs_operator"


def test_recreate_exception_is_reported_not_raised(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    mutate_and_post(env, cid)
    monkeypatch.setattr(env.provider, "recreate", lambda services: (_ for _ in ()).throw(RuntimeError("boom")))
    report = env.svc.restore(cid)
    assert report.status == "failed" and report.partial and "RuntimeError" in report.reason
    assert status(env) == "needs_operator" and events(env, "checkpoint.restore_refused")


def test_destination_revalidated_just_before_rename(tmp_path, monkeypatch):
    from planet_express.missions.checkpoint import safefs
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    mutate_and_post(env, cid)
    real, count = safefs.write_atomic, []

    def racing(root, path, *a, **kw):
        count.append(path)
        if len(count) == 2:
            Path(path).write_text("someone raced us\n")         # lands after the pre-check, before the rename
        return real(root, path, *a, **kw)
    monkeypatch.setattr(safefs, "write_atomic", racing)
    report = env.svc.restore(cid)
    assert report.status == "refused" and report.partial and len(report.written) == 1
    assert [d["path"] for d in report.drifted] == [count[1]]
    assert Path(count[1]).read_text() == "someone raced us\n"          # not overwritten
    assert not [p for p in env.stacks.rglob("*.tmp")]
    assert status(env) == "needs_operator"


def test_write_atomic_fsyncs_after_metadata_and_before_rename(tmp_path, monkeypatch):
    from planet_express.missions.checkpoint import safefs
    root = safefs.real_root(str(tmp_path))
    target = tmp_path / "f"
    target.write_text("old")
    log = []
    real = {n: getattr(os, n) for n in ("fsync", "fchmod", "fchown", "rename")}
    def spy(name):
        def wrapper(*a, **k):
            log.append(name)
            return real[name](*a, **k)
        return wrapper
    for name in real:
        monkeypatch.setattr(os, name, spy(name))
    safefs.write_atomic(root, str(target), b"new", mode=0o640, uid=os.getuid(), gid=os.getgid(), acl=None)
    last_meta = max(i for i, n in enumerate(log) if n in ("fchmod", "fchown"))
    rename = log.index("rename")
    assert any(n == "fsync" for n in log[last_meta + 1:rename])


# ── review round: capture accounting, torn reads, atomic slot ───────────────
def test_bytes_actually_read_count_against_the_total(tmp_path):
    class Grow(_Hooks):
        def before_capture(self, planned):
            with open(next(p.real for p in planned if p.real.endswith(".env")), "ab") as handle:
                handle.write(b"#" * 500)
    size = len(COMPOSE) + len(SECRET)
    env = make_env(tmp_path, hooks=Grow(), settings=CheckpointSettings(max_total_bytes=size + 10))
    with pytest.raises(DiskGuardError):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []


def test_store_limit_counts_actual_bytes(tmp_path):
    class Grow(_Hooks):
        def before_capture(self, planned):
            with open(next(p.real for p in planned if p.real.endswith(".env")), "ab") as handle:
                handle.write(b"#" * 500)
    size = len(COMPOSE) + len(SECRET)
    env = make_env(tmp_path, hooks=Grow(), settings=CheckpointSettings(max_store_bytes=size + 10))
    with pytest.raises(DiskGuardError):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []


@pytest.mark.parametrize("keep_mtime", [False, True])
def test_torn_read_is_refused(tmp_path, keep_mtime):
    class Writer(_Hooks):
        def after_file_read(self, real):
            if real.endswith(".env"):
                before = os.stat(real)
                with open(real, "r+b") as handle:              # same inode, same length, different bytes
                    handle.write(b"X" * before.st_size)
                if keep_mtime:
                    os.utime(real, ns=(before.st_atime_ns, before.st_mtime_ns))
    env = make_env(tmp_path, hooks=Writer())
    with pytest.raises(ScopeRefused, match="changed while it was captured"):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == []


def test_slot_is_reserved_atomically_and_the_loser_keeps_nothing(tmp_path):
    class Winner(_Hooks):
        def before_reserve(self, mission_id):
            with env.ms.store._write() as conn:
                conn.execute("UPDATE missions SET checkpoint_id='C-winner' WHERE id=?", (mission_id,))
    env = make_env(tmp_path, hooks=Winner())
    with pytest.raises(CheckpointExists):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == [] and events(env, "checkpoint.created") == []


def test_slot_refused_when_mission_went_terminal_during_capture(tmp_path):
    class Cancel(_Hooks):
        def before_reserve(self, mission_id):
            advance_to(env.ms, mission_id, "cancelled")
    env = make_env(tmp_path, hooks=Cancel())
    with pytest.raises(CheckpointExists):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == [] and env.ms.get_mission(env.mission.id).checkpoint_id is None


# ── review round: invalid YAML, env_file and include ────────────────────────
def test_invalid_yaml_captures_files_and_marks_facts_unavailable(tmp_path):
    env = make_env(tmp_path)
    env.compose.write_text("services: [unclosed\n")
    cid = env.svc.create(env.mission.id, env.scope)
    assert cid.facts_unavailable == ("media",)
    manifest = env.svc.store.read_manifest(cid)
    assert manifest["facts_unavailable"] == ["media"] and manifest["facts"]["services"] == {}
    assert len(manifest["files"]) == 2
    env.compose.write_text("x\n")
    env.svc.capture_post(cid)
    report = env.svc.restore(cid)
    assert report.status == "done" and env.compose.read_text() == "services: [unclosed\n"
    assert any(u["fields"] == ["unverifiable"] and u["service"] == "media" for u in report.unrestored)


def test_env_file_and_include_entries_are_captured(tmp_path):
    env = make_env(tmp_path)
    media = env.stacks / "media"
    (media / "app.env").write_text("K=v\n")
    (media / "extra").mkdir()
    (media / "extra" / "inc.yml").write_text("services:\n  worker:\n    env_file: [inc.env]\n")
    (media / "extra" / "inc.env").write_text("I=1\n")
    env.compose.write_text("include:\n  - extra/inc.yml\n  - path: [missing.yml]\n    required: false\n"
                           "services:\n  web:\n    image: x\n    env_file:\n      - app.env\n"
                           "      - path: opt.env\n        required: false\n")
    cid = env.svc.create(env.mission.id, env.scope)
    names = {Path(f["path"]).name for f in env.svc.store.read_manifest(cid)["files"]}
    assert {"app.env", "inc.yml", "inc.env", ".env", "docker-compose.yml"} <= names
    assert "opt.env" not in names


def test_unreadable_reference_refuses_scope(tmp_path):
    env = make_env(tmp_path)
    for text, why in (("services:\n  web:\n    env_file: gone.env\n", "cannot be inspected"),
                      ("services:\n  web:\n    env_file: ../outside.env\n", "'..'"),
                      ("services:\n  web:\n    env_file: /etc/passwd\n", "outside the stacks root"),
                      ("services:\n  web:\n    env_file: ${X}/a.env\n", "variables")):
        env.compose.write_text(text)
        with pytest.raises(ScopeRefused, match=why):
            env.svc.create(env.mission.id, env.scope)
        assert leftovers(env) == []


# ── review round: quarantine accounting and incomplete purge ────────────────
def test_quarantine_is_counted_separately_and_expires(tmp_path):
    import time
    env = make_env(tmp_path)
    store = CheckpointStore(env.root)
    cid = "C-" + "cd" * 8
    store.new_checkpoint(cid)
    store.write_payload(cid, 0, b"x" * 100)
    assert env.svc.startup_scan()["quarantined"] == [cid]
    assert store.total_bytes() == 0 and store.quarantine_bytes() == 100
    env.clock.t = time.time() + 3600
    env.svc.startup_scan()
    assert (env.root / ".quarantine" / cid).exists()                 # younger than 24 h
    env.clock.t = time.time() + 25 * 3600
    env.svc.startup_scan()
    assert not (env.root / ".quarantine" / cid).exists()


def test_incomplete_purge_is_not_success_and_retries_at_startup(tmp_path, monkeypatch):
    from planet_express.missions.checkpoint import store as store_module
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    advance_to(env.ms, env.mission.id, "cancelled")
    monkeypatch.setattr(store_module.shutil, "rmtree", lambda *a, **k: None)
    assert env.svc.purge(cid) is False
    assert events(env, "checkpoint.purged") == []
    assert not (env.root / cid).exists()                             # unusable at once
    assert env.svc.store.pending_removals() == [f".purging-{cid}"]
    monkeypatch.undo()
    assert env.svc.startup_scan()["purge_pending"] == []
    assert env.svc.store.pending_removals() == []


# ── review round 2: project_directory, references re-expanded at capture ────
def test_include_project_directory_carries_into_nested_references(tmp_path):
    env = make_env(tmp_path)
    media = env.stacks / "media"
    (media / "sub").mkdir()
    (media / "shared").mkdir()
    (media / "sub" / "inc.yml").write_text("services:\n  w:\n    env_file: nested.env\n")
    (media / "shared" / "nested.env").write_text("N=1\n")                    # relative to project_directory
    env.compose.write_text("include:\n  - path: sub/inc.yml\n    project_directory: shared\n"
                           "services:\n  web:\n    image: x\n")
    cid = env.svc.create(env.mission.id, env.scope)
    paths = {f["path"] for f in env.svc.store.read_manifest(cid)["files"]}
    assert os.path.realpath(media / "shared" / "nested.env") in paths
    # without project_directory the same reference resolves next to the included file
    (media / "sub" / "nested.env").write_text("N=2\n")
    env2 = make_env(tmp_path / "b")
    m2 = env2.stacks / "media"
    (m2 / "sub").mkdir()
    (m2 / "sub" / "inc.yml").write_text("services:\n  w:\n    env_file: nested.env\n")
    (m2 / "sub" / "nested.env").write_text("N=2\n")
    env2.compose.write_text("include:\n  - path: sub/inc.yml\nservices:\n  web:\n    image: x\n")
    cid2 = env2.svc.create(env2.mission.id, env2.scope)
    assert os.path.realpath(m2 / "sub" / "nested.env") in {f["path"] for f in env2.svc.store.read_manifest(cid2)["files"]}


def test_reference_added_between_validation_and_capture_retries_once(tmp_path):
    class Edit(_Hooks):
        calls = 0

        def before_capture(self, planned):
            Edit.calls += 1
            if Edit.calls == 1:
                (env.stacks / "media" / "late.env").write_text("L=1\n")
                env.compose.write_text(COMPOSE + "  other:\n    image: y\n    env_file: late.env\n")
    env = make_env(tmp_path, hooks=Edit())
    cid = env.svc.create(env.mission.id, env.scope)
    names = {Path(f["path"]).name for f in env.svc.store.read_manifest(cid)["files"]}
    assert "late.env" in names and Edit.calls == 2 and len(list(env.root.iterdir())) == 1


def test_reference_changing_on_every_attempt_is_refused(tmp_path):
    class Edit(_Hooks):
        calls = 0

        def before_capture(self, planned):
            Edit.calls += 1
            name = f"late{Edit.calls}.env"
            (env.stacks / "media" / name).write_text("L=1\n")
            env.compose.write_text(COMPOSE + f"  other:\n    image: y\n    env_file: {name}\n")
    env = make_env(tmp_path, hooks=Edit())
    with pytest.raises(ScopeChanged):
        env.svc.create(env.mission.id, env.scope)
    assert leftovers(env) == [] and env.ms.get_mission(env.mission.id).checkpoint_id is None


# ── review round 3: implicit .env of includes, bounded `compose config` ─────
def test_include_without_env_file_captures_implicit_dotenv_candidates(tmp_path):
    env = make_env(tmp_path)
    media = env.stacks / "media"
    for d in ("sub", "shared", "other"):
        (media / d).mkdir()
    (media / "sub" / "inc.yml").write_text("services: {}\n")
    (media / "sub" / ".env").write_text("A=1\n")
    (media / "shared" / ".env").write_text("B=1\n")
    env.compose.write_text("include:\n  - path: sub/inc.yml\n    project_directory: shared\n"
                           "  - path: other/inc2.yml\n    env_file: other/custom.env\n"
                           "services:\n  web:\n    image: x\n")
    (media / "other" / "inc2.yml").write_text("services: {}\n")
    (media / "other" / "custom.env").write_text("C=1\n")
    (media / "other" / ".env").write_text("NOT=captured\n")        # explicit env_file: no implicit .env
    cid = env.svc.create(env.mission.id, env.scope)
    paths = {f["path"] for f in env.svc.store.read_manifest(cid)["files"]}
    assert os.path.realpath(media / "sub" / ".env") in paths and os.path.realpath(media / "shared" / ".env") in paths
    assert os.path.realpath(media / "other" / "custom.env") in paths
    assert os.path.realpath(media / "other" / ".env") not in paths
    # CAS applies like any captured file: an edit after the post manifest blocks the restore
    (media / "shared" / ".env").write_text("B=changed\n")
    env.svc.capture_post(cid)
    (media / "shared" / ".env").write_text("B=other\n")
    assert env.svc.restore(cid).status == "refused"


def test_include_without_any_dotenv_is_fine(tmp_path):
    env = make_env(tmp_path)
    (env.stacks / "media" / "inc.yml").write_text("services: {}\n")
    env.compose.write_text("include:\n  - inc.yml\nservices:\n  web:\n    image: x\n")
    assert env.svc.create(env.mission.id, env.scope)


def test_compose_config_output_is_capped_and_never_leaks(tmp_path, caplog):
    import logging
    env = make_env(tmp_path)
    secret = "SUPER-SECRET-VALUE-9f8e7d"
    env.docker.config_output = json.dumps({"name": "media", "pad": secret * 400_000})      # ~10 MB
    with caplog.at_level(logging.DEBUG):
        assert env.provider.project_name("media") is None
        assert env.provider.inspect("media/web") is None
        cid = env.svc.create(env.mission.id, env.scope)
        env.compose.write_text("x\n")
        env.svc.capture_post(cid)
        report = env.svc.restore(cid)
    assert any(u["fields"] == ["unverifiable"] for u in report.unrestored)
    everything = json.dumps([e.body_json for e in env.ms.events(env.mission.id)]) + caplog.text + repr(report)
    assert secret not in everything
    assert env.docker.calls and not any(c[:3] == ["docker", "ps", "-aq"] for c in env.docker.calls)


def test_compose_config_name_is_read_from_a_document_full_of_secrets(tmp_path):
    env = make_env(tmp_path)
    env.docker.config_output = json.dumps({"name": "media", "services": {"web": {"environment": {"K": "s3cr3t"}}}})
    assert env.provider.project_name("media") == "media"
