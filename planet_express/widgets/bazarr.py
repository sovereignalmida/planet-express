"""Bazarr: subtitles still wanted, and whether its providers are answering."""

WIDGET = {
    "name": "bazarr",
    "match": ["lscr.io/linuxserver/bazarr", "ghcr.io/linuxserver/bazarr", "linuxserver/bazarr",
              "ghcr.io/hotio/bazarr", "hotio/bazarr"],
    "port": 6767,
    "auth": {"type": "header", "header": "X-API-KEY", "env": "BAZARR_API_KEY"},
    # badges for the wanted counts; providers for provider health, because badges' own
    # `providers` number is NOT a count of healthy ones. Measured on the live CASA_BAZARR:
    # /api/providers listed 8, every one "Good", while badges said `providers: 3`. Whatever
    # that 3 counts, a tile reading it would have been telling a confident untruth -- so the
    # health comes from the endpoint that states it per provider.
    "get": ["/api/badges", "/api/providers"],
}


def summarise(responses) -> dict:
    badges, providers = responses
    badges = badges if isinstance(badges, dict) else {}
    episodes = _count(badges.get("episodes"))
    movies = _count(badges.get("movies"))
    rows = providers.get("data") if isinstance(providers, dict) else providers
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    unhealthy = [str(r.get("name") or "?") for r in rows
                 if str(r.get("status") or "").strip().lower() != "good"]
    return {
        "stats": [
            # No level on either count: a wanted subtitle is ordinary, Bazarr is looking.
            {"k": "EPISODES", "v": episodes},
            {"k": "MOVIES", "v": movies},
            {"k": "PROVIDERS", "v": f"{len(rows) - len(unhealthy)}/{len(rows)}" if rows else "—",
             "level": "warn" if (unhealthy or not rows) else "ok"},
        ],
        "rows": [],
        "rows_label": "",
        "line": ("providers struggling: " + ", ".join(unhealthy[:3])) if unhealthy
                else (f"{episodes + movies} wanting subtitles" if episodes or movies else None),
    }


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
