# Task T34 — Overview services panel as stack cards

Branch `v2`. Python 3.11+, vanilla JS, no new dependencies. Web side only: `dashboard_data.py`,
`templates/dashboard.html`, `static/cockpit.css`, `static/dashboard.js` (or a new `static/services.js`),
tests. Do NOT change `casa_leela.py`, `casa_farnsworth.py`, `casa_bender.py`, `casa_scruffy.py` routes,
`planet_express/**`, or any RPC.

## Why
The live host runs 88 containers in 15 stacks. The SERVICES panel on Overview renders one row per
service (~3,900px of scroll at 390px wide), every row repeating `running(healthy)`, so a fault is an
8px LED thousands of pixels down. Chris's design handoff replaces it with one card per stack, sorted
worst-first, with status words only where something is wrong.

## Read first
- `docs/designs/planet_express_design_v20/handoffs/SERVICES-STACK-CARDS.md` — the spec. Its visual
  reference `reference/Services Grid - Stack Cards.dc.html` gives exact spacing/colour; don't lift its
  inline-styled markup.
- `docs/designs/planet_express_design_v20/system/DATA-CONTRACT.md` § "Stack rollup".
- `dashboard_data.py`: `summarize_services()` (current flat list), `_container_state()` (reactor
  cells), `summarize_pipeline_status()`, `_MODES_WITH_STACK_COMPLETENESS`.
- `casa_leela.py` ~`:348-375` — where each service's `status` (`healthy|failing|unknown`) and `state`
  (`running(healthy)`, `running(unhealthy)`, `running(starting)`, `running`, `exited(<code>)`,
  `restarting`, `absent`, comma-joined when a service has several containers) come from. Note: a
  container listed in `paused_containers` that is not running is reported `status: "healthy"` with a
  non-running state. A stack whose `docker compose ps` was unreadable has entry `status: "unknown"`,
  an `error`, and no `services`.
- `templates/dashboard.html` SERVICES panel (`~:172`), FLEET STATUS reactor strip (`~:120`).
- `static/dashboard.js` — the 60s refresh replaces `#dashboard-live`'s innerHTML and then calls
  `bindInteractions()`; the SERVICES panel lives inside it.
- `static/cockpit.css` §16 and the existing `.pe-card` level modifiers and LED animations
  (`beacon`, `critpulse`).

## Required behaviour

### 1. Data (`dashboard_data.py`)
Replace `summarize_services()`'s flat list with a rollup, keeping it a pure reduce over the monitor
snapshot (no new collection, no subprocess):

```python
{"available": bool,          # False unless the last snapshot mode collected stack completeness
 "stacks": [                 # sorted (RANK[level], -total, name); RANK crit0 warn1 idle2 ok3
   {"name", "up", "total", "level", "note",
    "members": [{"service", "level", "word", "state"}]}],   # members sorted worst-first, then name
 "total_stacks", "up", "total", "attention"}               # attention = stacks with level != ok
```

Member level from the Leela observation (reactor-cell semantics, derived from what the service
record actually carries):

| observation | level | word |
| --- | --- | --- |
| `healthy`, state starts `running` | ok | — |
| `healthy`, state not running (configured paused container) | idle | `paused` |
| `unknown` (e.g. `running(starting)`) | warn | `starting` |
| `failing`, state starts `running` (e.g. `running(unhealthy)`) | warn | `degraded` |
| `failing`, anything else (`exited(..)`, `restarting`, `absent`, …) | crit | `down` |

A multi-container service's state is comma-joined; use the worst component. Stack level = worst
member. `up` = members at `ok`. `note` = `" · ".join(f"{service} {word}")` over non-ok members, `""`
when clean. **An unreadable stack (`status == "unknown"`, no services) is its own card at `warn`
with `note` = "state unreadable" and no members — never dropped, never shown green.** Guard every
`.get()`: the snapshot is data from disk.

Keep `ctx.services` as the context key (update the template); don't rename other keys.

### 2. Template + CSS
Per the handoff: panel header (`SERVICES` · `15 stacks · 86 of 88 services online` · ALL/ATTENTION
filter · NAMED/DOTS density), the attention rail (only in ALL when anything is non-ok:
`NEEDS YOU NOW` if any crit, else `WORTH A LOOK`, joined `service word (stack)` notes), the
`auto-fill / minmax(306px, 1fr) / align-items:start` grid, stack cards (level LED, name, tally pill,
note line, chips), 30px named chips (no chip ever says `running(healthy)`; non-ok chips append the
uppercase word), 13px DOTS squares with the service name in `title` and `aria-label`. Every chip/dot
is a link to `url_for('container_detail', stack=..., service=...)` exactly as today's rows are.
Add the `.pe-stack-grid`, `.pe-stack-card`, `.pe-chip-svc`, `.pe-dot-svc`, `.is-expanded` rules to
§16 using existing tokens only (no new colours); remove the now-unused flat-list rules
(`.service-row` and the SERVICES `h3` heading use) only if nothing else uses them — grep first.

Say "services" not "containers" in copy: the data is per compose service (live: 87 services, 88
containers — one container belongs to no stack).

### 3. States (honest to the data we have — no new RPCs)
- **default**: the grid.
- **attention filter, nothing wrong**: dashed card, `✓`, `NOTHING NEEDS ATTENTION`, "All N services
  across M stacks are online.", plus the last full-scan time from the snapshot (we do not track a
  "last state change"; don't invent one), and a `SHOW ALL M STACKS` button that switches the filter.
- **busy** (`ctx.pipeline_status.state == "running"`): keep the cards, dimmed, with a cyan
  `RE-READING THE FLEET` header. No `n / 15` counter (we don't have progress). Never blank the grid.
- **unavailable** (`available` False: snapshot missing or last scan mode didn't collect stacks):
  keep today's honest message ("Available after the next full scan"). Do NOT render the handoff's
  `CAN'T REACH DOCKER` / `RETRY SCAN` card — the dashboard has no scan trigger and no Docker error
  source; unknown must not look like down.
- **empty** (available, zero stacks): dashed card `NO COMPOSE STACKS FOUND` with the handoff's copy;
  there is no Config tab yet (slice 4), so replace `CHECK STACK PATHS` with a static line naming the
  config setting that lists stack directories (find its real name in `config_schema.py`).

### 4. Interaction (JS, house style: `textContent`, no innerHTML from data)
- Filter and density are client-side view state. They must survive the 60s `#dashboard-live` swap
  (re-apply after `bindInteractions()`), and persist per viewer in `localStorage` wrapped in
  try/catch (the page must work when storage throws). Defaults: ALL; NAMED on desktop, DOTS at
  ≤720px when the viewer has no stored choice.
- Mobile ≤720px: one column; tapping a card head in DOTS expands it to named chips (cyan
  `.is-expanded`, `⌄` affordance, one expanded at a time); tapping a chip/dot navigates. Touch
  targets 44px for the segmented controls, 32px chips. The expanded card should survive the 60s swap
  if it still exists (key by stack name).
- Without JS the panel must still render the full grid (NAMED, ALL) with working links.

## Non-goals
No search box, no per-stack action buttons, no change to FLEET STATUS, Leela, the scan, or RPC.

## Tests
- `tests/test_dashboard_data.py`: the level table above row by row (incl. paused, starting,
  comma-joined multi-container worst-of, absent), sort order (crit before bigger ok stack; ties by
  name), `note` text and empty note, unreadable stack → warn card with no members, non-full snapshot
  → `available: False`, malformed service entries don't raise.
- `tests/test_scruffy_routes.py` / `tests/test_render_template.py` (whichever renders the dashboard):
  the rendered Overview contains no `running(healthy)`; a crit stack card precedes an ok one; every
  service has a link to its container page; the attention rail appears only with a non-ok stack;
  empty and unavailable states render their copy.
Keep the full suite green.

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (1161 pass today; a sandbox that blocks
  AF_UNIX fails the RPC socket tests: report that, never skip or fake them)
- `.venv/bin/ruff check .`, `node --check` on changed JS, `git diff --check`
- Report every file changed and any deviation from this brief.
