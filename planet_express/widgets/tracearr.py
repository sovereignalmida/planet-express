"""Tracearr: whether its pieces are up. No key: /health needs none."""

WIDGET = {
    "name": "tracearr",
    "match": ["ghcr.io/connorgallopo/tracearr", "connorgallopo/tracearr"],
    "port": 3000,
    # No auth at all. /health is unauthenticated on this app, and a widget that needs no
    # credential is a widget that can never leak one -- the registry allows auth to be absent.
    "get": ["/health"],
}


def summarise(responses) -> dict:
    (health,) = responses
    health = health if isinstance(health, dict) else {}
    status = health.get("status")
    pieces = [("DB", health.get("db")), ("REDIS", health.get("redis")),
              ("GEOIP", health.get("geoip"))]
    down = [name for name, ok in pieces if ok is not True]
    return {
        "stats": [
            {"k": "STATUS", "v": str(status or "?"),
             "level": "ok" if status == "ok" else "warn"},
            *[{"k": name, "v": "up" if ok is True else "down",
               "level": "ok" if ok is True else "warn"} for name, ok in pieces],
        ],
        "rows": [],
        "rows_label": "",
        "line": ", ".join(down).lower() + " down" if down else None,
    }
