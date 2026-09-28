"""Audiobookshelf: how much has been listened to, and what is in the libraries."""

from planet_express.core.numbers import finite

WIDGET = {
    "name": "audiobookshelf",
    "match": ["ghcr.io/advplyr/audiobookshelf", "advplyr/audiobookshelf"],
    # The container listens on 80 inside its own network, whatever it is published as.
    "port": 80,
    "auth": {"type": "header", "header": "Authorization", "prefix": "Bearer",
             "env": "AUDIOBOOKSHELF_TOKEN"},
    # Both measured against the live CASA_ABS. listening-stats carries totalTime and a
    # per-item map; libraries is the library list. /api/libraries/<id>/stats is a 404 here.
    "get": ["/api/me/listening-stats", "/api/libraries"],
}


def summarise(responses) -> dict:
    stats, libraries = responses
    stats = stats if isinstance(stats, dict) else {}
    libraries = libraries if isinstance(libraries, dict) else {}
    listed = libraries.get("libraries") if isinstance(libraries.get("libraries"), list) else []
    total = stats.get("totalTime")
    items = stats.get("items") if isinstance(stats.get("items"), dict) else {}
    return {
        "stats": [
            {"k": "LISTENED", "v": _hours(total)},
            {"k": "TITLES", "v": len(items)},
            {"k": "LIBRARIES", "v": len(listed)},
        ],
        "rows": [],
        "rows_label": "",
        "line": None,
    }


def _hours(seconds) -> str:
    # A formatted number is past the point normalise_summary can help: "infh" is a
    # plausible-looking lie once a widget has made it a string.
    if not finite(seconds) or seconds <= 0:
        return "—"
    hours = seconds / 3600
    return f"{hours:,.0f}h" if hours >= 1 else f"{seconds / 60:.0f}m"
