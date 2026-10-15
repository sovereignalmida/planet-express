"""Slice 6 checks that need a real host: real `ss`, real `visudo`, a real root-owned /etc, the real signal collectors.

They refuse to run unless PE_PRIVILEGED_TESTS_OK=1, which scripts/run_privileged_tests.sh sets only inside a disposable
environment (docker or the MOS VM). Never run them on a developer machine: they write under /etc/sudoers.d and /etc/hosts.
"""
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENABLED = os.geteuid() == 0 and os.environ.get("PE_PRIVILEGED_TESTS_OK") == "1"
pytestmark = [pytest.mark.skipif(
    not ENABLED, reason="root-only: run scripts/run_privileged_tests.sh (disposable container or the MOS VM)")]

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)

from planet_express.missions.launcher.setup_steps import MemoryJournal
from planet_express.missions.launcher.side_doors import (
    DockerTcpExposure,
    SideDoorPaths,
    SudoersManaged,
    WorkerIdentity,
)
from planet_express.missions.protected import Discovery, build_protected
from planet_express.missions.resources import canon
from planet_express.missions.tripwire.diff import DiffContext, diff_snapshots
from planet_express.missions.tripwire.signals import (
    ScanEnv,
    Scanner,
    collect_boot,
    collect_files,
    collect_firewall,
    collect_mounts,
    collect_packages,
    collect_ports,
)

CORE_USER = "pe-s6-core"
BAD = "/etc/sudoers.d/zz-pe-s6-test"


def real_paths(**over):
    kw = dict(core_user=CORE_USER, uid_min=61000, uid_max=61999, approve=lambda step, detail: True,
              visudo=shutil.which("visudo") or "/usr/sbin/visudo", ss=shutil.which("ss") or "ss")
    kw.update(over)
    return SideDoorPaths(**kw)


@pytest.fixture
def core_user():
    subprocess.run(["useradd", "-M", "-s", "/usr/sbin/nologin", CORE_USER], check=True)
    yield CORE_USER
    subprocess.run(["userdel", CORE_USER], check=False)
    if os.path.exists(BAD):
        os.unlink(BAD)


needs_sudo = pytest.mark.skipif(not os.path.isdir("/etc/sudoers.d") or shutil.which("visudo") is None,
                             reason="needs sudo (/etc/sudoers.d and visudo): MOS has neither")


@needs_sudo
def test_real_sudoers_unmanaged_wildcard_is_found_repaired_and_undone(core_user):
    original = f"{CORE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart *\n"
    Path(BAD).write_text(original)
    os.chmod(BAD, 0o440)
    step = SudoersManaged(real_paths())
    assert not step.probe().ok
    journal = MemoryJournal()
    step.repair(journal)
    assert step.probe().ok
    assert subprocess.run([shutil.which("visudo") or "visudo", "-c"], capture_output=True).returncode == 0
    assert oct(os.stat(BAD).st_mode & 0o777) == "0o440" and os.stat(BAD).st_uid == 0
    step.undo(journal, journal.evidence(step.name))
    assert Path(BAD).read_text() == original


@needs_sudo
def test_real_sudoers_repair_refuses_without_approval(core_user):
    Path(BAD).write_text(f"{CORE_USER} ALL=(ALL) NOPASSWD: ALL\n")
    os.chmod(BAD, 0o440)
    from planet_express.missions.launcher.setup_steps import StepError
    with pytest.raises(StepError, match="approval"):
        SudoersManaged(real_paths(approve=lambda s, d: False)).repair(MemoryJournal())
    assert "NOPASSWD: ALL" in Path(BAD).read_text()


@pytest.mark.skipif(shutil.which("ss") is None, reason="needs ss (iproute2)")
def test_real_listener_on_the_docker_tcp_port_is_seen_by_real_ss(core_user):
    srv = socket.socket()
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 2375))
        srv.listen(1)
    except OSError:
        pytest.skip("port 2375 is busy in this environment")
    try:
        step = DockerTcpExposure(real_paths(docker_units=()))
        probe = step.probe()
        assert not probe.ok and "2375" in probe.detail
        assert DockerTcpExposure(real_paths(docker_units=(), proxy_ports=(2375,))).probe().ok
    finally:
        srv.close()


def test_real_worker_identity_probe_passes_on_a_clean_host(core_user):
    probe = WorkerIdentity(real_paths()).probe()
    assert probe.ok, probe.detail


def test_real_signals_are_stable_across_two_scans_and_see_a_planted_change(tmp_path):
    """No false positives from a real host (counters, uptime, mount churn) and a real file change is caught."""
    from planet_express.missions.checkpoint.provider import (
        default_bounded_runner,
        default_runner,
    )

    def runner(argv, timeout):
        if argv[0] == "docker":
            return 127, "", "no docker here"
        return default_runner(argv, timeout)

    def bounded(argv, timeout, limit):
        if argv[0] == "docker":
            return 127, "", "no docker here", False
        return default_bounded_runner(argv, timeout, limit)

    class Provider:
        def listening_ports(self):
            rc, out, _ = default_runner(["ss", "-H", "-ltnu"], 5)
            return sorted({f"{p[0]} {p[4]}" for p in (line.split() for line in out.splitlines()) if len(p) >= 5})

        def mount_table(self):
            return sorted(Path("/proc/self/mounts").read_text().splitlines())

    protected = build_protected(site=[], discovery=Discovery(stacks_root="/srv/stacks"))
    env = ScanEnv(runner=runner, bounded=bounded, provider=Provider(), protected=protected)
    signals = {"files": collect_files, "firewall": collect_firewall, "boot": collect_boot, "packages": collect_packages,
               "ports": collect_ports, "mounts": collect_mounts}
    scanner = Scanner(env, signals)
    first, second = scanner.scan(), scanner.scan()
    assert first.ok, first.errors
    assert second.ok, second.errors
    ctx = DiffContext(scope=(canon("stack:media"),), protected=protected.resources, stacks_root="/srv/stacks")
    stable = [c for c in diff_snapshots(first.snapshot, second.snapshot, ctx) if c.signal != "ports"]
    assert stable == [], [c.as_dict() for c in stable]
    hosts = Path("/etc/hosts")
    saved = hosts.read_bytes()
    try:
        hosts.write_bytes(saved + b"10.9.9.9 planted.example\n")
        third = scanner.scan()
    finally:
        hosts.write_bytes(saved)
    changes = diff_snapshots(first.snapshot, third.snapshot, ctx)
    assert any(c.signal == "files" and c.resource.endswith("path:/etc/hosts") and c.verdict == "protected"
               for c in changes)
