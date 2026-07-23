"""
casa_scruffy_net.py — live (per-request) network pollers for the dashboard's Network
tab. Deliberately separate from dashboard_data.py, which promises zero I/O beyond
STATE_DIR in its own docstring -- these two functions are the one place in the
dashboard that talk to the network directly, on every page load, not from a state
file. Both are best-effort: they never raise, so a Traefik/AdGuard hiccup degrades the
Network tab instead of taking down the whole dashboard.
"""

import logging

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
            raise ValueError(f"expected a list of routers, got {type(payload).__name__}")
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
    except Exception as e:
        log.warning(f"AdGuard stats fetch failed: {e}")
        return {"available": False, "configured": True}
