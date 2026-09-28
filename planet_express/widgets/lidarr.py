"""Lidarr: what is downloading, what is missing, and whether it is healthy."""

from planet_express.core.numbers import MAX_BYTES, finite, rounded

WIDGET = {
    "name": "lidarr",
    "match": ["lscr.io/linuxserver/lidarr", "ghcr.io/linuxserver/lidarr", "linuxserver/lidarr",
              "ghcr.io/hotio/lidarr", "hotio/lidarr"],
    "port": 8686,
    "auth": {"type": "header", "header": "X-Api-Key", "env": "LIDARR_API_KEY"},
    # v1, not v3: Lidarr's API is a version behind Sonarr's and Radarr's. /api/v1/health was
    # checked against the live CASA_LIDARR and answers the same [{source,type,message}] shape.
    "get": ["/api/v1/queue?pageSize=5", "/api/v1/wanted/missing?pageSize=1", "/api/v1/health"],
}


def summarise(responses) -> dict:
    queue, missing, health = responses
    queue = queue if isinstance(queue, dict) else {}
    missing = missing if isinstance(missing, dict) else {}
    warnings = [h for h in (health if isinstance(health, list) else [])
                if isinstance(h, dict) and h.get("type") in ("warning", "error")]
    records = queue.get("records") if isinstance(queue.get("records"), list) else []
    current = next((item for item in records
                    if isinstance(item, dict) and item.get("status") == "downloading"), None)
    return {
        "stats": [
            {"k": "QUEUE", "v": queue.get("totalRecords") or 0},
            {"k": "WANTED", "v": missing.get("totalRecords") or 0},
            {"k": "HEALTH", "v": "ok" if not warnings else f"{len(warnings)}",
             "level": "ok" if not warnings else "warn"},
        ],
        "rows": [],
        "rows_label": "",
        "line": f"downloading: {_title(current)} · {_progress(current)}%" if current else None,
    }


def _title(item) -> str:
    artist = item.get("artist")
    if isinstance(artist, dict) and artist.get("artistName"):
        return str(artist["artistName"])
    return str(item.get("title") or "?")


def _progress(item) -> int:
    """Percent done, or 0 when the two numbers do not make one."""
    size, left = item.get("size"), item.get("sizeleft")
    # MAX_BYTES: these are byte counts, and a 4K season pack clears the 1e12 count bound.
    if not finite(size, MAX_BYTES) or not finite(left, MAX_BYTES) or not size:
        return 0
    pct = rounded((size - left) / size * 100)
    return max(0, min(100, pct)) if pct is not None else 0
