"""Miniflux: how much is unread, across how many feeds."""

WIDGET = {
    "name": "miniflux",
    "match": ["miniflux/miniflux", "ghcr.io/miniflux/miniflux"],
    "port": 8080,
    "auth": {"type": "header", "header": "X-Auth-Token", "env": "MINIFLUX_TOKEN"},
    # {"reads": {feed_id: n}, "unreads": {feed_id: n}} -- one small object, no entry bodies.
    # Measured against the live CASA_MINIFLUX.
    "get": ["/v1/feeds/counters"],
}


def summarise(responses) -> dict:
    (counters,) = responses
    counters = counters if isinstance(counters, dict) else {}
    unreads = counters.get("unreads") if isinstance(counters.get("unreads"), dict) else {}
    total = sum(n for n in unreads.values() if isinstance(n, int) and not isinstance(n, bool))
    with_unread = sum(1 for n in unreads.values()
                      if isinstance(n, int) and not isinstance(n, bool) and n > 0)
    return {
        "stats": [
            # No level: unread mail is not a fault.
            {"k": "UNREAD", "v": f"{total:,}"},
            {"k": "FEEDS", "v": with_unread},
        ],
        "rows": [],
        "rows_label": "",
        "line": None,
    }
