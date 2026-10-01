# Handoff: Backups tab — corrections to v3

## What this is
The v3 cockpit re-skin landed, but the **Backups tab is showing the wrong data, in the wrong order, with the loudest field being the one that means nothing.** This is not a styling pass — four of the seven faults below need a backend change before any markup can be right.

Read against `sovereignalmida/planet-express@main` (`d09f6a8`): `templates/dashboard.html` (BACKUPS panel), `static/dashboard.css` (`.cryo-*`, `.cols-certs`), `casa_leela.py`, `casa_stackctl.py`, `dashboard_data.py`.

**Order of work:** 1) backend keys → 2) collected-state markup → 3) awaiting-state markup → 4) CSS.

**Nothing here surfaces a shell command.** The `fix_steps` contract from handoff v3 is unchanged and the `assert "command" not in json.dumps(result)` guard test stays green.

## Files in this bundle
- `README.md` — this spec. The deliverable.
- `Backups Tab Reference.dc.html` + `support.js` — optional visual reference showing the corrected tab in both states. Read it for exact colors/spacing; don't lift its markup.

---

# The faults

### F1 · BLOCKER · UI only — `STATE` is the loudest field and it means nothing
The cryo pod leads with `STATE`, which is systemd `ActiveState`. Borg jobs are **oneshot units fired by timers** — between runs that value is *always* `inactive`. A perfectly healthy backup reads as dead.

`casa_hermes.py:56` already says this out loud: *"because state is 'inactive' — check result and last_run instead."* The dashboard didn't get the memo.

**Fix:** demote `state` to a small grey footnote (`unit idle between runs (oneshot) — normal`). Never colour it, never lead with it.

### F2 · BLOCKER · needs backend — no next-run time
A disabled timer and a healthy idle job render identically. This is the most load-bearing fact on the tab and it isn't collected.

`casa_stackctl.check_backups()` **already reads it** (`LastTriggerUSec`, `NextElapseUSecRealtime` off the `.timer` unit). `casa_leela.check_backups()` — the one the dashboard actually uses — only queries the `.service`.

### F3 · HIGH · needs backend — `last_run` is a raw systemd string with no age
You get `Sat 2026-07-25 03:10:42 UTC` and have to do date arithmetic in your head. "How fresh is my backup" is the question this panel exists to answer. Jinja can't parse that format; the age must be computed in `dashboard_data.py`.

### F4 · BLOCKER · needs backend — a nine-day-old backup renders green
The pod's only alert condition is `result != 'success'`. A daily job that last succeeded nine days ago and hasn't fired since passes that test. **Silent staleness is exactly how backups fail.** Freshness must be derived from age vs. cadence, not from `result` alone.

### F5 · HIGH · needs backend — certs are a table of raw `notAfter` strings
A cert expiring in 5 days and one expiring in 300 render identically — same colour, same weight, same row. `casa_leela.py:452` emits openssl's `notAfter` (`"Aug 15 12:00:00 2026 GMT"`) untouched.

### F6 · HIGH · UI only — broken-cert rows shatter the table
When some certs parse and one doesn't, `check_certs()` (casa_leela.py:479-484) appends an error dict with a full sentence stuffed into `domain` and no other keys. The template only inspects `cert_list[0]` for `error`/`note`, so that row lands as a paragraph crammed into the DOMAIN column with `?` in the other three cells.

### F7 · MED · UI only — two orphan panels, no verdict
The tab opens with two panel headers and no answer to "is my data safe." Every other tab leads with a verdict (FLEET STATUS, HULL DIAGNOSTICS). Also: the pending-plan `.dashed-footer` is nested inside the `{% else %}` branch (dashboard.html:366-372), so plan context vanishes the moment data arrives.

---

# Backend changes

## 1. `casa_leela.check_backups()` — fixes F2, F3, F4
Query the `.timer` unit alongside the `.service`. Keep returning a dict keyed `daily`/`weekly` so nothing downstream breaks.

```python
# .service props as today, plus two off the .timer
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

`state` stays in the payload — it's just demoted in the UI.

## 2. `dashboard_data.summarize_system_and_backups()` — fixes F3, F4, F7
Derive everything the template needs and pass it down pre-computed.

```python
# per job, added alongside the raw fields
"age_human":   "14h",          # or "9d 4h", "never"
"age_hours":   14.2,
"next_human":  "in 9h 48m",    # or "timer not armed"
"timer_armed": True,
"window_pct":  58,             # age / cadence, capped at 100
"freshness":   "fresh",        # fresh | stale | overdue | failed

# tab-level, new key on the summary dict
"verdict": {
    "level":  "ok",            # ok | warn | crit | unknown
    "title":  "DATA IS SAFE",
    "detail": "Both borg jobs succeeded on schedule. Newest snapshot 14h ago…",
}
```

**Freshness rules** (worst job wins the tab verdict):

| tier | condition |
| --- | --- |
| `fresh` | age < 1.5× cadence **and** `result == "success"` |
| `stale` | age ≥ 1.5× cadence, **or** timer not armed |
| `overdue` | age ≥ 3× cadence |
| `failed` | `result != "success"` |

Thresholds are a starting point — widen if your borg schedule normally drifts more than that.

## 3. `casa_leela._parse_cert_file()` / `check_certs()` — fixes F5, F6
Parse `notAfter` once at the source, and tag error rows explicitly instead of smuggling the message through `domain`.

```python
return {
    "domain":    domain,
    "sans":      sans,
    "resolver":  path.stem,        # label as SOURCE FILE in the UI
    "expires":   end_str,
+   "days_left": (parsed_end - now).days,
+   "tier":      "valid" | "renew_soon" | "expiring" | "expired",
}

# error rows — stop overwriting `domain` (casa_leela.py:481)
-   e["domain"] = f"⚠ {e['error']}"
+   e["kind"] = "error"    # template renders these as their own card
```

**Cert tiers:** `valid` > 30d · `renew_soon` ≤ 30d · `expiring` ≤ 7d (LED pulses) · `expired` ≤ 0d.

Note the naming: the code comment at `casa_leela.py:452` is explicit that these are **static file-provider certs labeled by source file stem, not ACME resolvers** (ACME is disabled on this host). Label the column **SOURCE FILE** in the UI so nobody reads `letsencrypt` into it.

---

# The corrected tab

## Verdict strip (new, top of tab)
Structurally identical to Hull Diagnostics' `.alarm-banner` — 58px orb, 18px gap, same `.crit/.high/.medium/.none` level classes. The two should be visually interchangeable.

- Orb glyph by level: `✓` ok / `!` warn / `⚠` crit / `?` unknown.
- Title: `ctx.…verdict.title`. Sub: `verdict.detail`.
- Right-aligned tally chips: `N JOBS OK` · `TIMERS ARMED` · `N CERTS`, same pill styling as `.alarm-chip`.
- Renders in **all** states, including awaiting-scan (where it reads `BACKUP STATUS UNKNOWN` in amber, with a `RUN FULL SCAN ✈` action).

## Cold Storage · Borg
Header: `COLD STORAGE · BORG` + `COLLECTED`/`AWAITING FULL SCAN` chip + right-aligned note `source: systemd units · never touches the borg repo`.

One pod per job. **Reading order top to bottom, most-answering-the-question first:**

1. **Head row** — freshness LED (green/amber/red, pulses on stale+), job name in Chakra Petch 700 20px, cadence as mono 10px (`03:10 · every 24h`), right-aligned tier badge (`FRESH` / `STALE` / `OVERDUE` / `FAILED`).
2. **Hero** — `age_human` at Chakra Petch 700 34px in the tier colour, label `SINCE LAST SNAPSHOT`.
3. **Window bar** — 5px track (reuse `.disk-bar-track` / `.disk-bar-fill`), width `window_pct`, tier colour + glow. Caption below: `58% through the 24h window` when fresh, `2d 4h past its 7d window` when not.
4. **Readout** (dashed top border): `NEXT RUN` (`next_human`, amber `timer not armed` when `timer_armed` is false) → `RESULT` (`success · exit 0`) → `LAST RUN` (absolute, dim).
5. **Footnote** — `unit idle between runs (oneshot) — normal`, or when stale: `last run succeeded, but nothing is scheduled to run again`.

Pending-plan `.dashed-footer` sits below the grid **in both branches**.

## Certificate Vault
Header: `CERTIFICATE VAULT` + availability chip + right note `Traefik file provider · ACME disabled on this host`.

Cards, not a table. Each card is structurally a `.cryo-pod` with a tier modifier — same 300px grid track, so the two panels read as one system.

1. **Head row** — tier LED (pulses on `expiring`), domain (Chakra Petch 700 15px, ellipsis), tier badge.
2. **Hero** — `days_left` + `d` at 26px in tier colour, label `UNTIL EXPIRY`.
3. **Bar** — `min(days_left / 365, 1)` of the track, tier colour.
4. **Readout** — `SOURCE FILE` (`resolver`) → `EXPIRES` (absolute) → `N SANS` (joined with ` · `).

**Error cards** (`kind == "error"`) get their own treatment: red border, pulsing LED, `UNREADABLE` in place of the domain, the error message as body copy, and only `SOURCE FILE` in the readout. Never a table row.

Keep the existing `note` empty state (`No TLS certificates declared in Traefik's file provider…`) as a plain `.muted-body` paragraph.

## Awaiting-scan state
Both panels gate on `mode == "full"`. Today the tab goes near-blank and the verdict disappears — this is the state a user hits most often, so it has to hold up.

- Verdict strip stays, amber, `BACKUP STATUS UNKNOWN`, detail: *"Last scan ran `mode: status`, which doesn't read backup or certificate state. This is not a failure — it's a blind spot."* Plus a `RUN FULL SCAN ✈` button.
- Pods still render, one per known job (daily/weekly), dashed border, `opacity: .62`, grey LED, `NO DATA` badge, and `—` in all three readout rows. The shape of what's missing is itself information.
- Pending-plan footer visible.

---

# CSS

Keep `.cryo-grid` / `.cryo-pod` / `.cryo-led` / `.cryo-stats` (dashboard.css:682-698) — the shell is right, the contents aren't.

- Bump `.cryo-grid` min track **220px → 300px**; pods now carry a hero number and a bar.
- Replace binary `.cryo-pod.alert` with four states: `.fresh` / `.stale` / `.overdue` / `.failed`. Each sets a 3px `border-left` + tinted radial glow. Reuse the severity ramp already in `.hull-card`.
- Retire `.cols-certs` (dashboard.css:561) and the cert `.grid-head`/`.grid-row` markup. Certs become `.cert-grid` / `.cert-card` with tier modifiers, same 300px track.
- New `.verdict-strip`: same geometry as `.alarm-banner`, same level classes.
- Reuse `.disk-bar-track` / `.disk-bar-fill` for both the backup-window and cert-expiry bars; only the fill colour differs.
- Move the pending-plan `.dashed-footer` out of the `{% else %}` branch.

## Colours (existing cockpit tokens, no new values)
| tier | accent | text | bg tint |
| --- | --- | --- | --- |
| fresh / valid | `#6fcf5f` | `#8fe6a0` | `rgba(111,207,95,.04)` |
| stale / renew_soon | `#f5b820` | `#f5c94a` | `rgba(245,184,32,.05)` |
| overdue / expiring / failed | `#ef3c2e` | `#ff6a5a` | `rgba(239,60,46,.06)` |
| no data | `#3f5470` | `#5f7391` | `#08101c`, dashed border, `opacity:.62` |

Motion: `beacon` on stale LEDs, `critpulse` on overdue/expiring LEDs. Both already exist.
