# V4 — Operator Console (handoff)

Screen: `screens/Console.dc.html` (Flows 1–10 on desktop and phone, plus a Component sheet mode).
CSS: `design-system/components/25-console.css` (appended, `pe-` prefixed, imported last in `styles.css`). The sealed first 582 lines of `static/cockpit.css` are untouched.
Reused unchanged: `topbar/tabs`, `pe-verdict` (+ `compact`, `high`, `none` modifiers added), `pe-card`, `pe-sheet`/`pe-sheet-scrim`, `pe-rail`/`pe-marker`, `pe-check`, `pe-output`, `pe-attrib`, `pe-code`, `pe-badge`/`pe-chip`/`pe-bar`/`pe-led`, `pe-spinner`/`pe-caret`, `pe-btn`.

## Structure decisions
- One screen file with a flow switcher; the component sheet renders the same block markup the flows use.
- Responsive by **container query** on `.pe-con-main` (`pecon`), not by flags. Same markup serves 1440 and 390. Below 620px: plan options become a swipe row (scroll-snap, 86% cards, dots), actions stack, and the context panel becomes a bottom sheet (`pe-sheet` pattern).
- Operator messages are quiet right-aligned notes; agent blocks are full width; no avatars per line. Crew persona appears only in the first-run invite.
- Confirm sheet is `pe-sheet` (bottom on phone) and `pe-sheet.center` (modal on desktop).

## New classes → components split
Proposed `design-system/components/25-console.css`, single file; split further if the drift check wants one file per component.

| Block (brief §5) | Classes |
|---|---|
| Shell, rail, row, group | `pe-con`, `pe-con-rail`, `pe-con-row`, `pe-con-origin`, `pe-con-group`, `pe-con-new`, `pe-con-search`, `pe-con-main`, `pe-con-scroll`, `pe-con-thread`, `pe-con-top`, `pe-con-titlebar` |
| Verdict (compact) | `pe-verdict.compact`, `.high`, `.none` |
| 1 Operator message | `pe-opmsg`, `pe-ref`, `pe-chips` |
| 2 Agent message | `pe-agent`, `pe-prose`, `pe-m`, `pe-ev`, `pe-kn`, `pe-kn-col(.unsure)`, `pe-src`, `pe-stream` |
| 3 Activity trail | `pe-trail`, `pe-trail-step`, `pe-kind(.changed/.denied/.failed)` |
| 4 Plan card | `pe-plan(.is-confirmed/.is-dead)`, `pe-plan-opts`, `pe-opt(.is-sel/.is-picked)`, `pe-opt-sec`, `pe-opt-rec`, `pe-plan-actions`, `pe-btn.go/.danger/.ghost` |
| 5 Edit scope | `pe-scope`, `pe-tickrow(.on/.off/.locked)` |
| 6 Confirm sheet | `pe-sheet.center`, `pe-auth`, `pe-auth-row(.warn)`, `pe-fp`, `pe-who` |
| 7 Timeline | `pe-run`, `pe-marker.paused/.skipped` (rest reuses `pe-rail`) |
| 8 Tripwire | `pe-tripwire` |
| 9 Take over | `pe-takeover`, `pe-composer.is-live` |
| 10 Verification | `pe-verify(.ok/.warn/.crit)`, `pe-check.unknown`, `pe-check-mark`, `pe-check-ev`, `pe-by-pe` |
| 11 Close and learn | `pe-learn`, `pe-cand` |
| 12 Notices | `pe-note(.warn/.high/.crit/.ok)`, `pe-err`, `pe-skel` |
| 13 Composer | `pe-composer`, `pe-composer-box`, `pe-composer-text`, `pe-composer-meta`, `pe-mention` |
| 14 Budget | `pe-budget(.warn/.stop)` |
| First run | `pe-invite`, `pe-prompts`, `pe-prompt` |
| Context panel | `pe-con-ctx`, `pe-ctabs`, `pe-ctab`, `pe-ctx-body`, `pe-ctx-card` |

No new tokens and no hardcoded hex (check: all colour is `var(--pe-*)` or `rgba` of the existing status values, same as the sealed block does).

## Status vocabulary → verdict
Component sheet group `00` shows all 12 operator-facing states with their level. Internal names never appear in markup.

## Flows covered (screen `Console.dc.html`)
1 Ask (loading, streaming, answered, long, error) · 2 Plan→modify→confirm→run→verified (+3 options) · 3 Scrap and redirect · 4 Tripwire, rollback · 5 Verification failed / could-not-check / keep working / take over · 6 From incident · 7 PE started it, operator replied · 8 Phone (list, read, answer plan, confirm, running+pause, context sheet) · 9 Degraded (settling, proposal-only, budget exhausted) · 10 First run.

## Data each region needs (mark ★ = assumed, not in brief §5/§6)
- **Rail row:** title (editable), origin (`you|incident|pe`), public status (§6 table), age, needs-operator flag. ★ Group assignment (Needs you / Working / Recent) derived from status level.
- **Verdict strip:** public status, one-sentence reason, optional tally chips. ★ Per-state sentence is authored by the backend or templated from state + counts.
- **Title bar:** origin line, budget `{used_pct, limit_state}`.
- **Agent block:** paragraphs with inline evidence refs `[n]`, `known[]`, `unsure[]`, `sources[{n, label, summary}]`. ★ Refs carried as structured spans, not parsed from text. ★ Follow-up suggestions `follow[]` (optional).
- **Trail step:** `kind` (`read|changed|denied|failed`), label, short output, ★ `why` (reason for denied/failed, plain words).
- **Plan card:** `state`, `created_at`, `options[{name, recommended, why_recommended, summary, changes[], will_not_touch[], risk{level, text}, rolls_back_to, checks[], effort}]`, `diff[]` when edited, ★ `expires_at`.
- **Confirm sheet:** scope text, backup status `{covered, text}`, protected exclusions, limits `{minutes, steps, restarts}`, operator identity, fingerprint (short).
- **Timeline:** `steps[{state, label, meta, output[]}]`, `progress`, `paused_by`.
- **Tripwire:** what changed (path or area), when, rollback availability + checkpoint time.
- **Verification:** `checks[{state: pass|fail|could_not_check, label, evidence}]`, headline, ★ `could_not_check` as a third result.
- **Memory candidates:** `{text, scope}` with accept/edit/discard state.
- **Context panel:** Evidence (incident, containers with level, context used %), Scope (services, paths, protected), Limits (budget, time, effort), Record (event count, last checkpoint, links).
- **Composer:** mode (`idle|busy|live|paused|take|off`), disabled reason string.

## Accessibility notes
- **Focus order:** top bar → rail (New conversation, search, rows) → thread title bar → verdict (not focusable) → thread blocks in reading order → composer → context tabs. On phone: list → thread → composer; context sheet traps focus and returns it to the Context button.
- **Plan options** are `role=radio` in a `radiogroup`: Arrow/Enter/Space select, Tab leaves the group to the action row.
- **Keyboard path to Confirm:** Tab to plan card → Space on an option → Tab to **Confirm option X** → Enter (opens sheet, focus on the sheet, primary button first in the DOM after the details) → Enter again authorises. Escape closes the sheet and returns focus to the Confirm button.
- **Live regions:** verdict strip `role=status aria-live=polite` (announces state changes); streaming caret and spinner are decorative, the stream line is `role=status`; **tripwire banner `role=alert`** (assertive) and also moves focus to its first button; errors `role=alert`; notices `role=status`.
- **Motion:** only `warn` beacon, `crit` pulse, spinner/caret for in-flight work. Reduced motion is already handled globally in `motion.css`.
- **Every status carries text** (badge, verdict, check meta `PASS`/`FAIL`/`COULD NOT CHECK`); evidence chips are buttons with a numeric label.
- Targets on phone: action buttons ≥46px, primary 48px.

## Open questions for engineering
1. Who writes the one-sentence verdict, the agent or a template? The design assumes a template over state plus counts.
2. `could_not_check` as a first-class verification result (design assumes yes).
3. Plan `expires_at` and what stale means (design shows an "expired" state with a plain reason).
4. Work log lives under Console as a link; confirm it replaces Missions entirely.
