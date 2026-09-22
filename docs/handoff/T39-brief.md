# Task T39 — slice 5b-1b (part 1): the runbook engine, and existing actions on it

Branch `v2`. Python 3.11+, no new dependencies. Spec: `docs/designs/slice-5b-multistep-execution.md`
(ACCEPTED, rev 3.1) §3-§4, §6-§7. Builds on T38 (schema v5, `runbook.py`, `decide_runbook`, store
step/attempt/candidate APIs) — read those first. T40 adds the abort/rollback **controls** (RPC,
dashboard, Telegram); T39 builds the engine behaviour they will call. 5b-1 deploys after T40. The
second-review gate (CLAUDE.md) applies in full: this becomes the only path that mutates the host
for typed actions.

## 1. Engine (`planet_express/execution/engine.py`, new)

`RunbookEngine.run(execution_id, runbook, *, origin, is_abort_requested) -> EngineResult` executes
an already-approved, already-validated runbook. The caller (CommandService) holds the mutation lock
for the whole call, as today. Per the design:

- `create_steps` from the runbook; then **one** `reserve_runbook_attempts` for
  `mutating_pairs(runbook)` (limits are enforced only for non-operator origins, via
  `policy.limit_lookback_seconds` / cooldown / `max_attempts_per_day`, exactly as T24 does; operator
  origins still record reservations). A refusal fails the execution before any step runs.
  **T24 continuity:** for `service.restart` pairs also count pre-v5 history from the existing
  approvals/executions join (`Store.recent_attempts("docker.restart_service", key, …)`), so the
  upgrade does not reset an incident target's cooldown.
- For each step `n` in order:
  1. If `is_abort_requested()` → mark this and all later steps `aborted`, release their attempts,
     execution `aborted`. (Abort is only honoured between steps and inside `wait`.)
  2. **Re-resolve the binding** (per step type; reuse `actions.resolve_target`,
     `actions.resolve_stack_target`, compose file sha, container name **and id**). Any drift → step
     `failed` with effect `not_applied` and a precise reason; stop.
  3. Resolve references (`from_step`/`output`) from the store, re-validate, substitute (§4.3).
  4. Record `pre_state` (what the conditional inverse needs: e.g. running/stopped for
     start/stop/unit start-stop) **before** dispatch.
  5. `consume_attempt` (mutating steps) → `mark_step_dispatched` → run the argv through
     `bender.run_argv_bounded` (per-type byte/time caps) → redact everything before storage or
     notification.
  6. Determine **effect** from the step's postcondition check (host state), not the exit code;
     run the step's verifier; `finish_step(passed|failed, effect, outputs, reason)`. Outputs are
     validated with `validate_outputs` before being stored.
  7. On failure: stop; release attempts of steps not dispatched; execution `failed`.
- After a failure or abort: call an injected `on_failure(context)` hook with redacted, bounded step
  outputs, bindings, verifier evidence (CommandService wires it to Amy's existing investigation
  path for runbooks that touched a container; best-effort, never raises into the engine).
- Execution statuses: `running` while steps run (`verifying` while a step's verifier runs, so the
  existing UI keeps working), then `passed | failed | aborted`.

## 2. Step executors (one module per family is fine) — the 5b-1 catalogue

`service.restart` (today's restart + `verify_after_restart`, byte-for-byte same behaviour),
`service.start`, `service.stop`, `stack.up` (with `services` outputs: project/service/container
name/id for every approved service; fail on missing/duplicate), `stack.down`, `stack.up_all`,
`stack.down_all`, `stack.down_ingress` (T37 behaviour and verifiers), `wait` (1-300s, polls abort
every ≤1s), `check.container`, `check.log_since_start` (fixed string, `--since` the bound
container's `StartedAt`, bounded), `read.qbittorrent_session_port` (`docker exec <bound container>`
reading **only** the `Session\Port=` line of the qBittorrent config — argv, no shell; the port must
parse as 1-65535), `unit.action` (only if `bender._check_sudo_allowlist` would grant exactly
`sudo systemctl <action> <unit>`; argv `["sudo","-n","systemctl",action,unit]`; verify with
`systemctl is-active`), `prune.safe` (the fixed `SAFE_PRUNE_STEPS` list converted to argv — no
`_run_command`).

## 3. CommandService re-routing (behaviour unchanged for users)

- `propose`, `request_action`, `propose_incident` and the stack actions build **one-step
  runbooks** (restart → `service.restart`; `compose.up_stack` → `stack.up`; `compose.down_stack` →
  `stack.down`; `compose.up_all` → `stack.up_all`; `compose.down_all` → `stack.down_all`;
  `compose.down_ingress` → `stack.down_ingress`) with proposal-time bindings, validate them,
  hash them, and store them via `propose_runbook` / `create_direct_runbook_execution` with a
  **server-assigned** origin (the existing `requested_via` value). `approvals.action` stays the
  legacy action name so cards, dedup, incident links and dashboard titles are unchanged.
- `Store.propose` raises `PendingPlanConflict(approval_id)` when a pending approval for the same
  (action, target) carries a different plan (its target drifted since it was proposed): catch it
  in every propose path and answer with the existing "already awaiting approval" result for that
  approval id — never let it escape to Telegram/RPC. (T38 own-review fix.)
- `validate_outputs` returns plain JSON values (T38 own-review fix); store them as returned.
- `policy.decide_runbook` replaces `policy.decide` for these paths; the existing
  `direct_request_risks`, forbidden-risk, D23/D31 and T24 semantics must produce **the same
  outcomes** for every existing test (keep them green; do not rewrite them).
- `decide()` / execution start: load the plan with `load_stored_plan(plan_json, plan_sha256)`;
  **refuse** (durably, with an event and card update) any approval without a verified plan hash —
  after T38's cutover no such pending approval can legitimately exist.
- `_run_execution` delegates to the engine; `_finish` / notifications / card texts stay exactly as
  today for these actions (the engine result carries what they need).
- `get_status` adds `steps` (n, type, human label, status, effect, reason, started/finished) —
  `summary`, `action`, `target` unchanged.
- **Startup reconciliation** (`reconcile_on_startup`): for every `dispatched` step, run its
  postcondition check to set effect `applied | not_applied | unknown`; then
  `reconcile_reserved_attempts()`; then the existing interrupted-execution handling and messages.

## 4. Dashboard (read-only in T39)
`execution.js` renders one rail item per `steps` entry (label + status + effect when not
`applied`), falling back to today's two-item rail when `steps` is absent. No new controls (T40).

## 5. Tests
- Engine with fake runners/resolvers: happy path; drift before a step (compose sha changed,
  container id replaced) → not run; reference substitution and a missing/invalid output; abort
  between steps and during `wait`; failure stops and releases later attempts; outputs validated;
  redaction of stored output; `on_failure` called once with bounded context and never raises.
- Attempts: one reservation per runbook; operator vs non-operator limits; pre-v5 restart history
  counted for `service.restart`.
- Executors: argv shapes (no shell anywhere), `unit.action` refused outside the sudo allowlist,
  `read.qbittorrent_session_port` never reads more than the one line, `prune.safe` argv list.
- Re-routing: every existing command-service, farnsworth-restart, incident-action, local-RPC and
  scruffy test stays green untouched; new tests assert stored `plan_json`/`plan_sha256`/`origin`,
  a tampered plan is refused, and a plan-less approval is refused.
- Startup reconciliation of a `dispatched` step (applied / not applied / unknown) and reserved
  attempts.

## 6. VM rehearsal helper (write it; the coordinator runs it)
`tests/homelab/t39-engine.py`: run as `casaroot` **with casa-planetexpress stopped**. Builds and
stores real runbooks against the fixture stacks through the real CommandService/engine (real
Docker, real store), e.g. `[check.container healthy/web running, service.stop slow-start/app,
wait 3, service.start slow-start/app, check.container slow-start/app healthy]`, a drift case
(edit a fixture compose file between proposal and execution), and an abort-during-wait case
(abort flag set from a timer). Prints each execution's steps/effects. Flags: `--kill-after-dispatch
<n>` to `os._exit` right after step n is dispatched (to rehearse startup reconciliation when the
core starts next).

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (1288 pass today; sandbox AF_UNIX failures
  reported, never skipped or faked); `.venv/bin/ruff check .`; `node --check static/execution.js`;
  `git diff --check`.
- Report every file changed and every deviation from this brief or the design, with the reason.

## Must NOT change
Legacy LLM plan execution (`bender.execute`, `_run_command`, `_safety_check`), Zoidberg,
`/install`/diff flow, auth, chat, config editing, the schema (v5 as T38 left it — if a change seems
necessary, stop and report).
