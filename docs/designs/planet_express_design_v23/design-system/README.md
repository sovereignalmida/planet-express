# Planet Express design system (v2.3)

Pulled from the live dashboard: `sovereignalmida/planet-express` @ main (3c11d659), `static/cockpit.css`, `templates/dashboard.html` and `templates/container.html`. The repo has no `dashboard.css`; `cockpit.css` is the only stylesheet, and this is it split by section.

## Use

```html
<link rel="stylesheet" href="design-system/styles.css">
```

`styles.css` imports everything in the same order as `cockpit.css`, so the cascade is identical. The fonts (Chakra Petch, Space Grotesk, JetBrains Mono) come in through `tokens/typography.css`. In the Flask app, keep shipping the single `static/cockpit.css`. This split is for reading and design work, not a second source.

## Tokens

| File | Holds |
| --- | --- |
| `tokens/colors.css` | Surfaces (`--pe-bg` … `--pe-border-dash`), ink ramp (`--pe-ink` … `--pe-ink-ghost`), the four-level status ramp (`ok`, `warn`, `high`, `crit` + `idle`, each with `-ink`, `-line`, `-bg`, `-glow`), accent (navigation only), `--pe-provider-file` |
| `tokens/typography.css` | `--pe-font-display`, `--pe-font-body`, `--pe-font-mono` + font import |
| `tokens/spacing.css` | Radii `--pe-r-sm` 6 · `md` 9 · `lg` 12 · `xl` 14 · `pill` 20; gaps 9 / 14 / 18; card and panel padding; `--pe-track` |
| `tokens/base.css` | body, links, headings, code |
| `tokens/motion.css` | `pe-beacon` (warn), `pe-critpulse` (crit), `pe-sweep`; reduced motion |
| `tokens/aliases.css` | `--cp-*`, `--text`, `--green` … compatibility names the tab rules still read |

## Components

| File | Classes |
| --- | --- |
| `01-primitives` | `.pe-panel` `.pe-led` `.pe-badge` `.pe-chip` `.pe-bar` `.pe-btn` `.pe-verdict` `.pe-card` `.pe-hero` `.pe-footnote` `.pe-code` |
| `02-action-screens` | `.pe-sheet` `.pe-logwell` `.pe-attrib` `.pe-hint` `.pe-steps` `.pe-rail` `.pe-check` `.pe-totp` `.pe-spinner` |
| `03-chrome` | `.topbar` `.tabs` `.tab` `.scanbar` `.body-grid` `.tab-panel` `.scanline` `.vignette` |
| `04-panels-hull` | `.panel` `.panel-green` `.panel-head-row` `.status-chip` `.fleet-*` `.alarm-banner` `.alarm-chip` `.hull-card` `.hull-drawer` |
| `05-data-system` | `.grid-head` `.grid-row` `.cols-*` `.disk-bar-*` `.stat-tile` `.mem-bar` `.errors-*` `.verdict-strip` |
| `06-cryo` | `.cryo-grid` `.cryo-pod` (`fresh` `stale` `overdue` `failed` `nodata`) `.cryo-*` |
| `07-certs` | `.cert-grid` `.cert-card` (`valid` `renew_soon` `expiring` `expired` `error`) `.cert-*` |
| `08-network` | `.net-adguard` `.net-stat` `.net-zone` `.net-pill` `.net-tag` |
| `09-manifest-banner` | v3 `.deploy-run` `.deploy-timeline` `.chip-board` `.telegram-banner` `.plan-*` |
| `10-docks` · `11-crew-chrome` | persistent tab docks, `.crew-card` `.computer-log`, config review sheet |
| `12-stack-cards` | `.pe-stack-grid` `.pe-stack-card` `.pe-dot-svc` `.container-detail` `.container-facts` |
| `13-approvals` | `.approval-card` `.incident-card` `.execution-*` |
| `14-overview` | `.overview-tiles` `.overview-tile` `.overview-control-grid` `.overview-disk-row` `.overview-professor` |
| `15-backups-layout` | `.backups-split` (the pod lies down: `.cryo-main` `.cryo-side`) |
| `16`–`19` | Actions (`.actions-split` `.approval-empty` `.approval-recent` `.incident-row`), History (`.history-split` `.deploy-run` `.run-detail-*`), Chat (`.chat-split` `.chat-verdict` `.chat-evidence-*`), Config (`.config-split` `.config-editor-shell`) |
| `20-launch-links` | `.pe-launch` (`primary` `neutral`) `.pe-tile-links` `.pe-drawer` `.pe-drawer-row` `.pe-pill-split` `.container-launch` |
| `21-widgets` | `.pe-widget` (`needs-key` `is-error` `is-loading` `is-unrefreshed`) `.pe-widget-stat` `.pe-widget-row` `.pe-log-toggle` |
| `22-app-icons` | `.pe-icon` (`sm` 28 · `lg` 40) `.pe-icon-led` `.pe-icon-mono` `.pe-icon-bare` (`xs` 13 · `md` 18) |
| `23-elevation` · `24-stack-controls` | passphrase sheet, `.pe-stack-act` `.pe-svc-act` |

`class-index.json` lists every class each file defines.

## Rules

See `docs/RULES.md`. Short version: four status levels, cyan is navigation and never status, every tab opens with a verdict, no hex in templates, no shell command ever reaches the UI.

App icons come from selfh.st (`selfhst/icons`), and the server caches them under `/icons/<slug>`. The design screens load them from the CDN only because there is no server.
