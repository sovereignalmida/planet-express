"""AdGuard Home: queries, how many were blocked, and how fast it answered."""

from planet_express.core.numbers import finite, ratio, rounded

WIDGET = {
    "name": "adguard",
    # Docker Hub's, which is where AdGuard publishes; ghcr.io/adguard is someone else's account.
    "match": ["adguard/adguardhome"],
    # AdGuard answers on :80 once it is set up. 3000 is the first-run setup port, exposed by
    # the image and wrong for every configured install -- Traefik routes this host's AdGuard
    # to 172.20.0.4:80.
    "port": 80,
    # Basic auth, and the two halves the dashboard already has: config.adguard_credentials()
    # reads exactly these from /etc/planetexpress-dashboard.env. An earlier draft named a
    # single ADGUARD_CREDENTIALS variable that does not exist anywhere, which would have read
    # as "not configured" on a host that is configured.
    "auth": {"type": "basic", "username_env": "ADGUARD_USERNAME",
             "password_env": "ADGUARD_PASSWORD"},
    "get": ["/control/stats"],
}


def summarise(responses) -> dict:
    (stats,) = responses
    queries = (stats or {}).get("num_dns_queries") or 0
    blocked = (stats or {}).get("num_blocked_filtering") or 0
    average = (stats or {}).get("avg_processing_time")
    top = ((stats or {}).get("top_blocked_domains") or [{}])[0]
    domain = next(iter(top), None) if isinstance(top, dict) else None
    return {
        "stats": [
            {"k": "QUERIES", "v": f"{queries:,}" if finite(queries) else "—"},
            # Guarded: a freshly started AdGuard reports zero queries, and the dashboard has
            # already been bitten once by a percentage computed in a template.
            {"k": "BLOCKED", "v": f"{_blocked}%" if (_blocked := rounded(ratio(blocked, queries), 1)) is not None else "—"},
            {"k": "AVG", "v": f"{_avg}ms" if (_avg := rounded(average * 1000 if finite(average) else None)) is not None else "—"},
        ],
        "rows": [],
        "rows_label": "",
        "line": f"most blocked: {domain}" if domain else None,
    }
