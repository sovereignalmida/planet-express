"""Speedtest Tracker: the last result off the line."""

from planet_express.core.numbers import rounded

WIDGET = {
    "name": "speedtest",
    "match": ["lscr.io/linuxserver/speedtest-tracker", "ghcr.io/linuxserver/speedtest-tracker",
              "linuxserver/speedtest-tracker", "ghcr.io/alexjustesen/speedtest-tracker"],
    "port": 80,
    "auth": {"type": "header", "header": "Authorization", "prefix": "Bearer",
             "env": "SPEEDTEST_TOKEN"},
    # {"data": {ping, download_bits_human, upload_bits_human, ...}}. The *_human fields are
    # the app's own formatting, so the tiles read the same as its UI. Measured live.
    "get": ["/api/v1/results/latest"],
}


def summarise(responses) -> dict:
    (latest,) = responses
    latest = latest if isinstance(latest, dict) else {}
    data = latest.get("data") if isinstance(latest.get("data"), dict) else {}
    ping = data.get("ping")
    return {
        "stats": [
            {"k": "DOWN", "v": _human(data.get("download_bits_human"))},
            {"k": "UP", "v": _human(data.get("upload_bits_human"))},
            {"k": "PING", "v": f"{_ping}ms" if (_ping := rounded(ping)) is not None else "—"},
        ],
        "rows": [],
        "rows_label": "",
        "line": f"via {data['service']}" if isinstance(data.get("service"), str) else None,
    }


def _human(value) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else "—"
