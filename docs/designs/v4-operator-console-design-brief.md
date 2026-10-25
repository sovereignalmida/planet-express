# Planet Express — Operator Console: design brief for Claude Design

Hand this document, plus the existing design package (`docs/designs/planet_express_design_v23/`), to Claude Design.
The product plan it implements is `v4-operator-console-plan.md`. Read that first if you need the why.

## 1. What you are designing

A single working surface where one operator **talks to a PE-aware agent about their homelab**, gets options, and
confirms, scraps or modifies them, then watches PE carry out the confirmed work and prove it worked. Think of the
experience of using a coding agent in a terminal, rebuilt as a calm, instrument-grade page inside the existing Planet
Express dashboard.

It replaces two screens that exist today and are poor: a Chat tab (one question, one answer) and a Missions page (a
status board that fits PE's internals, not the operator's task). Do not restyle those. Design the Console that makes
them unnecessary.

The product feel: a trustworthy cockpit, not a chat toy and not a ticket queue. The operator is making consequential
decisions about their own infrastructure, often tired, sometimes on a phone. Calm, legible, honest about what is known
and what is guessed.

## 2. Hard constraints (the design system already exists)

Extend the system; do not invent a new one. Everything below is in `planet_express_design_v23/system/README.md` and
`cockpit.css`, and is the ground truth if this brief is silent.

- Tokens only, no hardcoded hex. Fonts: Chakra Petch (headings, hero numbers), Space Grotesk (prose), JetBrains Mono
  (every machine value: paths, units, hashes, times, exit codes).
- Four status levels only: `ok`, `warn`, `high`, `crit`, plus `none` for absent data. Cyan is navigation and primary
  action, never status.
- Every surface opens with a **verdict**: one sentence that answers the page's question before detail (`pe-verdict`).
- One hero number per card; absent data has a shape (dashed `none` card), never a blank panel.
- Motion is a severity signal only (`warn` slow beacon, `crit` fast pulse). Never animate `ok` or `none`.
- Destructive or system-changing actions are two-step, name their blast radius, and show who it will be logged as
  (`pe-sheet`).
- The first 582 lines of `static/cockpit.css` are sealed. New CSS is appended with the `pe-` prefix, and each new
  component is also added to `design-system/components/` so the drift check passes. Use the existing crew avatars where a
  persona helps; do not add new characters.
- Existing pieces to reuse, not redo: topbar and tabs, `pe-card`, `pe-verdict`, `pe-sheet`, status chips, the approval
  card and execution-progress screens in `Cockpit v4 - Action Screens` (the plan card is an evolution of that approval
  card), the stack drawer, the "host still settling" banner.
- Responsive: designed at 1440×900 and 390×844. Keyboard operable; visible focus; contrast per the system's table;
  reduced motion respected; every status carries text, not just colour.

## 3. Anti-goals

- No kanban board of internal states. No columns called "investigating" or "awaiting decision".
- No separate pages for the approval card, the execution log and the result. They live in the conversation.
- No raw JSON, grant hashes or event sequence numbers in the main flow. Machine values appear in mono, in a collapsed
  "details" layer, and as a short fingerprint where the operator must confirm exactly what they approved.
- No endless scroll of tool output. Tool activity is a compact, expandable trail.
- No decorative chat bubbles with avatars on every line.
- No generic dashboard widgets (no pie charts, no vanity counts).

## 4. Information architecture

- Top bar tab **Console** (replaces Chat and Missions; a **Work log** link lives inside it).
- Console layout, desktop: left rail (conversation list), centre (the conversation), right panel (context, collapsible).
- Console layout, phone: list screen and conversation screen as separate views; the right panel becomes a bottom sheet.

### 4.1 Left rail: conversations

Each row: title (first line of what was asked, editable), one status line in plain words (see 6), origin icon
(you / incident / PE started it), age, and a small level dot when something needs the operator. Groups: **Needs you**,
**Working**, **Recent**. A prominent **New conversation** entry and a search box. Empty state: a short invitation with
three example prompts the operator can click.

### 4.2 Centre: the conversation

Top: a verdict strip for the whole thread ("Waiting for your choice between two options", "Working: step 2 of 4",
"Done and verified"). Below it, the turns as a vertical sequence of **blocks**, newest at the bottom, with a composer
pinned under them.

### 4.3 Right panel: context

Tabs or stacked sections: **Evidence** (what the agent is looking at: incident, relevant containers, memory used),
**Scope** (services, paths, exclusions in play), **Limits** (model budget left, time and effort limits), **Record**
(link to the full event log). Collapsed by default on phone.

## 5. The blocks (the new components)

Design each with its states. Names are suggestions; keep the `pe-` convention.

1. **Operator message.** Plain text; may carry attached references (an incident, a service) as chips.
2. **Agent message.** Prose answer. Claims that rest on evidence carry small numbered evidence chips that open the
   source. A visible split between **What I know** and **What I'm unsure of** when the agent is uncertain. Streaming
   state with a calm progress indicator.
3. **Activity trail.** A single compact line group per agent turn ("Looked at 3 containers, read 2 logs, checked the
   network config") that expands into the individual steps with their output. Read-only vs changed marked clearly.
   Failed or denied steps visible and explained.
4. **Plan card** (the centrepiece). One card holding 1 to 3 **options**. Each option shows: a one-sentence summary,
   **what changes** (services, files), **what it will not touch**, **risk** (level chip plus one sentence),
   **how it rolls back**, **how PE will check it worked** (the checks, in words), and an estimated effort. Options can
   be compared side by side on desktop and swiped on phone. A recommended option is marked, with the reason. Actions on
   the card: **Confirm** (primary), **Modify** (opens the composer pre-filled to talk about it, plus **Edit scope**),
   **Scrap**. States: pending, edited (shows a diff of what the edit changed), superseded by a newer plan, confirmed,
   scrapped, expired.
5. **Edit scope.** Inline controls to untick services, add exclusions or tighten limits. Shows protected things as
   locked with the reason. Any edit visibly marks the plan as changed and requires re-confirmation.
6. **Confirm sheet.** The two-step confirmation: plain-language statement of exactly what is being authorised (scope,
   backup status with a clear warning if not covered, protected exclusions, time and effort limits), the operator it
   will be logged as, and a short fingerprint of the approved content. Primary button names the action ("Authorise
   the restart of 3 services"), never just "OK".
7. **Execution timeline.** After confirmation, a live vertical timeline inside the thread: checkpoint taken, step 1..n
   with status and what changed, with a **Pause** control always in reach. Calm while healthy.
8. **Tripwire banner.** If something outside the plan changes, a `crit`-level banner interrupts the timeline: what
   changed, that PE paused, whether rollback is available, and the choices **Resume**, **Roll back**, **Take over**.
9. **Interject / take over.** The composer changes state while a run is live: sending a message pauses the run and says
   so. A visible **Take over** control marks the thread as operator-driven and explains that PE will still check the
   outcome.
10. **Verification result.** PE's checks as a short list, each pass, fail or could-not-check, with the evidence behind
    it. Headline verdict ("All 3 checks passed" or "1 of 3 failed: the VPN-routed apps still can't reach the internet").
    On failure: **Roll back**, **Keep working on it**, **Take over**. Clear that this is PE's check, not the agent's
    opinion.
11. **Close and learn.** Closing offers the lessons PE noticed as **memory candidates**, each with accept, edit,
    discard. Makes clear nothing becomes trusted without the operator.
12. **System notices in-thread.** "The host is still settling after a reboot", "Model budget 80% used", "PE paused this
    because...", rendered as quiet inline notices, not toasts.
13. **Composer.** Multiline, send on Enter, with the ability to reference a service, stack or incident by typing a
    mention. Shows a short, honest line when the agent is busy ("working, your message will pause it"). Disabled with
    an explanation when the host is in proposal-only mode.
14. **Budget meter.** A small, always-visible indicator of model budget remaining for the conversation, with a warn
    state at 80% and a stop state at the limit, and what happens then.

## 6. Plain-language status vocabulary

The UI never shows internal state names. Use these (internal state on the left is for engineers only):

| Internal | Operator sees | Level |
|---|---|---|
| draft, queued | Getting ready | none |
| investigating | Looking into it | ok (calm) |
| awaiting_decision, Observe/Propose finished | Result ready | warn if it asks something of you |
| awaiting_decision, Repair approval open | Needs your call | warn |
| executing | Working | ok |
| paused_tripwire | Paused: something unexpected changed | crit |
| verifying | Checking it worked | ok |
| completed | Done and verified | ok |
| failed | Didn't work | high |
| rolled_back | Rolled back | warn |
| needs_operator, recovery_required | Needs you: PE isn't sure what state this is in | crit |
| cancelled | Scrapped | none |

## 7. Key flows to design end to end

1. **Ask and investigate** (the happy path to an answer, no change).
2. **Investigate to plan to confirm to verified** (the full loop, with the plan card, a modify turn and the confirm
   sheet).
3. **Scrap and redirect.**
4. **A run goes wrong**: tripwire banner, then roll back.
5. **Verification fails**, then keep working or take over.
6. **Start from an incident** (the evidence is already in the thread; the operator did not write a prompt).
7. **PE started this** (a conversation PE opened for automated work, and the operator replies to it).
8. **Phone**: reading a thread, answering a plan card, confirming, pausing, entirely in a thumb-reachable layout.
9. **Post-reboot / degraded**: host settling, proposal-only mode, model budget exhausted. What the Console says and
   what is disabled.
10. **First run**: empty list, the invitation, the first conversation.

## 8. Content and tone

- Direct, plain, specific. Short sentences. Say what was done and what is known. Never claim certainty the evidence
  doesn't support.
- Name consequences. "This restarts three services and recreates the VPN gateway. Apps routed through it will be
  offline for about a minute."
- The existing UI has light Futurama personality in the side commentary only. Keep the Console serious in decision
  surfaces (plan card, confirm sheet, tripwire); personality is allowed only in empty states and the sidebar.
- Machine values (container names, paths, units) in mono, always.

## 9. Sample content to design against (real shapes, generic names)

- **Prompt:** "The download apps lost internet after the reboot. Figure out why."
- **Agent answer:** "The VPN gateway container came back up before its port-forward was assigned, so the apps that join
  its network can't route. I looked at: [1] gateway health, [2] the port-forward status, [3] which containers share its
  network. **Known:** the gateway is healthy; four dependents are running but unreachable. **Unsure:** whether the
  provider's port rotation caused this or the start order did."
- **Plan, Option A (recommended):** "Recreate the four dependents in order so they rebind to the gateway." Changes:
  4 services in the `media` stack. Will not touch: the gateway, ingress, DNS, any data volume. Risk: low, about one
  minute of downtime. Rolls back to: the checkpoint taken just before. PE will check: each dependent can reach the
  internet from inside the gateway's network, and the stack is complete.
- **Plan, Option B:** "Restart the gateway only and wait for the port-forward." Risk: medium, dependents may not
  recover. 
- **Verification:** "2 of 3 checks passed. Failed: `qbittorrent` cannot reach the internet from inside the gateway's
  network."
- **Tripwire:** "Paused. A file under the ingress configuration changed (not part of this plan). I can roll back to the
  checkpoint from 14:02."

## 10. What to deliver back

1. Desktop (1440×900) and phone (390×844) screens for flows 1 to 10, each with its states (loading, empty, error,
   streaming, long content, many options).
2. Every block in section 5 as a component sheet with all states.
3. New CSS classes and tokens (appended, `pe-` prefixed) and the matching `design-system/components/` split.
4. A short mapping from each screen to the data it needs, so engineering can wire it. Mark any datum you assume that
   is not listed in section 5 or 6.
5. Accessibility notes: focus order across the three columns, live-region behaviour for streaming and for the tripwire
   banner, and the keyboard path for Confirm.
