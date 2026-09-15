# Handoff: Planet Express — Sysadmin Agent Dashboard

## Overview
Planet Express is a Futurama-themed, self-hosted sysadmin agent for a Docker Compose homelab. It watches stacks, diagnoses real failures, proposes fixes, canary-updates images with rollback, and talks over Telegram. This handoff covers a **redesign of the web dashboard** — currently a bare read-only status page (Uptime-Kuma/Beszel-like). The redesign turns it into a "ship's console": fleet health at a glance, an in-app agent (Professor Farnsworth) that narrates and surfaces a pending fix plan, and honest framing of the read-only → actionable roadmap.

The current app is **read-only** (no auth, LAN-trust). Actions today execute via a Telegram bot (`@casasrvrbot`). The design deliberately shows an "Execute here · SOON" affordance and an "Approve via Telegram" button to signal the intended migration toward taking actions from the dashboard **or** Telegram.

## About the Design Files
The file in this bundle (`Planet Express Dashboard.dc.html`) is a **design reference created in HTML** — a working prototype showing intended look and behavior, **not production code to copy directly**. It is authored as a self-contained "Design Component" (a custom streaming-HTML format), so its markup/logic split is not meant to be lifted verbatim.

Your task: **recreate this design in the existing dashboard codebase**, using that project's framework, patterns, and libraries. If the current dashboard is plain server-rendered HTML/JS, keep it that way; if it's a JS framework, use it. This README is the source of truth — a developer who wasn't in the conversation should be able to build from it alone.

## Fidelity
**High-fidelity.** Final colors, typography, spacing, and interactions are specified below. Recreate the UI to match, adapting only where the codebase's conventions demand.

---

## Global Shell

- **Page background:** `#080b11` with two faint radial glows: `radial-gradient(1200px 600px at 78% -10%, rgba(70,201,90,.06), transparent 60%)` (green, top-right) and `radial-gradient(900px 500px at 10% 110%, rgba(226,59,46,.05), transparent 55%)` (red, bottom-left).
- **Scanline overlay:** fixed, full-viewport, `pointer-events:none`, `z-index:50`, `background: repeating-linear-gradient(180deg, rgba(255,255,255,.012) 0 1px, transparent 1px 3px)`. Very subtle CRT texture.
- **Body font:** `'Space Grotesk'`. **Data/mono font:** `'JetBrains Mono'`. **Display/headers:** `'Chakra Petch'` (500/600/700). Load from Google Fonts.
- **Default text color:** `#d6deea`. **Muted:** `#7c8a9a`. **Faint:** `#4a5666` / `#5c6a7a`.

### Top bar
- Flex row, `padding:18px 26px 16px`, bottom border `1px solid #16202c`, background `linear-gradient(180deg, rgba(16,22,30,.85), rgba(10,14,19,.6))`.
- **Logo lockup (left):** 48×48 circle, `background: radial-gradient(circle at 40% 32%, #f3e8cd, #d9c9a2)`, `border:2px solid #e23b2e`, `box-shadow:0 0 0 2px #0c1017, 0 0 22px rgba(226,59,46,.25)`. Centered monogram "PE" in Chakra Petch 700 18px `#c22a1f`. A small 8px green dot (`#46c95a`, glow) sits at top-right of the circle. Next to it: wordmark `PLANET EXPRESS` (Chakra Petch 700, 20px, letter-spacing `.14em`, `#ece0c4`) and tagline `sysadmin agent · our crew is replaceable, your containers aren't` (JetBrains Mono 11px `#6f7d8d`).
- **Right side:** pipeline status line — green LED dot (pulsing, see keyframes) + `pipeline` (muted) + `idle` (`#8fe6a0` 600) + `· pending plan` (`#6f7d8d`) + `p1` pill (`#f4b942`, bg `rgba(244,185,66,.12)`, border `rgba(244,185,66,.3)`, radius 5px, padding `1px 7px`). Below it: `last scan: <ISO timestamp>` (JetBrains Mono 11px `#5c6a7a`).
- **Refresh button:** border `1px solid #2f6b3a`, bg `rgba(70,201,90,.1)`, text `#8fe6a0`, 600 13px, padding `9px 15px`, radius 9px, `↻ refresh now`. Hover: bg `rgba(70,201,90,.2)`, border `#46c95a`.

### Scan progress bar
- Shown only while scanning. 2px tall track `#0e1420`; a 25%-wide bar `linear-gradient(90deg, transparent, #46c95a, transparent)` sweeps left→right via `shimmer` keyframe (1.1s linear infinite).

### Tab bar
- Flex row, `padding:14px 26px 0`, gap 8px. Tabs: **Overview, Backups, Network, Actions**.
- **Inactive tab:** border `1px solid #1a2532`, transparent bg, text `#8b98a8`, Space Grotesk 600 13px, letter-spacing `.03em`, padding `9px 18px`, radius 9px.
- **Active tab:** border `#2f6b3a`, bg `rgba(70,201,90,.12)`, text `#8fe6a0`.
- Far right: a static chip `READ-ONLY · LAN-TRUST` (JetBrains Mono 11px `#4a5666`, border `#16202c`, radius 6px, padding `4px 10px`).

### Body layout
- CSS grid, `grid-template-columns: minmax(0,1fr) 360px`, gap 22px, `padding:22px 26px 40px`, `align-items:start`. Left = main content (per-tab), right = sticky agent sidebar (`position:sticky; top:22px`).
- Each tab's content fades in via `bootin` keyframe (`.3s ease both`).

### Footer
- Centered, JetBrains Mono 12px `#4a5666`: `Planet Express — read-only, no auth, LAN-trust · auto-refreshes every 60s · held together by good news`

---

## Screens / Views (tabs)

### 1. Overview (default) — *the hero*
Vertical stack, gap 18px.

- **Fleet health hero panel** — border `1px solid #1c3524`, radius 14px, bg `linear-gradient(180deg, rgba(70,201,90,.06), rgba(16,22,30,.5))`, padding `22px 24px`. Label `FLEET STATUS` top-right (JetBrains Mono 11px `#3f7a4c`). Left block: `CONTAINERS & STACKS` eyebrow, then big count — `70` (Chakra Petch 700, 52px, `#5fd06e`, `text-shadow:0 0 26px rgba(70,201,90,.4)`) + `/ 70` (26px `#6f7d8d`) + `containers healthy` (`#8fe6a0`). Subline `70 online · 0 degraded · 0 down`. Right block: **container matrix** — 70 cells, `flex-wrap`, gap 5px; each cell 15×15, radius 3px, `background: rgba(70,201,90,.85)`, `box-shadow:0 0 5px rgba(70,201,90,.5)`. The whole matrix gently pulses (`livepulse` 3s). Bottom, dashed top-border: `Stack completeness: not available until the next full scan.`
- **Findings panel** — border `#1a2531`, radius 14px, bg `rgba(16,22,30,.5)`, padding `20px 22px`. Header `FINDINGS` (Chakra Petch 600 15px `#ece0c4`) + severity pills: `0 critical` (red `#e2554a`), `0 high` (orange `#f0864a`), `1 medium` (amber `#f4b942`), `0 low` (cyan `#56b6e0`) — each JetBrains Mono 11.5px 600, tinted bg + border, radius 20px, padding `3px 11px`. Table cols `SEVERITY / RESOURCE / DESCRIPTION` (grid `120px 130px 1fr`). One row, left-accented with `3px solid #f4b942` and faint amber bg: **MEDIUM · backups · "Backups are marked inactive despite previous successful runs, so scheduling/automation may be disabled."**
- **Disk panel** — same panel style. Header `DISK`. Cols `MOUNT / SOURCE / USED` (grid `1.6fr 1.4fr 220px`). Rows (mount, source, used%):
  - `/` · `/dev/sdb2` · **76%**
  - `/home` · `/dev/sdb3` · **37%**
  - `/media/casaroot/CMSSTORAGE` · `/dev/sda` · **4%**
  - `/home/casaroot/casafast` · `/dev/sdc` · **18%**
  - `/casamedia` · `//192.168.1.171/data` · **54%**
  - Each row: mount (JetBrains Mono 12.5px `#c3ccd8`), source (12px `#7c8a9a`), then a bar (7px tall, track `#0e1622`, radius 4px) filled to `used%` + the % label (`#b6c1cf`). **Bar color by threshold:** `>=80` → `#e2554a`, `>=60` → `#f4b942`, else `#46c95a`.
- **System panel** — has **two states**, driven by whether the last scan was a full scan (see 🆕 **System panel — populated** below):
  - *Empty (mode: status):* header `SYSTEM` + amber `SENSOR OFFLINE` chip. Body: `Not available — last scan (mode: status) didn't collect system data. Available after the next full scan.` `(mode: status)` in mono.
  - *Populated (mode: full):* the new tiles + memory + recent-errors layout described below.

#### 🆕 System panel — populated (mode: full)  *[NEW — added after the initial handoff]*
> **New since the first bundle.** This state was already built in the prototype (`sysOnline` flag in `renderVals`, default `true`). It renders when a full scan has collected system data; the empty `SENSOR OFFLINE` state above is behind the `sysOffline` flag. Source of the data below is the scan payload keys `memory_summary`, `uptime`, `recent_errors`, `recent_error_count`.

Two stacked panels, gap 18px.

**System panel** (standard panel style):
- Header `SYSTEM` + a green online chip: `<green dot> casamediaserver` (JetBrains Mono 10.5px `#8fe6a0`, border/bg green tints).
- **Three stat tiles**, grid `repeat(3,1fr)`, gap 12px; each tile border `#16202c`, bg `#0c1119`, radius 10px, padding `13px 15px`, with a mono 10px `#5c6a7a` eyebrow, a Chakra Petch 600 19px value, and a mono 11px `#6f7d8d` subline:
  - **UPTIME** → `20d 23h 43m` / `since Jul 02 · 16:26`. (Parsed from `uptime`: `up 20 days, 23:43`.)
  - **LOAD AVG** → `1.74` (green) `/ 1.31 / 1.12` (muted) / `1m · 5m · 15m`. (From `load average: 1.74, 1.31, 1.12`.)
  - **SESSIONS** → `5 users` / `local time 16:09:30`. (From `5 users` + the leading `16:09:30`.)
- **Memory** (parsed from `memory_summary`: total 15Gi, used 10Gi, free 556Mi, shared 662Mi, buff/cache 5.5Gi, available 5.1Gi):
  - Label row: `MEMORY` eyebrow (left) + `10Gi used / 15Gi total · 5.1Gi available` (right, mono 12px).
  - **Stacked bar** — 10px tall, radius 5px, track `#0e1622`: segment 1 = used `width:67%` solid green `#46c95a`; segment 2 = buff/cache `width:23%` `rgba(86,182,224,.5)`. (Percentages = value ÷ 15Gi total; used ≈ 67%, buff/cache ≈ 23%.)
  - **Legend** (mono 11px `#7c8a9a`, gap 18px): square swatches — `used 10Gi` (green), `buff/cache 5.5Gi` (cyan), `free 556Mi` (empty/outlined), plus `shared 662Mi` (no swatch).

**Recent Errors panel** (amber-tinted: border `#3a2a20`, bg `linear-gradient(180deg, rgba(244,185,66,.04), rgba(16,22,30,.5))`):
- Header `RECENT ERRORS` + amber count pill = `recent_error_count` (`3`; JetBrains Mono 11px 600, bg `rgba(244,185,66,.14)`, border `rgba(244,185,66,.4)`, radius 20px).
- **Plain-language diagnosis** (12.5px `#c3b48a`): `3 auth failures — a sudo restart of casa-dashboard.service is prompting for a password it can't supply non-interactively.` (`casa-dashboard.service` in mono `#f4c968`.) This is derived from the log lines, not a raw field — recreate an equivalent human summary.
- **Terminal log block** — bg `#0a0e14`, border `#16202c`, radius 10px, padding `12px 14px`, `overflow-x:auto`, JetBrains Mono 11.5px line-height 1.75. One row per entry in `recent_errors`, `white-space:nowrap`, left border `2px solid #6b5220`, padding-left 10px. Each line is colorized: timestamp `#5c6a7a`, host (`casamediaserver`) `#8fd0ef`, process (`sudo[785667]:`) `#c3b48a`, message `#b6a06a`. Parse each raw line into `{ts, host, proc, msg}` (split on the first 3 whitespace tokens = `Mon DD HH:MM:SS`, then host, then `proc:`, then the remainder as message).

### 2. Backups
Two panels, gap 18px, same panel style.
- **BACKUPS** + amber `AWAITING FULL SCAN` chip. Body: `Not available — last scan (mode: status) didn't collect backup data. Available after the next full scan.` Dashed-top footer with amber LED: `next full scan queued via plan p1`.
- **CERTIFICATES** + amber chip. Body: `Not available — last scan (mode: status) didn't collect certificate data. Available after the next full scan.`

### 3. Network
Single panel.
- Header row: `TRAEFIK ROUTERS` + `showing <shown> of <total>` counter + a right-aligned **filter input** (bg `#0c1119`, border `#1e2836`, radius 8px, JetBrains Mono 12.5px, width 230px, placeholder `filter routers…`, focus border `#2f6b3a`).
- Table cols `ROUTER / RULE / SERVICE / STATUS` (grid `1.1fr 2.4fr 1fr 90px`). Router in `#8fd0ef`, rule in `#7c8a9a` (truncated with ellipsis, full value in `title` attr), service in `#c3ccd8`, status = green dot + `enabled` (`#8fe6a0`). All mono. See **Data → routers** for the full 31-row list. Filter matches router + rule + service, case-insensitive.

### 4. Actions
- **Telegram banner** — border `#23364a`, radius 12px, bg `rgba(86,182,224,.06)`, padding `13px 16px`, `✈` glyph + `Actions currently execute through Telegram — @casasrvrbot. In-dashboard execution is on the way; this view is your read-only audit trail for now.` (`@casasrvrbot` in mono `#8fd0ef`).
- **RECENT UPDATE HISTORY** panel — cols `WHEN / STACK / SERVICE / STATUS / REASON` (grid `2.2fr 1fr 1.4fr 100px 80px`). All rows: stack `services`, status green-dot `updated`, reason `—`. See **Data → updates** for the 20-row list.

---

## Agent Sidebar (sticky, right column, all tabs)

### Ship's Computer / Professor Farnsworth panel
- Border `#24384c`, radius 14px, bg `linear-gradient(180deg, rgba(86,182,224,.05), rgba(16,22,30,.6))`.
- Header strip: cyan LED (pulsing) + `SHIP'S COMPUTER` (Chakra Petch 600 12px `#9fc9e0`), bottom border `#16202c`, bg `rgba(10,14,19,.4)`.
- Body: 40×40 avatar circle (`radial-gradient(circle at 42% 35%, #d9e4ef, #9fb2c4)`, border `2px solid #56b6e0`, monogram `PF` `#2a4a5e`), name `Prof. Farnsworth` (700 13.5px), subtitle `agent · online · slightly worried` (mono 10.5px `#6f7d8d`). Below: a speech box (bg `#0c1119`, border `#1c2836`, radius 10px, padding `13px 14px`, 13px `#c9d3df`) holding the **per-tab Professor line** (see Copy).

### Pending Plan p1 panel
- Border `#3a3320`, radius 14px, bg `linear-gradient(180deg, rgba(244,185,66,.05), rgba(16,22,30,.6))`.
- Header strip: amber LED + `PLAN p1 · PENDING` (Chakra Petch 600 12px `#f4c968`).
- Body:
  - `DIAGNOSIS` eyebrow → `Backups look inactive though they ran fine before — the scheduler for the services stack may be switched off.` (`services` in amber mono).
  - `PROPOSED FIX` eyebrow → bulleted list: *Re-enable backup scheduling for `services`* / *Kick off a full scan (mode: full)* / *Verify next run + certificates*.
  - **Pending state:** primary button `Approve via Telegram ✈` (green: border `#2f6b3a`, bg `rgba(70,201,90,.14)`, text `#8fe6a0`, hover bg `rgba(70,201,90,.24)`), then a row of `Dismiss` (ghost) + a **disabled** `Execute here · SOON` chip (dashed border `#23303e`, `not-allowed`, `SOON` badge).
  - **Approved state** (after clicking Approve or Dismiss): replace buttons with a green confirmation box — `✈ Sent to @casasrvrbot — tap 👍 in Telegram to let me run it.`

---

## Interactions & Behavior
- **Tab switching:** click a tab → set active tab, swap main content, re-run agent line. Active-tab styling per above. Content animates in via `bootin` (`.3s`).
- **Refresh now:** if not already scanning, set `scanning=true`, show the sweeping progress bar, and after **2200ms** set `scanning=false` and update `lastScan` to the current ISO timestamp (format `YYYY-MM-DDTHH:mm:ss.SSS...00+00:00`). While scanning the Professor line switches to the scanning message.
- **Network filter:** typing filters the router list live (case-insensitive substring over router+rule+service); the "showing N of M" counter updates.
- **Approve / Dismiss plan p1:** both flip the plan into the "approved/sent to Telegram" state (in the real app, Approve should POST to the Telegram-bot approval flow; Dismiss should mark the plan dismissed — here both just show the sent confirmation as a placeholder).
- **Execute here:** intentionally disabled (`SOON`) — placeholder for future in-dashboard action execution.
- **Auto-refresh:** real app refreshes data every 60s (stated in footer; not simulated in the prototype).

### Keyframes
- `led`: opacity 1 → .35 → 1, 2–2.4s ease-in-out infinite (status LEDs).
- `livepulse`: opacity .92 ↔ 1, 3s (container matrix).
- `shimmer`: translateX(-100% → 400%), 1.1s linear (scan bar).
- `bootin`: opacity 0 + translateY(6px) → settle, .3s ease (tab content).

## State Management
- `active`: `'overview' | 'backups' | 'network' | 'actions'` (default `overview`).
- `scanning`: boolean (drives progress bar + Professor line; auto-clears after 2.2s).
- `lastScan`: ISO string (updated on refresh).
- `netQuery`: string (network filter input).
- `planApproved`: boolean (false = pending buttons; true = sent-to-Telegram confirmation).
- 🆕 `sysOnline` / `sysOffline`: which System state renders. In the real app this is driven by whether the latest scan was `mode: full` (populated) vs `mode: status` (empty). Prototype defaults to `sysOnline: true`.
- Data fetching (real app): fleet/container health, findings, disk usage, Traefik routers, update history, and the current pending plan should come from the scan API. The prototype hardcodes the values below.

## Design Tokens
**Colors**
- Backgrounds: `#080b11` (page), `#10151d`/`rgba(16,22,30,.5)` (panels), `#0c1119`/`#0c1622`/`#0e1420`/`#0e1622` (insets/tracks).
- Borders: `#16202c`, `#1a2531`/`#1a2532`, `#1e2836`, `#1c3524` (green panel), `#24384c` (cyan panel), `#3a3320` (amber panel).
- Green (healthy/primary): `#46c95a`, `#5fd06e`, `#8fe6a0`, dim `#2f6b3a`, `#3f7a4c`.
- Red (critical/brand): `#e23b2e`, `#e2554a`, `#c22a1f`.
- Orange (high): `#f0864a`. Amber (medium/warn): `#f4b942`, `#f4c968`.
- Cyan (info/links): `#56b6e0`, `#8fd0ef`, `#9fc9e0`.
- Cream (brand): `#ece0c4`, `#f3e8cd`, `#d9c9a2`.
- Text: `#d6deea`, `#c3ccd8`, `#b6c1cf`; muted `#8b98a8`, `#7c8a9a`; faint `#6f7d8d`, `#5c6a7a`, `#4a5666`.

**Typography**
- Display: `'Chakra Petch'` 500/600/700 — wordmark, panel headers, big numbers.
- UI: `'Space Grotesk'` 400–700 — labels, buttons.
- Mono: `'JetBrains Mono'` 400–600 — all data, timestamps, rules, chips, eyebrows.
- Sizes: hero number 52px; panel headers 15px; body 12.5–13.5px; table cells 12.5px; eyebrows/chips 10.5–11.5px.

**Radius:** panels 14px; buttons/inputs/insets 8–10px; pills 20px; chips 5–6px.
**Spacing:** page padding `22px 26px`; panel padding `20–22px`; grid gaps 14–22px.

## Assets
No external image assets. The Planet Express logo and Professor avatar are CSS-drawn (gradient circles + monogram). If the real app has proper brand art, swap it in. All glyphs used (`↻`, `✈`, `👍`) are Unicode — replace with the codebase's icon set if preferred.

## Files
- `Planet Express Dashboard.dc.html` — the full design reference (all four tabs + agent sidebar + interactions). Open in a browser to see it live.
