"""G4-F3 #13 (docker list/inspect race) and #20 (scan bounds, no collector pile-up)."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripwire_fakes import make_rig

from planet_express.missions.tripwire.signals import Scanner, SignalError, collect_containers


@pytest.fixture
def rig(tmp_path):
    return make_rig(tmp_path)


# ── #13 a container that vanishes between `docker ps` and `docker inspect` is not a failed signal ─────

def _vanishing(rig, gone, stderr="Error response from daemon: No such object: {id}", stdout="partial", times=1):
    """`docker inspect` that reports `gone` as missing (exit 1, the others on stdout), for the first `times` calls."""
    real = rig.scan_env.bounded
    calls = {"n": 0}

    def bounded(argv, timeout, max_bytes):
        if argv[:2] == ["docker", "inspect"] and calls["n"] < times:
            calls["n"] += 1
            docs = [d for d in rig.host._docs() if d["Id"] in argv[2:] and d["Name"].lstrip("/") != gone]
            victim = next(d for d in rig.host._docs() if d["Name"].lstrip("/") == gone)
            out = json.dumps(docs) if stdout == "partial" else stdout
            return 1, out, stderr.format(id=victim["Id"]), False
        return real(argv, timeout, max_bytes)
    rig.scan_env.bounded = bounded
    return calls


def test_a_container_that_vanished_mid_scan_is_left_out_not_a_failure(rig):
    _vanishing(rig, "other-db-1")
    found = collect_containers(rig.scan_env)
    assert "other-db-1" not in found and {"media-qbit-1", "media-traefik-1"} <= set(found)


def test_a_vanished_container_with_nothing_readable_is_retried_once(rig):
    calls = _vanishing(rig, "other-db-1", stdout="")
    found = collect_containers(rig.scan_env)
    assert calls["n"] == 1 and len(found) == 3          # the second read lists afresh and sees them all


def test_a_genuine_docker_failure_still_fails_the_signal(rig):
    _vanishing(rig, "other-db-1", stderr="permission denied while trying to connect to the Docker daemon socket")
    with pytest.raises(SignalError):
        collect_containers(rig.scan_env)


def test_a_mixed_error_is_a_failure_not_a_vanished_container(rig):
    _vanishing(rig, "other-db-1", stderr="Error: No such object: {id}\nError: permission denied")
    with pytest.raises(SignalError):
        collect_containers(rig.scan_env)


# ── #20 both scan bounds count, and a stuck collector is not respawned ────────────────────────────────

def test_a_signal_still_running_from_the_last_scan_is_not_started_again(rig):
    release = threading.Event()
    started = []

    def stuck(env):
        started.append(1)
        release.wait(10)
        return {}
    scanner = Scanner(rig.scan_env, {"stuck": stuck}, signal_timeout_s=0.05, deadline_s=1.0)
    try:
        assert scanner.scan().errors == {"stuck": "timeout"}
        assert scanner.scan().errors == {"stuck": "timeout"} and scanner.scan().errors == {"stuck": "timeout"}
        assert len(started) == 1                       # three scans, one thread
        assert sum(t.name == "tripwire-stuck" for t in threading.enumerate()) == 1
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and scanner._running:
        time.sleep(0.01)
    assert scanner.scan().ok and len(started) == 2        # released: a fresh read starts again


def test_the_scan_deadline_bounds_the_scan_when_it_is_the_shorter_one(rig):
    release = threading.Event()
    scanner = Scanner(rig.scan_env, {"slow": lambda env: release.wait(10)}, signal_timeout_s=30, deadline_s=0.1)
    began = time.monotonic()
    try:
        assert scanner.scan().errors == {"slow": "timeout"}
        assert time.monotonic() - began < 2
    finally:
        release.set()


def test_the_per_signal_timeout_bounds_a_signal_when_it_is_the_shorter_one(rig):
    release = threading.Event()
    scanner = Scanner(rig.scan_env, {"slow": lambda env: release.wait(10), "fast": lambda env: {"ok": 1}},
                      signal_timeout_s=0.1, deadline_s=30)
    began = time.monotonic()
    try:
        result = scanner.scan()
        assert result.errors == {"slow": "timeout"} and result.snapshot == {"fast": {"ok": 1}}
        assert time.monotonic() - began < 2
    finally:
        release.set()
