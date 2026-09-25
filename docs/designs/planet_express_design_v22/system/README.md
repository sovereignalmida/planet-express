# Planet Express — Cockpit Design System (v20)

Drop-in visual system for the sysadmin dashboard. **`cockpit.css` is the deliverable** — everything else in this bundle explains how to use it.

## Files

| File | What it is |
| --- | --- |
| `cockpit.css` | The whole system. Tokens + components. Copy to `static/cockpit.css`. |
| `COMPONENTS.md` | Markup recipes for the dashboard components, copy-paste ready. |
| `ACTION-SCREENS.md` | The v4 action surfaces — login, container detail, approval, execution. 20 states. |
| `DATA-CONTRACT.md` | Backend keys the templates need, incl. the Backups tab fixes. |
| `static/` | Logo + crew portraits (7 members + Zoidberg). |
| `reference/` | Live design references. Open in a browser to read exact spacing and colour; don't lift the markup — it's inline-styled for the design tool. |

## Install

```html
<head>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{{ url_for('static', filename='cockpit.css') }}">
</head>
```

Load it *instead of* the legacy `dashboard.css`, not alongside — the old `.cols-*` grid classes conflict.

## The rules

**1. Four status levels, no more.** `ok` / `warn` / `high` / `crit`, plus `none` for absent data. Every status surface in the product — reactor cells, cryo pods, cert cards, hull findings, router nodes — is the same `.pe-card` with one of those modifiers. If a new thing needs a colour, it maps onto one of the four.

**2. Cyan is navigation, never status.** `--pe-accent` marks the active tab, links and primary actions. A cyan thing is never "healthy" — green is healthy.

**3. Every tab opens with a verdict.** `.pe-verdict` answers the tab's question in one sentence before any detail. Identical geometry on every tab so they read as the same instrument. Never a tab that opens with two panel headers and no answer.

**4. One hero number per card.** The card exists to answer one question — that answer is `.pe-hero-num` at 34px. Everything else is a mono readout row underneath. If you can't name the hero number, the card is the wrong component.

**5. Demote fields that don't mean anything.** Raw machine state that reads alarming but isn't (systemd `ActiveState` on a oneshot unit, for instance) goes in `.pe-footnote` at 9.5px grey — never coloured, never in the head row. See `DATA-CONTRACT.md`.

**6. Absent data has a shape.** Never blank a panel. Render `.pe-card.none` — dashed, 62% opacity, `—` in every readout row — so the user sees what *would* be there. Pair with `.pe-sensor-dark` explaining which scan mode would collect it.

**7. Motion is a severity signal.** `pe-beacon` (1.8s) = warn, "look when you can". `pe-critpulse` (0.9s) = crit, "look now". Never animate `ok` or `none`. Reduced-motion is respected globally.

**8. Mono for every machine value.** Timestamps, paths, unit names, counts, exit codes, domains. Prose is Space Grotesk; headings and hero numbers are Chakra Petch. Never mix.

**9. Grid tracks are 300px minimum.** `--pe-track`. Cards carry a hero number and a bar and need the room. The tight 220px variant is for dense node grids (routers) with no hero number.

**10. Destructive actions are two-step, and attributed.** Anything that changes the system opens a `.pe-sheet` naming blast radius, data risk, and the operator it will be logged as. Resolved approvals keep the portrait, timestamp and reason rather than disappearing — with three operators, "who handled this" is the common question. Full rules in `ACTION-SCREENS.md`.

**11. No hardcoded hex in templates.** Everything is a `var(--pe-*)`. If you're reaching for a literal, the token is missing — add it to `:root` rather than inlining it.

## Type scale

| Use | Font | Size / weight |
| --- | --- | --- |
| Page title | Chakra Petch | 26px / 700, `.06em` |
| Verdict title | Chakra Petch | 19px / 700, `.06em` |
| Card hero number | Chakra Petch | 34px / 700 (26px small) |
| Card title | Chakra Petch | 20px / 700 (15px small) |
| Panel heading | Chakra Petch | 14px / 700, `.1em` |
| Body copy | Space Grotesk | 13–14px / 400, 1.6 |
| Mono value | JetBrains Mono | 11px |
| Mono label | JetBrains Mono | 9.5px, `.08em` |
| Badge | JetBrains Mono | 9.5px / 700, `.1em` |

## Contrast

All ink tokens clear 4.5:1 on their intended surface. The `-ink` variants (`--pe-ok-ink` etc.) are the *text* colours; the base tokens (`--pe-ok`) are for LEDs, bars and borders only. Don't set body text in `--pe-ok` — it fails on `--pe-panel`.
