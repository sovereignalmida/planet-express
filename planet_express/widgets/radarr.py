"""Radarr: what is downloading, what is missing, and whether it is healthy."""

import math

WIDGET = {
    "name": "radarr",
    # Registry-qualified, as Sonarr's: linuxserver publishes on lscr.io, ghcr.io and Docker
    # Hub; hotio on ghcr.io and Docker Hub.
    "match": ["lscr.io/linuxserver/radarr", "ghcr.io/linuxserver/radarr", "linuxserver/radarr",
              "ghcr.io/hotio/radarr", "hotio/radarr"],
    "port": 7878,
    "auth": {"type": "header", "header": "X-Api-Key", "env": "RADARR_API_KEY"},
    # Needs Radarr 5.6 or later: /api/v3/wanted/missing is new in 5.6, and an older Radarr
    # answers it 404. The spec's MOVIES tile would need /api/v3/movie, every movie in full
    # and unpaged, only to be counted: the trade Sonarr's SERIES tile lost. HEALTH takes its
    # place. Five queue items with their movie, so the line can name one actually
    # downloading rather than an import stuck at 100%.
    "get": ["/api/v3/queue?pageSize=5&includeMovie=true", "/api/v3/wanted/missing?pageSize=1",
            "/api/v3/health"],
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
            # No level: Radarr counts every monitored movie without a file, announced ones
            # included, so a number here is not by itself something wrong.
            {"k": "WANTED", "v": missing.get("totalRecords") or 0},
            {"k": "HEALTH", "v": "ok" if not warnings else f"{len(warnings)}",
             "level": "ok" if not warnings else "warn"},
        ],
        "rows": [],
        "rows_label": "",
        "line": f"downloading: {_title(current)} · {_progress(current)}%" if current else None,
    }


def _title(item) -> str:
    movie = item.get("movie")
    if isinstance(movie, dict) and movie.get("title"):
        year = movie.get("year")
        return f"{movie['title']} ({year})" if isinstance(year, int) and year else str(movie["title"])
    return str(item.get("title") or "?")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _progress(item) -> int:
    size, left = item.get("size"), item.get("sizeleft")
    if not _number(size) or not _number(left) or not size:
        return 0
    return max(0, min(100, round((size - left) / size * 100)))
