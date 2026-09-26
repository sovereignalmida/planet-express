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
from urllib.parse import urlparse

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
# The services endpoint is what ties a route back to the container serving it: for a
# docker-provider service, loadBalancer.servers[].url is the container's own address.
_TRAEFIK_SERVICES_API = "http://127.0.0.1:8079/api/http/services"

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


def fetch_traefik_routers() -> dict:
    try:
        resp = requests.get(_TRAEFIK_API, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise TypeError(f"expected a list of routers, got {type(payload).__name__}")
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


def fetch_traefik_services() -> list[dict]:
    """Each enabled service's name, provider and backend hosts. Best-effort like the rest of
    this module: an empty list costs launch links, never the page."""
    try:
        resp = requests.get(_TRAEFIK_SERVICES_API, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise TypeError(f"expected a list of services, got {type(payload).__name__}")
        services = []
        for item in payload:
            if not isinstance(item, dict) or item.get("status") != "enabled":
                continue
            balancer = item.get("loadBalancer")
            servers = balancer.get("servers") if isinstance(balancer, dict) else None
            hosts = []
            for server in servers or []:
                url = server.get("url") if isinstance(server, dict) else None
                host = urlparse(url).hostname if isinstance(url, str) else None
                if host:
                    hosts.append(host)
            services.append({"name": item.get("name", "?"),
                             "provider": item.get("provider", "?"),
                             "hosts": hosts})
        return services
    except (requests.RequestException, ValueError, TypeError, AttributeError) as e:
        log.warning(f"Traefik service fetch failed: {e}")
        return []


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


def _rule_is_host_only(rule: str) -> bool:
    """True when removing every Host() term leaves nothing but the `||` that joined them.

    Deliberately not a Traefik expression parser. Anything with `&&`, a negation, a bare
    PathPrefix or a HostRegexp fails this and emits no URL.
    """
    remainder = _HOST_IN_RULE.sub("", rule or "")
    for token in ("||", "(", ")"):
        remainder = remainder.replace(token, "")
    return remainder.strip() == ""


def same_backends(before: list, after: list) -> bool:
    """Did every service's backend set stay put between two reads of Traefik?

    The join reads Traefik's services, then the container addresses. Those are two moments,
    and between them a container can be recreated and its old address handed to another one
    -- at which point exactly one container claims it, the join looks confident, and the link
    lands on the wrong card. Reading the services again afterwards turns that into something
    observable: if nothing moved, the addresses describe the same host the services did.
    """
    def shape(services):
        return {str(item.get("name")): sorted(str(host) for host in item.get("hosts") or [])
                for item in services or [] if isinstance(item, dict)}

    return shape(before) == shape(after)


def service_containers(services: list, addresses: dict) -> dict[str, str]:
    """{"<service>@<provider>": container name}, for services backed by exactly one container.

    The join is the backend IP. Traefik's docker provider points a service at the container's
    own address, so that address is the only reliable way back from a route to the container
    serving it. Measured on the live host: 108 container IPs with none shared, and 56 of 70
    enabled routers resolving to exactly one container. The 14 that do not are genuinely not
    container routes -- Traefik's own @internal ones, and file-provider routes to other
    machines or to host-networked services, which is what the config `links:` escape hatch is
    for.

    The router's service NAME cannot do this job. It is a Traefik label: on this host it
    matches a compose service for 27 of 64 routers (`actual` is compose service
    `actual_server`, `adguard` is `adguardhome`, `wiki` is `wiki-go`, `sabnzbd` is
    `SabNZBD`), and compose service names are not unique across projects either --
    CASA_SUBWAVE_WEB and CASA_KARA_KEEP are both service `web`, so keying on the name would
    hang one service's URL on another's container.
    """
    by_ip: dict[str, list[str]] = {}
    for name, ips in (addresses or {}).items():
        if not name or not isinstance(ips, (list, tuple)):
            continue
        for ip in ips:
            by_ip.setdefault(str(ip), []).append(str(name))

    resolved: dict[str, str] = {}
    for service in services or []:
        if not isinstance(service, dict):
            continue
        hosts = service.get("hosts")
        if not isinstance(hosts, (list, tuple)) or not hosts:
            continue
        # EVERY backend must resolve, and all to the same container. Resolving the ones that
        # happen to be local and ignoring the rest would claim a service that is half
        # somewhere else -- a mixed local/remote balancer, or a stale backend mid-rollout --
        # and hang its link on whichever half was recognised.
        names = set()
        for host in hosts:
            claimants = by_ip.get(str(host), [])
            if len(claimants) != 1:          # unknown, or an address two containers claim
                names = None
                break
            names.add(claimants[0])
        if names and len(names) == 1:
            resolved[str(service.get("name", ""))] = names.pop()
    return resolved


def container_urls(routers: list, services: list, addresses: dict, *, lan_domain: str) -> dict:
    """{container name: [{"href", "zone"}]} for every route that can be turned into a link.

    Keyed by the container, resolved through service_containers() above. A container reachable
    from two routers (`x` and its `-lan` twin) collects both their URLs, de-duplicated: the
    twins point at different hosts, so both are real.
    """
    owners = service_containers(services, addresses)
    by_service: dict[str, list] = {}
    for router in routers or []:
        if not isinstance(router, dict) or router.get("status") != "enabled":
            continue
        rule = router.get("rule", "")
        hosts = _HOST_IN_RULE.findall(rule or "")
        if not hosts or not _rule_is_host_only(rule):
            continue
        entry_points = router.get("entry_points") or []
        scheme = next((_ENTRYPOINT_SCHEME[e] for e in entry_points if e in _ENTRYPOINT_SCHEME), None)
        if scheme is None:
            # An entrypoint we do not have a scheme for is not a guess worth making.
            continue
        # The services endpoint names every service "<name>@<provider>". A router usually
        # reports its service without the suffix, meaning its own provider -- but it can name
        # one across providers, and on this host four do (traefik@docker is served by
        # api@internal). So a service that already carries a provider is taken as written.
        service = str(router.get("service", ""))
        if "@" not in service:
            _short, _, provider = str(router.get("name", "")).partition("@")
            service = f"{service}@{provider}"
        container = owners.get(service)
        if not container:
            # No container behind this route: an @internal router, or a file-provider route
            # to another machine. A missing link is better than a wrong one.
            continue
        links = by_service.setdefault(container, [])
        for host in hosts:
            href = f"{scheme}://{host}"
            if any(link["href"] == href for link in links):
                continue
            links.append({
                "href": href,
                "zone": "lan" if _registrable_domain(host) == lan_domain else "public",
            })
    # LAN first: it is the default target, and the one that works when the WAN is down.
    for links in by_service.values():
        links.sort(key=lambda link: (link["zone"] != "lan", link["href"]))
    return by_service


def merge_declared_links(derived: dict, declared: list) -> dict:
    """Fold config's `links:` over what the routers produced. Both are keyed by container.

    A declared link wins for its container: the operator wrote it down precisely because the
    route could not be read honestly, so it is not something to merge with a guess. `name` is
    the container name -- when derived links moved from Traefik service names to containers,
    a declared link under the old key would have matched nothing, silently, which is how the
    host-networked services that depend entirely on this escape hatch would have lost theirs.
    """
    merged = {service: list(links) for service, links in (derived or {}).items()}
    for link in declared or []:
        if not isinstance(link, dict) or not link.get("name") or not link.get("href"):
            continue
        merged[str(link["name"])] = [{
            "href": str(link["href"]),
            "zone": "public" if link.get("zone") == "public" else "lan",
        }]
    return merged
