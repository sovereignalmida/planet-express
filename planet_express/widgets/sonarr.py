"""Sonarr: what is queued, what is missing, and whether it is healthy."""

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
    warnings = [h for h in (health or []) if isinstance(h, dict) and h.get("type") in ("warning", "error")]
    wanted = (missing or {}).get("totalRecords", 0)
    return {
        "stats": [
            {"k": "QUEUE", "v": (queue or {}).get("totalRecords", 0)},
            {"k": "WANTED", "v": wanted, "level": "warn" if wanted else "ok"},
            {"k": "HEALTH", "v": "ok" if not warnings else f"{len(warnings)}",
             "level": "ok" if not warnings else "warn"},
        ],
        "rows": [
            {"title": item.get("title", "?"), "pct": _progress(item)}
            for item in ((queue or {}).get("records") or [])[:3] if isinstance(item, dict)
        ],
        "rows_label": "DOWNLOADING",
        "line": None,
    }


def _progress(item) -> int:
    size = item.get("size") or 0
    left = item.get("sizeleft")
    if not isinstance(size, (int, float)) or not isinstance(left, (int, float)) or not size:
        return 0
    return max(0, min(100, round((size - left) / size * 100)))
