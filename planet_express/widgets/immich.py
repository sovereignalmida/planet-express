"""Immich: how many photos and videos it holds, and how much space they use."""

import math

WIDGET = {
    "name": "immich",
    # Immich publishes only on ghcr.io. The machine-learning image is a different repo and
    # has no API to read, so it is deliberately not matched.
    "match": ["ghcr.io/immich-app/immich-server"],
    "port": 2283,
    # The endpoint is admin-only and permission-scoped: the key must be created by an admin
    # user and carry server.statistics (or "all"). Either missing answers 403.
    "auth": {"type": "header", "header": "x-api-key", "env": "IMMICH_API_KEY"},
    # /api/server/statistics also lists usage per user; bounded by the number of users.
    "get": ["/api/server/statistics"],
}


def summarise(responses) -> dict:
    (stats,) = responses
    stats = stats if isinstance(stats, dict) else {}
    return {
        "stats": [
            {"k": "PHOTOS", "v": _count(stats.get("photos"))},
            {"k": "VIDEOS", "v": _count(stats.get("videos"))},
            {"k": "USED", "v": _size(stats.get("usage"))},
        ],
        "rows": [],
        "rows_label": "",
        # The spec's "last upload" line needs a search request; left out rather than guessed.
        "line": None,
    }


def _count(value):
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "—"


def _size(value) -> str:
    # Binary units, as Immich's own server-stats page shows them.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"
