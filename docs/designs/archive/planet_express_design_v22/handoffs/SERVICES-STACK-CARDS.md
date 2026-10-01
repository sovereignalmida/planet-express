# Handoff: Services panel → stack cards

Replaces the flat container list on the Overview tab. Nothing else on Overview changes — the FLEET STATUS panel, Ship's Computer and Crew panels stay exactly as they are.

Read against `sovereignalmida/planet-express@main`: `templates/dashboard.html` (SERVICES panel), `static/cockpit.css`, `dashboard_data.py`.

**Visual reference:** `reference/Services Grid - Stack Cards.dc.html` — open it in a browser. It's interactive: the DEMO button flips incident/all-green, and the NAMED/DOTS + ATTENTION controls are live. Read exact spacing and colour from it; don't lift its markup (it's inline-styled for the design tool).

---

## The problem

On a host with 88 containers in 15 stacks the current panel renders one row per container:

- **~3,900px of scroll** for one panel, measured at 390px wide.
- The **widest element is `running(healthy)`**, repeated 88 times. It is the only thing on each row that carries no information, and it's set at the same weight as the container name.
- Stack headings are plain text with no rollup, so you can't tell a 16-container stack from a 1-container stack without counting.
- A degraded container looks exactly like a healthy one apart from an 8px LED, 3,000px down the page.

The panel's job is "is anything wrong, and where" — and at 88 containers it answers that worse than the 88-dot strip directly above it.

## The fix in one line

**The stack becomes the unit, not the container.** 15 cards in a responsive grid, each sizing to its own contents, sorted worst-first, with a status word only where the status isn't green.

---

## Layout

### Panel header

One row, wrapping: `SERVICES` heading · `15 stacks · 86 of 88 containers online` · spacer · filter group · density group.

- **Filter:** `ALL 15` / `ATTENTION n`. Segmented, cyan when active.
- **Density:** `NAMED` / `DOTS`. Segmented, cyan when active.
- Both are 34px tall on desktop; 44px on touch.

### Attention rail

Renders only when at least one stack is non-green, and only in the `ALL` filter (it would be redundant in `ATTENTION`).

`.pe-card.crit` geometry, one line: pulsing LED + `NEEDS YOU NOW` (any crit) or `WORTH A LOOK` (warn/idle only) + the joined notes, e.g. *"sonarr down (media) · tika down (paperless) · frigate degraded (home)"*.

### The grid

```css
display: grid;
grid-template-columns: repeat(auto-fill, minmax(306px, 1fr));
gap: 13px;
align-items: start;   /* ← cards size to content, not to the tallest sibling */
```

`align-items: start` is the load-bearing line. Without it `dockge` (1 container) is as tall as `media` (16).

### Stack card

`.pe-card` with the level modifier — 3px `border-left` in the level colour, `#0c1524 → #0a1420` gradient.

1. **Head row** — level LED (8px; `beacon` on warn, `critpulse` on crit) · stack name (Chakra Petch 700, 15.5px, ellipsis) · spacer · tally pill `14/16` in the level colour.
2. **Note line** (only when something is wrong) — mono 10.5px, e.g. `sonarr down · bazarr degraded`. This is the whole reason you don't need to scan the chips.
3. **Container chips** — `flex-wrap`, 6px gap.

### Container chip

30px tall (32px on touch), `border-radius: 7px`, LED + name in mono 10.5px. Healthy chips are `#08101c` on `#1c3344`; non-healthy chips take the level tint and append a 9px tag (`DOWN` / `DEGRADED` / `PAUSED`).

**No chip ever renders `running(healthy)`.** Green is the default and costs nothing.

### DOTS density

Chips collapse to 13px rounded squares, 5px gap — the same vocabulary as the FLEET STATUS strip above, so the two panels read as the same instrument at two scales. 88 containers fit in roughly 620px.

Still individually clickable. Keep the container name in `title`/`aria-label`.

---

## Sorting

Always, regardless of filter:

```python
RANK = {"crit": 0, "warn": 1, "idle": 2, "ok": 3}
stacks.sort(key=lambda s: (RANK[s["level"]], -s["total"], s["name"]))
```

Worst level first, then biggest, then alphabetical. **Never alphabetical-only** — that's what makes the current list require scrolling to find a fault.

---

## States

| State | Treatment |
| --- | --- |
| **default** | The grid. Attention rail present iff something is wrong. |
| **attention filter, nothing wrong** | Grid replaced by a dashed card: `✓` glyph, `NOTHING NEEDS ATTENTION`, "All 88 containers across 15 stacks are online. Last state change was 6 days ago.", and a `SHOW ALL 15 STACKS` button. State the last change — it's the useful part. |
| **busy (scan running)** | Cards **keep their last-known state**, dimmed, with a cyan `RE-READING THE FLEET` header and an `n / 15` counter. Never blank the grid — a scan is not an outage. |
| **error (daemon unreachable)** | Grid replaced by a `.pe-card.crit`: `CAN'T REACH DOCKER`, "The socket refused the connection. Container state is unknown, not bad.", then a mono well with last-good-read time and the real error, then `RETRY SCAN`. Distinguish *unknown* from *down*. |
| **empty (no stacks)** | Dashed card, Zoidberg desaturated, `NO COMPOSE STACKS FOUND`, "The daemon answered, but nothing on this host carries a compose project label.", `CHECK STACK PATHS`. |

---

## Mobile (≤ 720px)

- Grid → one column (`minmax(100%, 1fr)`).
- **Density defaults to DOTS.** ~88px per card × 15 ≈ two screens for the whole fleet, against twenty-plus for the list.
- Tapping a card **expands it in place** to named chips (cyan border while expanded, `⌄` affordance in the head row). One expanded at a time.
- Single-container stacks render their dot inline on the head row rather than on a second line.
- Tapping a chip opens the container detail (see `ACTION-SCREENS.md` § B).

Measured scroll at 390px: flat list ~3,900px · cards NAMED ~1,050px · cards DOTS ~620px.

---

## Backend

Grouping by compose project already exists — the current list renders its headings from it. Four derived fields per stack, all a reduce over data you already have:

```python
# dashboard_data.py — per stack
"up":    14,                 # count of state == ok
"total": 16,
"level": "crit",             # worst member: crit > warn > idle > ok
"note":  "sonarr down · bazarr degraded",   # non-ok members only, "" when clean
```

`note` is built from the non-ok members only:

```python
WORD = {"crit": "down", "warn": "degraded", "idle": "paused"}
note = " · ".join(f'{c["name"]} {WORD[c["level"]]}' for c in bad)
```

Container `level` maps from the existing 4-way container state exactly as the reactor cells already do: `online → ok`, `degraded → warn`, `down → crit`, `paused → idle`.

No new collection, no new scan mode, no change to `check_containers()`.

---

## CSS

Add to `cockpit.css` §16. Reuses existing tokens throughout — no new colours.

- `.pe-stack-grid` — the `auto-fill / minmax(306px) / align-items:start` grid.
- `.pe-stack-card` — `.pe-card` + level modifier; head row, note line, chip wrap.
- `.pe-chip-svc` — 30px container chip, level modifiers, `.is-tagged` for the trailing status word.
- `.pe-dot-svc` — 13px dot variant; same level modifiers.
- `.pe-stack-card.is-expanded` — mobile tap state, cyan border + gradient.

Retire the flat-list rules: the per-container row, the stack `<h4>` heading, and the right-aligned status-text column.

---

## Non-goals

- **No search box.** With 15 stacks you don't need to search, you need "show me the ones that matter" — that's the ATTENTION filter.
- **No per-stack action buttons on the card.** Restart/stop stay in the container detail and the Actions tab, behind the existing two-step confirm sheet.
- **No change to FLEET STATUS.** The 88-dot strip stays; the cards deliberately echo its dot vocabulary in DOTS mode.
