"""Prowlarr: which indexers are failing, and whether it is healthy."""

WIDGET = {
    "name": "prowlarr",
    "match": ["lscr.io/linuxserver/prowlarr", "ghcr.io/linuxserver/prowlarr", "linuxserver/prowlarr",
              "ghcr.io/hotio/prowlarr", "hotio/prowlarr"],
    "port": 9696,
    "auth": {"type": "header", "header": "X-Api-Key", "env": "PROWLARR_API_KEY"},
    # Two small answers. The spec's INDEXERS count would need /api/v1/indexer, every
    # indexer's full field schema and category tree, only to be counted -- enough on a large
    # library to spend the whole budget and lose the two answers that matter. GRABS 24H
    # would need a date in the path, and a widget's paths are fixed at load time.
    # indexerstatus lists only the indexers currently blocked after failures; the health
    # check names them.
    "get": ["/api/v1/indexerstatus", "/api/v1/health"],
}


def summarise(responses) -> dict:
    status, health = (r if isinstance(r, list) else [] for r in responses)
    failing = [item for item in status if isinstance(item, dict)]
    warnings = [h for h in health if isinstance(h, dict) and h.get("type") in ("warning", "error")]
    return {
        "stats": [
            {"k": "FAILING", "v": len(failing), "level": "warn" if failing else "ok"},
            {"k": "HEALTH", "v": "ok" if not warnings else f"{len(warnings)}",
             "level": "ok" if not warnings else "warn"},
        ],
        "rows": [{"title": str(h.get("message") or "?"), "meta": str(h.get("type"))} for h in warnings[:3]],
        "rows_label": "HEALTH" if warnings else "",
        "line": None,
    }
