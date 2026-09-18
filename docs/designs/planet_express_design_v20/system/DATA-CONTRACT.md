# Data contract

Keys the templates expect from `build_dashboard_context()`. Where a key doesn't exist yet, the change is listed with its file.

**Nothing here surfaces a shell command.** The `fix_steps` contract is descriptions-only and the `assert "command" not in json.dumps(result)` guard stays green.

---

## Tab verdict (new — every tab)

Each tab summary dict gains one key. Without it, tabs open with panel headers and no answer.

```python
"verdict": {
    "level":  "ok",            # ok | warn | crit | unknown
    "title":  "DATA IS SAFE",
    "detail": "Both borg jobs succeeded on schedule. Newest snapshot 14h ago…",
}
```

Worst constituent state wins the level.

---

## Backups tab

The v3 tab renders the wrong fields. Four faults need backend changes.

### `casa_leela.check_backups()`

Borg jobs are **oneshot units fired by timers** — `ActiveState` is *always* `inactive` between runs, so a healthy backup reads as dead. `casa_hermes.py:56` already warns against alarming on it. Worse, without the timer there's no way to tell a disarmed timer from a healthy idle job.

`casa_stackctl.check_backups()` already reads the timer. Lift the two properties across:

```python
result[key] = {
    "state":         props.get("ActiveState", "unknown"),
    "result":        props.get("Result", "unknown"),
    "exit_code":     props.get("ExecMainStatus", "?"),
    "last_run":      props.get("InactiveExitTimestamp", "n/a"),
+   "next_run":      tmr.get("NextElapseUSecRealtime", "n/a"),
+   "last_trigger":  tmr.get("LastTriggerUSec", "n/a"),
+   "cadence_hours": 24 if key == "daily" else 168,
}
```

`state` stays in the payload — it's demoted to `.pe-footnote` in the UI, never coloured.

### `dashboard_data.summarize_system_and_backups()`

Jinja can't do date maths on systemd timestamps. Derive it here.

```python
"age_human":   "14h",          # or "9d 4h", "never"
"age_hours":   14.2,
"next_human":  "in 9h 48m",    # or "timer not armed"
"timer_armed": True,
"window_pct":  58,             # age / cadence, capped at 100
"window_caption": "58% through the 24h window",   # or "2d 4h past its 7d window"
"freshness":   "fresh",        # fresh | stale | overdue | failed
```

**Freshness rules.** Today the only alert condition is `result != "success"` — so a daily job that last succeeded nine days ago and hasn't fired since renders green. Silent staleness is how backups actually fail.

| `freshness` | condition | card modifier |
| --- | --- | --- |
| `fresh` | age < 1.5× cadence **and** `result == "success"` | `ok` |
| `stale` | age ≥ 1.5× cadence, **or** timer not armed | `warn` |
| `overdue` | age ≥ 3× cadence | `crit` |
| `failed` | `result != "success"` | `crit` |

Thresholds are a starting point — widen if the borg schedule normally drifts more than that.

### `casa_leela._parse_cert_file()` / `check_certs()`

`notAfter` ships raw (`"Aug 15 12:00:00 2026 GMT"`), so a cert expiring in 5 days and one expiring in 300 render identically. Parse once, at the source:

```python
return {
    "domain":    domain,
    "sans":      sans,
    "resolver":  path.stem,        # label SOURCE FILE in the UI, not "resolver"
    "expires":   end_str,
+   "days_left": (parsed_end - now).days,
+   "tier":      "valid",          # valid | renew_soon | expiring | expired
+   "life_pct":  min(days_left / 365 * 100, 100),
}
```

| `tier` | condition | card modifier |
| --- | --- | --- |
| `valid` | > 30d | `ok` |
| `renew_soon` | ≤ 30d | `warn` |
| `expiring` | ≤ 7d | `crit` (LED pulses) |
| `expired` | ≤ 0d | `crit` |

**Error rows.** `check_certs()` currently stuffs a full sentence into `domain` (`casa_leela.py:481`), and the template only inspects `cert_list[0]` — so a mixed list renders one row as a paragraph in the DOMAIN column with `?` everywhere else.

```python
-   e["domain"] = f"⚠ {e['error']}"
+   e["kind"]   = "error"    # template renders these as their own .pe-card.crit
```

Naming note: the comment at `casa_leela.py:452` is explicit that these are **static file-provider certs labelled by source-file stem, not ACME resolvers** — ACME is disabled on this host. Label the field `SOURCE FILE` so nobody reads `letsencrypt` into it.

---

## Availability gating

Panels gate on scan mode (`mode == "full"`). Never blank the panel:

- keep the verdict strip, switch it to `unknown` / amber with a `RUN FULL SCAN ✈` action;
- render one `.pe-card.none` per *known* entity with `—` in every readout row;
- add `.pe-sensor-dark` naming the mode that would collect the data;
- keep `.pe-dashed-footer` (pending plan) visible — it currently lives inside the `{% else %}` branch at `dashboard.html:366-372` and vanishes the moment data arrives.

---

## Status level mapping

Every domain's states collapse onto the four card modifiers. Add new sources to this table rather than inventing a colour.

| Source | `ok` | `warn` | `high` | `crit` | `none` |
| --- | --- | --- | --- | --- | --- |
| Container | online | degraded | — | down | paused |
| Backup job | fresh | stale | — | overdue, failed | not collected |
| Certificate | valid | renew_soon | — | expiring, expired | not collected |
| Hull finding | — | MED | HIGH | CRIT | LOW (rolled up) |
| Disk | < 75% | 75–89% | 90–94% | ≥ 95% | unreadable |
| Router | reachable | slow | — | unreachable | not probed |

---

## Stack rollup (Overview services panel)

Four derived fields per compose stack, so the panel can render as cards instead of 88 rows. Full spec in `handoffs/SERVICES-STACK-CARDS.md`.

```python
# dashboard_data.py — per stack
"up":    14,                 # count of members at level ok
"total": 16,
"level": "crit",             # worst member: crit > warn > idle > ok
"note":  "sonarr down · bazarr degraded",   # non-ok members only, "" when clean
```

```python
WORD = {"crit": "down", "warn": "degraded", "idle": "paused"}
RANK = {"crit": 0, "warn": 1, "idle": 2, "ok": 3}
note = " · ".join(f'{c["name"]} {WORD[c["level"]]}' for c in bad)
stacks.sort(key=lambda s: (RANK[s["level"]], -s["total"], s["name"]))
```

Member `level` maps from the existing 4-way container state, same as the reactor cells: `online → ok`, `degraded → warn`, `down → crit`, `paused → idle`. No new collection and no change to `check_containers()`.
