# Planet Express — Design (v20)

Everything the dashboard's visual layer needs, in one place. Built against `sovereignalmida/planet-express`.

Start here, then read `system/README.md`.

## What's in the box

```
system/           the design system — this is what you implement against
  cockpit.css       ← the deliverable. Copy to static/cockpit.css
  README.md         install, the 11 rules, type scale, contrast
  COMPONENTS.md     Jinja markup recipes for the dashboard components
  ACTION-SCREENS.md the v4 action surfaces (login, container, approval, execution)
  DATA-CONTRACT.md  context keys the templates need, and what's missing today

handoffs/         point-in-time specs, newest first
  SERVICES-STACK-CARDS.md  Overview services panel → stack cards (88 containers / 15 stacks)
  BACKUPS-TAB-FIXES.md   corrections to the v3 Backups tab — 7 faults, 3 Python diffs
  V3-COCKPIT-RESKIN.md   the original cockpit re-skin spec
  V1-ORIGINAL.md         first-pass dashboard spec, kept for provenance

assets/           logo + 8 crew portraits. Copy to static/
reference/        live design references — open in a browser
```

## Install

```html
<head>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{{ url_for('static', filename='cockpit.css') }}">
</head>
```

```
cp -r assets/* static/
cp system/cockpit.css static/
```

Load `cockpit.css` **instead of** the legacy `dashboard.css`, not alongside — the old `.cols-*` grid classes conflict.

## About `reference/`

These are design artefacts, not source. Open them in a browser to read exact spacing, colour and state treatment — they're the ground truth when the prose is ambiguous. **Don't lift their markup:** they're inline-styled for the design tool, and the whole point of `cockpit.css` is that templates carry no hex values. Each needs `support.js` and `static/` beside it, which is why they ship as a folder.

| File | What it shows |
| --- | --- |
| `Services Grid - Stack Cards` | The services panel as stack cards. **Interactive** — DEMO flips incident/all-green, NAMED/DOTS and ATTENTION are live |
| `Cockpit v4 - Action Screens` | Login, container detail, approval card, execution progress — 20 states + desktop split |
| `Dashboard v3 - Cockpit` | The four main tabs as shipped |
| `Backups Tab - Corrected` | The Backups tab as it should be, both data states, with the fault list |
| `Explorations - 4 directions` | The four original directions. Cockpit Console won. Kept for context on what was rejected |
| `Dashboard v1 - Original` | Pre-cockpit baseline |

## Suggested order of work

1. `system/DATA-CONTRACT.md` — the backend keys. Several screens can't be honest until these exist.
2. `handoffs/SERVICES-STACK-CARDS.md` — the Overview panel that doesn't survive 88 containers.
3. `handoffs/BACKUPS-TAB-FIXES.md` — the outstanding correction to shipped code.
4. `system/cockpit.css` + `COMPONENTS.md` — re-skin the existing tabs.
5. `system/ACTION-SCREENS.md` — build the four new surfaces.

## Two things that hold across all of it

**No shell command ever reaches the UI.** Fix steps render `description` only. The `assert "command" not in json.dumps(result)` guard test stays green.

**No hardcoded hex in templates.** Everything is a `var(--pe-*)`. If you're reaching for a literal, the token is missing — add it to `:root`.
