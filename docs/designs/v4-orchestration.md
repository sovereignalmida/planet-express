# v4 orchestration: reviews, agent routing, usage budget, and account handoff

Companion to `v4-plan.md`. The primary orchestrator (a Claude Code session) runs the slice map;
other models and agents are added as workers or reviewers. Everything the next session needs lives
**in the repo**, so any Claude account, machine or model can pick up cold.

## 1. Review pipeline (before and after)

Every slice passes two independent reviews, at two points. Codex is the standing second reviewer
(`CLAUDE.md`); other models may be added to the roster below without changing the process.

| Gate | When | Who | What | Output |
|---|---|---|---|---|
| G1 Spec review | Before any code | Codex (`codex exec` with the spec + brief sections) plus the orchestrator | Missing acceptance tests, unsafe assumptions, contract holes, scope creep | Findings appended to the slice brief; spec amended |
| G2 Test-first check | Tests written, before implementation | Orchestrator | Tests fail for the right reason; every acceptance bullet has one | Slice brief ticks |
| G3 Diff review | Implementation done, tests green | `codex review --base v4-next` (or `--uncommitted`) **and** `/code-review` | Bugs, safety regressions, v4 surface items in `CLAUDE.md` | Each finding fixed or declined in writing in the slice notes |
| G4 Release review | Before any deploy conversation | `codex review --base v3-next`, `/code-review high`, `/cso` on the v4 security surface | Whole-branch | Review record in `docs/handoff/v4/` |

Rules:
- Safety-surface slices (1 leases, 5 checkpoint, 6 tripwires/grants, 7 approval/privilege, 8
  recovery) get G1 **and** G3 from two different reviewers; one of them must not be Claude.
- Never self-review as the only review. The author of a slice is not its G3 reviewer.
- A finding is never dropped silently. Declined findings carry a reason and go in the slice notes.
- Review prompts are narrow (the slice brief plus the diff plus the named `CLAUDE.md` surface),
  not "review the repo". That is cheaper and finds more.

## 2. Agent roster and routing

Keep this table current as agents and models are added. Route by task, cheapest capable first.

| Role | Default | Use for | Not for |
|---|---|---|---|
| Orchestrator | Claude (primary session) | Slice map, contracts, merges, review gates, decisions, handoff files | Bulk implementation or log reading |
| Implementer | Sonnet-class Claude subagent in an isolated worktree, or another model | One slice from its brief, tests first | Cross-slice design |
| Explorer | Haiku/Explore subagent | Fan-out reading, "where is X" | Judgement calls |
| Spec/diff reviewer | Codex CLI | G1, G3, G4 | Writing code in the same slice it reviews |
| Security pass | `/cso` | v4 security surface, G4 | Routine slices |
| Additional models | operator-added | Add a row: name, how invoked, cost class, strengths | — |

Dispatch rules:
- One slice brief per worker (`docs/handoff/v4/S<n>-brief.md`, the repo's existing `T<nn>-brief`
  convention). The brief states the goal, files to touch, acceptance tests, constraints, and the
  report format. Workers do not read the whole design, only the sections the brief cites.
- Independent slices run in parallel worktrees (1∥2, 3∥4, 5∥6, 8∥6/7); each reports tests run,
  files changed, and open questions. The orchestrator is the only merger.
- A worker that gets stuck reports and stops. It does not widen scope.

## 3. Usage efficiency

- **Read narrowly:** cite file and line ranges in briefs; use `graphify`/grep first; do not paste
  whole files into prompts. Prefer the existing T-brief style of precise pointers.
- **Delegate bulk work down-tier:** exploration, mechanical edits, fixture writing and test
  scaffolding go to cheaper agents. Judgement, contracts and review triage stay with the
  orchestrator.
- **Batch tool calls and reviews:** one `codex review` per slice, not per commit; one combined G1
  prompt per slice.
- **Keep the orchestrator context small:** decisions go to files, then the conversation moves on.
  Compact at slice boundaries, not mid-slice.
- **Token hygiene:** test runs use targeted selection first (`pytest tests/test_<area>.py -x -q`),
  the full suite once per slice at G3. Large outputs are summarised to a file, not echoed.
- **Do not poll.** Background workers notify on completion; schedule long fallbacks only.
- **Budget check at every slice boundary:** record remaining usage in `STATE.md` and choose the
  next slice's worker tier accordingly (§4).

## 4. Handoff across accounts, machines and models

Handoff is a repo-level protocol, not a conversation summary. Source of truth:
`docs/handoff/v4/STATE.md`, updated at every checkpoint and committed.

### STATE.md contains
- Current slice and status (not started / spec / tests / impl / G3 / done) for all slices.
- The exact next action, written as an instruction to a stranger.
- Branch, last commit, worktrees in use, background agents running (and their briefs).
- Open review findings and their disposition.
- Decisions made since the brief (with date), and anything the operator must answer.
- Usage note: which account/model is working, rough budget left, planned handoff point.
- Commands to resume: how to run tests, how to run `codex review`, how to reach test hosts.

### Handoff triggers
- A session reaches ~80% of its usage window, or the operator says "hand off".
- Before any long-running or background operation the next session would need to know about.
- At every slice boundary regardless (cheap insurance).

### Handoff procedure (the outgoing session)
1. Finish or cleanly stop the current unit; never leave a half-edit uncommitted without noting it.
2. Run the slice's tests; record the result.
3. Update `STATE.md` and the slice brief; commit with a `wip:` or slice message on `v4-next`
   (or the slice branch).
4. Push the branch if the operator has authorised pushing (the receiving account may be on another
   machine). Without a push, the receiving account must work in the same checkout.
5. Print a one-paragraph resume prompt: "Read `docs/handoff/v4/STATE.md` on branch `v4-next` and
   continue from 'Next action'."

### Pick-up procedure (the incoming session, any account)
1. `git checkout v4-next && git pull`; read `CLAUDE.md`, `STATE.md`, the current slice brief.
2. Verify the working tree matches `STATE.md` (branch, last commit, worktrees); if not, trust
   git, then correct `STATE.md`.
3. Run the targeted tests for the current slice to confirm the baseline.
4. Continue from "Next action". Do not re-derive decided matters (brief §14, locked decisions in
   `CLAUDE.md`).

### Constraints that travel with the repo
- Codex and other CLIs may not be authenticated on a different account's machine; STATE.md names
  the review tool each gate needs so the incoming session can substitute (e.g. a different
  reviewer model) without skipping the gate.
- Memory files under a Claude account's home are **not** portable; anything that matters goes in
  `docs/handoff/v4/` or the design docs.
- No deploy, push to a shared remote, or release action happens without an explicit operator go,
  on any account.

## 5. Open items
- Operator to confirm whether pushing `v4-next` to `origin` is authorised for handoff.
- Operator to add further agents/models to the §2 roster as they become available.
