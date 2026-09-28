"""Karakeep: what is saved, and how much of it is still unfiled."""

WIDGET = {
    "name": "karakeep",
    "match": ["ghcr.io/karakeep-app/karakeep", "karakeep-app/karakeep",
              "ghcr.io/hoarder-app/hoarder", "hoarder-app/hoarder"],
    "port": 3000,
    "auth": {"type": "header", "header": "Authorization", "prefix": "Bearer",
             "env": "KARAKEEP_TOKEN"},
    # One call that already carries every count the tiles need. Measured against the live
    # CASA_KARA_KEEP: {numBookmarks, numFavorites, numArchived, numTags, numLists, ...}.
    "get": ["/api/v1/users/me/stats"],
}


def summarise(responses) -> dict:
    (stats,) = responses
    stats = stats if isinstance(stats, dict) else {}
    return {
        "stats": [
            {"k": "SAVED", "v": f"{_count(stats.get('numBookmarks')):,}"},
            {"k": "ARCHIVED", "v": f"{_count(stats.get('numArchived')):,}"},
            {"k": "TAGS", "v": _count(stats.get("numTags"))},
            {"k": "LISTS", "v": _count(stats.get("numLists"))},
        ],
        "rows": [],
        "rows_label": "",
        "line": None,
    }


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
