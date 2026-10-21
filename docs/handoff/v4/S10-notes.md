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
