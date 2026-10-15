"""Setup preflight side doors (brief 6.8): worker identity, Docker TCP exposure, sudoers. Each fails closed.

Everything runs on a throwaway tree with injected passwd/group/ss/visudo; nothing here needs root."""
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.missions.launcher.setup_steps import (
    MemoryJournal,
    StepError,
    StepRefused,
)
from planet_express.missions.launcher.side_doors import (
    DockerTcpExposure,
    SideDoorPaths,
    SudoersManaged,
    WorkerIdentity,
    repair_gate_report,
    side_door_steps,
)

CORE = "planetexpress"
MANAGED_LAUNCH = "# Managed by Planet Express (session launcher). Do not edit.\nplanetexpress ALL=(root) NOPASSWD: /usr/libexec/planetexpress/pe-launch \"\"\n"
MANAGED_MAIN = "# Managed by Planet Express.\nplanetexpress ALL=(root) NOPASSWD: /usr/bin/systemctl restart plex.service\n"


class Host:
    """A temp host: files plus scripted `ss` and `visudo`."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.etc = tmp / "etc"
        (self.etc / "sudoers.d").mkdir(parents=True)
        (self.etc / "docker").mkdir()
        (tmp / "systemd").mkdir()
        self.passwd = self.etc / "passwd"
        self.group = self.etc / "group"
        self.passwd.write_text("root:x:0:0::/root:/bin/sh\nplanetexpress:x:1001:1001::/home/pe:/bin/sh\n"
                               "chris:x:1000:1000::/home/chris:/bin/sh\n")
        self.group.write_text("root:x:0:\ndocker:x:998:chris,planetexpress\nplanetexpress:x:1001:\nsudo:x:27:chris\n")
        (self.etc / "sudoers").write_text("Defaults env_reset\nroot ALL=(ALL:ALL) ALL\n%sudo ALL=(ALL:ALL) ALL\n"
                                          f"@includedir {self.etc}/sudoers.d\n")
        (self.etc / "sudoers.d" / "planetexpress").write_text(MANAGED_MAIN)
        (self.etc / "sudoers.d" / "planetexpress-launch").write_text(MANAGED_LAUNCH)
        (self.etc / "docker" / "daemon.json").write_text("{}\n")
        self.ss = ""
        self.approved = True
        self.visudo_ok = True
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0].endswith("visudo"):
            return SimpleNamespace(returncode=0 if self.visudo_ok else 1, stdout=b"", stderr=b"")
        if argv[0] == "ss":
            return SimpleNamespace(returncode=0, stdout=self.ss.encode(), stderr=b"")
        return SimpleNamespace(returncode=127, stdout=b"", stderr=b"")

    def paths(self, **over):
        me = os.geteuid()
        kw = dict(core_user=CORE, uid_min=61000, uid_max=61999, passwd=str(self.passwd), group=str(self.group),
                  sudoers=str(self.etc / "sudoers"), sudoers_dir=str(self.etc / "sudoers.d"),
                  daemon_json=str(self.etc / "docker" / "daemon.json"),
                  docker_units=(str(self.tmp / "systemd"),), run=self.run, visudo="visudo",
                  managed={str(self.etc / "sudoers.d" / "planetexpress"): MANAGED_MAIN.encode(),
                           str(self.etc / "sudoers.d" / "planetexpress-launch"): MANAGED_LAUNCH.encode()},
                  approve=lambda step, detail: self.approved, owner_uid=me, owner_gid=os.getegid())
        kw.update(over)
        return SideDoorPaths(**kw)


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# ── (a) worker identity ─────────────────────────────────────────────────────────────────────────────

def test_worker_identity_passes_on_a_clean_host(host):
    probe = WorkerIdentity(host.paths()).probe()
    assert probe.ok, probe.detail


def test_worker_identity_fails_when_the_range_contains_core(host):
    probe = WorkerIdentity(host.paths(uid_min=1000, uid_max=1500)).probe()
    assert not probe.ok and "Core" in probe.detail


def test_worker_identity_fails_when_the_range_contains_root_or_is_malformed(host):
    assert not WorkerIdentity(host.paths(uid_min=0, uid_max=500)).probe().ok
    assert not WorkerIdentity(host.paths(uid_min=700, uid_max=600)).probe().ok


def test_worker_identity_fails_when_a_range_account_is_in_the_docker_group(host):
    host.passwd.write_text(host.passwd.read_text() + "sess:x:61005:61005::/nonexistent:/sbin/nologin\n")
    host.group.write_text("root:x:0:\ndocker:x:998:chris,sess\n")
    probe = WorkerIdentity(host.paths()).probe()
    assert not probe.ok and "docker" in probe.detail and "sess" in probe.detail


def test_worker_identity_fails_on_a_primary_group_of_docker(host):
    host.passwd.write_text(host.passwd.read_text() + "sess:x:61005:998::/nonexistent:/sbin/nologin\n")
    assert not WorkerIdentity(host.paths()).probe().ok


def test_worker_identity_fails_when_the_docker_gid_is_in_the_session_range(host):
    host.group.write_text("root:x:0:\ndocker:x:61100:\n")
    assert not WorkerIdentity(host.paths()).probe().ok


def test_worker_identity_fails_closed_when_the_databases_cannot_be_read(host):
    host.group.unlink()
    probe = WorkerIdentity(host.paths()).probe()
    assert not probe.ok and probe.blocked


def test_worker_identity_repair_needs_approval_removes_only_range_members_and_undoes(host):
    host.passwd.write_text(host.passwd.read_text() + "sess:x:61005:61005::/nonexistent:/sbin/nologin\n")
    original = "root:x:0:\ndocker:x:998:chris,sess,planetexpress\n"
    host.group.write_text(original)
    step = WorkerIdentity(host.paths())
    host.approved = False
    with pytest.raises(StepError, match="approval"):
        step.repair(MemoryJournal())
    assert host.group.read_text() == original
    host.approved = True
    journal = MemoryJournal()
    step.repair(journal)
    assert host.group.read_text() == "root:x:0:\ndocker:x:998:chris,planetexpress\n"
    assert step.probe().ok
    step.undo(journal, journal.evidence(step.name))
    assert host.group.read_text() == original


# ── (b) docker TCP ──────────────────────────────────────────────────────────────────────────────────

def test_docker_tcp_passes_with_only_the_unix_socket(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["fd://", "unix:///var/run/docker.sock"]}\n')
    assert DockerTcpExposure(host.paths()).probe().ok


def test_docker_tcp_fails_naming_the_exact_exposure_from_daemon_json(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["tcp://0.0.0.0:2375"]}\n')
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "tcp://0.0.0.0:2375" in probe.detail and "daemon.json" in probe.detail


def test_docker_tcp_loopback_is_still_reachable_by_a_local_worker(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["tcp://127.0.0.1:2375"]}\n')
    assert not DockerTcpExposure(host.paths()).probe().ok


def test_docker_tcp_with_verified_tls_is_authenticated(host):
    (host.etc / "docker" / "daemon.json").write_text(
        '{"hosts": ["tcp://0.0.0.0:2376"], "tlsverify": true, "tlscacert": "/ca.pem", "tlscert": "/c.pem", "tlskey": "/k.pem"}\n')
    assert DockerTcpExposure(host.paths()).probe().ok
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["tcp://0.0.0.0:2376"], "tls": true}\n')
    assert not DockerTcpExposure(host.paths()).probe().ok          # encryption without client verification is not auth


def test_docker_tcp_fails_on_a_systemd_unit_that_adds_a_tcp_host(host):
    (host.tmp / "systemd" / "docker.service").write_text(
        "[Service]\nExecStart=\nExecStart=/usr/bin/dockerd -H fd:// -H tcp://0.0.0.0:2375\n")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "docker.service" in probe.detail and "2375" in probe.detail


def test_docker_tcp_fails_on_a_live_listener_even_when_the_config_says_nothing(host):
    host.ss = 'LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:* users:(("dockerd",pid=812,fd=9))\n'
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "0.0.0.0:2375" in probe.detail


def test_docker_tcp_unknown_listener_on_2375_fails_unless_the_operator_named_the_proxy(host):
    host.ss = "LISTEN 0 4096 127.0.0.1:2375 0.0.0.0:*\n"
    assert not DockerTcpExposure(host.paths()).probe().ok
    ok = DockerTcpExposure(host.paths(proxy_ports=(2375,))).probe()
    assert ok.ok and "proxy" in ok.detail
    # the acknowledgement covers a proxy, never the daemon itself
    host.ss = 'LISTEN 0 4096 127.0.0.1:2375 0.0.0.0:* users:(("dockerd",pid=812,fd=9))\n'
    assert not DockerTcpExposure(host.paths(proxy_ports=(2375,))).probe().ok


def test_docker_tcp_fails_closed_on_unreadable_config_and_unparseable_json(host):
    (host.etc / "docker" / "daemon.json").write_text("{not json")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and probe.blocked
    (host.etc / "docker" / "daemon.json").unlink()
    (host.etc / "docker" / "daemon.json").mkdir()
    assert not DockerTcpExposure(host.paths()).probe().ok


def test_docker_tcp_repair_is_refused_and_says_what_to_do(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["tcp://0.0.0.0:2375"]}\n')
    with pytest.raises(StepError, match="socket proxy"):
        DockerTcpExposure(host.paths()).repair(MemoryJournal())
    assert "2375" in (host.etc / "docker" / "daemon.json").read_text()


# ── (c) sudoers ─────────────────────────────────────────────────────────────────────────────────────

def test_sudoers_passes_with_only_managed_rules_and_distro_defaults(host):
    probe = SudoersManaged(host.paths()).probe()
    assert probe.ok, probe.detail


def test_sudoers_fails_on_an_unmanaged_wildcard_rule_for_core(host):
    (host.etc / "sudoers.d" / "old-pe").write_text("planetexpress ALL=(root) NOPASSWD: /usr/bin/systemctl restart *\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "old-pe" in probe.detail and "wildcard" in probe.detail


@pytest.mark.parametrize("rule", [
    "ALL ALL=(ALL) NOPASSWD: ALL",
    "planetexpress ALL=(ALL) NOPASSWD: ALL",
    "%planetexpress ALL=(root) NOPASSWD: /usr/bin/docker *",
    "planetexpress ALL = (root) /bin/sh",
])
def test_sudoers_fails_on_unmanaged_rules_that_reach_core(host, rule):
    (host.etc / "sudoers.d" / "extra").write_text(rule + "\n")
    assert not SudoersManaged(host.paths()).probe().ok


def test_sudoers_ignores_rules_for_other_people(host):
    (host.etc / "sudoers.d" / "ops").write_text("chris ALL=(ALL) NOPASSWD: ALL\n")
    assert SudoersManaged(host.paths()).probe().ok


def test_sudoers_fails_when_a_managed_file_was_edited(host):
    (host.etc / "sudoers.d" / "planetexpress").write_text(MANAGED_MAIN + "planetexpress ALL=(ALL) NOPASSWD: ALL\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "planetexpress" in probe.detail


def test_sudoers_fails_closed_when_it_cannot_be_read(host):
    (host.etc / "sudoers").unlink()
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and probe.blocked


def test_sudoers_repair_needs_approval(host):
    bad = host.etc / "sudoers.d" / "old-pe"
    bad.write_text("planetexpress ALL=(root) NOPASSWD: /usr/bin/systemctl restart *\n")
    host.approved = False
    step = SudoersManaged(host.paths())
    with pytest.raises(StepError, match="approval"):
        step.repair(MemoryJournal())
    assert "restart *" in bad.read_text()


def test_sudoers_repair_removes_only_the_offending_rule_validates_and_undo_restores_the_file(host):
    bad = host.etc / "sudoers.d" / "old-pe"
    original = "# keep me\nchris ALL=(ALL) ALL\nplanetexpress ALL=(root) NOPASSWD: /usr/bin/systemctl restart *\n"
    bad.write_text(original)
    bad.chmod(0o440)
    step = SudoersManaged(host.paths())
    journal = MemoryJournal()
    step.repair(journal)
    text = bad.read_text()
    assert "chris ALL=(ALL) ALL" in text and "# keep me" in text
    assert all(line.startswith("#") for line in text.splitlines() if "restart *" in line)
    assert text.count("removed by Planet Express") == 1
    assert stat.S_IMODE(bad.stat().st_mode) == 0o440
    assert step.probe().ok
    assert any(c[0] == "visudo" for c in host.calls)
    step.undo(journal, journal.evidence(step.name))
    assert bad.read_text() == original


def test_sudoers_repair_does_not_install_a_file_visudo_rejects(host):
    bad = host.etc / "sudoers.d" / "old-pe"
    original = "planetexpress ALL=(root) NOPASSWD: /usr/bin/systemctl restart *\n"
    bad.write_text(original)
    host.visudo_ok = False
    with pytest.raises(StepError, match="visudo"):
        SudoersManaged(host.paths()).repair(MemoryJournal())
    assert bad.read_text() == original
    assert not list((host.etc / "sudoers.d").glob(".*"))             # no staging file left behind


def test_sudoers_repair_restores_a_managed_file_to_the_generator_output(host):
    managed = host.etc / "sudoers.d" / "planetexpress"
    managed.write_text(MANAGED_MAIN + "planetexpress ALL=(ALL) NOPASSWD: ALL\n")
    step = SudoersManaged(host.paths())
    journal = MemoryJournal()
    step.repair(journal)
    assert managed.read_text() == MANAGED_MAIN
    step.undo(journal, journal.evidence(step.name))
    assert "NOPASSWD: ALL" in managed.read_text()


def test_sudoers_undo_refuses_once_the_file_moved_on(host):
    bad = host.etc / "sudoers.d" / "old-pe"
    bad.write_text("planetexpress ALL=(root) NOPASSWD: /usr/bin/systemctl restart *\n")
    step = SudoersManaged(host.paths())
    journal = MemoryJournal()
    step.repair(journal)
    bad.write_text("something else entirely\n")
    with pytest.raises(StepRefused):
        step.undo(journal, journal.evidence(step.name))
    assert bad.read_text() == "something else entirely\n"


# ── the gate ────────────────────────────────────────────────────────────────────────────────────────

def test_the_repair_gate_passes_only_when_every_side_door_is_closed(host):
    report = repair_gate_report(side_door_steps(host.paths()))
    assert report.ok and [c.name for c in report.checks] == ["worker-identity", "docker-tcp-exposure",
                                                              "sudoers-managed"]
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["tcp://0.0.0.0:2375"]}\n')
    report = repair_gate_report(side_door_steps(host.paths()))
    assert not report.ok and report.failures[0].name == "docker-tcp-exposure"


def test_a_step_that_crashes_is_a_failed_check(host):
    steps = side_door_steps(host.paths())

    def boom():
        raise RuntimeError("unexpected")
    steps[0].probe = boom
    report = repair_gate_report(steps)
    assert not report.ok and report.failures[0].name == "worker-identity"


def test_the_gate_fails_with_no_steps(host):
    assert not repair_gate_report([]).ok
