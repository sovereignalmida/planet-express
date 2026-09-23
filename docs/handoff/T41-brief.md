# Task T41 — slice 5b-2: the planner emits typed runbooks; legacy plans off by default

Branch `v2`. Spec: `docs/designs/slice-5b-multistep-execution.md` §5 and §7 (landing 5b-2), decisions
D36 (switch-guarded cutover) and D37 (no typed remediation → diagnosis only). Builds on 5b-1
(`runbook.py`, `engine.py`, `binding.py`, `CommandService`, schema v5), which is live as `v2.0.0-8`.

## Why
Farnsworth's planner still writes **shell strings** executed by `bender.execute` (`shell=True`),
guarded only by `_safety_check`'s pattern list — the last unenforced safety boundary. After this
landing the planner may only choose typed steps from the catalogue, and a plan it cannot express is
reported as a diagnosis instead of being sent for approval.

## Required behaviour

1. **`planet_express/application/planner.py` (new)**
   - `PLAN_RUNBOOK_PROMPT`: the model returns `{"plans": [{id, priority, title, finding_ids,
     container, steps: [{type, params}] | {"recipe": name, "params": {...}}}]}` — **intent only**:
     no bindings, no risk, no rollback, no shell.
   - `RECIPES`: code-owned step lists. First: `vpn.resync_port_forward(mode)` with
     `mode ∈ {dead_forward, sync_mismatch}` — gluetun → wait 20 → GSP → qBit (dead_forward) or
     GSP → qBit (sync_mismatch), then `read.qbittorrent_session_port` on qBit and
     `check.log_since_start` on GSP for `New : <port>` via a typed reference. Container→(stack,
     service) identities come from `actions.container_compose_identities`, never from the model.
   - `build_runbook(intent, *, binder, resolve_target, resolve_identities)` → `runbooks.Runbook`
     with server-built bindings, or raises `PlanRefused(reason)`. Every param is validated by the
     step model; unknown types, unknown recipes and unresolvable targets are refusals.
2. **`CommandService.propose_plan(runbook, *, requested_by, finding_ids)`** — origin `planner`
   (server-assigned), `action="runbook"`, `target_key` = the plan hash (so an identical plan dedups
   to one card), risk computed, T24 limits applied per mutating pair for this non-operator origin,
   stored via `propose_runbook`, card text listing every step in plain words. Approval, execution,
   abort and rollback then use the existing 5b-1 paths unchanged.
3. **`config_schema`**: `legacy_plans_enabled: bool = False` (D36). `run_pipeline` uses the typed
   path by default; with the switch on it keeps today's `plan()`/`save_plans()`/plan-card path
   exactly as it is. The switch is removed in 5b-5.
4. **Refusals are recorded, never silent**: every refused plan writes a `planner.refused` event
   (reason, step type, title — redacted, bounded) and Telegram gets the finding summary plus
   "no typed remediation — needs a human" (D37). `pending_plan.json` is not written by the typed path.

## Tests
Recipe shape and identity resolution; refusal for unknown step type/recipe/params and unresolvable
target; bindings built server-side (model params never reach the binding); propose_plan dedups by
plan hash, applies T24 per pair, stores plan+origin; run_pipeline typed vs legacy switch; refusal
event recorded and the operator told; existing planner/pipeline tests stay green.

## Verify
`CASA_CONFIG=config.example.yaml .venv/bin/pytest -q`; `.venv/bin/ruff check .`; Codex gate; VM
rehearsal with a fixture finding that produces a typed plan and one that must be refused.

## Must NOT change
`bender.execute`/`_run_command`/`_safety_check` (5b-5 deletes them), Zoidberg, `/install`, the 5b-1
engine contract, schema v5.
