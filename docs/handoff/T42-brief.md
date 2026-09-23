# Task T42 — slice 5b-3: `update.canary` on the engine; Zoidberg and safe prune routed through it

Branch `v2`. Spec: `docs/designs/slice-5b-multistep-execution.md` §4.5 (two-phase canary), §5
(flow mapping), §6 (rollback candidates) and §7 (landing 5b-3), decisions D34 (canary stays
automatic) and D38 (safe prune stays automatic). Builds on 5b-2, live as `v2.0.0-9`.

## Why
`casa_zoidberg.py` still updates containers with `bender._run_command` (`shell=True`, guarded by
`_safety_check`), keeps its rollback window in `state/rollback_candidates.json`, and decides on its
own when to roll back. Safe prune reads that same JSON file and **fails open**: a malformed file is
caught and treated as "no open window", so a prune can delete the image a rollback needs. After this
landing both run as typed runbooks on the engine, with the rollback window in the database.

## Required behaviour

1. **`update.canary` step type** (`runbook.py`)
   - Params: `{stack, service}`; binding: `ServiceBinding`; risk **R2**; rollback `automatic`
     (the inverse is the step's own, not an operator control); target `service`.
   - Outputs: `{old_image_id, new_image_id, image_reference}` so the execution row and the card can
     say what moved.
2. **Eligibility, checked before the step is built** — the service's Compose image must be a
   canonical mutable `name:tag`. `repo@sha256:…` and build-only services are **ineligible**, and the
   refusal is recorded (`canary.ineligible`), never silently skipped.
3. **Phase 1 (pull)** — persist `pre_state` = `{compose_sha256, image_reference, running_image_id,
   reference_image_id}` and open a **rollback-candidate row** (`Store.open_rollback_candidate`),
   then mark the sub-state `pull_dispatched` and run `compose pull <svc>` (argv). Persist the newly
   resolved image id immediately after. Unchanged image → the step passes as `not_applied` and the
   candidate row closes.
4. **Phase 2 (deploy)** — refuse if the compose sha or the image reference drifted since phase 1.
   Tag the new image id to the exact reference, `compose up -d --pull never <svc>`, inspect the
   container and require its image id to equal the new one, then the canary watch as today
   (`CANARY_WATCH_SECONDS`).
5. **Automatic inverse on failure** — retag the recorded **old** image id to the same reference,
   `up -d --pull never`, verify the container's image id equals the old one. Outcome recorded on the
   step: `rolled_back` (effect `not_applied`) or `rollback_failed` (effect `unknown`). The candidate
   row closes either way; a failed inverse leaves it open.
6. **Crash recovery** — a step left at `pull_dispatched` is reconciled by inspecting both the running
   container and what the reference now points to: running == old → `not_applied`; running == new →
   `unknown` (the watch never finished, so it is not claimed as passed).
7. **Zoidberg routed through the engine** — `run_update_pass` builds one `update.canary` runbook per
   eligible service, origin `zoidberg`, run automatically under D34 with T24 limits per service
   (`CommandService.run_automatic(runbook, origin=...)`, a direct runbook execution, no card). The
   weekly window, `/patchnow`, the inter-service delay, the update history log and the Telegram
   summary keep today's shape.
8. **Safe prune routed through the engine** — `maybe_run_safe_prune` builds a `prune.safe` runbook,
   origin `system`, automatic under D38, with today's gates unchanged **plus** the rollback-candidate
   gate read from the store: an open row *or an unreadable table* refuses the prune (fail closed —
   `Store.any_open_rollback_candidate` deliberately does not swallow errors).
9. **`state/rollback_candidates.json` is no longer the source of truth.** Rows in the store are.
   Any file left on disk is ignored by the new path (the file itself goes in 5b-5 with the rest).

## Tests
Eligibility (tag, digest-pinned, build-only); phase 1 no-change; phase 2 drift refusal; the happy
path's image-id verification; the automatic inverse on a failed watch and on a failed `up`;
`rollback_failed`; crash reconciliation from `pull_dispatched`; prune refused while a candidate is
open and when the table cannot be read; Zoidberg's pass building one runbook per eligible service
and honouring T24; the weekly window and `/patchnow` unchanged.

## VM rehearsal
A local registry serving a **good** tag and a **crashing** tag (spec §7): update to the good tag
(passes, candidate closed, image id verified), update to the crashing tag (watch fails, automatic
inverse restores the old image, container healthy again), a prune attempted while a candidate is
open (refused), and a kill during phase 1 reconciled on the next core start.

## Out of scope
`compose.write` (5b-4). Deleting `_run_command`, `shell=True`, `_safety_check`, the legacy switch
and `rollback_candidates.json` (5b-5).
