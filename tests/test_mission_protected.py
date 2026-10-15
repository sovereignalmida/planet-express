"""Protected domains: the built-in default set covers every brief 5.3 domain and cannot be emptied silently."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.missions.protected import (
    DEFAULT_PROTECTED,
    REQUIRED_DOMAINS,
    Discovery,
    OverrideRefused,
    ProtectedOverride,
    build_protected,
)
from planet_express.missions.resources import canon

BRIEF_53 = {"ingress", "dns", "credentials", "firewall", "backup", "sudoers", "docker_daemon", "data_volumes",
            "packages", "reboot", "publishing", "vcs", "cross_host"}


def test_every_brief_domain_has_a_default_and_removing_one_fails_ci():
    # If this fails someone removed a domain from the built-in set: that needs an explicit, reviewed decision.
    assert set(REQUIRED_DOMAINS) == BRIEF_53
    assert set(DEFAULT_PROTECTED) | {"data_volumes", "cross_host"} >= BRIEF_53
    for domain, ids in DEFAULT_PROTECTED.items():
        assert ids, domain


def test_empty_site_list_still_yields_every_domain(tmp_path):
    got = build_protected(site=[], discovery=Discovery(stacks_root=str(tmp_path)))
    for domain in ("ingress", "dns", "credentials", "firewall", "backup", "sudoers", "docker_daemon", "packages",
                   "reboot", "publishing", "vcs"):
        assert got.domains[domain], domain
    assert canon("path:/etc/sudoers") in got.resources
    assert canon("path:/etc/docker/daemon.json") in got.resources
    assert canon("path:/var/run/docker.sock") in got.resources
    assert canon("detector:boot") in got.resources
    assert canon("detector:packages") in got.resources
    assert canon("detector:vcs") in got.resources
    assert canon("detector:listeners") in got.resources


def test_discovery_adds_concrete_resources_and_data_volumes(tmp_path):
    d = Discovery(stacks_root=str(tmp_path / "stacks"), storage_mounts=["/mnt/pool"],
                  volumes=["/var/lib/docker/volumes/db/_data"],
                  ingress=["/opt/traefik/dynamic"], backup=["/etc/restic/jobs"],
                  cross_host=[("nas", "/etc/exports"), ("router", None)], live_trees=["/home/x/repo"])
    got = build_protected(site=["service:media/traefik"], discovery=d)
    assert canon("mount:/mnt/pool") in got.domains["data_volumes"]
    assert canon("path:/var/lib/docker/volumes/db/_data") in got.domains["data_volumes"]
    assert canon("path:/opt/traefik/dynamic") in got.domains["ingress"]
    assert canon("path:/etc/restic/jobs") in got.domains["backup"]
    assert canon("system:nas") in got.domains["cross_host"] and canon("system:router") in got.domains["cross_host"]
    assert canon("service:media/traefik") in got.resources     # the operator's own additions are kept
    assert got.cross_host_paths == {"nas": "/etc/exports"}
    assert got.live_trees == ("/home/x/repo",)


def test_storage_paths_never_swallow_the_stacks_root():
    d = Discovery(stacks_root="/srv/stacks", storage_mounts=["/srv", "/mnt/data"])
    got = build_protected(site=[], discovery=d)
    assert canon("mount:/mnt/data") in got.resources
    assert canon("mount:/srv") not in got.resources     # it contains the stacks root: carved out, and reported
    assert canon("mount:/srv") in got.carved_out


def test_removing_a_default_needs_a_recorded_override():
    d = Discovery(stacks_root="/srv/stacks")
    ov = ProtectedOverride(resource="path:/etc/hosts", reason="managed elsewhere", operator="operator:chris", at=5)
    got = build_protected(site=[], discovery=d, overrides=[ov])
    assert canon("path:/etc/hosts") not in got.resources
    assert got.overrides == (ov,)
    assert canon("path:/etc/resolv.conf") in got.resources


@pytest.mark.parametrize("bad", [
    dict(resource="path:/etc/hosts", reason="", operator="operator:c", at=1),
    dict(resource="path:/etc/hosts", reason="x", operator="chris", at=1),
    dict(resource="path:/etc/hosts", reason="x", operator="operator:", at=1),
    dict(resource="path:/not/in/the/set", reason="x", operator="operator:c", at=1),
])
def test_bad_overrides_are_refused(bad):
    with pytest.raises(OverrideRefused):
        build_protected(site=[], discovery=Discovery(stacks_root="/s"), overrides=[ProtectedOverride(**bad)])


def test_override_events_are_emitted():
    seen = []
    ov = ProtectedOverride(resource="detector:packages", reason="we patch by hand", operator="operator:c", at=1)
    build_protected(site=[], discovery=Discovery(stacks_root="/s"), overrides=[ov], record=seen.append)
    assert seen and seen[0]["override"] == canon("detector:packages")


def test_bad_site_resource_is_refused_not_ignored():
    with pytest.raises(ValueError):
        build_protected(site=["nonsense"], discovery=Discovery(stacks_root="/s"))
