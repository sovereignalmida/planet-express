# Task T44 — slice 5b-5: delete the legacy execution path; Planet Express 2.0

Branch `v2`. Spec: `docs/designs/slice-5b-multistep-execution.md` §5 and §7 (landing 5b-5), decision
D36 (the switch is removed here). Builds on 5b-4, live as `v2.0.0-11`.

## Why
Every mutation now goes through the engine as a typed runbook. What is left of the old path is dead
weight that still *works* — `_run_command` with `shell=True`, guarded by `_safety_check`'s pattern
list. That is the "unenforced boundary" CLAUDE.md warns about, and this landing is what lets that
paragraph be rewritten. **The slice is not the deletion; the deletion is what makes the claim
true.**

## What comes out

1. **`casa_bender.py`** — `_run_command`, `_safety_check`, `_step_succeeded`, `FORBIDDEN_COMMANDS`,
   `execute`, `execute_rollback`, `run_safe_prune`, the whole pending-diffs family
   (`_load/_save_pending_diffs`, `propose_compose_diff`, `get_pending_diff`,
   `discard_pending_diff`, `apply_pending_diff`, `_apply_pending_diff_locked`,
   `PENDING_DIFFS_FILE`, `_PENDING_DIFFS_LOCK`), and the plan-executing CLI entry point.
   **Stays:** `run_argv`, `run_argv_bounded`, `_check_sudo_allowlist` and its helpers,
   `SafetyError`/`SudoScopeError`, `core_service_active`, `_log_step`, `read_service_block`,
   the read-only diagnostic allowlist, and `SAFE_PRUNE_STEPS` — **as argv lists**, not strings.
2. **`run_diagnostic`** loses its `_safety_check` call. Its guard becomes the read-only allowlist
   (an allowlist, strictly stronger than the pattern blocklist it replaces) plus
   `_check_sudo_allowlist`. No behaviour change for anything that was previously allowed.
3. **`casa_farnsworth.py`** — `plan()`, `save_plans()`, `load_pending_plan()`, `_execute_plan`,
   `_do_rollback`, `_investigate_failure` (the typed twin stays), the `plan` and `diff` approval
   callbacks, `/skip`'s legacy branch, and `PipelineState.AWAITING_APPROVAL` if nothing else uses
   it. `_process_fry_resolution` keeps only its typed branch.
4. **`config_schema.legacy_plans_enabled`, `config.LEGACY_PLANS_ENABLED`, `config.STATE_PLAN`**,
   `state_models.PlanSet`/`PlanStep`, `dashboard_data.summarize_pending_plan` and the template's
   pending-plan panel, `state/pending_plan.json`, `state/pending_diffs.json`,
   `state/rollback_candidates.json` and its loader.
5. **`CLAUDE.md`** — the "unenforced boundary" paragraph is rewritten to describe what is actually
   true afterwards: every mutation is a typed step, argv only, with its own binding, verifier and
   inverse; the second-review gate stays, and what it is now most load-bearing on is the step
   catalogue, the policy/limit rules and `compose.write`'s compare-and-swap.

## What must keep working
`/up`, `/down`, `/restart`, `/abort`, `/rollback <execution>`, `/patchnow`, `/install`, chat,
diagnostics, the dashboard (including its execution screen and its config editor), the weekly canary
window, safe prune, incident proposals, and startup reconciliation. Any live row written by the old
path is read-only history: the store keeps it, nothing re-animates it.

## Migration on the live host
A pending **legacy** plan or diff at upgrade time can no longer be approved — the card's callback
is gone. Core says so once at startup for each, and supersedes the card, rather than leaving a
button that does nothing. `state/pending_plan.json` on the live host holds one such plan (`p1`).

## Tests
The deleted names are gone (a test that greps for them keeps them gone); every surviving flow keeps
its existing tests; the startup message for an orphaned legacy card; the diagnostic allowlist still
refuses what it refused before, including sudo scope.

## VM rehearsal
The full T37/T39/T40/T41/T42/T43 rehearsal scripts, in sequence, on one guest: typed actions, the
engine, abort/rollback, the planner, the canary, and compose writes — plus a start with a legacy
`pending_plan.json` in place to see the orphaned-card message.

## Out of scope
The live soak. 5b-5 does not deploy until 5b-3 has been through a weekly canary window (Saturday
05:00), per the slice's own exit condition.
