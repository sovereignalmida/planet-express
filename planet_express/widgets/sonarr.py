"""Sonarr: what is queued, what is missing, and whether it is healthy."""

from planet_express.core.numbers import MAX_BYTES, finite, rounded

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


def _progress(item) -> int:
    """Percent done, or 0 when the two numbers do not make one."""
    size, left = item.get("size"), item.get("sizeleft")
    # MAX_BYTES: these are byte counts, and a 4K season pack clears the 1e12 count bound.
    if not finite(size, MAX_BYTES) or not finite(left, MAX_BYTES) or not size:
        return 0
    pct = rounded((size - left) / size * 100)
    return max(0, min(100, pct)) if pct is not None else 0
