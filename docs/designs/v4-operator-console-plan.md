# Planet Express v4 — The Operator Console (plan)

Status: approved direction, 2026-10-25 (decisions in section 7 recorded). No code until the design from Claude Design is in and phase 0 is scoped.
Companion: `v4-operator-console-design-brief.md` (what to hand to Claude Design).
Supersedes, for the operator-facing experience only: the Chat tab and the Missions board as built in v4.0.0-rc6.
Does not change: the safety model in `v4-mission-control.md` §5–6.

## 1. The disconnect, stated plainly

What the operator asked for, repeatedly: **what I do in a Claude CLI terminal today, inside PE.** I describe an
issue. The agent investigates, comes back with a plan or a few options. I talk to it: confirm, scrap, or modify.
When I confirm, it does the work and shows me it worked. A dedicated agent that is PE-aware, PE-ingrained, and takes
on the tasks that sit outside PE's automation.

What was built:

| Surface | What it actually is | Why it fails the ask |
|---|---|---|
| **Chat tab** | One question in, one answer out (a "ticket"). No conversation, no memory of the last turn. A small fixed action registry it may *propose*. 100 calls/day, its own model path. | You cannot follow up, redirect, or refine. It is a search box, not a colleague. |
| **Missions** | A status board (queued, investigating, awaiting decision...) fed only by an INVESTIGATE button on an incident, or Telegram commands. A headless agent runs once to completion. | You cannot start one from a sentence, watch it, talk to it, or choose between options. Its status names describe PE's state machine, not what you need to do. "Awaiting decision" shows a result and asks for nothing. |
| **Telegram** | Slash commands for the same board. | Same gap, smaller screen. |

The v4 design (§7) promised "attach to a live mission session, observe, interject, or take over ... this is what makes a
mission resemble the operator's existing workflow." Only the record of a takeover was built; attach, observe and
interject were not. That promise is the product, and it was left out. This plan puts it back at the centre.

## 2. The product in one paragraph

PE has **one place where the operator works: the Console.** It is a conversation with a PE-aware agent. The agent can
see the host's evidence, memory and history, investigate with real tools, and answer. When the right response is a
change, it lays out options as **plan cards** inside the conversation. The operator confirms, scraps, or modifies in
plain language or by editing the scope. Confirming mints the same bounded, hash-bound grant v4 already enforces; PE
then executes under checkpoint and tripwire supervision, streams progress into the same conversation, and reports the
result of **PE's own verification**, never the agent's say-so. The Missions board stops being a destination and becomes
the work log behind the Console.

The conversation agent never mutates the host. It reads, reasons and proposes. Only an approved grant, executed by PE's
supervisor, changes anything. That one rule keeps the whole v4 safety model intact while giving the operator the
terminal-like experience.

## 3. User stories

Actor: the operator (one person, signed in, on a laptop or a phone). "PE" is the system. The agent is PE's own session.

**Ask and investigate**
- U1. *I type "why is the media stack slow after the reboot?"* PE starts a conversation, attaches current evidence, and
  the agent investigates. I see what it is doing as it goes (commands it ran, logs it read), then an answer that cites
  the evidence and separates what it knows from what it guesses.
- U2. *I come back an hour later and ask a follow-up in the same conversation.* The agent remembers the thread, not
  just the last message.
- U3. *An incident opens.* I press Investigate on it (or tap a Telegram link) and land in a conversation that already
  holds the incident, its timeline and the relevant memory. I did not write a prompt.

**Plan, choose, shape**
- U4. *The agent finds the cause and offers two or three options*, each with what it changes, the risk, what it will not
  touch, how it rolls back, and how PE will check it worked. I can compare them side by side.
- U5. *I say "option B, but leave the router alone."* The agent revises; the card updates and shows exactly what changed
  in scope.
- U6. *I scrap it.* Nothing was changed. The conversation stays, and I can go another way.
- U7. *I narrow the scope by hand* (untick a service, add an exclusion) without typing, and the plan re-checks itself.

**Confirm and watch**
- U8. *I confirm.* I see one plain summary of exactly what I am authorising (scope, backup status, protected things
  excluded, time and effort limits). Approval is bound to that exact content. If anything changes, I must approve again.
- U9. *PE executes and I watch live*: steps, what changed, a visible checkpoint, and a Pause. If something outside the
  plan changes, PE pauses by itself and tells me why and what it can roll back.
- U10. *I interject mid-run* ("stop, not that container") and PE pauses and hands me control. *I take over* and PE
  records it and still checks the outcome.

**Verify and close**
- U11. *It finishes.* The conversation shows PE's checks, each pass or fail, in plain words, with the evidence. A failed
  check offers rollback or "keep going", not silence.
- U12. *I close it.* PE offers the lessons it noticed as memory candidates; I accept, edit or discard each one inline.
  Nothing becomes trusted memory without me.

**Everywhere else**
- U13. *I am on my phone.* I can read the thread, answer a plan card, confirm with the grant fingerprint, pause, and roll
  back from Telegram. Long output links to the Console.
- U14. *PE did something on its own* (weekly update pass, standing grant repair). It shows up as a conversation PE
  opened, in the same list, with the same record. I can reply to it.
- U15. *I always know what this is costing and what limits apply* (model budget remaining, how long this run may take).
- U16. *After a reboot*, the Console says the host is still settling and nothing it reports is final yet.

## 4. How this maps onto what is already built

Most of the machinery exists. The work is a front door, a conversation model and a real UI.

| Need | Exists today | Gap |
|---|---|---|
| Read-only investigating agent | Observe/Propose sessions: contained uid, own cgroup, hooks, brokered model access (verified live 2026-10-24) | One run per mission. Needs to persist across turns. |
| Bounded, hash-bound approval | Grant, approval card, `mission.approve` | Card is a separate page; it must be an in-thread plan card. |
| Execution safely | Repair supervisor: checkpoint, tripwire, Docker proxy, leases, verification | Output not surfaced live in a conversation. |
| Start from an incident | `mission.from_incident` | Needs to open a conversation, not a board row. |
| Start from free text | Nothing | New: a conversation can begin from a sentence. |
| Memory | Ship's Memory with quarantine | Review UI is a separate page; make it inline at close. |
| Telegram | Slash commands | Needs conversation replies and plan-card buttons. |
| Live view | Event stream (append-only) | Needs readable rendering and a live feed to the browser. |

### 4.1 The conversation model

- A **conversation** (thread) has an id, a title, an origin (operator, incident, PE-initiated), a state, and a list of
  **turns**. It owns a persistent workspace and agent-session identity for its lifetime.
- Each operator message starts one **supervised agent turn** (Observe/Propose tier) that resumes the same agent context.
  Everything the turn does is an event in the existing append-only stream, redacted before persistence as today.
- The agent may emit a **plan** (structured: options, each with steps, scope, risk, rollback, and verification checks
  drawn from PE's verification catalogue). PE validates it and renders the plan card. The agent cannot approve its own
  plan and cannot widen a grant.
- **Confirm** on a plan option creates a Repair/Change mission from that option (`mission.propose_repair` already does
  this), with the grant hash shown to the operator. Execution is a **separate** supervised session seeded with the
  approved plan. The chat session stays read-only.
- **Verification** is PE's, as in v4. Its result is posted into the thread.
- **Close** ends the conversation, offers memory candidates, and cleans up the workspace and session identity.

A mission remains the unit of authority and audit. The conversation is the unit of working. A conversation may spawn
zero, one or several missions. The Missions page becomes "Work log": every conversation and mission, filterable, for
audit and for PE-initiated work.

### 4.2 What happens to the old Chat tab

Retire it once the Console covers it. A one-shot question is simply the first message of a conversation. The old path
(separate model route, ticket store, 100/day quota) is removed so there is one agent path to secure and pay for. Until
then it stays, labelled as the quick answer box.

## 5. What does not change (safety invariants)

These are not negotiable and carry over exactly:

1. The conversation agent has no mutation authority. Only an approved grant executes.
2. Approval is bound to the hash of exactly what is shown. Edit the scope and the hash changes and re-approval is needed.
3. Protected domains (ingress, DNS, credentials, firewall, data volumes) always escalate; standing grants never cover
   Change or Critical.
4. Checkpoint before the first mutation; tripwire pause; verification is PE's, never the agent's assertion.
5. The real model key stays in Core; sessions hold a per-session code through the broker.
6. Redaction before persistence; memory candidates are quarantined until the operator confirms.
7. Any change to these surfaces gets the second review (Codex when available, Claude `/code-review` otherwise).

## 6. Phases

Each phase ends with the operator using it on the live host and saying whether it feels right. No phase starts until
the previous one has been used.

**P0. Spike (small, throwaway).** Prove, on the VM and then the live host, that one agent context can persist across
several turns under containment (the CLI's resume and streaming-input modes), that its output can be parsed into events
live, and that an operator message can be delivered mid-run or at the next turn boundary. Outcome decides turn-by-turn
versus true streaming; the plan above works with either.

**P1. Read-only Console.** Conversations from a sentence, from an incident, and from Telegram; follow-ups; live tool
trail; evidence citations; close with workspace cleanup. Includes the leaked-workspace fix. Acceptance: the operator can
investigate a real incident entirely in the Console and never opens the old Missions page.

**P2. Plans and confirmation.** Plan cards with options, modify and scrap, scope editing, grant summary, confirm, live
execution view, pause, interject, take over, PE verification in-thread, rollback. Acceptance: the first real Repair
(the rehearsed VPN-dependents recreate) is done from the Console end to end on the live host.

**P3. Telegram parity and memory.** Reply, plan-card buttons with grant fingerprint, pause/rollback; inline memory
candidates at close; PE-initiated conversations for automated work.

**P4. Retire and tidy.** Remove the old Chat path, fold the Missions board into the Work log, delete dead pages.

## 7. Decisions (recorded 2026-10-25, all as recommended)

1. **Retire the old Chat path** once P1 is live; one agent path to secure and pay for.
2. **Plan-level confirmation only**: one approval per plan, bounded by scope, with tripwire. No per-step prompts.
3. **Mid-run interjection pauses the run** and returns control to the operator.
4. **Telegram is a miniature console**: replies, plan-card buttons carrying the grant fingerprint, pause and rollback.
5. **Per-conversation model budget** is shown and capped by default, because the model key has its own limit.

## 8. Risks and honest unknowns

- The CLI's multi-turn behaviour under our containment is unproven; P0 exists to find out before we build on it.
- Agent output is untrusted. Plan JSON is validated and rendered by PE; free text is never executed or auto-linked to
  grants.
- Cost: every turn spends model budget. The budget display is part of P1, not an afterthought.
- UI scope is large. The design brief limits it to a small set of new components built on the existing design system.

## 9. Design received; engineering answers to its open questions (2026-10-25)

The Claude Design package is in `docs/designs/planet_express_design_v23/` (`handoffs/V4-OPERATOR-CONSOLE.md`, `screens/Console.dc.html`,
`design-system/components/25-console.css`, also appended to `static/cockpit.css`; drift check passes with the single file).

1. **Who writes the verdict sentence?** PE, from a template over state and counts. Agent text is untrusted and never sets a verdict.
2. **`could_not_check`** is a first-class verification result, and it never counts as a pass. Today an unrunnable check fails closed; P2 makes
   it a distinct, visible result.
3. **Plan expiry.** A plan is valid for 24 hours or until the evidence it rests on changes (scope or grant hash re-derived), whichever is first.
   Expired asks the operator to re-check and re-propose; it never executes.
4. **Missions replaced?** Yes, in P4. Old `/missions/<id>` links redirect to the matching work-log entry. Work log is a list inside the Console.
5. **Drift check with one file:** confirmed.
