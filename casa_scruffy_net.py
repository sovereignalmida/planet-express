"""
casa_scruffy_net.py — live (per-request) network pollers for the dashboard's Network
tab. Deliberately separate from dashboard_data.py, which promises zero I/O beyond
STATE_DIR in its own docstring -- these two functions are the one place in the
dashboard that talk to the network directly, on every page load, not from a state
file. Both are best-effort: they never raise, so a Traefik/AdGuard hiccup degrades the
Network tab instead of taking down the whole dashboard.
"""

import logging
import re

import requests
import urllib3

import config

log = logging.getLogger("planetexpress.scruffy_net")

# AdGuard is only reachable through Traefik's LAN-only self-signed cert (verify=False
# below) -- same acceptance already established for this host's other LAN-only routes.
# Suppressed explicitly so it doesn't spam a warning into the log on every page load.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TIMEOUT = 2

# Traefik's API is published directly on this host (api.insecure: true in traefik.yml),
# same endpoint Homepage's own Traefik widget already polls -- see traefik.yml/homepage
# labels in ~/stacks/network/docker-compose.yml.
_TRAEFIK_API = "http://127.0.0.1:8079/api/http/routers"

# AdGuard sits on the casapilan macvlan, unreachable from its own Docker host by
# design -- routed through Traefik instead, the same Host-header trick already used to
# verify LAN-only services from this host (traefik.http.routers.adguard.rule in
# ~/stacks/network/docker-compose.yml). Built from config.LAN_ONLY_DOMAIN, not
# hardcoded to this deployment's "casalan.com" -- an install with a different
# lan_only_domain in config.yaml routes AdGuard under a different hostname, and a
# hardcoded Host header would silently 404 against the wrong (or no) Traefik router.
_ADGUARD_STATS_URL = "https://127.0.0.1/control/stats"


def _adguard_host_header() -> str:
    return f"adguard.{config.LAN_ONLY_DOMAIN}"


# Traefik's API pages its lists at 100 by default and names the next page in X-Next-Page,
# which reads "1" on the last one. This host is at 70 routers; the cap only stops a
# misbehaving server from looping us.
_TRAEFIK_MAX_PAGES = 20


def _get_traefik_list(url: str) -> list:
    items: list = []
    page = 1
    for _ in range(_TRAEFIK_MAX_PAGES):
        resp = requests.get(url, params={"page": page}, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise TypeError(f"expected a list, got {type(payload).__name__}")
        items.extend(payload)
        try:
            next_page = int((getattr(resp, "headers", None) or {}).get("X-Next-Page", "1"))
        except (TypeError, ValueError):
            next_page = 1
        if next_page <= page:
            break
        page = next_page
    return items


def fetch_traefik_routers() -> dict:
    try:
        payload = _get_traefik_list(_TRAEFIK_API)
        routers = [
            {
                "name": r.get("name", "?"),
                "rule": r.get("rule", ""),
                "service": r.get("service", "?"),
                "status": r.get("status", "?"),
                # The launch link's scheme comes from here, so it is collected rather than
                # assumed: this host is 67 websecure, 2 traefik and 1 web.
                "entry_points": [e for e in r.get("entryPoints", []) or [] if isinstance(e, str)],
            }
            for r in payload
            if isinstance(r, dict)
        ]
        routers.sort(key=lambda r: r["name"])
        return {"available": True, "routers": routers}
    except (requests.RequestException, ValueError, TypeError, AttributeError) as e:
        log.warning(f"Traefik router fetch failed: {e}")
        return {"available": False, "routers": []}


def fetch_adguard_stats() -> dict:
    username, password = config.adguard_credentials()
    if not username or not password:
        return {"available": False, "configured": False}
    try:
        resp = requests.get(
            _ADGUARD_STATS_URL,
            headers={"Host": _adguard_host_header()},
            auth=(username, password),
            verify=False,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "available": True,
            "configured": True,
            "num_dns_queries": data.get("num_dns_queries"),
            "num_blocked_filtering": data.get("num_blocked_filtering"),
            "avg_processing_time": data.get("avg_processing_time"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning(f"AdGuard stats fetch failed: {e}")
        return {"available": False, "configured": True}


# ── Routing matrix grouping (T45.3) ──────────────────────────────────────────────
# Pure shaping over whatever fetch_traefik_routers() returned. No I/O, so it is unit
# tested directly. The Network tab used to draw 78 two-line cards in a 20-column wall;
# this turns them into ~73 one-line pills in four labelled rows.

_HOST_IN_RULE = re.compile(r"Host\(`([^`]+)`\)")
# Traefik's own default provider. Tagging it would put the same three letters on nine
# pills in ten, which is how a tag stops meaning anything.
_DEFAULT_PROVIDER = "docker"
_PROVIDER_TAGS = {"file": "FILE"}
_UNZONED = "internal / external"


def _registrable_domain(host: str) -> str:
    """"immich.casalan.com" -> "casalan.com". Two labels is right for this host's zones
    (casalan.com, casaalmida.com, chrisalmida.com); a public-suffix list would be the
    correct general answer and is more machinery than a LAN dashboard earns.

    An IP-literal Host() rule has no zone at all -- taking its last two labels would file
    192.168.1.94 under a domain called "1.94"."""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2 or all(label.isdigit() for label in labels):
        return ""
    return ".".join(labels[-2:])


def _router_zone(rule: str) -> tuple[str, str]:
    """(zone, primary host). A router with no Host() rule -- a pure PathPrefix, Method or
    Headers match -- has no zone to sort into and lands in the catch-all bucket."""
    match = _HOST_IN_RULE.search(rule or "")
    if not match:
        return _UNZONED, ""
    host = match.group(1)
    return _registrable_domain(host) or _UNZONED, host


def group_routers(routers: list) -> dict:
    """Zones of pills, ready to render.

    Three things happen here that the template must not try to do itself:
      * the provider suffix is split off `name@provider` and dropped when it is docker;
      * `x-lan` collapses into its sibling `x`, carrying a `+LAN` tag -- an orphan `x-lan`
        with no sibling keeps its own pill, because dropping a router is worse than showing
        a twin;
      * a router that is not `enabled` sorts to the front of its zone with a DOWN tag.

    Twins are matched BEFORE zoning. `navidrome@docker` on the public domain and
    `navidrome-lan@docker` on the LAN one are the normal shape on this host, and matching
    them inside a zone would only ever have merged twins that shared a domain.

    Entries are keyed by full `name@provider` identity, not by short name: Traefik lets two
    providers define the same short name, and keying on the short one silently dropped a
    route.
    """
    pills = {}
    for router in routers:
        if not isinstance(router, dict):
            continue
        raw = str(router.get("name", "?"))
        short, _, provider = raw.partition("@")
        zone, host = _router_zone(router.get("rule", ""))
        pills[raw] = {
            "name": short, "raw": raw, "host": host, "zone": zone,
            "provider": provider, "down": router.get("status") != "enabled",
            "tag": "" if provider == _DEFAULT_PROVIDER else _PROVIDER_TAGS.get(provider, "EXT"),
            "lan": False, "routers": 1,
            # A pill can stand for two routers once a twin merges, so "is this pill down" and
            # "how many routers behind it are down" are different questions. The header asks
            # the second one.
            "down_count": 1 if router.get("status") != "enabled" else 0,
            # Every router behind this pill, once LAN twins merge: the links come from them.
            "raws": [raw],
            "filter_text": f'{raw} {router.get("rule", "")} {router.get("service", "")}'.lower(),
        }

    merged = 0
    for key in [k for k, pill in pills.items() if pill["name"].endswith("-lan")]:
        twin = pills[key]
        sibling_key = f'{twin["name"][: -len("-lan")]}@{twin["provider"]}'
        sibling = pills.get(sibling_key)
        if sibling is None:
            continue
        sibling["lan"] = True
        sibling["routers"] += 1
        # The hostname left the pill face for the title attribute, so filtering is the only
        # way to search by host. Merging must not make the twin's own hostname unfindable.
        sibling["filter_text"] += " " + twin["filter_text"]
        sibling["down"] = sibling["down"] or twin["down"]
        sibling["down_count"] += twin["down_count"]
        sibling["raws"].extend(twin["raws"])
        del pills[key]
        merged += 1

    by_zone = {}
    for pill in pills.values():
        by_zone.setdefault(pill["zone"], []).append(pill)

    zones = []
    for zone, items in by_zone.items():
        items.sort(key=lambda pill: (not pill["down"], pill["name"]))
        zones.append({
            "name": zone,
            # Two counts, because they differ wherever a twin merged: "3 routers" is what the
            # zone label promises, while names is what the header's "n names" reports.
            "count": sum(pill["routers"] for pill in items),
            "names": len(items),
            "items": items,
            "down": sum(pill["down_count"] for pill in items),
        })

    # Biggest zone first, but the catch-all bucket always last: it is a leftovers pile, not
    # a place, and reading it first tells you nothing about the host.
    zones.sort(key=lambda z: (z["name"] == _UNZONED, -z["count"], z["name"]))
    return {
        "zones": zones,
        "total": sum(zone["count"] for zone in zones),
        "names": sum(zone["names"] for zone in zones),
        "merged": merged,
        "down": sum(zone["down"] for zone in zones),
    }


# ── Launch links (T46.1) ─────────────────────────────────────────────────────────
# One URL per Host() in a router's rule, and only from a rule that is Host() terms joined
# by `||` and nothing else.
#
# Measured against the live host's 70 routers, that is 61 routers and 74 URLs. What it
# refuses, it refuses for a reason: `subwave-api` is `radio.casaalmida.com/api` (JSON),
# `subwave-stream` is `/stream.mp3` (a raw audio stream), `adventurelog-admin` serves
# `/media` and `/static`, and the three `@internal` routers are Traefik's own. Emitting a
# "launch" link to any of those is worse than emitting none, which is the spec's own rule.
#
# It costs one real link: `adventurelog@docker` is `(Host(a) || Host(b)) && !(PathPrefix(...))`
# so its host root IS launchable, but recovering it means a parser that reasons about negated
# path groups. That is covered by the `links:` escape hatch in config instead -- explicit,
# auditable, and it says who decided.
_ENTRYPOINT_SCHEME = {"websecure": "https", "web": "http"}
_PLAIN_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                         r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*")


# `<hosts> && !(PathPrefix(`/a`) || PathPrefix(`/b`))` -- a negation that carves specific
# prefixes OUT of a host still serves that host's root. adventurelog is exactly this shape:
# the frontend answers travel.casalan.com/ while the backend answers /admin, /media, /static
# and /accounts through a second router. Refusing it lost a link the host really has.
#
# Deliberately narrow, and still not a Traefik expression parser. Only a trailing negated
# group, only Path/PathPrefix terms inside it, and only when none of those covers the root --
# `!(PathPrefix(`/`))` excludes everything, so the root is NOT served and no URL is honest.
_EXCLUDED_PATHS = re.compile(
    r"&&\s*!\(\s*(?P<group>(?:PathPrefix|Path)\(`[^`]*`\)"
    r"(?:\s*\|\|\s*(?:PathPrefix|Path)\(`[^`]*`\))*)\s*\)\s*$")
_PATH_TERM = re.compile(r"(?:PathPrefix|Path)\(`([^`]*)`\)")


def _without_excluded_paths(rule: str) -> tuple[str, bool]:
    """(the rule with a trailing `&& !(paths)` removed, whether it may be linked at all)."""
    match = _EXCLUDED_PATHS.search(rule or "")
    if match is None:
        return rule or "", True
    paths = _PATH_TERM.findall(match.group("group"))
    if not paths or any(path.strip() in ("", "/") for path in paths):
        return rule or "", False
    return (rule or "")[: match.start()], True


def _rule_is_host_only(rule: str) -> bool:
    """True when removing every Host() term leaves nothing but the `||` that joined them.

    Deliberately not a Traefik expression parser. Anything with `&&`, a negation, a bare
    PathPrefix or a HostRegexp fails this and emits no URL.
    """
    remainder = _HOST_IN_RULE.sub("", rule or "")
    for token in ("||", "(", ")"):
        remainder = remainder.replace(token, "")
    return remainder.strip() == ""


def router_urls(router: dict, *, lan_domain: str) -> list:
    """[{"href", "zone"}] one router can honestly be opened at, else [].

    Only a rule that is Host() terms joined by `||` and nothing else, and only with an
    entrypoint whose scheme is known. A missing link is better than a wrong one.
    """
    if not isinstance(router, dict):
        return []
    rule, linkable = _without_excluded_paths(router.get("rule", ""))
    hosts = _HOST_IN_RULE.findall(rule)
    if not linkable or not hosts or not _rule_is_host_only(rule):
        return []
    entry_points = router.get("entry_points") or []
    scheme = next((_ENTRYPOINT_SCHEME[e] for e in entry_points if e in _ENTRYPOINT_SCHEME), None)
    if scheme is None:
        # An entrypoint we do not have a scheme for is not a guess worth making.
        return []
    urls = []
    for host in hosts:
        if not _PLAIN_HOST.fullmatch(host):
            # `app.casalan.com@evil.example` would open evil.example with a user part. A label
            # an image supplies can put anything between the backticks; only a hostname links.
            continue
        href = f"{scheme}://{host}"
        if not any(url["href"] == href for url in urls):
            urls.append({"href": href,
                         "zone": "lan" if _registrable_domain(host) == lan_domain else "public"})
    return urls


def link_pills(zones: dict, routers: list, *, lan_domain: str, detail_for=None) -> None:
    """Make each routing-matrix pill a link (v2.2 B). Mutates `zones` from group_routers().

    `href` opens the public host, `lan_href` the LAN one; a pill with both is drawn split.
    A pill with only one host has only `href`. A DOWN pill gets no URL -- its link is the
    container's detail view (`detail`), from `detail_for(router_name)`, where known.
    """
    by_name = {str(r.get("name")): r for r in routers or [] if isinstance(r, dict)}
    for zone in zones.get("zones", []):
        for pill in zone.get("items", []):
            pill.update(href=None, lan_href=None, detail=None)
            if pill.get("down"):
                if detail_for is not None:
                    pill["detail"] = next((d for raw in pill.get("raws", [pill["raw"]])
                                           if (d := detail_for(raw))), None)
                continue
            urls = [url for raw in pill.get("raws", [pill["raw"]])
                    for url in router_urls(by_name.get(raw), lan_domain=lan_domain)]
            public = next((u["href"] for u in urls if u["zone"] == "public"), None)
            lan = next((u["href"] for u in urls if u["zone"] == "lan"), None)
            if public and lan:
                pill["href"], pill["lan_href"] = public, lan
            else:
                pill["href"] = public or lan


def container_urls(routers: list, owners: dict, *, lan_domain: str) -> dict:
    """{container: [{"href", "zone"}]} for every router that can be turned into a link.

    Keyed by container name, through `owners` -- {"routers": {name: container}, "services":
    {name: container}} as core read them from the containers' own labels. A link is emitted
    only when ONE container declares both the router and the docker service it forwards to:
    a router declared on the Traefik container that forwards to another container's
    service, or to a file or internal service, has no honest row to sit on. (Core already
    withholds everything declared on a container whose network namespace others share.)
    Only docker-provider routers are joined; a file route is to another machine, or to a
    host-networked service, which config `links:` is for.

    A container reachable from two routers (`x` and its `-lan` twin) collects both their
    URLs, de-duplicated: the twins point at different hosts, so both are real.
    """
    by_container: dict[str, list] = {}
    router_owners = (owners or {}).get("routers") if isinstance(owners, dict) else None
    service_owners = (owners or {}).get("services") if isinstance(owners, dict) else None
    if not isinstance(router_owners, dict) or not isinstance(service_owners, dict):
        return by_container
    for router in routers or []:
        if not isinstance(router, dict) or router.get("status") != "enabled":
            continue
        short, _, provider = str(router.get("name", "")).partition("@")
        if provider != "docker":
            continue
        owner = router_owners.get(short)
        if not isinstance(owner, str) or not owner:
            continue
        service, _, service_provider = str(router.get("service", "")).partition("@")
        if service_provider not in ("", "docker") or service_owners.get(service) != owner:
            continue
        urls = router_urls(router, lan_domain=lan_domain)
        if not urls:
            continue
        links = by_container.setdefault(owner, [])
        for url in urls:
            if not any(link["href"] == url["href"] for link in links):
                links.append(url)
    # LAN first: it is the default target, and the one that works when the WAN is down.
    for links in by_container.values():
        links.sort(key=lambda link: (link["zone"] != "lan", link["href"]))
    return by_container


def merge_declared_links(derived: dict, declared: list) -> dict:
    """Fold config's `links:` over what the routers produced.

    A declared link wins for its container: the operator wrote it down precisely because the
    route could not be read honestly, so it is not something to merge with a guess.
    """
    merged = {name: list(links) for name, links in (derived or {}).items()}
    for link in declared or []:
        if not isinstance(link, dict) or not link.get("name") or not link.get("href"):
            continue
        merged[str(link["name"])] = [{
            "href": str(link["href"]),
            "zone": "public" if link.get("zone") == "public" else "lan",
        }]
    return merged
