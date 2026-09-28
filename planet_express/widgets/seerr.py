"""Seerr: requests waiting on a human, and what has been dealt with."""

WIDGET = {
    "name": "seerr",
    # Seerr is the maintained fork of Overseerr and answers the same /api/v1 on the same
    # port, so both are listed. Docker Hub's Overseerr is `sctx/overseerr`, not `sct/` --
    # the repo is matched whole, so the wrong spelling matches nothing at all.
    # Every registry each publisher uses, as the other linuxserver widgets do: matching
    # normalises the registry away, but provenance checks the digest's registry against this
    # list, so a source missing here is a container that matches and then gets no key.
    "match": ["ghcr.io/seerr-team/seerr", "seerr-team/seerr",
              "sctx/overseerr", "ghcr.io/sct/overseerr",
              "lscr.io/linuxserver/overseerr", "ghcr.io/linuxserver/overseerr",
              "linuxserver/overseerr"],
    "port": 5055,
    "auth": {"type": "header", "header": "X-Api-Key", "env": "SEERR_API_KEY"},
    "get": ["/api/v1/request/count"],
}


def summarise(responses) -> dict:
    (counts,) = responses
    counts = counts if isinstance(counts, dict) else {}
    pending = _count(counts.get("pending"))
    return {
        "stats": [
            # PENDING is the only one anybody acts on, so it is the only one that colours.
            {"k": "PENDING", "v": pending, "level": "warn" if pending else "ok"},
            {"k": "APPROVED", "v": _count(counts.get("approved"))},
            {"k": "AVAILABLE", "v": _count(counts.get("available"))},
            {"k": "TOTAL", "v": _count(counts.get("total"))},
        ],
        "rows": [],
        "rows_label": "",
        "line": f"{pending} waiting on you" if pending else None,
    }


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
