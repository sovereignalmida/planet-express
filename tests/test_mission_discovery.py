"""G4-F1: the protected set is fed from the host, and a required domain that cannot be established withholds Repair."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest
from checkpoint_fakes import FakeDocker
from missions_helpers import make_store
from verify_fakes import snapshot

from config_schema import MissionsConfig
from planet_express.missions.assembly import RepairWiring
from planet_express.missions.checkpoint import SystemdCheckpointProvider
from planet_express.missions.containment import FakeContainment
from planet_express.missions.discovery import (
    DiscoveredInputs,
    discover_protected_inputs,
    parse_mountinfo,
)
from planet_express.missions.resources import canon, classify
from planet_express.missions.runner import build_mission_runner
from planet_express.missions.tripwire.build import build_protected_set

MOUNTINFO = (
    "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
    "30 22 8:2 / /boot rw - ext4 /dev/sda2 rw\n"
    "40 22 8:3 / /mnt/pool rw,relatime shared:1 - xfs /dev/sdb1 rw\n"
    "41 22 0:50 / /mnt/with\\040space rw - ext4 /dev/sdc rw\n"
    "42 22 0:51 / /proc rw - proc proc rw\n"
    "43 22 0:52 / /opt/data rw - lustre srv:/data rw\n"
    "44 22 0:53 / /run/user/1000 rw - tmpfs tmpfs rw\n"
)


class Host:
    """A fake docker + filesystem. `fail` names a docker subcommand that exits non-zero."""

    def __init__(self, volumes=None, containers=None, mounts=None, fail=(), truncate=()):
        self.volumes = volumes if volumes is not None else {}
        self.containers = containers if containers is not None else {}      # name -> (image, [bind sources])
        self.fail, self.truncate, self.calls = set(fail), set(truncate), []

    def bounded(self, argv, timeout, max_bytes):
        self.calls.append(argv)
        sub = argv[1]
        if sub in self.fail:
            return 1, "", "boom", False
        trunc = sub in self.truncate
        if argv[1:3] == ["volume", "ls"]:
            return 0, "\n".join(self.volumes), "", trunc
        if argv[1:3] == ["volume", "inspect"]:
            return 0, "\n".join(f"{n}\t{self.volumes[n]}" for n in argv[5:] if self.volumes.get(n)), "", trunc
        if sub == "ps":
            return 0, "\n".join(f"{n}\t{img}" for n, (img, _) in self.containers.items()), "", trunc
        if sub == "inspect":
            mounts = [{"Type": "bind", "Source": s} for s in self.containers[argv[-1]][1]]
            return 0, json.dumps(mounts), "", trunc
        return 1, "", "unknown", False


def stacks(tmp_path, *extra):
    root = tmp_path / "stacks"
    for rel in ("media", "photos", *extra):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return str(root)


def run(tmp_path, host, cfg=None, *, mountinfo=MOUNTINFO, exists=lambda p: False, scan=None, root=None):
    root = root or stacks(tmp_path)
    kw = {"scan": scan} if scan else {}
    return discover_protected_inputs(
        cfg or MissionsConfig(), stacks_root=root, runner=lambda a, t: (1, "", ""), bounded=host.bounded,
        read_text=lambda p: mountinfo, exists=exists, **kw)


def test_mountinfo_parse_decodes_escapes_and_rejects_garbage():
    assert ("/mnt/with space", "ext4") in parse_mountinfo(MOUNTINFO)
    with pytest.raises(ValueError):
        parse_mountinfo("nonsense\n")


def test_a_clean_host_without_ingress_dns_or_backup_is_not_locked_out(tmp_path):
    got = run(tmp_path, Host())
    assert got.failures == {}
    assert got.storage_mounts == ("/mnt/pool", "/mnt/with space", "/opt/data")   # unconventional paths and fs types count; /, /boot, tmpfs do not
    assert got.volumes == () and got.ingress == () and got.dns == () and got.backup == ()


def test_discovery_finds_volumes_ingress_dns_credentials_and_backup(tmp_path):
    root = stacks(tmp_path, "traefik", "adguard", "media/secrets")
    Path(root, "media", ".env").write_text("X=1\n")
    host = Host(volumes={"pgdata": "/var/lib/docker/volumes/pgdata/_data"},
                containers={"edge": ("docker.io/library/traefik:v3", ["/opt/edge/dynamic", "/var/run/docker.sock"]),
                            "dns": ("adguard/adguardhome", ["/opt/dns/conf"]),
                            "bk": ("borgmatic/borgmatic:1", ["/opt/bk/cfg"]), "web": ("nginx", ["/opt/web"])})
    cfg = MissionsConfig(checkpoint={"backup_sources": [{"path": root, "marker": "/var/lib/pe/marker"}]})
    got = run(tmp_path, host, cfg, root=root, exists=lambda p: p == "/etc/borgmatic")
    assert got.failures == {}
    assert got.volumes == ("/var/lib/docker/volumes/pgdata/_data",)
    assert "/opt/edge/dynamic" in got.ingress and f"{root}/traefik" in got.ingress
    assert "/var/run/docker.sock" not in got.ingress and "/opt/web" not in got.ingress
    assert "/opt/dns/conf" in got.dns and f"{root}/adguard" in got.dns
    assert {f"{root}/media/.env", f"{root}/media/secrets"} <= set(got.credentials)
    assert {"/opt/bk/cfg", "/etc/borgmatic", "/var/lib/pe/marker"} <= set(got.backup)


@pytest.mark.parametrize("fail, domains", [
    ("volume", {"data_volumes"}),
    ("ps", {"ingress", "dns", "backup"}),
])
def test_a_failing_discovery_command_is_a_failure_of_what_depends_on_it(tmp_path, fail, domains):
    got = run(tmp_path, Host(volumes={"v": "/x"}, fail=[fail]))
    assert set(got.failures) == domains


def test_truncated_output_is_a_failure_not_a_partial_answer(tmp_path):
    got = run(tmp_path, Host(volumes={"v": "/x"}, truncate=["volume"]))
    assert "data_volumes" in got.failures


def test_volumes_that_exist_but_do_not_resolve_fail_closed(tmp_path):
    got = run(tmp_path, Host(volumes={"v": ""}))
    assert "data_volumes" in got.failures


def test_a_component_with_no_discoverable_path_is_a_failure(tmp_path):
    got = run(tmp_path, Host(containers={"edge": ("traefik", [])}))
    assert "ingress" in got.failures and "dns" not in got.failures


def test_unreadable_mount_table_and_stacks_tree_fail(tmp_path):
    def boom(_):
        raise OSError("denied")
    host = Host()
    got = discover_protected_inputs(MissionsConfig(), stacks_root="/nope", runner=lambda a, t: (1, "", ""),
                                    bounded=host.bounded, read_text=boom, scan=boom, exists=lambda p: False)
    assert {"data_volumes", "credentials"} <= set(got.failures)


def test_explicit_absent_acknowledgement_clears_the_failure_and_is_reported(tmp_path):
    cfg = MissionsConfig(protected_absent=["ingress", "dns", "backup", "data_volumes"])
    got = run(tmp_path, Host(fail=["ps", "volume"]), cfg)
    assert got.failures == {} and set(got.acknowledged) == {"ingress", "dns", "backup", "data_volumes"}


def test_protected_absent_rejects_unknown_domains():
    with pytest.raises(ValueError):
        MissionsConfig(protected_absent=["firewall"])


def test_a_discovered_volume_is_protected_and_protected_wins_in_the_tripwire_and_the_proxy_scope(tmp_path):
    from planet_express.missions.dockerproxy.policy import Scope
    from planet_express.missions.models import Grant
    from planet_express.missions.tripwire.signals import volume_paths
    root = stacks(tmp_path)
    got = run(tmp_path, Host(volumes={"pg": "/var/lib/docker/volumes/pg/_data"}), root=root)
    ps = build_protected_set(MissionsConfig(), stacks_root=root, **got.kwargs())
    inside = canon("path:/var/lib/docker/volumes/pg/_data/base/1")
    assert ps.covers(inside)
    assert classify(inside, [canon("path:/var/lib/docker")], ps.resources) == "protected"
    assert ("host:local/path:/var/lib/docker/volumes/pg/_data", "/var/lib/docker/volumes/pg/_data") in volume_paths(ps)
    scope = Scope.from_grant(Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"]}}),
                             protected=ps.resources, stacks_root=root)
    assert scope.classify(inside) == "protected"


# -- runner wiring ------------------------------------------------------------------------------

def wiring(stacks_root):
    docker = FakeDocker()
    provider = SystemdCheckpointProvider(SimpleNamespace(stacks_root=stacks_root), run=docker, run_bounded=docker.bounded,
                                         read_text=lambda path: (_ for _ in ()).throw(OSError(path)))
    return RepairWiring(provider=provider, scan=lambda: snapshot(), core_user="pe")


def cfg(**over):
    return MissionsConfig(enabled=True, runtime={"argv": ["/usr/bin/agent"]}, **over)


def test_runner_feeds_discovery_into_the_protected_set(tmp_path):
    store, _ = make_store(tmp_path)
    root = stacks(tmp_path)
    found = DiscoveredInputs(volumes=("/srv/vols/pg",), ingress=("/opt/edge",))
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), stacks_root=root,
                                  repair_wiring=wiring(root), discovery=lambda c, **k: found)
    assert runner.controls is not None
    assert runner.supervisor.repair_gate is runner.controls.repair_gate


def test_a_discovery_failure_withholds_repair_controls_and_records_why(tmp_path, caplog):
    store, _ = make_store(tmp_path)
    root = stacks(tmp_path)
    found = DiscoveredInputs(volumes=("/srv/vols/pg",), failures={"ingress": "docker ps exited 1"})
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), stacks_root=root,
                                  repair_wiring=wiring(root), discovery=lambda c, **k: found)
    assert runner is not None and runner.controls is None
    assert runner.supervisor.repair_gate is None and runner.supervisor.tripwire is None
    assert "ingress" in caplog.text
    events = [e for e in store.list_events() if e["kind"] == "mission.protected_discovery_incomplete"]
    assert events and "ingress" in json.dumps(events[0]["payload"])


def test_a_discovery_that_raises_also_withholds_repair(tmp_path):
    store, _ = make_store(tmp_path)
    root = stacks(tmp_path)

    def boom(c, **k):
        raise RuntimeError("kaboom")
    runner = build_mission_runner(store, cfg(), containment=FakeContainment(), stacks_root=root,
                                  repair_wiring=wiring(root), discovery=boom)
    assert runner is not None and runner.controls is None


def test_the_acknowledged_domain_lets_controls_build(tmp_path):
    store, _ = make_store(tmp_path)
    root = stacks(tmp_path)
    found = DiscoveredInputs(acknowledged=("backup",))
    runner = build_mission_runner(store, cfg(protected_absent=["backup"]), containment=FakeContainment(),
                                  stacks_root=root, repair_wiring=wiring(root), discovery=lambda c, **k: found)
    assert runner.controls is not None


def test_an_unreadable_subtree_is_a_failure_not_a_partial_scan(tmp_path):
    from planet_express.missions.discovery import default_scan
    root = Path(stacks(tmp_path, "media/inner"))
    locked = root / "media"
    (locked / "inner" / ".env").write_text("X=1\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running with privileges that ignore permissions")
        with pytest.raises(OSError):
            default_scan(str(root))
        got = discover_protected_inputs(MissionsConfig(), stacks_root=str(root), bounded=Host().bounded,
                                        read_text=lambda p: MOUNTINFO, exists=lambda p: False)
        assert "credentials" in got.failures
    finally:
        locked.chmod(0o755)


def test_a_permission_failure_probing_backup_config_is_not_absence(tmp_path):
    from planet_express.missions.discovery import _default_exists
    closed = tmp_path / "closed"
    (closed / "borg").mkdir(parents=True)
    closed.chmod(0)
    try:
        if os.access(closed, os.X_OK):
            pytest.skip("running with privileges that ignore permissions")
        with pytest.raises(PermissionError):
            _default_exists(str(closed / "borg"))
        assert _default_exists(str(tmp_path / "nothing")) is False
        got = run(tmp_path, Host(), exists=_default_exists_for(closed))
        assert "backup" in got.failures
    finally:
        closed.chmod(0o755)


def _default_exists_for(closed):
    from planet_express.missions.discovery import _default_exists
    return lambda p: _default_exists(str(closed / "borg")) if p == "/etc/borgmatic" else False


def test_an_oversized_mount_table_is_a_failure(tmp_path):
    from planet_express.missions import discovery
    big = tmp_path / "mountinfo"
    big.write_text("x" * (discovery.MAX_OUTPUT + 1))
    with pytest.raises(OSError):
        discovery._default_read(str(big))
    got = discover_protected_inputs(MissionsConfig(), stacks_root=stacks(tmp_path), bounded=Host().bounded,
                                    mountinfo=str(big), exists=lambda p: False)
    assert "data_volumes" in got.failures
