"""Mealie: how much is in the recipe box."""

WIDGET = {
    "name": "mealie",
    # Only Mealie v1. `hkotel/mealie` is the legacy v0 image: it listens on 80, not 9000, and
    # predates /api/households/statistics -- matching it would mean a panel that can only ever
    # be unreachable or 404, which is worse than no panel.
    "match": ["ghcr.io/mealie-recipes/mealie", "mealie-recipes/mealie"],
    "port": 9000,
    "auth": {"type": "header", "header": "Authorization", "prefix": "Bearer",
             "env": "MEALIE_TOKEN"},
    # /api/households/statistics, not /api/groups/statistics: the second answers non-JSON on
    # this version. Measured against the live CASA_MEALIE2.
    "get": ["/api/households/statistics"],
}


def summarise(responses) -> dict:
    (stats,) = responses
    stats = stats if isinstance(stats, dict) else {}
    return {
        "stats": [
            {"k": "RECIPES", "v": _count(stats.get("totalRecipes"))},
            {"k": "TAGS", "v": _count(stats.get("totalTags"))},
            {"k": "CATEGORIES", "v": _count(stats.get("totalCategories"))},
        ],
        "rows": [],
        "rows_label": "",
        "line": None,
    }


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
