# State machines in Planet Express: the map

Written 2026-11-10 against v4.0.0-rc14.7.3 (`6d6acb6`), from three read-only sweeps (Console; v4 missions; v3 Core) and a
check of the riskiest claims against the code. Claims marked **[checked]** were re-read by hand before this was written;
the rest carry the sweep's file:line and should be re-read before code is changed on their strength.

This is the map the state-machine project (TODOS.md, "State machines") asked for. It changes nothing.

## What "explicit" means here

One machine in PE is explicit: the mission status, `planet_express/missions/state.py`.

- One table, `TRANSITIONS`, says which edges exist. `OPERATOR_EXITS` says which of them need an operator.
- One function writes the status, `transition_in_txn`: it checks the edge and its rules (tier, actor, checkpoint, the
  approval predicate on entering `executing`, `completion_problems` on entering `completed`), compare-and-sets the row,
  records a `state` event, and carries the side effects that belong to the edge (`closed_at`, voiding the verification
  result, releasing leases and revoking sessions on terminal entry).
- No other code sets `missions.status` (checked by grep in the sweep).

Every other machine below falls short of this in one or more of four ways: no table (the legal edges are implied by
scattered `if` chains), no single writer (several functions `UPDATE` the column directly), state kept in memory (lost on
restart), or a state that is not stored at all (recomputed from columns, timestamps or event text by each reader). The
fourth is the one operators see: each surface recomputes it and they come out different.

## The inventory

| # | Machine | Where | Stored | One guarded writer | Verdict |
|---|---|---|---|---|---|
| C1 | Conversation (open / archiving / archived) | `console_conversations.state` | yes | yes (forward-only, no CAS) | keep; make "closed" one helper |
| C2 | Conversation display (label, group, composer) | `console/api.py` `describe`, `_view`; `execution.conversation_status` | no | n/a (two parallel functions) | **make explicit** (with C3) |
| C3 | Console run (execution) | `console_executions.status` + mission status + outcome + in-memory rollback | partly | CAS, return values ignored | **make explicit, first** |
| C4 | Turn and message consumption | `console_messages.consumed_turn`, runtime `_runs` | partly | yes (CAS on consume) | keep; drop the `0` sentinel |
| C5 | Plan (pending / superseded / scrapped / expired / confirmed) | `console_plans.status` + age | partly | CAS, plus a bulk bypass | **make explicit** (small) |
| C6 | Rollback sub-view | derived from events + `_rolling_back` | no | n/a | fold into C3 |
| C7 | Telegram follow cursor | `console_telegram` | yes | direct writes | keep |
| M0 | Mission status | `missions.status` | yes | **yes** | the reference |
| M1 | Cancel request (requested / held / dismissed) | `cancel_requested_*` + `_cancel_held` (memory) | partly | no | **make explicit** (sub-state) |
| M2 | Rollback / undo | status + `_rolling_back` (memory) + event text | partly | no (two writers decide the end) | **make explicit** |
| M3 | Checkpoint (set / retired / purged / kept) | `checkpoint_id` + disk + events | partly | CAS writes | keep; derive "kept" once |
| M4 | Verification and "held" | result JSON + `_verifying` (memory) + a predicate | partly | yes for the result | **make explicit** (`held_reason`) |
| M5 | Outcome and flags | `outcome`, `outcome_flags` (one string) | yes | no | fold into M0 |
| M6 | Leases | `mission_leases` (+ suspect, expiry) | yes | no | make explicit (small) |
| M7 | Sessions | `mission_sessions.state` + timestamps, `_runs` (memory) | partly | no | **make explicit** (terminal states) |
| M8 | Mission approvals (per phase, hash-bound) | `mission_approvals` (append-only) | yes | yes (insert + predicate) | keep; fix the standing-grant fallback |
| M9 | Standing grants | `standing_grants` | yes | yes (atomic) | keep; one state function |
| M10 | Intent journal | events | yes | no | fold into M0 |
| M11 | Start waits and deferrals | supervisor memory | no | n/a | keep; persist the 30-minute clock if it must hold |
| M12 | Launcher preflight | supervisor memory | no | n/a | keep |
| M13 | Recovery classification | computed, one event | no | n/a | keep |
| M14 | Memory cards | `memory_cards.status` | yes | yes (CAS + history) | keep |
| V1 | PipelineState (scan slot, mutation lock, re-scan wish) | memory + `run_status.json` | partly | yes for the lock | **map only**; fix the two bugs around it |
| V2 | Pipeline status as the dashboard sees it | `run_status.json` | yes | two writers | fold into V1 (read over RPC) |
| V3 | v3 approvals (pending / approved / denied / expired) | `approvals.status` | yes | no (several direct writers) | **make explicit** (add superseded, refused) |
| V4 | v3 executions (+ rollback children) | `executions.status` | yes | no from-state check | **make explicit** |
| V5 | Execution steps (status x effect) | `execution_steps` | yes | yes (conditional updates) | fold into V4 |
| V6 | Attempt ledger | `attempts.state` | yes | one stray raw-SQL writer | keep; one writer |
| V7 | Canary rollback windows | expiry sentinels and `closed_at` | partly | no | make explicit (`state` column) |
| V8 | Zoidberg canary outcomes | `update_history.json` | yes | n/a | keep; rename colliding words |
| V9 | Incidents (+ hint) | `incidents.status` | yes | yes | keep; make "dismissed" real |
| V10 | Safe-prune gate | guard sequence + memory dedupe | no | n/a | keep |
| V11 | Config apply | result + hash compare | no | yes | keep |
| V12 | Boot settle, maintenance | computed / marker file | no | n/a | keep |
| V13 | Chat tickets | `chat_tickets.status` | yes | yes | retire with Chat (TODOS) |

## The machines worth making explicit

### C2 + C3 + C6: the Console run (first)

**Today.** What the operator sees about a confirmed plan's run is computed from four inputs:
- `console_executions.status`: `proposed`, `authorised`, `withdrawn`, `ended`.
- The Repair mission's status.
- The outcome flags.
- The supervisor's in-memory `_rolling_back`.

Six places put words and buttons on it:
- `REPAIR_STATUS` (`console/execution.py:82-93`)
- `conversation_status` (`execution.py:262-283`)
- `_view`'s override (`api.py:487`)
- the thread lines (`service.py:192-210`)
- the JS button labels
- the Telegram replies

The allowed actions are declared four times:
- `CONTROLS`
- the RPC `_choice`
- the web route allow-list (`casa_scruffy.py:1029`)
- Telegram's `ACTIONS = ("rollback",)`

**What goes wrong.** All of these are **[checked]** unless noted otherwise.
- **An undo's result is never reported.** The run is marked `ended` as soon as its mission reaches `completed`, because `completed` is in `TERMINALISH` (`service.py:211-212`). From then on `live_executions` skips it (`store.py:267`). But `completed` can still go to `rollback_pending` and on to `rolled_back` (`state.py:59-61`), and nothing reports that.
- **A run you kept reads "Didn't work".** Nothing in `console/` knows `kept_by_operator`. The run shows "Didn't work" and sits under "Needs you", which offers no action.
- **Scrapping a plan leaves its prepared run waiting.** `scrap_plan` (`api.py:286-293`) never withdraws the proposed run. Superseding, expiring or archiving doesn't either. Its Repair mission waits in `awaiting_decision` until someone opens the Work log.
- From the sweep, not re-read:
  - A Work-log approval of a Console proposal never shows in the conversation (`api.py:479`, `store.py:267`).
  - A new Repair can be prepared while an older run's undo is pending, and the view then hides the older run (`api.py:349`, `479-482`).
  - A held run whose checks failed is labelled "PE isn't sure what state this is in" (`execution.py:91`), while the dashboard says it is held.
  - "Check again" and "Keep" are dashboard-only; the thread line and Telegram offer only roll back.
  - Idle auto-archive can archive a conversation whose run is still going (`service.py:164`). Telegram then unfollows it and the outcome is never pushed.

**Make explicit.** One run-phase function over `(execution status, mission status, outcome flags, rolling back, held reason)`. It returns:
- the phase
- the label and level
- the rail group
- the thread line
- the allowed actions

The API view, the thread reporter, Telegram, the RPC allow-list and the web route all read it. `ended` becomes "nothing left can happen": `rolled_back`, `failed`, `cancelled`, or `completed` with no undo possible. Withdrawing a proposed run becomes a side effect of scrapping, superseding, expiring or archiving its plan (C5).

### M1, M2, M4, M5, M10: what the mission status does not say

These are the second dimensions of a mission that `missions.status` does not carry, each kept its own way:
- **Why a mission is `needs_operator`.** Failed checks, a failed restore, an unknown host or a held cancel. `held_after_failed_checks` is a predicate over five columns.
- **Whether a rollback is running.** In-memory `_rolling_back`, and only the async path registers there.
- **Whether a cancel is held.** In-memory `_cancel_held`.
- **How it ended.** `outcome` and `outcome_flags`, written at one moment and never updated.
- **Whether an intent is closed.** Events.

**What goes wrong.**
- **A drift refusal ends in `needs_operator`.** **[checked]** Every refused or failed restore goes there through `CheckpointService._stop` (`checkpoint/service.py:780-808`). The supervisor's `roll_back` docstring (`supervisor.py:1253-1260`) and the Console comments say a drift refusal stays in `rollback_pending`. Two writers decide the post-restore state, and the documentation follows the one that loses.
- From the sweep:
  - A rollback from the dashboard or Telegram goes the synchronous way and is invisible to `rollback_in_flight`, so the Console can queue a second one (`supervisor.py:1253` vs `1351`).
  - `outcome` is stale after a rollback: an undone run still says "completed" on Telegram and the dashboard.
  - A recheck run without a verifier loses the held state for good (`supervisor.py:1931-1950`).
  - After a failed rollback of a failed run, "Keep the changes" is offered on a half-restored host.
  - `keep_and_close` closes intents as an operator, which `open_intents` ignores (`recovery.py:77`).
  - Held cancels re-read the host every 2 s.

**Make explicit.**
- A persisted `held_reason` on the mission (`failed_checks`, `failed_restore`, `unknown_host`, `cancel_held`), written by the edge that enters `needs_operator`.
- A persisted rollback sub-state (`requested`, `running`, `refused_drift`, `incomplete`, `done`) with one writer.
- The outcome and the closing of intents written inside `transition_in_txn` on terminal entry, derived from the edge, with flags as a set.
- `kept_by_operator`, `partially_applied` and the rest become flags the run-phase table (C3) reads.

### M7 + M6: sessions and leases

**[checked]** `mission_sessions.state` is only ever written `'launching'` or `'live'` (`missions/store.py:411`). A session's end is implied by `revoked_at` and `uid_released_at`. `leases.claim_reason` (`leases.py:294-318`) exempts a held `needs_operator` mission only when no session row has `revoked_at IS NULL OR state = 'live'`. Every session that ever ran is still `'live'`, so the exemption never applies in production. The one test that covers it writes `state='ended'` by hand (`tests/test_actions_missions_exclusion.py:140`).

**Effect.** While a run is held for the operator, its leases keep refusing every v3 host change: Actions, config apply, update rollbacks and safe prune. This is the opposite of the docstring's promise. It is how M-34 held 47 leases while the operator worked.

**Make explicit.**
- Give sessions terminal states (`revoked`, `reaped`), written where the timestamps are written today.
- Write the lease expectation per mission state beside `TRANSITIONS`.
- Lease expiry (`mark_expired_suspect`) and checkpoint purge (`purge_expired`) have no production caller (sweep). Either schedule them or delete them.

### V3 + V4 + V5 + V7: v3 approvals, executions and rollback windows

These are under the second-review gate (execution safety).

**V3: approvals.** Superseded and cutover cards are stored as `expired`, and policy refusals as `denied`. The real reason survives only in events, so the dashboard shows "EXPIRED · lapsed" where Telegram says "superseded", and it shows "Refused by current policy" for a missing or tampered plan. Expired Telegram cards keep working buttons (sweep).
- Add `superseded` and `refused` as states.
- Route every write through one guard that also edits the card.

**V4 + V5: executions.**
- `set_execution_status` checks that the value is a known status, but not the state it comes from. A rollback deliberately overwrites the parent's terminal status, so the run's own outcome survives only in events.
- **[checked]** An abort is accepted for automatic and Zoidberg runs but never acted on. `run_automatic` → `_run_decided` passes no `abort_check` (`command_service.py:955-961`), so the engine defaults to `lambda: False` (`engine.py:117`). The operator is told "the run stops before its next step".
- The execution page's "STEP 1 OF 2 / 2 OF 2" is derived from `running`/`verifying`, which flip on every step of a longer runbook (sweep).
- Fix: a transition table with a from-state guard. Record the rollback outcome in its own column instead of overwriting the run's status. Fold the step status and effect in as a sub-machine.

**V7: rollback windows.** These are expiry sentinels (`expires_at >= INDEFINITE_EXPIRY` means held), and the test is repeated in four places. The comments promise a grace period after a successful canary, but the engine closes the window as soon as the step ends (sweep: `engine.py:684-691`).
- Add a `state` column (`watching`, `held`, `closed`, `settled`).
- Decide whether the grace period is wanted.

### V1: PipelineState (mapped, not changed)

The lock logic is sound. Check-and-set happens in one acquisition, a release by anyone but the owner is ignored, and maintenance and mission claims are read under the lock.

What is wrong is around it, not in it:
- **The status file can say "running" after a restart.** **[checked]** Only `awaiting_approval` is normalised at startup (`casa_farnsworth.py:3010`). A crash mid-scan leaves `run_status.json` saying `running`, and the dashboard shows SCANNING with the button disabled until the next scheduled run. That run can be held off for up to 15 minutes by boot settle, or 6 hours by maintenance.
- **The dashboard RPC starts before the mission-claims check is registered.** **[checked]** `_start_dashboard_rpc` runs at `casa_farnsworth.py:3140`, and `set_mission_claims` at `3150`. A dashboard action in that gap does not see a lease left over from before the restart. The window is small, but the order is wrong.
- **Dead paths** (sweep): `EXECUTING`, `pending_plan_id` and `pending_msg_id`, and `end_mutation(reset_to_idle=True)` have no production writer. `transition()` accepts any string. The "…or awaiting approval" wording survives in refusals.
- **V2.** The dashboard reads the status from a side file. It should come over RPC with the owner and maintenance, so a running mutation stops showing as "pipeline idle".

## The machines to leave

- **C1, C4, C7, M8, M9, M12, M13, M14, V6, V9, V10, V11, V12.** Each one has one guarded writer, or is a pure function, or is small enough that a table adds nothing.
- Small follow-ups recorded with them:
  - C1: one `is_closed` helper instead of five recomputations.
  - C4: replace the `consumed_turn = 0` "taken" sentinel.
  - M8: a lapsed standing grant must fall back to operator approval. Today the mission can never start (sweep).
  - M9: the standing page shows ACTIVE for a grant `evaluate` refuses.
  - V6: move the engine's raw `UPDATE attempts` into the store.
  - V9: make "dismissed" a real event.

## Bugs found on the way

These are fixable without building any machine, so they need not wait for the project.

| | Bug | Status | Where |
|---|---|---|---|
| 1 | A held mission keeps blocking every v3 host change (the exemption is dead) | fixed rc14.7.4 | `leases.py:302-307`, `missions/store.py:411` |
| 2 | `run_status.json` says "running" after a crash mid-scan; SCAN stays disabled | fixed rc14.7.4 | `casa_farnsworth.py:3010` |
| 3 | Dashboard RPC starts before mission claims are wired | fixed rc14.7.4 | `casa_farnsworth.py:3140` vs `3150` |
| 4 | Abort accepted but ignored for automatic and Zoidberg runs | fixed rc14.7.4 | `command_service.py:955-961`, `engine.py:117` |
| 5 | An undo's result never reaches the conversation or Telegram | fixed rc14.7.5 | `console/service.py:211-212`, `console/store.py:267` |
| 6 | A kept run reads "Didn't work" under "Needs you" in the Console | fixed rc14.7.5 | `console/execution.py` (no `kept_by_operator`) |
| 7 | Scrapping or superseding a plan leaves its prepared Repair waiting | fixed rc14.7.5 | `console/api.py:286-293` |
| 8 | Drift-refused restore ends in `needs_operator`; docstring and Console say `rollback_pending` | fixed rc14.7.6 | `checkpoint/service.py:780-808`, `supervisor.py:1253-1260` |
| 9 | Sync rollback (dashboard, Telegram) invisible to the Console; a second restore can be queued | fixed rc14.7.5 (the supervisor registers it) | `supervisor.py:1253` vs `1351` |
| 10 | `outcome` stale after a rollback (an undone run says "completed") | fixed rc14.7.6 | `supervisor.py:1983-1989` |
| 11 | Recheck without a verifier loses Check again / Keep for good | fixed rc14.7.6 | `supervisor.py:1126-1147`, `1931-1950` |
| 12 | "Keep the changes" offered after a failed rollback of a failed run | fixed rc14.7.6 | `verify/plan.py:85-91` |
| 13 | Idle auto-archive can archive a conversation whose run is still going | fixed rc14.7.5 | `console/service.py:164` |
| 14 | Dashboard shows "EXPIRED" for a superseded v3 card and "Refused by current policy" for a missing plan | sweep | `static/approvals.js:174-177`, `integrations/rpc.py:466-475` |
| 15 | Expired Telegram cards keep working buttons | sweep | `core/store.py:953-959` |
| 16 | "STEP 1 OF 2" counter wrong for longer runbooks | sweep | `static/execution.js:206` |
| 17 | No re-scan after a failed rollback or an interrupted canary update | sweep | `casa_farnsworth.py:2738`, `2797` |
| 18 | Lease expiry and checkpoint purge sweeps never run | sweep | `leases.py:520`, `checkpoint/service.py:1062` |
| 19 | The NEEDS YOU count ignores missions held for the operator | fixed rc14.7.4 | `casa_scruffy.py` `needs_count` |

## Proposed order

1. **rc14.7.4: bug fixes, no machines.**
   - Bugs 1, 2 and 3: small, high-value, and around the v3/v4 lock.
   - Bug 4: abort means abort, or stop offering it.
   - Bug 19: add held missions to the NEEDS YOU count.
   - All under the second-review gate.
2. **Console run phase (C2 + C3 + C5 + C6).** Characterization tests first, then one run-phase table that every surface reads. It fixes bugs 5, 6, 7 and 13, plus the wording splits.
3. **Mission sub-states (M1, M2, M4, M5, M10).**
   - Persisted `held_reason` and rollback sub-state.
   - Outcome and intents written in `transition_in_txn`.
   - It fixes bugs 8 to 12 and gives C3 the held reason it needs.
   - Replay the host's real mission events to prove the derived states match.
4. **Sessions and leases (M6, M7).** Terminal session states, a per-state lease expectation, and the two sweeps scheduled or removed. Bug 1's permanent fix lands here if step 1 only patched the query.
5. **v3 approvals, executions and windows (V3, V4, V5, V7).** Fixes bugs 14 to 17.
6. **V2:** pipeline status over RPC.

Each step:
- one release
- characterization tests before the change
- stored values and schemas unchanged (legacy values map onto the table)
- a replay of the live host's events where the machine has them
- the agent's brief told the state and allowed next moves from the same table, where the agent can act on it
