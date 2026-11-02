"""rc13's first live Repair (M-9) never got past the tripwire baseline on a host where Core is not root: the Traefik access
log was protected and too big to hash, Docker volumes under the root-only /var/lib/docker raised instead of reading as
blind, and one 3 s budget walking 36 volumes ran out inside a single NFS directory. Each failure made the whole signal
fail, so no Repair could start."""
import os
from types import SimpleNamespace

import pytest
from test_mission_discovery import Host, run

from planet_express.missions.discovery import _log_mount
from planet_express.missions.tripwire import signals as S


def env(volume_paths=(), clock=None, deadline=None):
    e = S.ScanEnv(runner=None, bounded=None, protected=SimpleNamespace(), volume_paths=tuple(volume_paths),
                  **({"clock": clock} if clock else {}))
    e.deadline = deadline
    return e


# ── discovery: a container's log mount is not protected ─────────────────────────────────────────────

@pytest.mark.parametrize("dest, log", [
    ("/var/log/traefik", True), ("/app/logs/access.log", True), ("/var/log", True), ("/data/log", True),
    ("/srv/proxy.log", True), ("/etc/traefik/dynamic", False), ("/letsencrypt", False), ("/catalog", False),
    ("/opt/blog", False), ("", False)])
def test_what_counts_as_a_log_mount(dest, log):
    assert _log_mount(dest) is log


def test_the_ingress_access_log_is_left_out_and_its_config_still_protected(tmp_path):
    host = Host(containers={"edge": ("traefik:v3", [("/opt/proxy/dynamic", "/etc/traefik/dynamic"),
                                                    ("/opt/proxy/accesslogs", "/var/log/traefik"),
                                                    ("/opt/proxy/accesslogs/access.log", "/app/logs/access.log")])})
    got = run(tmp_path, host)
    assert got.failures == {} and got.ingress == ("/opt/proxy/dynamic",)


def test_an_ingress_whose_only_bind_is_its_log_still_fails_closed(tmp_path):
    got = run(tmp_path, Host(containers={"edge": ("traefik:v3", [("/opt/proxy/logs", "/var/log/traefik")])}))
    assert "ingress" in got.failures


# ── files: a large file is watched by its stat signature, not a failed signal ───────────────────────

def test_a_file_too_large_to_hash_is_watched_by_size_and_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MAX_FILE_BYTES", 10)
    big = tmp_path / "access.log"
    big.write_bytes(b"x" * 100)
    assert S._file_digest(str(big), os.lstat(big)).startswith("large:100:")
    first = S.hash_path(env(), str(big))
    assert len(first) == 64 and first == S.hash_path(env(), str(big))
    assert not S.blind_resources({"files": {"r": first}}), "a large file is still watched, so it is not blind"
    stat_before = os.lstat(big)
    with open(big, "r+b") as handle:                     # same size, then mtime put back: only ctime shows it
        handle.write(b"z")
    os.utime(big, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert os.lstat(big).st_size == stat_before.st_size and os.lstat(big).st_mtime_ns == stat_before.st_mtime_ns
    assert S.hash_path(env(), str(big)) != first
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "big").write_bytes(b"x" * 100)
    assert len(S.hash_path(env(), str(tmp_path / "dir"))) == 64, "a directory holding one is hashed as usual"


# ── volumes ─────────────────────────────────────────────────────────────────────────────────────────

def test_a_volume_core_cannot_even_stat_is_blind_not_a_failed_signal(tmp_path, monkeypatch):
    real = os.statvfs

    def statvfs(path):
        if "docker" in str(path):
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(S.os, "statvfs", statvfs)
    (tmp_path / "ok").mkdir()
    got = S.collect_volumes(env([("r:docker", "/var/lib/docker/volumes/x/_data"), ("r:ok", str(tmp_path / "ok"))]))
    assert got["r:docker"] == {"blind": True} and got["r:ok"]["capped"] is False
    assert S.blind_resources({"volumes": got}) == ["r:docker"]


class Tick:
    """A clock that moves on every read, so walking costs time."""

    def __init__(self, step):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def tree(path, n):
    path.mkdir()
    for i in range(n):
        (path / f"f{i}").write_bytes(b"x")
    return str(path)


def test_one_big_volume_does_not_starve_the_next(tmp_path):
    clock = Tick(0.01)
    big, small = tree(tmp_path / "big", 400), tree(tmp_path / "small", 3)
    got = S.collect_volumes(env([("r:big", big), ("r:small", small)], clock=clock, deadline=clock.t + 1.5))
    assert got["r:big"]["capped"] is True and got["r:big"]["entries"] < 400
    assert got["r:small"]["capped"] is False and got["r:small"]["entries"] == 3


def test_time_runs_out_inside_one_large_directory(tmp_path):
    clock = Tick(0.01)
    got = S.collect_volumes(env([("r", tree(tmp_path / "flat", 400))], clock=clock, deadline=clock.t + 1.0))
    assert got["r"]["capped"] is True and 0 < got["r"]["entries"] < 400
    assert got["r"]["top"] == 400, "the cheap figures the diff falls back to are still there"


def test_a_volume_that_turns_unreadable_after_the_baseline_is_a_change():
    """Review, rc13.1: a blind reading is now a result, not a failed signal, so readable -> blind must not look unchanged."""
    from planet_express.missions.tripwire.diff import _shrunk
    readable = {"size": 10, "entries": 2, "capped": False, "used": 10, "top": 2}
    assert _shrunk(readable, {"blind": True}) == "volume became unreadable"
    assert _shrunk({"blind": True}, {"blind": True}) is None, "a baseline that was blind (and accepted) stays as it was"
    assert _shrunk({"blind": True}, readable) is None
