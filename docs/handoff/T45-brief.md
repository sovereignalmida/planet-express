# T45 — v2.1 above-the-fold layout pass

Source spec: `docs/designs/planet_express_design_v21/handoffs/V2.1-ABOVE-THE-FOLD.md`
Visual reference: `docs/designs/planet_express_design_v21/reference/Dashboard v2.1 - Above the Fold.dc.html`

The design system (`system/cockpit.css`, 582 lines) is byte-identical to the v20 copy already
in `static/cockpit.css`. Nothing in the system changes. Everything here lands in our adapter
layer: `static/cockpit.css` (below line 582), `templates/dashboard.html`, `static/dashboard.js`,
plus a small amount of derivation in Python where Jinja can't do the work honestly.

Target: every tab's primary content sits above 900px at 1440 wide.

---

## What is *not* layout-only

The spec calls itself layout-only. Two items need backend derivation, because doing them in
Jinja would mean string-parsing router rules in a template:

1. **Network matrix grouping** — routers grouped by DNS zone, LAN twins merged, provider tag
   suppressed for `docker`. Pure function over the router list, lives beside the existing
   `extract_host` helpers, with its own tests.
2. **Overview tile summaries** — the five tiles are readouts over values that already exist
   (`ctx.containers`, `ctx.findings`, `ctx.system_and_backups`, `ctx.certs`, `ctx.traefik`,
   `ctx.adguard`). Assembling them in the template is fine; deriving "worst level" per tile is
   not, so that lands in `dashboard_data.py`.

No new collection. No new scan mode. No new field on any snapshot model.

---

## Slices

### T45.1 — Global chrome

- Header collapses to one 54px row: logo · wordmark + tagline · **tabs inline** · spacer ·
  `pipeline idle · scanned 4m ago` · `SCAN ✈` · `LOG OUT`.
- Delete `.lamp-rail` (the `PIPELINE IDLE · SENSORS NOMINAL` row) and the header `.osc-gauge`
  sparkline. Both are duplicated by the one status line and the System tile.
- Remove `<aside class="aside">` entirely. `.body-grid` becomes a single column.
- New **Crew** tab: 4-column crew cards on the left, `SHIP'S COMPUTER LOG` on the right holding
  the per-tab `professor_lines` that used to live in the sidebar.
- `main` padding `14px 18px 18px`, 12px row gap, panels 12px radius / `13px 14px` padding.

Touches: `templates/dashboard.html`, `static/cockpit.css`, `static/dashboard.js`
(`TAB_NAMES` + `DETACHED_TABS` gain `crew`).

**Constraint:** the Crew panel carries no live data, so it lives outside `#dashboard-live`
with the other detached panels and is listed in `DETACHED_TABS` — `tests/test_dashboard_tabs.py`
already fails a tab that sits outside the live grid without being declared detached.

### T45.2 — Overview

- Row 1: five tiles (`FLEET`, `HULL`, `BACKUPS`, `NETWORK`, `SYSTEM`), `repeat(5, minmax(0,1fr))`,
  10px gap. Each is a `<button>` that switches to its tab; `SYSTEM` and `FLEET` are inert.
- Tile level (border-left, gradient, hero colour) = worst state it summarises.
- Row 2: `minmax(0,1fr) 380px`. Left `SERVICES` stack tiles at DOTS density, worst-first.
  Right column: `DISK` (one row per mount, **fullest first**, source moves to `title`),
  `RECENT ERRORS` (single line, expands only when non-empty), Farnsworth one-liner.
- Remove the full-width Hull Diagnostics panel and the System panel from Overview.

Touches: `templates/dashboard.html`, `static/cockpit.css`, `dashboard_data.py`
(`summarize_overview_tiles()`), `static/dashboard.js` (tile → tab delegation).

**Constraint:** tiles live inside `#dashboard-live`, so their handlers must be re-bound by
`bindInteractions()` after every 60s swap — same as the tab buttons.

### T45.3 — Backups and Network

Backups:
- Full-width verdict strip (40px orb).
- `minmax(0,1fr) minmax(0,1fr)`: cold-storage pod laid out horizontally (head + hero + window
  bar on the left half, NEXT RUN / RESULT / LAST RUN readout on the right half of the panel);
  certificate vault as 2-up cert cards.
- Panel header carries `daily disabled on purpose · <date>` so the missing daily pod doesn't
  read as a fault. **This host's daily borg timer is off deliberately — never render it as
  broken.**

Network:
- AdGuard strip moves to the top: one row, `LIVE · n RESOLVERS` chip, QUERIES 24H, BLOCKED
  (count + %), AVG RESPONSE, allowed/blocked split bar.
- Routing matrix grouped by zone, one row per zone, 150px label column, pills wrapping right.
  Pill = LED + name. Hostname on `title`. `docker` untagged; `FILE` and `EXT` tagged. LAN twins
  merged into one pill with a `+LAN` tag. Down routers sort to the front of their zone and carry
  the level tint + `DOWN` tag.
- Header: `n routers · n names · n LAN twins merged · all up`, legend, filter input (kept).

Touches: `templates/dashboard.html`, `static/cockpit.css`, `casa_scruffy_net.py`
(`group_routers()`), `tests/`.

### T45.4 — Actions and History

Actions: `minmax(0,1fr) minmax(0,1.35fr)`. Left = flight authorisation (pending cards, else the
compact dashed empty strip + `RECENTLY AUTHORISED` rows). Right = incident console with an
`OPEN n` / `RESOLVED n` segmented filter and incidents as rows, not cards.

History: `minmax(0,1fr) 420px`. Left = run cards, 3 columns, selected card cyan. Right = sticky
run-detail pane, default selection = most recent run.

Touches: `templates/dashboard.html`, `static/cockpit.css`, `static/dashboard.js`
(`buildDeployManifest()` rewritten to emit cards + a detail pane).

**Constraint:** the approval, incident, rollback-candidate and manifest panels all sit outside
`#dashboard-live` on purpose (a 60s swap must not reset a countdown or destroy an in-flight
decision). The two-column grid therefore wraps them *outside* the live div, and
`manifest-panel` keeps its id — `refreshDashboard()` swaps that one panel by id.

### T45.5 — Chat and Config

Chat: `400px minmax(0,1fr)`, both columns full height. Left = ask form + `EARLIER TODAY` list.
Right = answer header, one-line verdict in a tinted callout, reasoning, `EVIDENCE · n CHECKS`
as 2-up cards with the raw command folded behind the `›`.

Config: `400px minmax(0,1fr)`, full height. Left = active status, the three chip groups
(`EDITABLE HERE` / `LOCKED · SENSITIVE` / everything else), the `PE_ALLOW_SENSITIVE_CONFIG_EDITS`
note, last check result. Right = full-height YAML editor with a 44px line-number gutter, lines
touching a locked key tinted red, footer toolbar `CHECK` · `REVERT` · `REVIEW CHANGES`.

**Constraint:** no shell command reaches the UI. The chat evidence card face shows the
plain-language check name; the raw argv stays behind the disclosure, and the existing
`assert "command" not in json.dumps(result)` guard stays green.

### T45.6 — Fold verification and mobile

- Screenshot every tab at 1440×900 against live-state fixtures; confirm primary content is
  above the fold.
- Every 2-column layout collapses to one column below 1024px, left column first. Overview tiles
  go `repeat(2, 1fr)` below 720px with Fleet spanning both.

---

## Process per slice

1. Implement.
2. `pytest` — full run, exit code checked directly, not through a pipe.
3. Visual check in the local preview (`scratchpad/design_preview.py`, port 8773) against the
   reference at port 8771.
4. Codex gate: `codex review --uncommitted < /dev/null`. Every finding fixed or explicitly
   declined in writing.
5. Commit with only the files that slice is about — staged by name, never `git add -A`.

One release at the end rather than five. The tabs share the chrome changed in T45.1, so an
intermediate deploy would put a half-migrated layout on the live host for no benefit; this is a
presentation-only change with no host-mutation surface.

---

## Gate ledger

| Slice | Codex rounds | Findings fixed | Declined | Status |
| --- | --- | --- | --- | --- |
| T45.1 | 1 | 2 | 0 | landed `7e47063` |
| T45.2 | 7 | 15 + 3 from my own review | 0 | landed `5f638fd`, gate fixes in `347d89d` |
| T45.3 | 1 | 4 + 1 from my own review | 1 | landed `e02f926`, gate fixes in `347d89d` |
| T45.4 | 1 | 4 | 0 | landed `5e971e6`, gate fixes in `347d89d` |
| (gate fixes) | 1 | 1 | 0 | landed `422743b` |
| T45.5 | 1 | 0 | 0 | landed `422743b`, clean first pass |

Declined, with the reason: the AdGuard chip reads `LIVE`, not `LIVE · 2 RESOLVERS`.
We do not collect a resolver count and inventing one would be fabricated data.

## T45.6 — fold verification

Measured at 1440×900 against live-state fixtures, as the bottom of the lowest visible
element in each tab's active panel:

| Tab | Bottom (px) |
| --- | --- |
| Actions | 359 |
| History | 401 |
| Backups | 468 |
| Network | 523 |
| Crew | 529 |
| Overview | 591 |
| Config | 870 |
| Chat | 878 |

All eight above the 900px fold. Chat and Config sit close to it by design: their
columns are sized to `100dvh - 104px`, so they fill the viewport rather than exceed it.

Mobile, at 375×812: every two-column grid collapses to one, the Overview tiles are
`repeat(2, 1fr)` with FLEET spanning both, and no tab scrolls horizontally
(`document.scrollWidth == 375` on all eight).

Every T45.2 finding was one shape: a summary tile claiming the host is healthier
than it is. Two causes recur and are worth checking first in every later slice:

1. **One sensor gating another's reading.** services gated the fleet, AdGuard gated
   the router count, uptime gated the memory bar, the cert list gated the backup
   jobs. Sensors that are collected separately fail separately, so they must
   degrade separately.
2. **Unknown rendered as zero.** A missing count is `—` or `?`, never `0`, and
   never "ALL NOMINAL".
