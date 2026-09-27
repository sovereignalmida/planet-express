"""Sonarr: what is queued, what is missing, and whether it is healthy."""

import math

WIDGET = {
    "name": "sonarr",
    # lscr.io/linuxserver/sonarr is what this host runs; the others are the common
    # alternatives. normalise_image() strips the registry, so one entry covers lscr.io,
    # ghcr.io and the bare docker.io form.
    # Registry-qualified: a key goes only to an image pulled from one of these exact
    # (registry, repo) pairs -- linuxserver publishes on lscr.io, ghcr.io and Docker Hub.
    "match": ["lscr.io/linuxserver/sonarr", "ghcr.io/linuxserver/sonarr", "linuxserver/sonarr",
              "ghcr.io/hotio/sonarr", "hotio/sonarr"],
    "port": 8989,
    # How the fetcher authenticates, said explicitly rather than left to a convention:
    # Sonarr takes its key in a header, AdGuard takes basic auth, and a widget that only
    # named "a key" could not express the difference.
    "auth": {"type": "header", "header": "X-Api-Key", "env": "SONARR_API_KEY"},
    # Small, paged answers only. The spec's SERIES tile would need /api/v3/series -- every
    # series in full, several MB on a real library, just to be counted -- and parsed JSON is
    # ~25x its wire size. Three tiles is within the contract; the count is not worth it.
    "get": ["/api/v3/queue?pageSize=3", "/api/v3/wanted/missing?pageSize=1", "/api/v3/health"],
}


def summarise(responses) -> dict:
    queue, missing, health = responses
    queue = queue if isinstance(queue, dict) else {}
    missing = missing if isinstance(missing, dict) else {}
    warnings = [h for h in (health if isinstance(health, list) else [])
                if isinstance(h, dict) and h.get("type") in ("warning", "error")]
    records = queue.get("records") if isinstance(queue.get("records"), list) else []
    wanted = missing.get("totalRecords") or 0
    return {
        "stats": [
            {"k": "QUEUE", "v": queue.get("totalRecords") or 0},
            {"k": "WANTED", "v": wanted, "level": "warn" if wanted else "ok"},
            {"k": "HEALTH", "v": "ok" if not warnings else f"{len(warnings)}",
             "level": "ok" if not warnings else "warn"},
        ],
        "rows": [
            {"title": item.get("title", "?"), "pct": _progress(item)}
            for item in records[:3] if isinstance(item, dict)
        ],
        "rows_label": "DOWNLOADING",
        "line": None,
    }


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _progress(item) -> int:
    size, left = item.get("size"), item.get("sizeleft")
    if not _number(size) or not _number(left) or not size:
        return 0
    return max(0, min(100, round((size - left) / size * 100)))
