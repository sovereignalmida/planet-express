# V4 Operator Console: package for Claude Code

Implements `v4-operator-console-design-brief.md`. Read `handoffs/V4-OPERATOR-CONSOLE.md` first.

## Brief §10 → where it is

| §10 deliverable | File |
|---|---|
| 1. Desktop (1440×900) and phone (390×844) screens, flows 1–10, with states | `screens/Console.dc.html` (Flows mode; Desktop / Phone switch; state chips per flow) |
| 2. Every §5 block as a component sheet with all states | `screens/Console.dc.html` (Component sheet mode, groups 00–15) |
| 3. New CSS (appended, `pe-` prefixed) and `design-system/components/` split | `design-system/components/25-console.css`, imported last in `design-system/styles.css` |
| 4. Screen → data mapping, assumed data marked ★ | `handoffs/V4-OPERATOR-CONSOLE.md` → "Data each region needs" |
| 5. Accessibility notes (focus order, live regions, Confirm keyboard path) | `handoffs/V4-OPERATOR-CONSOLE.md` → "Accessibility notes" |

## Destinations

| From this package | To |
|---|---|
| `handoffs/V4-OPERATOR-CONSOLE.md` | `docs/designs/planet_express_design_v23/handoffs/` (or the v3 folder once decided) |
| `screens/Console.dc.html` | `docs/designs/planet_express_design_v23/screens/` |
| `design-system/components/25-console.css` | `docs/designs/.../design-system/components/` **and** append to `static/cockpit.css` after line 582 |
| `design-system/styles.css` | replace; only change is the `25-console.css` import |
| `assets/.../leela.png` | already in the repo; included for the first-run invite render |

## Build order

1. Append the CSS; run the drift check. If it wants one file per component, split by the table in the handoff.
2. Console tab shell: rail, thread, context panel, composer, budget meter, verdict strip.
3. Message blocks: operator, agent (with evidence refs and known/unsure), activity trail, notices.
4. Plan card, edit scope, confirm sheet. Keyboard path is specified in the handoff.
5. Execution timeline, tripwire, verification, close and learn.
6. Phone layout: it is container-query driven, so no separate markup. Verify at 390.
7. Degraded states: host settling, proposal-only, budget exhausted.
8. Remove the Chat and Missions tabs only after the Console covers them. Work log is a link inside the Console.

## Notes
- Machine values render in mono through `pe-m`; status colour appears only on level chips, verdict, and check results.
- Renders use the real `pe-*` classes where they exist. Everything new is in `25-console.css`; there are no inline stand-ins to replace.
- The design package did not include `Cockpit v4 - Action Screens`. The plan card was built from the `pe-sheet`, `pe-rail`, `pe-check` and `pe-attrib` patterns. Compare against the shipped approval card before merging.

## Open questions
1. Who writes the verdict sentence: agent or template by state and counts?
2. Is `could_not_check` a first-class verification result?
3. Plan expiry: what is the window, and what does the expired state ask the operator to do?
4. Does the Console fully replace Missions, including old deep links?
5. Confirm that the 25-console.css single file satisfies the drift check.
