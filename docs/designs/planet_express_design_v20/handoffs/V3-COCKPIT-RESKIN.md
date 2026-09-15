# Handoff v3: Planet Express Dashboard — "Ship's Cockpit" redesign

## What this is
A **full visual redesign** of the Scruffy web dashboard, themed as the Planet Express ship's cockpit. It builds directly on the current `templates/dashboard.html` + `static/dashboard.css` (v2). **The data contract does not change** — every value still comes from `build_dashboard_context()` in `dashboard_data.py`. This is a re-skin + re-layout of the same four tabs and sidebar, plus **one small backend change** (documented under *Plan panel* below) to power the richer pending-plan card.

Your task: recreate this design in the real codebase (server-rendered Jinja + `dashboard.css` + `dashboard.js`), keeping that architecture. The `.dc.html` file in this bundle is a **working design reference** in a streaming-HTML prototype format — read it for exact styling/behavior, don't lift its markup verbatim.

**Fidelity: high.** Colors, type, spacing, motion, and interactions are specified here and in the reference file.

## Files in this bundle
- `Planet Express Dashboard v2.dc.html` — the cockpit design reference (all 4 tabs + sidebar + interactions). Open in a browser.
- `static/logo.png` + `static/characters/futurama/*.png` — the **real assets, already in your repo** (copied here so the reference renders). Use your existing `url_for('static', …)` paths; don't add new copies.
- `support.js` — runtime for the reference file only. **Not part of the app.** Ignore for the recreation.

---

## The theme, in one line
The old dashboard was "tasteful terminal." v3 is "you're sitting in the ship's cockpit": brushed-metal control strip with rivets, an oscilloscope load gauge, blinking status-lamp rail, a glowing reactor-core fleet grid, klaxon-grade severity escalation, and the crew on CRT monitors. A global CRT scanline + vignette ties it together.

## Palette additions (keep existing tokens; add these cockpit values)
- Page bg: `#080b14`. Control strip: `linear-gradient(180deg,#182234,#0d1626)`, border `#24344c`.
- Standard panel: bg `#0c1524`, border `#1c2c44`. Inset/track: `#08101c`.
- Reactor/green panel: `linear-gradient(180deg,#101d16,#0a1420)`, border `#1c4a3a`.
- Teal (cockpit accent): `#35c5d8` / text `#5fd6e8`. Green: `#6fcf5f` / text `#8fe6a0`.
- Amber: `#f5b820` / text `#f5c94a`. **Crit red (death button): `#ef3c2e`** / text `#ff6a5a`. High/orange: `#f0864a` / `#f4ad63`.
- Provider purple (Traefik `@file`): `#b58ff0`. Text `#dfe7f2`; dims `#9fb0c6` / `#6f849e` / `#5f7391` / `#4f6684`.
- Fonts unchanged: Chakra Petch (display/headers/big numbers), Space Grotesk (UI), JetBrains Mono (all data).

## Global shell
- **CRT overlays** (two fixed, `pointer-events:none`): scanlines `repeating-linear-gradient(180deg,transparent 0 2px,rgba(0,0,0,.12) 2px 4px)` (z 60, `mix-blend:multiply`) + vignette `radial-gradient(120% 120% at 50% 50%, transparent 62%, rgba(0,0,0,.45))` (z 59).
- **Control strip (header):** metal gradient bg, two riveted screw-dots (radial `#5c6f8a→#1a2436`) at the corners. Left: **real `logo.png`** in a 50px circle (`object-fit:contain`, dark radial fill behind the transparent cutout), red ring `2px #ef6f5e`, small green status dot top-right. Wordmark `PLANET EXPRESS` (Chakra Petch 700, `.13em`) + mono subtitle `SHIP COMPUTER v3000 · crew replaceable, containers aren't`.
- **Oscilloscope load gauge** (right of header): 132×58 CRT (`#03130f`, border `#1c4a3a`), an SVG polyline waveform stroked `#39d67f` with a marching `stroke-dashoffset` animation; overlaid `LOAD <load1>` label. Purely decorative motion — the number is real (`up.load1`).
- **Chunky SCAN button:** `linear-gradient(180deg,#6fcf5f,#3f9a34)`, hard bottom shadow `0 4px 0 #256b1f`; presses down on `:active`. Maps to the existing "refresh now" (`href="/"`).
- **Status-lamp rail** (below header): three pulsing beacons — `PIPELINE <state>`, `PLAN <id> PENDING` (only when awaiting approval), `SENSORS NOMINAL` — plus `last scan <ts>` right-aligned. Beacon = dot with `beacon` keyframe (opacity + box-shadow pulse).
- **Tabs:** cockpit toggle buttons, radius `9px 9px 0 0`; active = green-lit (`linear-gradient(180deg,#101d16,#0a1420)`, bottom border `2px #6fcf5f`, green glow). Right: `READ-ONLY · LAN-TRUST` chip. Behavior unchanged (hash-synced, per `dashboard.js`).
- **Body grid** `minmax(0,1fr) 350px`, sticky sidebar. Tab content fades via `bootin`.
- **Footer:** unchanged copy.

---

## Tab 1 — Overview

### Reactor Core (fleet health)  ← *maps to `ctx.containers`*
Green reactor panel. Big `{{ healthy }}/{{ total }}` (Chakra Petch 700, 58px, green glow) + `containers healthy`; header-right `{{ online }} ONLINE · {{ degraded }} DEGRADED · {{ down }} DOWN` (+ paused if any). Right: the **cell matrix** — one 15px rounded cell per `ctx.containers.cells` entry, **colored by 4-way state**:
- `online` → `#5fc94f` (glow), `degraded` → `#f5b820`, `down` → `#ef3c2e`, `paused` → `#8c96a0` (no glow).
- Cells softly pulse (`reactor` keyframe, staggered `animation-delay`).
- Add a **4-way legend** row (online/degraded/down/paused swatches).
- Keep the "big number turns amber/red when degraded/down" rule from your current `.fleet-num.warn/.crit`.
Dashed footer: stack completeness (same logic as today).

### Hull Diagnostics (findings)  ← *maps to `ctx.findings`* — **the important one**
Replaces the flat pills+rows table with a **severity-driven threat panel**. The panel's whole appearance is dictated by the **worst active severity**, so "is anything about to kill me?" is answerable at a glance:
- **Master alarm banner**, styled by worst tier:
  - **critical** → flashing red "button of death": red gradient bg, `deathflash` keyframe (pulsing red box-shadow), a throbbing ⚠ orb (`critpulse`), title `CRITICAL FAULT`, sub `N critical fault(s) — immediate action required`.
  - **high** → hard orange, bold + glowing, `HIGH ALERT`.
  - **medium** → calm amber, `CAUTION`.
  - **low-only** → quiet, `MINOR NOTES`.
  - **none** → green `ALL SYSTEMS NOMINAL`.
  - A 60px status **orb** (rounded square) on the left shows the tier glyph (⚠ / ! / i / ✓); severity **tally chips** (CRIT/HIGH/MED/LOW counts, zeros dimmed) sit on the right.
- **Finding cards:** render **`ctx.findings.top`** (critical/high/unknown) as individual left-accented alarm cards (crit dot flashes). This is exactly your existing `top` split.
- **Low + medium drawer:** do **not** list lows inline. Collapse `ctx.findings.medium_list` / `low_list` into a single clickable counter (`▸ N low-severity notes · informational · non-blocking`) that expands — mirrors your `<details class="findings-collapse">`, just folded harder so 20+ routine lows can never bury the signal. (Rationale is already in your `summarize_findings` comment.)
- The reference includes a **"cycle preview ⟳"** button that steps the banner through crit→high→med→none for demo. **Drop this in production** — real severity comes from the data.

### Storage Bays (disk)  ← *`ctx.disk`* — unchanged data
Glowing gauge rows: mount / source / bar+%. Bar color by `used_pct` threshold (`>=80` red `#ef3c2e`, `>=60` amber `#f5b820`, else green `#6fcf5f`) with a matching glow. Same thresholds as today.

### System + Recent Errors  ← *`ctx.system_and_backups.system`* — unchanged
Three stat tiles (UPTIME / LOAD AVG / SESSIONS) over inset panels, stacked memory bar (used green + buff/cache cyan), legend. Recent-errors panel: amber count pill (`recent_error_count`), the plain-language `diagnosis`, then the colorized terminal block over a `#03130f` phosphor background **with CRT scanlines**, one line per `parsed_errors` entry (`ts`/`host`/`proc`/`msg` coloring). Keep the `SENSOR OFFLINE` empty state for non-full scans.

## Tab 2 — Backups  ← *`ctx.system_and_backups.backups` (dict) + `ctx.certs`*
Reframed as **Cold Storage**:
- **Backup jobs as cryo pods** — one pod per `backups.items()`: icy radial glow, a status LED (green if `result == "success"`, else red/amber), the job `name`, cadence, and a STATE / RESULT / LAST RUN readout (`state`, `result`, `last_run`; exit code available too). Alert styling when `result != "success"` (matches your `alert-row`). When unavailable (non-full scan), show dormant "awaiting scan" pods + the "queued via plan {id}" note. **Two pods (daily/weekly) in the reference are placeholders** — render the real dict.
- **Certificate Vault** — `COLLECTED` grid (domain / SANs / resolver) when `ctx.certs.available` and populated; otherwise the "sealed · awaiting scan" panel. Preserve your existing error/note/empty cert states.

## Tab 3 — Network  ← *`ctx.traefik.routers` + `ctx.adguard`*
Replaces the router **table** with a **Routing Matrix**: a responsive grid (`repeat(auto-fill,minmax(212px,1fr))`) of compact **router nodes**. Each node = pulsing green status LED + **provider tag** (parsed from `name` after `@`: `docker`=cyan `#5fd6e8`, `internal`=amber `#f5c94a`, `file`=purple `#b58ff0`) + short router name + primary host (parsed from the `Host(\`…\`)` rule, with a `+N hosts` note when multiple). A legend explains the provider colors. **Click a node → it expands in place to full width** (`grid-column:1/-1`) revealing the full `rule`, `service`, router id, and status. The filter box still matches router+rule+service (your `data-filter-text` / `net-filter` logic). Down routers → red LED + `status-bad`. Keep the AdGuard panel as-is below.

## Tab 4 — Actions  ← *`ctx.update_history` + `ctx.rollback_candidates`*
Replaces the update-history **table** with a **Deployment Manifest**. Because a batch update writes many entries seconds apart, group them into a **run entry**: a card with a ↑ deploy glyph, `<stack> stack · batch update`, the time range + span, and a big service count. Below it a **timeline strip** — one glowing node per entry along a track, end-capped with first/last `ts` (hover a node for service + time) — then a **chip board**: a compact grid of `✓ <service> · <time>` chips. **Alert entries** (`h.is_alert`, i.e. status ∉ {`updated`,`no_change`}) must render red, not green (keep your `is_alert` styling). Keep the Telegram banner on top and the rollback-candidates panel when present.

---

## Agent Sidebar (sticky, all tabs)

### Ship's Computer  ← *`ctx.professor_lines` (per tab)*
Cyan-framed panel, `SHIP COMPUTER` header with pulsing LED. **Real `farnsworth.png`** in a 42px teal-ringed circle (`object-fit:cover; object-position:center top`, dark fill behind the transparent cutout). Name + `agent · online · slightly worried`. The Professor line renders in a **green-phosphor CRT readout** (`#03130f`, green text, scanline overlay), prefixed `>`. Use your existing per-tab `professor_lines` (overview/backups/network/actions) verbatim — they already have all the right conditional logic; do **not** hardcode the prototype's flavor text.

### Crew Monitors  ← static roster
Seven crew, each a **real portrait** (`static/characters/futurama/<name>.png`) in a rounded-square monitor tile with a per-member ring/glow color, name, and role. Roster + roles must match `templates/dashboard.html` exactly: Leela (system monitor), Hermes (issue analyzer), Bender (action executor), Dr. Zoidberg (canary-tested auto-patcher), Amy (diagnostician & researcher), Fry (onboarding), Scruffy (this dashboard · observes and does nothing). Portraits are transparent cutouts — frame on a dark radial fill, `object-position:center top`.

### Plan panel  ← *`ctx.pending_plan`* — **needs a small backend change**
Amber-framed `PLAN <id> · PENDING`. Design shows: `DIAGNOSIS` (the plan `title`) + a `PROPOSED FIX` **bulleted list of step descriptions** + the pending actions (`Approve via Telegram ✈` link to `t.me/<bot>`, `Dismiss`, disabled `Execute here · SOON`). Approved/dismissed state → green "Sent to `@<bot>` — tap 👍 in Telegram" confirmation (your `dashboard.js` dismiss/sessionStorage logic still applies).

**Why it needs a change:** `summarize_pending_plan()` currently forwards only `{id, priority, title, step_count}` — so the fix bullets can't render. The plan objects **do** carry the data: each `steps[]`/`rollback[]` entry has a human `description` (and a `command`). Forward the **descriptions only**:

```python
# in summarize_pending_plan(), per plan p:
"fix_steps": [s.get("description", "") for s in p.get("steps", []) if s.get("description")],
# optional, for a "rollback plan" sub-list:
"rollback_steps": [s.get("description", "") for s in p.get("rollback", []) if s.get("description")],
```

**Never forward `command`.** Your guard test `tests/test_dashboard_data.py` (`assert "command" not in json.dumps(result)`) stays green because only `description` strings cross the boundary. If you'd rather not touch the summarizer, fall back to the current lean card (`title` + `{step_count} steps` + "Full step commands live in the Telegram approval flow") — the design degrades cleanly.

Bot handle: the reference uses a single `botHandle` constant. In the app this is your templated `ctx.telegram_bot_username` (`@{{ ctx.telegram_bot_username }}`), with the "(bot not configured)" fallback you already have.

---

## Motion (keyframes to add)
- `beacon` — status-lamp dots (opacity + box-shadow pulse).
- `reactor` — reactor cells (subtle opacity breathe, staggered).
- `oscdash` — oscilloscope waveform (`stroke-dashoffset` march).
- `deathflash` — critical master-alarm banner (red box-shadow pulse).
- `critpulse` — critical orb + crit finding dots (scale + brightness).
- Keep your existing `led`, `shimmer` (scan bar), `bootin`.

## Interactions (unchanged from today unless noted)
- Tabs: hash-synced, per `dashboard.js`.
- SCAN: `href="/"` reload; while `pipeline_status.state == "running"` show the scan bar + oscilloscope reads `----`.
- Network filter: live substring over router+rule+service. **New:** click node to expand/collapse detail.
- **New:** Hull Diagnostics low/medium drawer toggle (client-side only).
- Plan Approve → `t.me/<bot>`; Dismiss → sessionStorage-scoped hide (existing logic).
- Auto-refresh 60s (existing meta refresh).

## Notes
- Everything is driven by the existing `build_dashboard_context()` — no new API. The only server change is the optional `fix_steps`/`rollback_steps` addition to `summarize_pending_plan()`.
- Respect the existing "not available (mode: status)" empty states on every panel; the cockpit styling has dormant/offline variants for each.
