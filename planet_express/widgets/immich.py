"""Immich: how many photos and videos it holds, and how much space they use."""

from planet_express.core.numbers import MAX_BYTES, finite

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
    # finite() bounds it too: a 310-digit JSON integer is not inf, but it is not a tile either.
    return f"{value:,}" if finite(value) and isinstance(value, int) else "—"


def _size(value) -> str:
    # Binary units, as Immich's own server-stats page shows them.
    # MAX_BYTES, not MAX: this is a library size in bytes, and the ordinary count bound
    # would blank out every library past 931 GiB.
    if not finite(value, MAX_BYTES) or value < 0:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"
