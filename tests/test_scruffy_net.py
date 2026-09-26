"""group_routers() — the pure shaping behind the Network tab's routing matrix.

No I/O here: fetch_traefik_routers() hands over a list of dicts and this decides zones,
tags and LAN merges. Everything the template must not try to do with a regex.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import pytest

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


def test_two_disabled_twins_count_as_two_routers_down():
    """A merged pill can stand for two routers, so "is this pill down" and "how many routers
    behind it are down" are different questions. The header asks the second one."""
    grouped = casa_scruffy_net.group_routers([
        _router("api@docker", "api.casalan.com", status="disabled"),
        _router("api-lan@docker", "api-lan.casalan.com", status="disabled"),
    ])
    assert grouped["down"] == 2
    assert grouped["zones"][0]["down"] == 2
    assert grouped["names"] == 1 and grouped["total"] == 2


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
    assert sum(zone["count"] for zone in grouped["zones"]) == len(routers)


def test_a_twin_on_a_different_domain_still_merges():
    """The normal shape on this host: navidrome on the public domain, navidrome-lan on the
    LAN one. Matching twins inside a zone would only ever have merged same-domain pairs."""
    grouped = casa_scruffy_net.group_routers([
        _router("navidrome@docker", "navidrome.casaalmida.com"),
        _router("navidrome-lan@docker", "navidrome.casalan.com"),
    ])
    pills = [pill for zone in grouped["zones"] for pill in zone["items"]]
    assert len(pills) == 1
    assert pills[0]["name"] == "navidrome" and pills[0]["lan"] is True
    assert grouped["merged"] == 1 and grouped["total"] == 2 and grouped["names"] == 1


def test_two_providers_may_share_a_short_name():
    """Traefik allows api@docker and api@file. Keying on the short name dropped one."""
    grouped = casa_scruffy_net.group_routers([
        _router("api@docker", "api.casalan.com"),
        _router("api@file", "api.casalan.com"),
    ])
    pills = grouped["zones"][0]["items"]
    assert len(pills) == 2
    assert {pill["tag"] for pill in pills} == {"", "FILE"}
    assert grouped["names"] == 2 and grouped["total"] == 2


def test_an_unrelated_lan_router_does_not_steal_another_providers_sibling():
    """x-lan@file must not merge into x@docker: they are different routes."""
    grouped = casa_scruffy_net.group_routers([
        _router("x@docker", "x.casalan.com"),
        _router("x-lan@file", "x-lan.casalan.com"),
    ])
    assert grouped["merged"] == 0 and grouped["names"] == 2


def test_a_zone_counts_routers_not_displayed_names():
    """The zone label says "n routers". With a twin merged, the displayed pill count and the
    router count differ, and the label must not quietly report the smaller one."""
    grouped = casa_scruffy_net.group_routers([
        _router("one@docker", "one.casalan.com"),
        _router("one-lan@docker", "one-lan.casalan.com"),
        _router("two@docker", "two.casalan.com"),
    ])
    zone = grouped["zones"][0]
    assert zone["count"] == 3      # routers
    assert zone["names"] == 2      # pills on screen
    assert grouped["total"] == 3 and grouped["names"] == 2


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


# ── container_urls (T46.1) ───────────────────────────────────────────────────────
# Every rule below is copied from the live host's Traefik API rather than invented.
# group_routers() shipped with tests that passed against fixtures tidier than the host,
# and a reviewer found three bugs the suite could not see; these shapes are the real ones.

def _live(name, rule, service=None, status="enabled", entry_points=("websecure",)):
    return {"name": name, "rule": rule, "status": status,
            "service": service or name.split("@")[0], "entry_points": list(entry_points)}


# A route is joined to its container through the backend IP Traefik reports, because the
# router's service NAME is a Traefik label and on the live host it matches a compose service
# for only 27 of 64 routers. These helpers build that join the way the host presents it.
def _svc(name, ip, provider="docker"):
    return {"name": f"{name}@{provider}", "provider": provider, "hosts": [ip]}


def _ctr(name, ip):
    """One entry of the {container: [ip]} map core serves."""
    return {name: [ip]}


def _urls(routers, services=None, addresses=None, lan_domain="casalan.com"):
    """container_urls with a one-container-per-router-service join built automatically, so a
    test about rules does not have to spell out plumbing it is not testing."""
    if services is None or addresses is None:
        services, addresses = [], {}
        for index, router in enumerate(routers):
            if not isinstance(router, dict):
                continue
            service = str(router.get("service", "")).split("@")[0]
            ip = f"172.20.0.{index + 10}"
            name = f"CASA_{service.upper().replace('-', '_')}"
            if not any(item["name"] == f"{service}@docker" for item in services):
                services.append(_svc(service, ip))
                addresses[name] = [ip]
    return casa_scruffy_net.container_urls(routers, services, addresses,
                                           lan_domain=lan_domain)


def test_a_multi_host_router_yields_one_url_per_host_with_opposite_zones():
    """This, not the -lan twin, is how most services get both addresses: 15 of the live
    host's 70 routers are a single router serving a LAN host and a public one."""
    urls = _urls([
        _live("actual@docker", "Host(`actual.casalan.com`) || Host(`actual.casaalmida.com`)"),
    ])
    assert urls["CASA_ACTUAL"] == [
        {"href": "https://actual.casalan.com", "zone": "lan"},
        {"href": "https://actual.casaalmida.com", "zone": "public"},
    ]


def test_lan_sorts_first_because_it_is_the_default_target():
    urls = _urls([
        _live("x@docker", "Host(`x.casaalmida.com`) || Host(`x.casalan.com`)"),
    ])
    assert [link["zone"] for link in urls["CASA_X"]] == ["lan", "public"]


def test_a_lan_twin_and_its_sibling_collect_onto_one_container():
    """They are separate routers pointing at different hosts, but the same container."""
    urls = _urls([
        _live("subwave-web@docker", "Host(`radio.casaalmida.com`)", service="subwave-web"),
        _live("subwave-web-lan@docker", "Host(`radio.casalan.com`)", service="subwave-web"),
    ])
    assert urls["CASA_SUBWAVE_WEB"] == [
        {"href": "https://radio.casalan.com", "zone": "lan"},
        {"href": "https://radio.casaalmida.com", "zone": "public"},
    ]


def test_the_same_host_twice_is_not_two_links():
    urls = _urls([
        _live("a@docker", "Host(`same.casalan.com`)", service="dup"),
        _live("b@docker", "Host(`same.casalan.com`)", service="dup"),
    ])
    assert urls["CASA_DUP"] == [{"href": "https://same.casalan.com", "zone": "lan"}]


def test_an_api_or_stream_path_is_never_a_launch_link():
    """subwave-api is radio.casaalmida.com/api (JSON) and subwave-stream is /stream.mp3 (a
    raw audio stream). A launch button onto either is worse than no button."""
    urls = _urls([
        _live("subwave-api@docker", "Host(`radio.casaalmida.com`) && PathPrefix(`/api`)"),
        _live("subwave-stream@docker", "Host(`radio.casalan.com`) && PathPrefix(`/stream.mp3`)"),
    ])
    assert urls == {}


def test_an_asset_route_is_not_a_launch_link():
    urls = _urls([
        _live("adventurelog-admin@docker",
              "(Host(`travel.casalan.com`) || Host(`travel.casaalmida.com`)) && "
              "(PathPrefix(`/media`) || PathPrefix(`/admin`) || PathPrefix(`/static`))"),
    ])
    assert urls == {}


def test_a_negated_path_group_is_skipped_and_that_costs_one_real_link():
    """adventurelog's host root IS launchable -- the negation only excludes asset paths. It is
    skipped anyway, because recovering it needs a parser that reasons about negation, and the
    config `links:` escape hatch covers it explicitly instead. Pinned so the decision is
    visible if anyone wonders why travel has no button."""
    urls = _urls([
        _live("adventurelog@docker",
              "(Host(`travel.casalan.com`) || Host(`travel.casaalmida.com`)) && "
              "!(PathPrefix(`/media`) || PathPrefix(`/static`))"),
    ])
    assert urls == {}


def test_traefiks_own_internal_routers_emit_nothing():
    urls = _urls([
        _live("api@internal", "PathPrefix(`/api`)", entry_points=("traefik",)),
        _live("dashboard@internal", "PathPrefix(`/`)", entry_points=("traefik",)),
        _live("web-to-websecure@internal", "HostRegexp(`^.+$`)", entry_points=("web",)),
    ])
    assert urls == {}


def test_the_scheme_comes_from_the_entrypoint_not_from_an_assumption():
    urls = _urls([
        _live("secure@docker", "Host(`a.casalan.com`)", entry_points=("websecure",)),
        _live("plain@docker", "Host(`b.casalan.com`)", entry_points=("web",)),
    ])
    assert urls["CASA_SECURE"][0]["href"].startswith("https://")
    assert urls["CASA_PLAIN"][0]["href"].startswith("http://")


def test_an_unknown_entrypoint_is_not_a_guess():
    urls = _urls([
        _live("odd@docker", "Host(`c.casalan.com`)", entry_points=("something-new",)),
    ])
    assert urls == {}


def test_a_disabled_router_offers_no_link():
    urls = _urls([
        _live("down@docker", "Host(`d.casalan.com`)", status="disabled"),
    ])
    assert urls == {}


def test_malformed_entries_do_not_raise():
    urls = _urls([None, "nonsense", _live("ok@docker", "Host(`e.casalan.com`)")])
    assert list(urls) == ["CASA_OK"]


def test_a_declared_link_wins_over_a_derived_one():
    """The operator wrote it down precisely because the route could not be read honestly.
    Merging it with a guess would defeat the point of declaring it."""
    derived = {"travel": [{"href": "https://wrong.casalan.com", "zone": "lan"}]}
    merged = casa_scruffy_net.merge_declared_links(
        derived, [{"name": "travel", "href": "https://travel.casalan.com", "zone": "lan"}])
    assert merged["travel"] == [{"href": "https://travel.casalan.com", "zone": "lan"}]


def test_a_declared_link_reaches_a_service_with_no_router_at_all():
    merged = casa_scruffy_net.merge_declared_links(
        {}, [{"name": "homeassistant", "href": "http://192.168.1.20:8123"}])
    assert merged["homeassistant"] == [{"href": "http://192.168.1.20:8123", "zone": "lan"}]


def test_a_malformed_declared_link_is_ignored_rather_than_rendered():
    merged = casa_scruffy_net.merge_declared_links(
        {"a": [{"href": "https://a.casalan.com", "zone": "lan"}]},
        [None, {}, {"name": "b"}, {"href": "https://nameless"}])
    assert list(merged) == ["a"]


# ── service_containers: the join that replaced the name match (T46.1 gate) ──────
# Keyed on the router's service NAME, this joined 27 of the live host's 64 router services.
# The name is a Traefik label, not a compose service, and compose service names are not
# unique across projects either. Every case below is from `docker ps` and the Traefik API.

def test_the_service_name_is_not_the_compose_service_name():
    """`audio@docker` is served by Traefik service `audiobookshelf-media`, and the container
    behind it is CASA_ABS, whose compose service is `audiobookshelf`. Three different names
    for one thing; only the address ties them together."""
    owners = casa_scruffy_net.service_containers(
        [_svc("audiobookshelf-media", "172.20.0.31")],
        _ctr("CASA_ABS", "172.20.0.31"))
    assert owners == {"audiobookshelf-media@docker": "CASA_ABS"}


@pytest.mark.parametrize(("service", "container"), [
    ("actual", "CASA_ACTUAL"),            # compose service actual_server
    ("adguard", "CASA_ADGUARD"),          # compose service adguardhome
    ("wiki", "CASA_WIKIGO"),              # compose service wiki-go
    ("sabnzbd", "CASA_SABNZBD"),          # compose service SabNZBD, different case
    ("billarr", "CASA_BILLARR"),          # compose service frontend
    ("immich-server-media", "CASA_IMMICH_SERVER"),
])
def test_every_live_name_shape_still_joins(service, container):
    owners = casa_scruffy_net.service_containers(
        [_svc(service, "172.20.0.51")], _ctr(container, "172.20.0.51"))
    assert owners == {f"{service}@docker": container}


def test_two_projects_sharing_a_service_name_do_not_collide():
    """CASA_SUBWAVE_WEB and CASA_KARA_KEEP are both compose service `web`. Keyed by name,
    one service's URL would hang on the other's container."""
    owners = casa_scruffy_net.service_containers(
        [_svc("subwave-web", "172.20.0.41"), _svc("kara", "172.20.0.42")],
        {"CASA_SUBWAVE_WEB": ["172.20.0.41"], "CASA_KARA_KEEP": ["172.20.0.42"]})
    assert owners == {"subwave-web@docker": "CASA_SUBWAVE_WEB",
                      "kara@docker": "CASA_KARA_KEEP"}


def test_a_route_to_another_machine_is_not_a_container():
    """opnsense is 192.168.1.1, unraid is .171, adguard-secondary is .25. File-provider
    routes to other hosts must not be attached to anything here."""
    owners = casa_scruffy_net.service_containers(
        [_svc("opnsense", "192.168.1.1", provider="file"),
         _svc("unraid", "192.168.1.171", provider="file")],
        _ctr("CASA_ACTUAL", "172.20.0.51"))
    assert owners == {}


def test_a_host_networked_service_is_not_claimed_either():
    """jellyfin, plex, qbit and planetexpress are routed to 192.168.1.94, the host itself.
    They are containers, but nothing here can say which -- and the config `links:` escape
    hatch is exactly what they are for."""
    owners = casa_scruffy_net.service_containers(
        [_svc("plex", "192.168.1.94", provider="file")],
        _ctr("CASA_ACTUAL", "172.20.0.51"))
    assert owners == {}


def test_an_address_two_containers_claim_identifies_neither():
    owners = casa_scruffy_net.service_containers(
        [_svc("ambiguous", "172.20.0.9")],
        {"CASA_ONE": ["172.20.0.9"], "CASA_TWO": ["172.20.0.9"]})
    assert owners == {}


def test_a_service_spread_over_two_containers_is_not_one_container():
    owners = casa_scruffy_net.service_containers(
        [{"name": "scaled@docker", "provider": "docker", "hosts": ["172.20.0.1", "172.20.0.2"]}],
        {"CASA_ONE": ["172.20.0.1"], "CASA_TWO": ["172.20.0.2"]})
    assert owners == {}


def test_a_container_on_several_networks_is_found_by_any_of_its_addresses():
    owners = casa_scruffy_net.service_containers(
        [_svc("tracearr", "172.20.0.20")],
        {"CASA_TRACEARR": ["172.22.0.9", "172.20.0.20"]})
    assert owners == {"tracearr@docker": "CASA_TRACEARR"}


@pytest.mark.parametrize(("services", "addresses"), [
    (None, None), ([], {}), ([None, "x"], {"": ["1"], "CASA_X": None}),
    ([{"hosts": None}], {"CASA_X": None}),
    ([{"name": "a@docker", "hosts": [None, 5]}], {"CASA_X": ["172.20.0.1"]}),
])
def test_a_malformed_join_does_not_raise(services, addresses):
    assert casa_scruffy_net.service_containers(services, addresses) == {}


def test_a_router_naming_a_service_in_another_provider_is_honoured():
    """traefik@docker is served by api@internal -- four live routers cross providers, and
    appending the router's own provider would look for a service that does not exist."""
    owners = casa_scruffy_net.service_containers(
        [{"name": "api@internal", "provider": "internal", "hosts": ["172.20.0.2"]}],
        _ctr("CASA_TRAEFIK", "172.20.0.2"))
    urls = casa_scruffy_net.container_urls(
        [_live("traefik@docker", "Host(`traefik.casalan.com`)", service="api@internal")],
        [{"name": "api@internal", "provider": "internal", "hosts": ["172.20.0.2"]}],
        _ctr("CASA_TRAEFIK", "172.20.0.2"), lan_domain="casalan.com")
    assert owners == {"api@internal": "CASA_TRAEFIK"}
    assert urls == {"CASA_TRAEFIK": [{"href": "https://traefik.casalan.com", "zone": "lan"}]}


def test_a_route_with_no_container_behind_it_yields_no_link():
    """A missing link is better than a wrong one."""
    urls = casa_scruffy_net.container_urls(
        [_live("opnsense@file", "Host(`opnsense.casalan.com`)")],
        [_svc("opnsense", "192.168.1.1", provider="file")],
        _ctr("CASA_ACTUAL", "172.20.0.51"), lan_domain="casalan.com")
    assert urls == {}


def test_a_declared_link_overrides_the_derived_one_for_that_container():
    """Both sides are keyed by container. They were not when the join moved off service
    names, and a declared link under the old key matched nothing at all."""
    derived = casa_scruffy_net.container_urls(
        [_live("advlog@docker", "Host(`travel.casalan.com`)", service="adventurelog")],
        [_svc("adventurelog", "172.20.0.61")],
        _ctr("CASA_ADVLOG_FRONTEND", "172.20.0.61"), lan_domain="casalan.com")
    assert list(derived) == ["CASA_ADVLOG_FRONTEND"]

    merged = casa_scruffy_net.merge_declared_links(derived, [
        {"name": "CASA_ADVLOG_FRONTEND", "href": "https://travel.casalan.com", "zone": "lan"},
    ])
    assert merged == {"CASA_ADVLOG_FRONTEND": [
        {"href": "https://travel.casalan.com", "zone": "lan"}]}


def test_a_declared_link_covers_a_container_no_route_can_reach():
    """plex, jellyfin, qbit and planetexpress are routed to 192.168.1.94, the host itself,
    which identifies no container. The escape hatch is all they have."""
    merged = casa_scruffy_net.merge_declared_links({}, [
        {"name": "CASA_PLEX", "href": "https://plex.casalan.com"},
    ])
    assert merged == {"CASA_PLEX": [{"href": "https://plex.casalan.com", "zone": "lan"}]}


def test_a_service_half_somewhere_else_claims_nothing():
    """A balancer with one local backend and one remote is not "backed by one container".
    Resolving the half that happens to be local would hang its link on that container."""
    owners = casa_scruffy_net.service_containers(
        [{"name": "mixed@file", "provider": "file",
          "hosts": ["172.20.0.51", "192.168.1.171"]}],
        _ctr("CASA_ACTUAL", "172.20.0.51"))
    assert owners == {}


def test_a_service_with_a_stale_backend_alongside_a_live_one_claims_nothing():
    """Mid-rollout Traefik can list the old container's address and the new one's."""
    owners = casa_scruffy_net.service_containers(
        [{"name": "rolling@docker", "provider": "docker",
          "hosts": ["172.20.0.51", "172.20.0.99"]}],
        _ctr("CASA_ACTUAL", "172.20.0.51"))
    assert owners == {}


def test_every_backend_pointing_at_the_same_container_still_resolves():
    owners = casa_scruffy_net.service_containers(
        [{"name": "twonets@docker", "provider": "docker",
          "hosts": ["172.20.0.20", "172.22.0.9"]}],
        {"CASA_TRACEARR": ["172.22.0.9", "172.20.0.20"]})
    assert owners == {"twonets@docker": "CASA_TRACEARR"}


def test_a_service_with_no_backends_claims_nothing():
    assert casa_scruffy_net.service_containers(
        [{"name": "empty@docker", "provider": "docker", "hosts": []}],
        _ctr("CASA_ACTUAL", "172.20.0.51")) == {}


def test_the_address_lookup_shares_one_deadline_across_both_commands(monkeypatch):
    """Two independent timeouts can add up past the dashboard's 5s deadline and leave an RPC
    worker busy on a reply nobody is waiting for. Same rule query.container already follows."""
    from planet_express.execution import actions

    budgets = []
    clock = [0.0]

    def fake_run_argv(argv, timeout=None, **kwargs):
        budgets.append(timeout)
        clock[0] += 3.5                       # docker is slow but does answer
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_ACTUAL\n", ""
        return 0, "/CASA_ACTUAL\t172.20.0.51\n", ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    monkeypatch.setattr(actions.time, "monotonic", lambda: clock[0])

    actions.container_addresses(timeout=4)

    assert budgets[0] == 4
    assert budgets[1] < 1            # what is left of the 4s, not another 4s


# ── same_backends: the stable sample around the address read ────────────────────

def test_an_unchanged_backend_map_is_the_same_moment():
    before = [_svc("actual", "172.20.0.51"), _svc("adguard", "172.20.0.4")]
    after = [_svc("adguard", "172.20.0.4"), _svc("actual", "172.20.0.51")]   # order is not news
    assert casa_scruffy_net.same_backends(before, after)


@pytest.mark.parametrize("after", [
    [_svc("actual", "172.20.0.99")],                       # recreated with a new address
    [_svc("actual", "172.20.0.51"), _svc("new", "172.20.0.7")],
    [],                                                     # Traefik went away mid-read
    [{"name": "actual@docker", "hosts": ["172.20.0.51", "172.20.0.52"]}],
])
def test_a_backend_that_moved_is_not_the_same_moment(after):
    assert not casa_scruffy_net.same_backends([_svc("actual", "172.20.0.51")], after)


def test_same_backends_does_not_raise_on_rubbish():
    assert casa_scruffy_net.same_backends(None, None)
    assert casa_scruffy_net.same_backends([None, "x"], [])
    assert casa_scruffy_net.same_backends([{"name": "a", "hosts": None}], [{"name": "a"}])


# ── the pill as a link (T46.4) ──────────────────────────────────────────────────

def test_a_pill_carries_the_same_links_the_container_join_would():
    """One rule for "is this launchable", so the matrix and the stack drawer can never
    disagree. group_routers and container_urls both go through router_links()."""
    zones = casa_scruffy_net.group_routers(
        [_live("actual@docker", "Host(`actual.casalan.com`) || Host(`actual.casaalmida.com`)")],
        lan_domain="casalan.com")
    pill = zones["zones"][0]["items"][0]
    assert [link["href"] for link in pill["links"]] == [
        "https://actual.casalan.com", "https://actual.casaalmida.com"]


def test_a_merged_twin_keeps_both_hosts_on_the_pill():
    """The merge used to drop the twin entirely. Five of this host's services reach both
    ways only through a twin, so that would have thrown away their only LAN address."""
    zones = casa_scruffy_net.group_routers([
        _live("subwave-web@docker", "Host(`radio.casaalmida.com`)", service="subwave-web"),
        _live("subwave-web-lan@docker", "Host(`radio.casalan.com`)", service="subwave-web"),
    ], lan_domain="casalan.com")
    pill = zones["zones"][0]["items"][0]
    assert pill["lan"] is True and pill["routers"] == 2
    assert [(link["zone"], link["href"]) for link in pill["links"]] == [
        ("lan", "https://radio.casalan.com"), ("public", "https://radio.casaalmida.com")]


def test_a_down_router_keeps_its_pill_and_loses_its_link():
    """Clicking must not open a host that is not being served."""
    zones = casa_scruffy_net.group_routers(
        [_live("gone@docker", "Host(`gone.casalan.com`)", status="disabled")],
        lan_domain="casalan.com")
    pill = zones["zones"][0]["items"][0]
    assert pill["down"] is True and pill["links"] == []


@pytest.mark.parametrize("rule", [
    "Host(`x.casalan.com`) && PathPrefix(`/api`)",
    "HostRegexp(`^.+$`)",
    "PathPrefix(`/media`)",
])
def test_a_rule_no_url_can_be_read_from_leaves_the_pill_as_it_was(rule):
    zones = casa_scruffy_net.group_routers([_live("x@docker", rule)], lan_domain="casalan.com")
    assert zones["zones"][0]["items"][0]["links"] == []


def test_without_a_lan_domain_no_pill_claims_a_link():
    """group_routers is called from one place with the domain; a caller that forgets it must
    get no links rather than links zoned by a guess."""
    zones = casa_scruffy_net.group_routers([_live("x@docker", "Host(`x.casalan.com`)")])
    assert zones["zones"][0]["items"][0]["links"] == []
