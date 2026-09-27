"""group_routers() — the pure shaping behind the Network tab's routing matrix.

No I/O here: fetch_traefik_routers() hands over a list of dicts and this decides zones,
tags and LAN merges. Everything the template must not try to do with a regex.
"""
import os
import sys
from pathlib import Path

import pytest

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
    """container_urls() with each docker router, and its service, owned by a container named
    after that service. For the tests about which *rules* become links, where the join is not
    the question. The join has its own tests below.
    """
    owners = {"routers": {}, "services": {}}
    for r in routers:
        if isinstance(r, dict):
            service = r["service"].split("@")[0]
            owners["routers"][str(r["name"]).partition("@")[0]] = service
            owners["services"][service] = service
    return casa_scruffy_net.container_urls(routers, owners, lan_domain="casalan.com")


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


# ── The join: router -> the container whose labels declare it ─────────────────
# Names below are the live host's (T46 brief, "T46.1's two open P1s").

def _owners(routers=None, services=None):
    return {"routers": routers or {}, "services": services or {}}


def test_the_join_is_by_declaring_container_not_by_the_routers_service_name():
    """Router `actual` routes to Traefik service `actual`, whose container is compose service
    `actual_server`. By service name this was a miss; by label it is CASA_ACTUAL."""
    urls = casa_scruffy_net.container_urls(
        [_live("actual@docker", "Host(`actual.casalan.com`) || Host(`actual.casaalmida.com`)")],
        _owners({"actual": "CASA_ACTUAL"}, {"actual": "CASA_ACTUAL"}), lan_domain="casalan.com")
    assert list(urls) == ["CASA_ACTUAL"]


def test_two_projects_each_with_a_web_service_do_not_swap_links():
    """CASA_SUBWAVE_WEB and CASA_KARA_KEEP are both compose service `web`."""
    urls = casa_scruffy_net.container_urls(
        [_live("subwave-web@docker", "Host(`radio.casalan.com`)", service="web-subwave"),
         _live("karakeep@docker", "Host(`keep.casalan.com`)", service="web-karakeep")],
        _owners({"subwave-web": "CASA_SUBWAVE_WEB", "karakeep": "CASA_KARA_KEEP"},
                {"web-subwave": "CASA_SUBWAVE_WEB", "web-karakeep": "CASA_KARA_KEEP"}),
        lan_domain="casalan.com")
    assert urls == {
        "CASA_SUBWAVE_WEB": [{"href": "https://radio.casalan.com", "zone": "lan"}],
        "CASA_KARA_KEEP": [{"href": "https://keep.casalan.com", "zone": "lan"}],
    }


def test_the_service_is_accepted_with_or_without_its_docker_suffix():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)", service="x@docker")],
        _owners({"x": "CASA_X"}, {"x": "CASA_X"}), lan_domain="casalan.com")
    assert list(urls) == ["CASA_X"]


def test_a_router_declared_on_one_container_forwarding_to_anothers_service_gets_no_link():
    """Routers labelled on gluetun (or Traefik itself) that forward to another container's
    service: the router's container is not the app, and the app did not declare the router."""
    urls = casa_scruffy_net.container_urls(
        [_live("qbit@docker", "Host(`qbit.casalan.com`)", service="qbit")],
        _owners({"qbit": "CASA_GLUETUN"}, {"qbit": "CASA_QBIT"}), lan_domain="casalan.com")
    assert urls == {}


@pytest.mark.parametrize("service", ["nas@file", "api@internal", "missing"])
def test_a_router_forwarding_outside_its_container_gets_no_link(service):
    urls = casa_scruffy_net.container_urls(
        [_live("nas@docker", "Host(`nas.casalan.com`)", service=service)],
        _owners({"nas": "CASA_TRAEFIK"}, {"nas": "CASA_TRAEFIK"}), lan_domain="casalan.com")
    assert urls == {}


def test_a_file_route_is_never_joined_to_a_container():
    """opnsense, unraid and solar are file routes to other machines; jellyfin and plex are
    file routes to host-networked services."""
    urls = casa_scruffy_net.container_urls(
        [_live("opnsense@file", "Host(`opnsense.casalan.com`)"),
         _live("jellyfin@file", "Host(`jellyfin.casalan.com`)")],
        _owners({"opnsense": "CASA_X", "jellyfin": "CASA_JELLYFIN"},
                {"opnsense": "CASA_X", "jellyfin": "CASA_JELLYFIN"}), lan_domain="casalan.com")
    assert urls == {}


def test_a_router_no_container_declares_gets_no_link():
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")], _owners(), lan_domain="casalan.com")
    assert urls == {}


@pytest.mark.parametrize("owners", [
    {"routers": {"x": None}, "services": {"x": None}},
    {"routers": {"x": ""}, "services": {"x": ""}},
    {"routers": {"x": 5}, "services": {"x": 5}},
    {"routers": ["x"], "services": {"x": "CASA_X"}},
    {"routers": {"x": "CASA_X"}, "services": ["x"]},
    {}, None])
def test_a_malformed_owner_map_is_not_rendered(owners):
    urls = casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")], owners, lan_domain="casalan.com")
    assert urls == {}


# ── Traefik pagination ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, next_page="1"):
        self._payload = payload
        self.headers = {"X-Next-Page": next_page}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_routers_fetch_follows_traefiks_pages(monkeypatch):
    """Traefik pages at 100. A router on page 2 must not silently vanish from the Network tab."""
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
    casa_scruffy_net.fetch_traefik_routers()
    assert len(calls) == casa_scruffy_net._TRAEFIK_MAX_PAGES


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
