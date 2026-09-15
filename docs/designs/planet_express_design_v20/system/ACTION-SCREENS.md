# Action surfaces (v4)

Four screens added once the dashboard could *change* things, not just report them. Phone-first — 1–3 operators, usually one-handed. 20 states total.

All of it composes from `cockpit.css`. The action-specific classes live in section 16 of that file.

**Nothing here surfaces a shell command.** Fix steps render `description` only; the `command`-never-surfaces guard test stays green.

---

## Cross-cutting rules

1. **Destructive actions are two-step.** Restart/prune/rollback open a `.pe-sheet` naming blast radius, data risk, and the operator the action will be logged as. The sheet's confirm button becomes its own busy state in place — never a second overlay.
2. **Approvals are attributed.** With three operators, "who already handled this" is the common question. Resolved cards keep the portrait, the timestamp and the denial reason instead of disappearing.
3. **Verification is a phase, not a footnote.** A run is done when the service answers, not when the commands exit. `passed`/`failed` is a verification verdict, never an exit code.
4. **Interrupted ≠ failed.** `failed` = we know it broke and rolled back. `interrupted` = we don't know what state the host is in, so it offers re-scan first and never auto-retries.
5. **Countdowns pause during submission.** A plan cannot expire between the tap and the server recording it.
6. **Empty states name the last thing that happened** rather than "nothing here" — on a 3-person system that's usually the information wanted.
7. **44px minimum hit target**, 50–54px for primary bottom-bar actions.

---

## A · Airlock (login)

`login.html` · states: `default` `rejected` `busy` `locked`

Passphrase + 6-digit TOTP, 30-day trusted device. No sign-up, no password reset — three known operators. Farnsworth's portrait + a one-line quip sits under a dashed divider; it's the one screen with a personality budget.

| State | Treatment |
| --- | --- |
| `default` | Focused TOTP cell gets `--pe-accent` border; caret blinks. |
| `rejected` | `.pe-card.crit` banner above the form, all TOTP cells red, remaining-attempts count stated explicitly. |
| `busy` | Form dimmed to 45% + `pointer-events:none`; submit becomes spinner + `sweep` shimmer, label `VERIFYING`. |
| `locked` | Form replaced entirely by an amber countdown (`mm:ss`, 44px) + progress bar, reassurance line, and a `NOTIFY @casafarnsworthbot` action. |

Lockout: 3 attempts → 15 minutes.

---

## B · Reactor cell detail (container)

`container_detail.html` · states: `healthy` `down` `paused` `restart-confirm`

Opens from a reactor cell on Overview. Order: verdict → vitals → facts → logs. Logs are deepest because by the time you're reading them you've already decided something is wrong.

- **Header:** back chevron, name + `stack · image` sub, status badge.
- **Verdict** (`.pe-verdict`, 48px orb on mobile): `RUNNING CLEAN` / `CRASH LOOPING` / `PAUSED BY OPERATOR`.
- **Vitals:** 2-up CPU + memory `.pe-card` with hero number and bar. `—` in ghost grey when paused.
- **Facts:** healthcheck, restart policy, ports.
- **Log well:** flex-fills remaining height, header LED matches container state (`beacon` when healthy, `critpulse` when crash-looping), tail count right-aligned, blinking block caret on the newest line. Empty: dashed glyph + "No output since the container was paused. Logs resume when it does."
- **Agent hint** (crash-loop only): dashed amber card, Leela portrait, one-sentence diagnosis → `Review →` to the approval card.
- **Bottom bar:** `LOGS ↗` secondary + primary `RESTART ⟳` (amber) / `RESUME ▶` (cyan when paused).

Restart confirm is a `.pe-sheet` over a dimmed screen: amber orb, "Search will be unavailable for roughly 20 seconds", then `AFFECTS` / `DATA LOSS` / `LOGGED AS` readout, then `CANCEL` + the confirm button in its busy state.

---

## C · Flight authorisation (approval card)

`approval_card.html` · states: `pending` `approved` `denied` `expired` `busy` `empty`

The agent proposes, a human authorises. The card answers three things in order: **what will change**, **what it costs if it's wrong**, **how long you have to decide**.

Structure (pending):

1. Status row — LED + `AWAITING AUTHORISATION` + plan id.
2. Title (19px) + one-paragraph rationale with the evidence that triggered it.
3. Proposed-by strip — portrait, agent name, module, time.
4. `FIX STEPS · N` — zero-padded index + description. **Descriptions only.**
5. Risk readout — `BLAST RADIUS`, `REVERSIBLE`.
6. Countdown — `mm:ss` at 29px + depleting bar, label `UNTIL THIS PLAN EXPIRES`.
7. `DENY` secondary + `AUTHORISE ✈` primary (green, 1.5× width).

| State | Treatment |
| --- | --- |
| `approved` | Green card. Countdown and buttons replaced by an attribution strip (portrait ringed green, "Approved by Hermes", time + device) and `WATCH EXECUTION ↗`. Steps dim to `--pe-ink-dim`. |
| `denied` | Dark-red card. Attribution strip carries the denial reason **verbatim in italics** — the most useful field on the card. Actions: `ARCHIVE` / `RE-PROPOSE`. |
| `expired` | Dashed, 75% opacity. Readout: `PROPOSED` / `LAPSED` / `STILL RELEVANT` (amber when yes — the plan lapsed but the problem didn't). Single `RE-PROPOSE NOW`. |
| `busy` | Content dims to 50%, buttons to 40% + `pointer-events:none`. Cyan strip: spinner, `RECORDING YOUR APPROVAL`, "countdown paused · don't close this". |
| `empty` | Dashed card, Bender portrait desaturated, `NOTHING TO AUTHORISE`, then what last happened and who did it. `VIEW HISTORY`. |

Default expiry window: **60 minutes**.

---

## D · Flight recorder (execution)

`execution.html` · states: `running` `verifying` `passed` `failed` `interrupted` `empty`

Vertical step rail, current step expanded. Connector line between markers is green behind completed steps, `--pe-border-strong` ahead of them.

Step markers: `✓` green filled (done) · pulsing amber dot (running) · `✕` red (failed) · dashed ring (pending).

| State | Treatment |
| --- | --- |
| `running` | Amber `STEP n OF N` header with spinner, elapsed + estimate, overall bar, expanded step showing a 3-line output well with caret. Bottom bar: `ABORT RUN` (red outline). |
| `verifying` | Cyan header — "Commands finished. The run isn't done until the service answers." Step rail swaps to a **check list**: passed rows green with measurement, in-flight row spinner + `2 of 3`, pending rows dashed at 60%. Footer: "rollback still available for 5m". |
| `passed` | Centred 76px green orb, `VERIFIED GOOD`, one-line outcome. All checks with measurements. Run summary: `TOTAL RUNTIME` / `DOWNTIME` / `AUTHORISED BY` / `ROLLBACK`. `ROLL BACK` + `DONE`. |
| `failed` | Red verdict `FAILED AT STEP n` + "Rolled back automatically. System is as it was." Then a **green rollback-confirmed card** — most urgent question is what shape the system is in. Failed step's output well ends on the real error and exit code. Leela's read → `NEW PLAN →`. |
| `interrupted` | Orange (`--pe-high`), pulsing `⏻` orb. `WHAT WE KNOW` card, one row per step: `✓` known-done, `?` uncertain with the specific ambiguity spelled out, `—` never started. Readout: `LAST CONTACT` / `AUTO-ROLLBACK: not attempted` / `SNAPSHOT: intact`. Primary is `RE-SCAN THE HOST`; `FORCE ROLLBACK` / `RESUME RUN` sit below as equals. |
| `empty` | Dashed glyph card, then a `LAST RUN` summary card (title, PASSED badge, plan, finished, authorised by) and a month tally with Scruffy's portrait. `VIEW RUN HISTORY`. |

---

## E · Desktop (≥1024px)

Nothing new is designed for desktop. Above 1024px the approval card and its execution run sit side by side (`minmax(0,1fr) minmax(0,1.1fr)`) so you can authorise and watch in one view; the abort action moves inline into the step header. Container detail keeps one column and gives the extra height to the log well. Below 1024px, everything is the phone layout at full width.

---

## Status level mapping (extends the table in DATA-CONTRACT.md)

| Source | `ok` | `warn` | `high` | `crit` | `none` |
| --- | --- | --- | --- | --- | --- |
| Login | authenticated | locked out | — | rejected | — |
| Approval | approved | pending | — | denied | expired, empty |
| Execution | passed | running, verifying | interrupted | failed | empty |
