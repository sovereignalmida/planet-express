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


def _urls(routers):
    """container_urls() with each router's service wired to one container of the same name.

    For the tests about which *rules* become links, where the join is not the question.
    The join has its own tests below, with explicit services and addresses.
    """
    services, container_ips = {}, {}
    for i, router in enumerate(r for r in routers if isinstance(r, dict)):
        key = casa_scruffy_net._router_service_key(router)
        if key not in services:
            ip = f"172.18.0.{i + 2}"
            services[key] = [ip]
            container_ips[router["service"].split("@")[0]] = [ip]
    return casa_scruffy_net.container_urls(routers, services, container_ips,
                                           lan_domain="casalan.com")


def test_a_multi_host_router_yields_one_url_per_host_with_opposite_zones():
    """This, not the -lan twin, is how most services get both addresses: 15 of the live
    host's 70 routers are a single router serving a LAN host and a public one."""
    urls = _urls([
        _live("actual@docker", "Host(`actual.casalan.com`) || Host(`actual.casaalmida.com`)"),
    ])
    assert urls["actual"] == [
        {"href": "https://actual.casalan.com", "zone": "lan"},
        {"href": "https://actual.casaalmida.com", "zone": "public"},
    ]


def test_lan_sorts_first_because_it_is_the_default_target():
    urls = _urls([
        _live("x@docker", "Host(`x.casaalmida.com`) || Host(`x.casalan.com`)"),
    ])
    assert [link["zone"] for link in urls["x"]] == ["lan", "public"]


def test_a_lan_twin_and_its_sibling_collect_onto_one_container():
    """They are separate routers pointing at different hosts, but the same container."""
    urls = _urls([
        _live("subwave-web@docker", "Host(`radio.casaalmida.com`)", service="subwave-web"),
        _live("subwave-web-lan@docker", "Host(`radio.casalan.com`)", service="subwave-web"),
    ])
    assert urls["subwave-web"] == [
        {"href": "https://radio.casalan.com", "zone": "lan"},
        {"href": "https://radio.casaalmida.com", "zone": "public"},
    ]


def test_the_same_host_twice_is_not_two_links():
    urls = _urls([
        _live("a@docker", "Host(`same.casalan.com`)", service="dup"),
        _live("b@docker", "Host(`same.casalan.com`)", service="dup"),
    ])
    assert urls["dup"] == [{"href": "https://same.casalan.com", "zone": "lan"}]


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
    assert urls["secure"][0]["href"].startswith("https://")
    assert urls["plain"][0]["href"].startswith("http://")


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
    assert list(urls) == ["ok"]


# ── The join: router -> service -> backend address -> container ─────────────────
# Names and addresses below are the live host's (T46 brief, "T46.1's two open P1s").

def test_the_join_is_by_address_not_by_the_routers_service_name():
    """Router `actual` routes to Traefik service `actual`, whose container is compose service
    `actual_server`. By name this was a miss; by address it is CASA_ACTUAL."""
    urls = casa_scruffy_net.container_urls(
        [_live("actual@docker", "Host(`actual.casalan.com`) || Host(`actual.casaalmida.com`)")],
        {"actual@docker": ["172.18.0.14"]},
        {"CASA_ACTUAL": ["172.18.0.14"], "CASA_OTHER": ["172.18.0.15"]},
        lan_domain="casalan.com")
    assert list(urls) == ["CASA_ACTUAL"]


def test_two_projects_each_with_a_web_service_do_not_swap_links():
    """CASA_SUBWAVE_WEB and CASA_KARA_KEEP are both compose service `web`. Keyed by that
    name, one would have carried the other's URL."""
    urls = casa_scruffy_net.container_urls(
        [_live("subwave-web@docker", "Host(`radio.casalan.com`)", service="subwave-web"),
         _live("karakeep@docker", "Host(`keep.casalan.com`)", service="karakeep")],
        {"subwave-web@docker": ["172.20.0.3"], "karakeep@docker": ["172.21.0.3"]},
        {"CASA_SUBWAVE_WEB": ["172.20.0.3"], "CASA_KARA_KEEP": ["172.21.0.3"]},
        lan_domain="casalan.com")
    assert urls == {
        "CASA_SUBWAVE_WEB": [{"href": "https://radio.casalan.com", "zone": "lan"}],
        "CASA_KARA_KEEP": [{"href": "https://keep.casalan.com", "zone": "lan"}],
    }


def test_a_container_on_two_networks_is_found_by_either_address():
    urls = casa_scruffy_net.container_urls(
        [_live("adguard@docker", "Host(`adguard.casalan.com`)")],
        {"adguard@docker": ["192.168.1.53"]},
        {"CASA_ADGUARD": ["172.18.0.9", "192.168.1.53"]},
        lan_domain="casalan.com")
    assert list(urls) == ["CASA_ADGUARD"]


def test_a_file_route_to_another_machine_is_not_a_container_link():
    """opnsense, unraid and solar are file-provider routes to other hosts on the LAN."""
    urls = casa_scruffy_net.container_urls(
        [_live("opnsense@file", "Host(`opnsense.casalan.com`)")],
        {"opnsense@file": ["192.168.1.1"]},
        {"CASA_ACTUAL": ["172.18.0.14"]},
        lan_domain="casalan.com")
    assert urls == {}


def test_a_host_networked_service_gets_no_derived_link():
    """jellyfin, plex and qbit run with network_mode: host, so their backend is the host's
    own LAN address and no container reports one. config `links:` is the way to give them a
    button; guessing from the address would be wrong for five containers at once."""
    urls = casa_scruffy_net.container_urls(
        [_live("jellyfin@file", "Host(`jellyfin.casalan.com`)")],
        {"jellyfin@file": ["192.168.1.94"]},
        {"CASA_JELLYFIN": [], "CASA_PLEX": []},
        lan_domain="casalan.com")
    assert urls == {}


def test_an_address_two_containers_report_is_given_to_neither():
    """Only possible from a stale snapshot -- a container recreated since the last scan can
    hand its address on -- but a link to the wrong app is worse than none."""
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")],
        {"x@docker": ["172.18.0.7"]},
        {"CASA_OLD": ["172.18.0.7"], "CASA_NEW": ["172.18.0.7"]},
        lan_domain="casalan.com")
    assert urls == {}


def test_a_service_balanced_across_two_containers_gets_no_link():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")],
        {"x@docker": ["172.18.0.7", "172.18.0.8"]},
        {"CASA_A": ["172.18.0.7"], "CASA_B": ["172.18.0.8"]},
        lan_domain="casalan.com")
    assert urls == {}


def test_a_service_only_partly_resolved_gets_no_link():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")],
        {"x@docker": ["172.18.0.7", "192.168.1.200"]},
        {"CASA_A": ["172.18.0.7"]},
        lan_domain="casalan.com")
    assert urls == {}


def test_a_router_naming_a_service_in_another_provider_uses_that_provider():
    """A file router pointing at a docker service writes `svc@docker`; one in its own provider
    writes the bare name. The services API always has the suffix."""
    urls = casa_scruffy_net.container_urls(
        [_live("x@file", "Host(`x.casalan.com`)", service="x@docker")],
        {"x@docker": ["172.18.0.7"], "x@file": ["192.168.1.9"]},
        {"CASA_X": ["172.18.0.7"]},
        lan_domain="casalan.com")
    assert list(urls) == ["CASA_X"]


def test_a_router_whose_service_is_missing_gets_no_link():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")], {}, {"CASA_X": ["172.18.0.7"]},
        lan_domain="casalan.com")
    assert urls == {}


def test_no_container_snapshot_means_no_derived_links():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")], {"x@docker": ["172.18.0.7"]}, {},
        lan_domain="casalan.com")
    assert urls == {}


# ── fetch_traefik_services ───────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, next_page="1"):
        self._payload = payload
        self.headers = {"X-Next-Page": next_page}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_services_fetch_keeps_each_backends_host(monkeypatch):
    """Shape copied from the live /api/http/services."""
    payload = [
        {"name": "actual@docker", "provider": "docker", "status": "enabled", "type": "loadbalancer",
         "loadBalancer": {"servers": [{"url": "http://172.18.0.14:5006"}], "passHostHeader": True},
         "serverStatus": {"http://172.18.0.14:5006": "UP"}, "usedBy": ["actual@docker"]},
        {"name": "api@internal", "provider": "internal", "status": "enabled"},
        {"name": "wrr@file", "provider": "file", "weighted": {"services": [{"name": "a"}]}},
        "junk",
        {"name": "bad@file", "loadBalancer": {"servers": [{"url": "http://[::1"}, {}, "x"]}},
    ]
    monkeypatch.setattr(casa_scruffy_net.requests, "get", lambda *a, **k: _Resp(payload))
    got = casa_scruffy_net.fetch_traefik_services()
    assert got["available"] is True
    assert got["servers"]["actual@docker"] == ["172.18.0.14"]
    assert "api@internal" not in got["servers"] and "wrr@file" not in got["servers"]
    # Unparseable backends are kept as unresolvable, so the service cannot resolve partly.
    assert got["servers"]["bad@file"] == [None, None, None]


def test_services_fetch_follows_traefiks_pages(monkeypatch):
    """Traefik pages at 100. A service on page 2 must not silently lose its link."""
    pages = {
        1: _Resp([{"name": "a@docker", "loadBalancer": {"servers": [{"url": "http://172.18.0.2"}]}}], "2"),
        2: _Resp([{"name": "b@docker", "loadBalancer": {"servers": [{"url": "http://[fd00:0::5]:80"}]}}], "1"),
    }
    seen = []

    def get(url, params=None, timeout=None):
        seen.append(params["page"])
        return pages[params["page"]]
    monkeypatch.setattr(casa_scruffy_net.requests, "get", get)
    got = casa_scruffy_net.fetch_traefik_services()
    assert seen == [1, 2]
    assert got["servers"] == {"a@docker": ["172.18.0.2"], "b@docker": ["fd00::5"]}


def test_routers_fetch_follows_traefiks_pages(monkeypatch):
    pages = {1: _Resp([{"name": "a@docker", "rule": "Host(`a.casalan.com`)"}], "2"),
             2: _Resp([{"name": "b@docker", "rule": "Host(`b.casalan.com`)"}], "1")}
    monkeypatch.setattr(casa_scruffy_net.requests, "get",
                        lambda url, params=None, timeout=None: pages[params["page"]])
    got = casa_scruffy_net.fetch_traefik_routers()
    assert [r["name"] for r in got["routers"]] == ["a@docker", "b@docker"]


def test_a_server_that_keeps_naming_a_next_page_is_not_followed_forever(monkeypatch):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params["page"])
        return _Resp([], str(params["page"] + 1))
    monkeypatch.setattr(casa_scruffy_net.requests, "get", get)
    casa_scruffy_net.fetch_traefik_services()
    assert len(calls) == casa_scruffy_net._TRAEFIK_MAX_PAGES


def test_an_ipv6_backend_joins_to_the_docker_spelling_of_the_same_address():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")],
        {"x@docker": ["fd00::5"]},
        {"CASA_X": ["fd00:0:0::5"]},
        lan_domain="casalan.com")
    assert list(urls) == ["CASA_X"]


def test_services_fetch_failure_degrades_to_no_servers(monkeypatch):
    def boom(*a, **k):
        raise casa_scruffy_net.requests.ConnectionError("refused")
    monkeypatch.setattr(casa_scruffy_net.requests, "get", boom)
    assert casa_scruffy_net.fetch_traefik_services() == {"available": False, "servers": {}}


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
