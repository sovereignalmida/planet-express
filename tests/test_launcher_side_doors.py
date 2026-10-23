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


def _tls_files(host):
    for name in ("ca.pem", "c.pem", "k.pem"):
        (host.tmp / name).write_text("x")
    return host.tmp / "ca.pem"


def test_docker_tcp_with_verified_tls_is_authenticated(host):
    ca = _tls_files(host)
    (host.etc / "docker" / "daemon.json").write_text(
        '{"hosts": ["tcp://0.0.0.0:2376"], "tlsverify": true, "tlscacert": "%s", "tlscert": "/c.pem", "tlskey": "/k.pem"}\n' % ca)
    assert DockerTcpExposure(host.paths()).probe().ok
    (host.etc / "docker" / "daemon.json").write_text(
        '{"hosts": ["tcp://0.0.0.0:2376"], "tlsverify": true, "tlscacert": "/nonexistent-ca.pem", "tlscert": "/c.pem", "tlskey": "/k.pem"}\n')
    assert not DockerTcpExposure(host.paths()).probe().ok          # a CA that is not there verifies nobody
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


def _unit(host, name, text, sub=None):
    d = host.tmp / "systemd" / (sub or "")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


@pytest.mark.parametrize("exec_start", [
    "/usr/bin/dockerd -H fd:// \\\n  -H tcp://0.0.0.0:2375",                  # continued line
    "/usr/bin/dockerd -Htcp://0.0.0.0:2375",                                   # no separator
    "/usr/bin/dockerd --host=tcp://127.0.0.1:2375",
    "/usr/bin/dockerd --host tcp://[::]:2375",
    "/usr/bin/dockerd -H=tcp://[::1]:2375 -H fd://",
    "/usr/bin/dockerd -H 'tcp://0.0.0.0:2375'",
    "/usr/bin/dockerd \\\n -H fd:// \\\n --containerd=/run/c.sock \\\n -H tcp://:2375",
])
def test_docker_tcp_exec_start_forms(host, exec_start):
    _unit(host, "docker.service", f"[Service]\nExecStart={exec_start}\n")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "tcp://" in probe.detail, exec_start


def test_docker_tcp_continued_exec_start_with_verified_tls_passes(host):
    ca = _tls_files(host)
    _unit(host, "docker.service", f"[Service]\nExecStart=/usr/bin/dockerd \\\n  -H tcp://0.0.0.0:2376 \\\n  --tlsverify --tlscacert={ca} --tlscert=/c.pem --tlskey=/k.pem\n")
    assert DockerTcpExposure(host.paths()).probe().ok


@pytest.mark.parametrize("listen", ["0.0.0.0:2375", "[::]:2375", "[::1]:2375", "127.0.0.1:9999", "2375", "*:2376"])
def test_docker_tcp_socket_unit_listen_stream_forms(host, listen):
    _unit(host, "docker.socket", f"[Socket]\nListenStream=/run/docker.sock\nListenStream={listen}\n")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "docker.socket" in probe.detail, listen


def test_docker_tcp_unix_listen_stream_is_fine(host):
    _unit(host, "docker.socket", "[Socket]\nListenStream=/run/docker.sock\nListenStream=@abstract\n")
    assert DockerTcpExposure(host.paths()).probe().ok


def test_docker_tcp_drop_ins_are_read(host):
    _unit(host, "10-tcp.conf", "[Service]\nExecStart=\nExecStart=/usr/bin/dockerd -H tcp://0.0.0.0:2375\n",
          sub="docker.service.d")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "10-tcp.conf" in probe.detail
    _unit(host, "20-sock.conf", "[Socket]\nListenStream=0.0.0.0:2375\n", sub="docker.socket.d")
    assert "20-sock.conf" in DockerTcpExposure(host.paths()).probe().detail


@pytest.mark.parametrize("line", [
    "ExecStart=/usr/bin/dockerd $DOCKER_OPTS",
    "ExecStart=/usr/bin/dockerd ${OPTIONS}",
])
def test_docker_tcp_unresolvable_exec_start_fails_closed(host, line):
    _unit(host, "docker.service", f"[Service]\n{line}\n")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and probe.blocked and "cannot" in probe.detail


def test_docker_tcp_daemon_json_hosts_with_a_tcp_entry_among_others(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["fd://", "tcp://[::]:2375", "unix:///x"]}\n')
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "tcp://[::]:2375" in probe.detail


def test_docker_tcp_hosts_that_are_not_strings_fail_closed(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": [2375, {"a": 1}]}\n')
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and probe.blocked


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


def test_docker_tcp_parses_ss_with_and_without_the_netid_column(host):
    for line in ('tcp   LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:* users:(("dockerd",pid=812,fd=9))',
                 'LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:* users:(("dockerd",pid=812,fd=9))',
                 "tcp LISTEN 0 4096 [::]:2376 [::]:*"):
        host.ss = line + "\n"
        probe = DockerTcpExposure(host.paths()).probe()
        assert not probe.ok and "listening socket" in probe.detail, line
    host.ss = "tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:631 0.0.0.0:*\n"
    assert DockerTcpExposure(host.paths()).probe().ok


@pytest.mark.parametrize("rule", [
    "#1001 ALL=(root) NOPASSWD: ALL",                     # Core by numeric uid
    "#61005 ALL=(root) NOPASSWD: /bin/sh",                # a session uid
    "%#1001 ALL=(root) NOPASSWD: /bin/sh",                # Core's primary gid
    "%#998 ALL=(root) NOPASSWD: /bin/sh",                 # the docker gid Core is a member of
    "%#61100 ALL=(root) NOPASSWD: /bin/sh",               # a session gid
])
def test_sudoers_numeric_principals_are_not_comments(host, rule):
    (host.etc / "sudoers.d" / "numeric").write_text(rule + "\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "numeric" in probe.detail


def test_sudoers_numeric_rules_for_other_uids_are_ignored(host):
    (host.etc / "sudoers.d" / "numeric").write_text("#1000 ALL=(ALL) NOPASSWD: ALL\n%#4242 ALL=(ALL) ALL\n")
    assert SudoersManaged(host.paths()).probe().ok


def test_sudoers_nested_includes_are_read(host):
    extra = host.tmp / "opt" / "extra-sudoers"
    extra.parent.mkdir()
    extra.write_text("planetexpress ALL=(root) NOPASSWD: ALL\n")
    (host.etc / "sudoers.d" / "chain").write_text(f"#include {extra}\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "extra-sudoers" in probe.detail


def test_sudoers_relative_and_directory_includes_are_followed_and_cycles_are_cut(host):
    nested = host.tmp / "opt" / "more.d"
    nested.mkdir(parents=True)
    (nested / "rule").write_text("planetexpress ALL=(root) NOPASSWD: ALL\n")
    (host.etc / "sudoers.d" / "chain").write_text("@include ../loop\n@includedir " + str(nested) + "\n")
    (host.etc / "loop").write_text(f"@include {host.etc}/sudoers.d/chain\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "rule" in probe.detail


def test_sudoers_an_include_that_cannot_be_resolved_or_read_fails_closed(host):
    (host.etc / "sudoers.d" / "chain").write_text("@include /nonexistent/file\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and probe.blocked
    (host.etc / "sudoers.d" / "chain").write_text("@include /etc/sudoers.%h\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and probe.blocked and "substitution" in probe.detail


def test_sudoers_a_missing_includedir_fails_closed(host):
    (host.etc / "sudoers.d" / "chain").write_text("@includedir /nonexistent/dir.d\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and probe.blocked and "includedir" in probe.detail


def test_sudoers_a_long_include_chain_fails_closed(host):
    for i in range(20):
        (host.etc / f"c{i}").write_text(f"@include {host.etc}/c{i + 1}\n")
    (host.etc / "c20").write_text("# end\n")
    (host.etc / "sudoers.d" / "chain").write_text(f"@include {host.etc}/c0\n")
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "deeper" in probe.detail


def _add_session_accounts(host):
    host.passwd.write_text(host.passwd.read_text() + "sess:x:61005:61005::/nonexistent:/sbin/nologin\n")
    host.group.write_text(host.group.read_text() + "sessgrp:x:61100:\nops:x:5000:sess\n")


@pytest.mark.parametrize("rule", [
    "sess ALL=(root) NOPASSWD: /bin/sh",                               # a session account by name
    "%sessgrp ALL=(root) NOPASSWD: /bin/sh",                           # a group whose gid is a session gid
    "%ops ALL=(root) NOPASSWD: /bin/sh",                               # a group with a session account as a member
    "chris, sess ALL=(root) /bin/sh",                                  # user list with a space after the comma
    "planetexpress web1, web2 = (root) NOPASSWD: /bin/sh",             # host list with spaces
    "planetexpress web1 ,web2 =(root) /bin/sh",
    "!bob, planetexpress ALL=(ALL) ALL",
])
def test_sudoers_session_identities_and_list_forms_are_reached(host, rule):
    _add_session_accounts(host)
    (host.etc / "sudoers.d" / "x").write_text(rule + "\n")
    assert not SudoersManaged(host.paths()).probe().ok


@pytest.mark.parametrize("text", [
    "User_Alias ADMINS = chris : OPS = planetexpress\nOPS ALL=(root) /bin/sh\n",     # second of two definitions
    "User_Alias A = planetexpress\nUser_Alias B = bob, A\nB ALL=(ALL) ALL\n",       # alias of an alias
    "User_Alias A = sess\nA ALL=(ALL) ALL\n",
    "Runas_Alias R = root : S = daemon\nplanetexpress ALL=(S) /bin/sh\n",
    "Cmnd_Alias C = /usr/bin/systemctl restart *\nplanetexpress ALL=(root) C\n",
    "Host_Alias H = web1, web2\nplanetexpress H=(root) /bin/sh\n",
    "User_Alias A = chris, \\\n  planetexpress\nA ALL=(ALL) ALL\n",                # continued definition
])
def test_sudoers_aliases_are_resolved(host, text):
    _add_session_accounts(host)
    (host.etc / "sudoers.d" / "x").write_text(text)
    assert not SudoersManaged(host.paths()).probe().ok


def test_sudoers_cmnd_alias_with_a_wildcard_is_labelled(host):
    (host.etc / "sudoers.d" / "x").write_text("Cmnd_Alias C = /usr/bin/systemctl restart *\nplanetexpress ALL=(root) C\n")
    assert "wildcard" in SudoersManaged(host.paths()).probe().detail


@pytest.mark.parametrize("text", [
    "planetexpress garbage\n",                         # cannot be parsed with confidence
    "FOO ALL=(ALL) ALL\n",                              # an alias that is never defined
    "+netgroup ALL=(ALL) ALL\n",                        # a netgroup cannot be resolved here
    "planetexpress ALL ALL = (root) /bin/sh\n",         # too many fields before the =
    "just some words without structure\n",
])
def test_sudoers_lines_that_cannot_be_parsed_fail_closed(host, text):
    (host.etc / "sudoers.d" / "x").write_text(text)
    probe = SudoersManaged(host.paths()).probe()
    assert not probe.ok and "unparseable" in probe.detail


def test_sudoers_rules_for_other_people_through_aliases_still_pass(host):
    _add_session_accounts(host)
    (host.etc / "sudoers.d" / "x").write_text(
        "User_Alias ADMINS = chris, bob : OPS = alice\nHost_Alias H = a, b\nCmnd_Alias C = /bin/ls\n"
        "ADMINS H = (root) C\nOPS ALL=(ALL) ALL\nchris web1, web2 = (root) /bin/sh\n")
    probe = SudoersManaged(host.paths()).probe()
    assert probe.ok, probe.detail


def test_sudoers_repair_comments_out_a_multi_line_rule_and_an_alias_rule(host):
    (host.etc / "sudoers.d" / "x").write_text("User_Alias A = planetexpress\nA ALL=(root) \\\n  NOPASSWD: /bin/sh\n")
    step = SudoersManaged(host.paths())
    step.repair(MemoryJournal())
    assert step.probe().ok


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


@pytest.mark.parametrize("flags,safe", [
    ("--tlsverify --tlscacert=CA --tlscert=/c --tlskey=/k", True),
    ("--tlsverify=true --tlscacert CA --tlscert /c --tlskey /k", True),
    ("--tlsverify=1 --tlscacert=CA --tlscert=/c --tlskey=/k", True),
    ("--tlsverify=false --tlscacert=CA --tlscert=/c --tlskey=/k", False),
    ("--tlsverify=0 --tlscacert=CA --tlscert=/c --tlskey=/k", False),
    ("--tlsverify", False),                                          # no CA to verify clients against
    ("--tlsverify --tlscacert=", False),
    ("--tlsverify --tlscacert=CA", False),                           # no server cert or key
    ("--tlsverify --tlscacert=CA --tlscert=/c", False),
    ("--tlsverify --tlscacert=/nonexistent/ca.pem --tlscert=/c --tlskey=/k", False),     # a CA that is not there
    ("--tlscacert=CA --tlscert=/c --tlskey=/k", False),             # a CA but no verification
    ("--tlsverify=false --tlsverify --tlscacert=CA --tlscert=/c --tlskey=/k", True),     # the last one wins
    ("--tlsverify --tlsverify=false --tlscacert=CA --tlscert=/c --tlskey=/k", False),
    ("--tlsverify=banana --tlscacert=CA --tlscert=/c --tlskey=/k", False),
])
def test_docker_tcp_exec_start_tlsverify_is_parsed_not_substring_matched(host, flags, safe):
    ca = _tls_files(host)
    _unit(host, "docker.service", f"[Service]\nExecStart=/usr/bin/dockerd -H tcp://0.0.0.0:2376 {flags.replace('CA', str(ca))}\n")
    assert DockerTcpExposure(host.paths()).probe().ok is safe, flags


def test_tls_flags_on_exec_start_pre_do_not_vouch_for_a_tcp_exec_start(host):
    ca = _tls_files(host)
    _unit(host, "docker.service", "[Service]\nExecStartPre=/bin/true --tlsverify --tlscacert=%s --tlscert=/c --tlskey=/k\n"
          "ExecStart=/usr/bin/dockerd -H tcp://0.0.0.0:2376\n" % ca)
    assert not DockerTcpExposure(host.paths()).probe().ok


def test_a_symlinked_socket_drop_in_is_scanned_as_a_socket_unit(host):
    real = host.tmp / "elsewhere"
    real.mkdir()
    (real / "override.conf").write_text("[Socket]\nListenStream=0.0.0.0:2375\n")
    d = host.tmp / "systemd" / "docker.socket.d"
    d.mkdir(parents=True)
    (d / "override.conf").symlink_to(real / "override.conf")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "ListenStream" in probe.detail


def test_a_dangling_unit_symlink_fails_closed(host):
    wants = host.tmp / "systemd" / "sockets.target.wants"
    wants.mkdir(parents=True)
    (wants / "docker.socket").symlink_to(host.tmp / "gone" / "docker.socket")
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and probe.blocked


def test_a_stock_wants_symlink_to_the_real_unit_is_followed_once_and_judged(host):
    real = host.tmp / "usr-lib"
    real.mkdir()
    (real / "docker.socket").write_text("[Socket]\nListenStream=/run/docker.sock\n")
    wants = host.tmp / "systemd" / "sockets.target.wants"
    wants.mkdir(parents=True)
    (wants / "docker.socket").symlink_to(real / "docker.socket")
    assert DockerTcpExposure(host.paths()).probe().ok
    (real / "docker.socket").write_text("[Socket]\nListenStream=0.0.0.0:2375\n")      # the link target is what is judged
    assert not DockerTcpExposure(host.paths()).probe().ok


def test_a_masked_unit_is_skipped_but_a_symlink_loop_or_a_link_to_a_directory_fails_closed(host):
    wants = host.tmp / "systemd"
    wants.mkdir(parents=True, exist_ok=True)
    (wants / "docker.service").symlink_to("/dev/null")
    assert DockerTcpExposure(host.paths()).probe().ok
    (wants / "docker.socket").symlink_to(wants / "docker.socket")                    # loop
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and probe.blocked


@pytest.mark.parametrize("exec_start", [
    "/usr/bin/dockerd -H unix:///var/run/docker.sock -H 0.0.0.0:4243",       # no scheme: docker treats it as TCP
    "/usr/bin/dockerd -H :2375",
    "/usr/bin/dockerd --host=myhost:2375",
])
def test_docker_tcp_exec_start_without_a_scheme_is_tcp(host, exec_start):
    _unit(host, "docker.service", f"[Service]\nExecStart={exec_start}\n")
    assert not DockerTcpExposure(host.paths()).probe().ok, exec_start


def test_docker_tcp_daemon_json_without_a_scheme_is_tcp(host):
    (host.etc / "docker" / "daemon.json").write_text('{"hosts": ["0.0.0.0:4243"]}\n')
    probe = DockerTcpExposure(host.paths()).probe()
    assert not probe.ok and "0.0.0.0:4243" in probe.detail


def test_docker_local_hosts_are_not_tcp(host):
    _unit(host, "docker.service", "[Service]\nExecStart=/usr/bin/dockerd -H fd:// -H unix:///run/docker.sock -H npipe:////./pipe/docker\n")
    assert DockerTcpExposure(host.paths()).probe().ok
