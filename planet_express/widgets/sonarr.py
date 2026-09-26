"""Sonarr: what is queued, what is missing, and whether it is healthy."""

WIDGET = {
    "name": "sonarr",
    # lscr.io/linuxserver/sonarr is what this host runs; the others are the common
    # alternatives. normalise_image() strips the registry, so one entry covers lscr.io,
    # ghcr.io and the bare docker.io form.
    "match": ["linuxserver/sonarr", "hotio/sonarr"],
    "port": 8989,
    # How the fetcher authenticates, said explicitly rather than left to a convention:
    # Sonarr takes its key in a header, AdGuard takes basic auth, and a widget that only
    # named "a key" could not express the difference.
    "auth": {"type": "header", "header": "X-Api-Key", "env": "SONARR_API_KEY"},
    "get": ["/api/v3/queue", "/api/v3/wanted/missing", "/api/v3/series", "/api/v3/health"],
}


def summarise(responses) -> dict:
    queue, missing, series, health = responses
    warnings = [h for h in (health or []) if h.get("type") in ("warning", "error")]
    wanted = (missing or {}).get("totalRecords", 0)
    return {
        "stats": [
            {"k": "QUEUE", "v": (queue or {}).get("totalRecords", 0)},
            {"k": "WANTED", "v": wanted, "level": "warn" if wanted else "ok"},
            {"k": "SERIES", "v": len(series or [])},
            {"k": "HEALTH", "v": "ok" if not warnings else f"{len(warnings)}",
             "level": "ok" if not warnings else "warn"},
        ],
        "rows": [
            {"title": item.get("title", "?"), "pct": _progress(item)}
            for item in ((queue or {}).get("records") or [])[:3]
        ],
        "rows_label": "DOWNLOADING",
        "line": None,
    }


def _progress(item) -> int:
    size = item.get("size") or 0
    left = item.get("sizeleft")
    if not size or left is None:
        return 0
    return max(0, min(100, round((size - left) / size * 100)))
