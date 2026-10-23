# S10 notes (carried items for slice 10)

## `mission_approved` is a bare bool, not scoped to a step (carried from the v4 security audit)

`approvals.mission_step_approved(store, mission_id)` (and the policy's `mission_approved` input built from it) answers one
question: "does this mission currently have an approving row bound to its current grant hash". It says nothing about WHICH
step is being run, what it targets, or whether the mission is in a state that may act.

Today nothing calls it for a typed step: the `mission` origin is in `RUNBOOK_ORIGINS` but no code path submits typed steps
from a mission yet. When slice 10 wires typed steps from the `mission` origin, the caller MUST, before the step is accepted:

1. re-check the step's target (unit, container, compose file, path, ...) against the grant's scope AND the protected set
   (`resources.classify`; protected wins; a target that does not resolve to a canonical resource id is refused);
2. require the mission to be `executing` (not `paused_tripwire`, `verifying`, `rollback_pending`, ...), with a live,
   non-revoked session, and the session's fences current;
3. bind the approval to the step type (a grant that approves "restart" must not approve "compose.write" or any Change step;
   Change and Critical keep their own per-step approvals);
4. record the check and its inputs in the mission's events before the step runs.

This is execution-safety code: it gets the second review (Codex plus Claude) before it is treated as done, and tests for each
of the four points (a step outside scope, a protected target, a non-executing mission, an approval of another step type).

## Built (2026-10-23)

`planet_express/missions/typed_steps.py` (`MissionStepGate`, `StepAuthorization`, `StepRefused`) and
`CommandService.run_mission_runbook`. All four items above are enforced and tested (`tests/test_mission_typed_steps.py`):

1. every target resolves to a canonical resource id, is inside the grant's scope, is not protected (site set, grant protected set
   and enforced exclusions; protected wins) and is covered by a lease this mission holds. A binding's container is checked against
   the daemon's Compose labels (`actions.container_compose_identities`): it must be the project/service the step names, so a
   binding cannot point the engine at a protected container. A mutating step with no target is refused, in the gate and again in
   `policy.decide_runbook` for the `mission` origin (closes G4-review row 9);
2. the mission is `executing` with no cancel pending; its session is the live, unexpired, non-revoked one; every fence is current;
3. only `MISSION_STEP_TYPES` (restart/start/stop, stack up/down, and the R0 checks and reads), at most R2, only from a Repair
   mission, only with the mission's approval row for its current grant hash, within `budgets.tool_calls` authorized steps. Left
   out on purpose: `unit.action` and `stack.down_ingress` (R3), `stack.up_all`/`down_all`/`prune.safe` (no target),
   `update.canary`, `compose.write`, `compose.restore`;
4. `step.authorized` / `step.refused` / `step.ran` are recorded in the mission's events (actor `mission-steps`); the authorized
   record is written before the step runs and, if it cannot be written, the step is refused. An oversize record keeps the plan
   hash, grant hash, step types and resources and drops only the step detail.

`run_mission_runbook` takes the host-mutation lock FIRST, asks the gate while holding it, requires a `MissionStepGate` and an
authorization minted by it for exactly that runbook hash, recomputes the approval itself from the store (the gate's say-so is not
trusted), applies `decide_runbook(origin="mission")` and T24's limits, then runs through the engine with an abort hook: between
steps the engine asks `gate.still_current`, so a cancel, a pause, a revoked or expired session stops what is left. Execution is
shared with `run_automatic` via `_run_decided`.

Known limits: T24's cooldown and daily cap are shared with every other origin (a mission's restart of a service puts that pair in
cooldown for Zoidberg too); a service whose Compose labels the daemon cannot report is refused rather than guessed; the seal on
`StepAuthorization` guards against accidents inside the process, not against hostile code in it.

**Not wired:** nothing exposes this to the worker yet. Reaching it from an agent session means adding a worker RPC method, which
changes the per-principal allowlist (the worker must never reach approval/decision methods) and needs its own decision and second
review. Today the only caller is Core.
