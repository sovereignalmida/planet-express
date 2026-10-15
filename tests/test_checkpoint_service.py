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

from checkpoint_fakes import COMPOSE, make_env, tree
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
    ScopeRefused,
    SystemdCheckpointProvider,
)
from planet_express.missions.checkpoint.provider import ProviderError
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


def test_restore_without_post_manifest_compares_to_original(tmp_path):
    env = make_env(tmp_path)
    cid = env.svc.create(env.mission.id, env.scope)
    env.compose.write_text("changed with no session\n")
    report = env.svc.restore(cid)             # session never started: current != original is drift
    assert report.status == "refused"
    assert env.svc.restore(cid, ack_drift=True, actor="operator:me").status == "done"


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
