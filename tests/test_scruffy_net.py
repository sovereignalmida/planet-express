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


# ── launch links (T46.1 / T46.4) ─────────────────────────────────────────────────
# Every rule below is copied from the live host's Traefik API rather than invented.
# group_routers() shipped with tests that passed against fixtures tidier than the host,
# and a reviewer found three bugs the suite could not see; these shapes are the real ones.

def _live(name, rule, service=None, status="enabled", entry_points=("websecure",)):
    return {"name": name, "rule": rule, "status": status,
            "service": service or name.split("@")[0], "entry_points": list(entry_points)}


def _declared(mapping):
    """Accept the short form {container: [routers]} in tests that are not about services.

    An empty service list means "declares no services", which is Traefik's implicit one --
    i.e. no constraint, which is what those tests intend.
    """
    return {name: (value if isinstance(value, dict) else {"routers": value, "services": []})
            for name, value in (mapping or {}).items()}


def _urls(routers, declared=None, lan_domain="casalan.com"):
    """container_urls with the container declarations built automatically, so a test about
    rules does not have to spell out plumbing it is not testing.

    A route is joined to its container by the container's own `traefik.http.routers.<name>.*`
    labels -- the labels Traefik's docker provider builds the router from. The alternatives
    were both tried and are worse: the router's service NAME is a Traefik label that matches
    a compose service for 27 of this host's 64 routes, and matching Traefik's reported
    backend address needs Traefik's view to be current, which it is not while it is still
    processing a Docker event.
    """
    if declared is None:
        declared = {}
        for router in routers:
            if not isinstance(router, dict):
                continue
            short = str(router.get("name", "")).partition("@")[0]
            declared[f"CASA_{short.upper().replace('-', '_')}"] = [short]
    return casa_scruffy_net.container_urls(routers, _declared(declared), lan_domain=lan_domain)


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
    urls = _urls([_live("x@docker", "Host(`x.casaalmida.com`) || Host(`x.casalan.com`)")])
    assert [link["zone"] for link in urls["CASA_X"]] == ["lan", "public"]


def test_a_lan_twin_and_its_sibling_collect_onto_one_container():
    """Separate routers pointing at different hosts, declared by the same container."""
    urls = _urls(
        [_live("subwave-web@docker", "Host(`radio.casaalmida.com`)"),
         _live("subwave-web-lan@docker", "Host(`radio.casalan.com`)")],
        {"CASA_SUBWAVE_WEB": ["subwave-web", "subwave-web-lan"]})
    assert urls["CASA_SUBWAVE_WEB"] == [
        {"href": "https://radio.casalan.com", "zone": "lan"},
        {"href": "https://radio.casaalmida.com", "zone": "public"},
    ]


def test_the_same_host_twice_is_not_two_links():
    urls = _urls([_live("dup@docker", "Host(`same.casalan.com`) || Host(`same.casalan.com`)")])
    assert urls["CASA_DUP"] == [{"href": "https://same.casalan.com", "zone": "lan"}]


@pytest.mark.parametrize("rule", [
    "Host(`adventurelog.casalan.com`) && !(PathPrefix(`/media`) || PathPrefix(`/static`))",
    "Host(`radio.casaalmida.com`) && PathPrefix(`/api`)",
    "HostRegexp(`^.+$`)",
    "PathPrefix(`/dashboard`)",
    "",
])
def test_a_rule_that_cannot_be_read_honestly_yields_nothing(rule):
    """A launch button onto a JSON endpoint, an asset path or a redirect is worse than no
    button. Nine of the live host's 70 routers are skipped for exactly these shapes."""
    assert _urls([_live("x@docker", rule)]) == {}


def test_the_scheme_comes_from_the_entrypoint_not_from_an_assumption():
    urls = _urls([
        _live("secure@docker", "Host(`a.casalan.com`)", entry_points=("websecure",)),
        _live("plain@docker", "Host(`b.casalan.com`)", entry_points=("web",)),
    ])
    assert urls["CASA_SECURE"][0]["href"].startswith("https://")
    assert urls["CASA_PLAIN"][0]["href"].startswith("http://")


def test_an_entrypoint_with_no_known_scheme_is_not_guessed_at():
    """Two of the live host's routers are on `traefik`, its own dashboard entrypoint."""
    assert _urls([_live("x@docker", "Host(`x.casalan.com`)", entry_points=("traefik",))]) == {}


def test_a_disabled_router_yields_nothing():
    assert _urls([_live("x@docker", "Host(`x.casalan.com`)", status="disabled")]) == {}


def test_malformed_entries_do_not_raise():
    urls = _urls([None, "nonsense", _live("ok@docker", "Host(`e.casalan.com`)")])
    assert list(urls) == ["CASA_OK"]


# ── the join: the container's own declaration ───────────────────────────────────

def test_a_route_is_joined_by_what_its_container_declares():
    """`audio@docker` is served by Traefik service `audiobookshelf-media` on container
    CASA_ABS, whose compose service is `audiobookshelf`. Three different names for one thing;
    the label is the one that is actually authoritative."""
    urls = _urls([_live("audio@docker", "Host(`audio.casalan.com`)",
                        service="audiobookshelf-media")],
                 {"CASA_ABS": ["audio"]})
    assert urls == {"CASA_ABS": [{"href": "https://audio.casalan.com", "zone": "lan"}]}


def test_a_router_no_container_declares_yields_no_link():
    """@internal routers, and file-provider routes to other machines or to host-networked
    services. A missing link is better than a wrong one, and `links:` is what those are for."""
    assert _urls([_live("opnsense@file", "Host(`opnsense.casalan.com`)")],
                 {"CASA_ACTUAL": ["actual"]}) == {}


def test_a_file_provider_router_never_picks_up_a_same_named_container():
    """Labels only ever produce docker-provider routers. A `qbit@file` route must not be
    handed to a container that happens to declare a router called `qbit`."""
    assert _urls([_live("qbit@file", "Host(`qbit.casalan.com`)")], {"CASA_QBIT": ["qbit"]}) == {}


def test_a_router_two_containers_claim_identifies_neither():
    assert _urls([_live("shared@docker", "Host(`shared.casalan.com`)")],
                 {"CASA_ONE": ["shared"], "CASA_TWO": ["shared"]}) == {}


def test_a_container_declaring_several_routers_collects_all_of_them():
    urls = _urls([_live("airwave@docker", "Host(`airwave.casalan.com`)"),
                  _live("airwave-tv@docker", "Host(`airwave-tv.casalan.com`)")],
                 {"CASA_AIRWAVE_WEB": ["airwave", "airwave-tv"]})
    assert [link["href"] for link in urls["CASA_AIRWAVE_WEB"]] == [
        "https://airwave-tv.casalan.com", "https://airwave.casalan.com"]


@pytest.mark.parametrize("declared", [
    None, {}, {"": ["x"]}, {"CASA_X": None}, {"CASA_X": "notalist"}, {"CASA_X": []},
])
def test_a_malformed_declaration_does_not_raise(declared):
    # container_urls directly: _urls() builds a declaration when it is not given one, and
    # `None` here means core answered with nothing, not "work it out".
    assert casa_scruffy_net.container_urls(
        [_live("x@docker", "Host(`x.casalan.com`)")], _declared(declared) if declared else declared,
        lan_domain="casalan.com") == {}


def test_the_router_lookup_shares_one_deadline_across_both_commands(monkeypatch):
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
        return 0, "/CASA_ACTUAL\ttrue\ttraefik.enable traefik.http.routers.actual.rule\n", ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    monkeypatch.setattr(actions.time, "monotonic", lambda: clock[0])

    assert actions.container_routers(timeout=4) == {
        "CASA_ACTUAL": {"routers": ["actual"], "services": []}}
    assert budgets[0] == 4
    assert budgets[1] < 1            # what is left of the 4s, not another 4s


def test_only_this_containers_own_router_labels_are_read(monkeypatch):
    """The full label blob is ~111KB across this host's 85 containers, and `docker ps
    --format {{.Labels}}` renders it as a string rather than a map, so it cannot be indexed.
    Keys only, filtered here."""
    from planet_express.execution import actions

    labels = ("/CASA_ADGUARD\ttrue\ttraefik.docker.network traefik.enable "
              "traefik.http.routers.adguard.entrypoints traefik.http.routers.adguard.rule "
              "traefik.http.routers.adguard.tls "
              "traefik.http.services.adguard.loadbalancer.server.port "
              "com.docker.compose.project\n"
              "/CASA_VIKUNJA_DB\t\tcom.docker.compose.project com.docker.compose.service\n")

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_ADGUARD\nCASA_VIKUNJA_DB\n", ""
        return 0, labels, ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    assert actions.container_routers() == {
        "CASA_ADGUARD": {"routers": ["adguard"], "services": ["adguard"]},
        "CASA_VIKUNJA_DB": {"routers": [], "services": []}}


def test_the_router_lookup_answers_empty_rather_than_raising(monkeypatch):
    from planet_express.execution import actions

    monkeypatch.setattr(actions.bender, "run_argv",
                        lambda *a, **k: (1, "", "Cannot connect to the Docker daemon"))
    assert actions.container_routers() == {}


# ── merge_declared_links ────────────────────────────────────────────────────────

def test_a_declared_link_overrides_the_derived_one_for_that_container():
    """Both sides are keyed by container."""
    derived = _urls([_live("advlog@docker", "Host(`travel.casalan.com`)")],
                    {"CASA_ADVLOG_FRONTEND": ["advlog"]})
    assert list(derived) == ["CASA_ADVLOG_FRONTEND"]

    merged = casa_scruffy_net.merge_declared_links(derived, [
        {"name": "CASA_ADVLOG_FRONTEND", "href": "https://travel.casalan.com", "zone": "lan"},
    ])
    assert merged == {"CASA_ADVLOG_FRONTEND": [
        {"href": "https://travel.casalan.com", "zone": "lan"}]}


def test_a_declared_link_covers_a_container_no_route_can_reach():
    """plex, jellyfin, qbit and planetexpress are routed by the file provider to the host's
    own address, which declares no router. The escape hatch is all they have."""
    merged = casa_scruffy_net.merge_declared_links({}, [
        {"name": "CASA_PLEX", "href": "https://plex.casalan.com"},
    ])
    assert merged == {"CASA_PLEX": [{"href": "https://plex.casalan.com", "zone": "lan"}]}


def test_a_declared_link_without_a_name_or_href_is_ignored():
    merged = casa_scruffy_net.merge_declared_links(
        {"CASA_X": [{"href": "https://x.casalan.com", "zone": "lan"}]},
        [None, "nonsense", {}, {"name": "CASA_Y"}, {"href": "https://z"}])
    assert merged == {"CASA_X": [{"href": "https://x.casalan.com", "zone": "lan"}]}


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


def test_a_router_no_container_declares_gets_no_link_even_if_a_name_matches():
    """Traefik can generate a router itself from its defaultRule. Matching those by
    normalising the container name was tried and removed: Traefik derives that name from its
    own notion of the service, which an explicit container_name or a replica suffix makes
    something else, so a normalised name that collided would put the link on an unrelated
    card. None of this host's 57 enabled docker routers is implicit."""
    assert _urls([_live("casa-x@docker", "Host(`x.casalan.com`)")],
                 {"CASA_X": [], "CASA_OTHER": []}) == {}


def test_an_explicit_declaration_is_the_only_thing_that_owns_a_router():
    urls = _urls([_live("casa-x@docker", "Host(`x.casalan.com`)")],
                 {"CASA_X": [], "CASA_OTHER": ["casa-x"]})
    assert list(urls) == ["CASA_OTHER"]


@pytest.mark.parametrize(("labels", "expected"), [
    (["traefik.http.routers.adguard.rule"], ["adguard"]),
    (["traefik.http.routers.adguard.tls.certresolver"], ["adguard"]),
    (["traefik.http.routers.adguard.entrypoints"], ["adguard"]),
    # Traefik permits a dot in a router name; stopping at the first dot would look for a
    # router called `api` that the API never reports.
    (["traefik.http.routers.api.v1.rule"], ["api.v1"]),
    (["traefik.http.services.adguard.loadbalancer.server.port"], []),
    (["traefik.enable", "com.docker.compose.project"], []),
])
def test_the_router_name_is_read_off_the_label_correctly(labels, expected, monkeypatch):
    from planet_express.execution import actions

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_X\n", ""
        return 0, "/CASA_X\ttrue\t" + " ".join(labels) + "\n", ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    assert actions.container_routers()["CASA_X"]["routers"] == expected


def test_a_stopped_predecessor_does_not_suppress_its_replacements_link(monkeypatch):
    """A recreate can leave the old container behind, stopped, carrying the same router
    labels. Listed with `docker ps -a` both would claim the router, it would look like
    something two containers claim, and the live link would vanish."""
    from planet_express.execution import actions

    listed = {}

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            listed["argv"] = argv
            return 0, "CASA_ACTUAL\n", ""          # running only
        return 0, "/CASA_ACTUAL\ttrue\ttraefik.http.routers.actual.rule\n", ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    assert actions.container_routers() == {"CASA_ACTUAL": {"routers": ["actual"], "services": []}}
    assert "-a" not in listed["argv"], "stopped containers must not claim routers"


@pytest.mark.parametrize(("label", "expected"), [
    ("traefik.http.routers.api.tls.rule", ["api.tls"]),
    ("traefik.http.routers.adguard.tls.certresolver", ["adguard"]),
    ("traefik.http.routers.adguard.tls", ["adguard"]),
])
def test_the_rightmost_field_decides_where_the_router_name_ends(label, expected, monkeypatch):
    """`api.tls` is a router; `adguard.tls` is router adguard's tls setting. Stopping at the
    first field-shaped component got the first one wrong."""
    from planet_express.execution import actions

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_X\n", ""
        return 0, f"/CASA_X\ttrue\t{label}\n", ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    assert actions.container_routers()["CASA_X"]["routers"] == expected


def test_an_ambiguous_router_is_not_rescued_by_the_implicit_name():
    """Two containers explicitly claim `casa-x`, and one of them is called CASA_X. The
    ambiguity is the answer; falling through to the implicit name would hand the route to
    whichever claimant happened to be named after it."""
    assert _urls([_live("casa-x@docker", "Host(`x.casalan.com`)")],
                 {"CASA_X": ["casa-x"], "CASA_OTHER": ["casa-x"]}) == {}


def test_a_container_traefik_has_been_told_to_ignore_claims_nothing(monkeypatch):
    """traefik.enable=false means the provider ignores it entirely. Counting its leftover
    router labels would make a live router look like something two containers claim, and the
    real one's launch link would disappear."""
    from planet_express.execution import actions

    lines = ("/CASA_OLD\tfalse\ttraefik.http.routers.actual.rule\n"
             "/CASA_ACTUAL\ttrue\ttraefik.http.routers.actual.rule\n")

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_OLD\nCASA_ACTUAL\n", ""
        return 0, lines, ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    declared = actions.container_routers()
    assert declared == {"CASA_ACTUAL": {"routers": ["actual"], "services": []}}, \
        "a disabled container is omitted entirely"
    assert _urls([_live("actual@docker", "Host(`a.casalan.com`)")], declared) == {
        "CASA_ACTUAL": [{"href": "https://a.casalan.com", "zone": "lan"}]}


@pytest.mark.parametrize("label", [
    "Traefik.HTTP.Routers.app.Rule",
    "TRAEFIK.HTTP.ROUTERS.APP.RULE",
    "traefik.http.routers.app.rule",
])
def test_traefik_label_keys_are_matched_case_insensitively(label, monkeypatch):
    """Traefik's label keys are case-insensitive: it creates the same router either way, and
    ignoring the mixed-case spelling would silently drop that container's launch link."""
    from planet_express.execution import actions

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_X\n", ""
        return 0, f"/CASA_X\ttrue\t{label}\n", ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    assert actions.container_routers()["CASA_X"]["routers"] == ["app"]


# ── same_routes: the stable sample around the label read ────────────────────────

def test_an_unchanged_router_set_is_the_same_moment():
    before = [_live("a@docker", "Host(`a.casalan.com`)"), _live("b@docker", "Host(`b.casalan.com`)")]
    assert casa_scruffy_net.same_routes(before, list(reversed(before)))


@pytest.mark.parametrize("after", [
    [_live("a@docker", "Host(`moved.casalan.com`)")],          # the rule changed
    [_live("a@docker", "Host(`a.casalan.com`)", status="disabled")],
    [_live("a@docker", "Host(`a.casalan.com`)", entry_points=("web",))],
    [],                                                         # Traefik went away mid-read
])
def test_a_route_that_moved_is_not_the_same_moment(after):
    assert not casa_scruffy_net.same_routes([_live("a@docker", "Host(`a.casalan.com`)")], after)


def test_same_routes_does_not_raise_on_rubbish():
    assert casa_scruffy_net.same_routes(None, None)
    assert casa_scruffy_net.same_routes([None, "x"], [])


def test_a_disabled_container_is_omitted_entirely(monkeypatch):
    """traefik.enable=false means the provider ignores it. Recording it with an empty router
    list would make it indistinguishable from an enabled container that simply declares no
    routers."""
    from planet_express.execution import actions

    lines = ("/CASA_X\tfalse\tcom.docker.compose.project\n"
             "/CASA_Y\t\tcom.docker.compose.project\n")

    def fake_run_argv(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return 0, "CASA_X\nCASA_Y\n", ""
        return 0, lines, ""

    monkeypatch.setattr(actions.bender, "run_argv", fake_run_argv)
    declared = actions.container_routers()
    assert declared == {"CASA_Y": {"routers": [], "services": []}}, \
        "a disabled container must not be a candidate at all"
    assert _urls([_live("casa-x@docker", "Host(`x.casalan.com`)")], declared) == {}


def test_a_router_serving_another_providers_service_gets_no_link():
    """`traefik@docker` on this host is served by `api@internal`. The container carrying the
    router label is not the container serving that route, so hanging the link on it would put
    it on the wrong card. One live router does this; a missing link is better than a wrong
    one, and config's `links:` covers it."""
    assert _urls([_live("traefik@docker", "Host(`traefik.casalan.com`)",
                        service="api@internal")],
                 {"CASA_TRAEFIK": ["traefik"]}) == {}


def test_a_router_naming_its_own_providers_service_is_still_fine():
    urls = _urls([_live("actual@docker", "Host(`actual.casalan.com`)",
                        service="actual@docker")],
                 {"CASA_ACTUAL": ["actual"]})
    assert urls == {"CASA_ACTUAL": [{"href": "https://actual.casalan.com", "zone": "lan"}]}


def test_an_unqualified_service_name_means_this_providers_own():
    urls = _urls([_live("actual@docker", "Host(`actual.casalan.com`)", service="actual")],
                 {"CASA_ACTUAL": ["actual"]})
    assert list(urls) == ["CASA_ACTUAL"]


@pytest.mark.parametrize("front_services", [["app"], []])
def test_a_router_served_by_another_containers_service_gets_no_link(front_services):
    """`traefik.http.routers.app.service=backend` where `backend` belongs to a different
    container is valid Traefik. Declaring the router is not the same as being the thing
    behind it, so the link must not land on the declarer -- including when the declarer
    declares no services of its own, which is not the same as "the service is mine"."""
    assert _urls([_live("app@docker", "Host(`app.casalan.com`)", service="backend")],
                 {"CASA_FRONT": {"routers": ["app"], "services": front_services},
                  "CASA_BACK": {"routers": [], "services": ["backend"]}}) == {}


def test_a_service_two_containers_declare_identifies_neither():
    assert _urls([_live("app@docker", "Host(`app.casalan.com`)", service="shared")],
                 {"CASA_FRONT": {"routers": ["app"], "services": ["shared"]},
                  "CASA_BACK": {"routers": [], "services": ["shared"]}}) == {}


def test_a_router_served_by_its_own_containers_service_is_fine():
    urls = _urls([_live("app@docker", "Host(`app.casalan.com`)", service="app")],
                 {"CASA_FRONT": {"routers": ["app"], "services": ["app"]}})
    assert urls == {"CASA_FRONT": [{"href": "https://app.casalan.com", "zone": "lan"}]}


def test_a_container_declaring_no_services_is_using_traefiks_implicit_one():
    """7 of this host's 57 routed containers declare no service labels at all."""
    urls = _urls([_live("app@docker", "Host(`app.casalan.com`)", service="app")],
                 {"CASA_FRONT": {"routers": ["app"], "services": []}})
    assert list(urls) == ["CASA_FRONT"]
