"""group_routers() — the pure shaping behind the Network tab's routing matrix.

No I/O here: fetch_traefik_routers() hands over a list of dicts and this decides zones,
tags and LAN merges. Everything the template must not try to do with a regex.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_scruffy_net


def _router(name, host=None, status="enabled", rule=None, service="svc"):
    return {"name": name, "service": service, "status": status,
            "rule": rule if rule is not None else (f"Host(`{host}`)" if host else "PathPrefix(`/x`)")}


def test_routers_group_by_registrable_domain():
    grouped = casa_scruffy_net.group_routers([
        _router("immich@docker", "immich.casalan.com"),
        _router("wiki@docker", "wiki.casalan.com"),
        _router("blog@docker", "blog.chrisalmida.com"),
    ])
    assert [zone["name"] for zone in grouped["zones"]] == ["casalan.com", "chrisalmida.com"]
    assert grouped["names"] == 3


def test_a_router_with_no_host_rule_lands_in_the_catch_all_bucket_last():
    """A pure PathPrefix/Method/Headers match has no zone to sort into. The bucket sorts
    last however big it gets: it is a leftovers pile, not a place."""
    grouped = casa_scruffy_net.group_routers([
        _router("api@external", rule="PathPrefix(`/api`)"),
        _router("dash@external", rule="PathPrefix(`/dash`)"),
        _router("one@docker", "one.casalan.com"),
    ])
    assert grouped["zones"][-1]["name"] == "internal / external"
    assert grouped["zones"][-1]["count"] == 2


def test_a_lan_twin_merges_into_its_sibling():
    grouped = casa_scruffy_net.group_routers([
        _router("subwave-api@docker", "subwave-api.casaalmida.com"),
        _router("subwave-api-lan@docker", "subwave-api-lan.casaalmida.com"),
    ])
    pills = grouped["zones"][0]["items"]
    assert len(pills) == 1
    assert pills[0]["name"] == "subwave-api" and pills[0]["lan"] is True
    assert grouped["merged"] == 1
    assert grouped["total"] == 2 and grouped["names"] == 1


def test_an_orphan_lan_router_keeps_its_own_pill():
    """Dropping a router is worse than showing a twin: an orphan -lan is a real route and
    the matrix is the only place it is listed."""
    grouped = casa_scruffy_net.group_routers([
        _router("orphan-lan@docker", "orphan-lan.casaalmida.com"),
    ])
    assert [pill["name"] for pill in grouped["zones"][0]["items"]] == ["orphan-lan"]
    assert grouped["merged"] == 0


def test_a_merged_twin_keeps_the_sibling_findable_by_its_lan_hostname():
    """The hostname line is gone from the pill face, so filtering is the only way to find a
    router by host. Merging must not make the LAN hostname unsearchable."""
    grouped = casa_scruffy_net.group_routers([
        _router("subwave-api@docker", "subwave-api.casaalmida.com"),
        _router("subwave-api-lan@docker", "subwave-api-lan.casaalmida.com"),
    ])
    assert "subwave-api-lan" in grouped["zones"][0]["items"][0]["filter_text"]


def test_a_down_twin_makes_the_merged_pill_down():
    grouped = casa_scruffy_net.group_routers([
        _router("api@docker", "api.casalan.com"),
        _router("api-lan@docker", "api-lan.casalan.com", status="disabled"),
    ])
    assert grouped["zones"][0]["items"][0]["down"] is True
    assert grouped["down"] == 1


def test_docker_is_untagged_and_other_providers_are_not():
    grouped = casa_scruffy_net.group_routers([
        _router("a@docker", "a.casalan.com"),
        _router("b@file", "b.casalan.com"),
        _router("c@someplugin", "c.casalan.com"),
    ])
    tags = {pill["name"]: pill["tag"] for pill in grouped["zones"][0]["items"]}
    assert tags == {"a": "", "b": "FILE", "c": "EXT"}


def test_a_down_router_sorts_to_the_front_of_its_zone():
    grouped = casa_scruffy_net.group_routers([
        _router("aaa@docker", "aaa.casalan.com"),
        _router("zzz@docker", "zzz.casalan.com", status="disabled"),
    ])
    assert [pill["name"] for pill in grouped["zones"][0]["items"]] == ["zzz", "aaa"]


def test_no_router_is_ever_lost():
    routers = [
        _router("one@docker", "one.casalan.com"),
        _router("one-lan@docker", "one-lan.casalan.com"),
        _router("two@file", "two.casaalmida.com"),
        _router("three@external", rule="PathPrefix(`/three`)"),
        _router("bare@docker", rule="Method(`GET`)"),
    ]
    grouped = casa_scruffy_net.group_routers(routers)
    assert grouped["names"] + grouped["merged"] == len(routers)
    assert grouped["total"] == len(routers)


def test_an_ip_literal_host_has_no_zone():
    """Taking the last two labels of 192.168.1.94 would file it under a domain called 1.94."""
    grouped = casa_scruffy_net.group_routers([_router("box@docker", "192.168.1.94")])
    assert grouped["zones"][0]["name"] == "internal / external"


def test_a_malformed_router_entry_does_not_raise():
    grouped = casa_scruffy_net.group_routers([None, "nonsense", _router("ok@docker", "ok.casalan.com")])
    assert grouped["names"] == 1


def test_empty_input_returns_no_zones():
    assert casa_scruffy_net.group_routers([]) == {
        "zones": [], "total": 0, "names": 0, "merged": 0, "down": 0}
