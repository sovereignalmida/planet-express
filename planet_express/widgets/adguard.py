"""AdGuard Home: queries, how many were blocked, and how fast it answered."""

WIDGET = {
    "name": "adguard",
    "match": ["adguard/adguardhome"],
    # AdGuard uses basic auth rather than an API key; the fetcher reads both halves from the
    # host secrets file under this prefix.
    "key": "ADGUARD_CREDENTIALS",
    "port": 3000,
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
            {"k": "QUERIES", "v": f"{queries:,}"},
            # Guarded: a freshly started AdGuard reports zero queries, and the dashboard has
            # already been bitten once by a percentage computed in a template.
            {"k": "BLOCKED", "v": f"{round(blocked / queries * 100, 1)}%" if queries else "—"},
            {"k": "AVG", "v": f"{round(average * 1000)}ms" if isinstance(average, (int, float)) else "—"},
        ],
        "rows": [],
        "rows_label": "",
        "line": f"most blocked: {domain}" if domain else None,
    }
