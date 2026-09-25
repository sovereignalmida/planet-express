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
      * `x-lan` collapses into its sibling `x` when that sibling exists in the same zone,
        carrying a `+LAN` tag -- an orphan `x-lan` with no sibling keeps its own pill,
        because dropping a router is worse than showing a twin;
      * a router that is not `enabled` sorts to the front of its zone with a DOWN tag.
    """
    pills_by_zone: dict[str, dict] = {}
    for router in routers:
        if not isinstance(router, dict):
            continue
        raw = str(router.get("name", "?"))
        short, _, provider = raw.partition("@")
        zone, host = _router_zone(router.get("rule", ""))
        down = router.get("status") != "enabled"
        pills_by_zone.setdefault(zone, {})[short] = {
            "name": short, "raw": raw, "host": host, "down": down,
            "tag": "" if provider == _DEFAULT_PROVIDER else _PROVIDER_TAGS.get(provider, "EXT"),
            "lan": False,
            "filter_text": f'{raw} {router.get("rule", "")} {router.get("service", "")}'.lower(),
        }

    merged = 0
    zones = []
    for zone, pills in pills_by_zone.items():
        for name in [n for n in pills if n.endswith("-lan")]:
            sibling = pills.get(name[: -len("-lan")])
            if sibling is None:
                continue
            sibling["lan"] = True
            # The twin's own hostname still has to match the filter, or filtering by a LAN
            # hostname would silently find nothing.
            sibling["filter_text"] += " " + pills[name]["filter_text"]
            sibling["down"] = sibling["down"] or pills[name]["down"]
            del pills[name]
            merged += 1
        items = sorted(pills.values(), key=lambda pill: (not pill["down"], pill["name"]))
        zones.append({"name": zone, "count": len(items), "items": items,
                      "down": sum(pill["down"] for pill in items)})

    # Biggest zone first, but the catch-all bucket always last: it is a leftovers pile, not
    # a place, and reading it first tells you nothing about the host.
    zones.sort(key=lambda z: (z["name"] == _UNZONED, -z["count"], z["name"]))
    names = sum(zone["count"] for zone in zones)
    return {
        "zones": zones,
        "total": len(routers),
        "names": names,
        "merged": merged,
        "down": sum(zone["down"] for zone in zones),
    }
