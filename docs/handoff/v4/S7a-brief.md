# S7a — Approval flow, daemon wiring, hooks (v4 slice 7, part A) — SAFETY SURFACE (G1 + G3, reviewer not Claude)

Read first: `docs/designs/v4-slice-0-contracts.md` §3, §4, §5, §6, §11, §12, §16; `docs/designs/v4-mission-control.md` §5,
§5.2, §6.4, §6.7; `docs/designs/v4-plan.md` slice 7 (incl. the carried S5 items); `planet_express/execution/policy.py`,
`planet_express/missions/{approvals,supervisor,runner,worker_rpc,tools,state,store}.py`, `planet_express/missions/
tripwire/`, `planet_express/missions/verify/`, `planet_express/missions/checkpoint/`, `planet_express/application/
mission_service.py`, `planet_express/integrations/rpc.py`, `casa_farnsworth.py` (daemon wiring and `handle_message`).
`git merge --ff-only v4-next` first; do not push; Co-Authored-By trailer. Missions stay OFF by default.

## Goal
Make Repair-tier missions runnable end to end under approval, on the pieces already built: slices 5, 6 and 8 left
interfaces and unconstructed objects. S7a constructs and wires them in the daemon, adds the approval flow and
standing grants, adds the `mission` origin to policy, and adds the hook layer. The Docker API proxy and the generated
sudo scope are S7b; until S7b lands a mutating-tier start still refuses (the §6.8 gate fails closed).

## Deliver
1. **Daemon wiring** (behind `missions.enabled`): construct `CheckpointService` (root default `<state>/checkpoints`,
   backup freshness from config), the tripwire monitor (call `monitor.begin(mission_id)` before launch, `recover()` at
   start), the `Verifier` (baseline provider = a real normalizer over `casa_leela.run_full`), the repair gate, and pass
   them to the Supervisor. The supervisor: creates the checkpoint before the first mutation (refusing without a
   baseline/backup per S5), calls `capture_post()` once the session is provably dead, runs `verify_and_close` when a
   session ends, and on crash recovery calls `recovery.collect_host_state/classify/apply_classification`
   (the `recover()` loop no longer skips mutating tiers; it must not replay). `missions.tripwire_accept_blind` plumbed.
2. **Approval flow**: operator RPC + dashboard + Telegram to approve/deny a mission's grant (bound to `grant_hash`
   via `mission_approvals`), with elevated approval for `sudo.mode=broad`/`docker=raw`/`acknowledged_no_backup`/Critical.
   The approval card shows scope, tier, protected exclusions, budgets, verification plan, sudo scope, backup status at
   the TOP when broad/no-backup. The ACTOR is derived from the authenticated dashboard/Telegram identity, never from
   params (carried item: `CheckpointService.restore(ack_drift=True)` actor). Operator actions for tripwire cards:
   resume (reopen broker via `broker.restore`, thaw cgroup), roll back (`monitor.roll_back`), take over, cancel.
3. **Standing grants** (scoped, expiring, listed, revocable, versioned; incident-backed only; never Change/Critical,
   broad sudo or `acknowledged_no_backup`): create/list/revoke RPC + UI; unattended start decided solely by
   `missions/` code (`approvals.evaluate`), recorded with `standing_grant_id`; expiry and revoke stop an unattended
   start; window rules (`window.deny`, e.g. backup window) enforced with an injected clock; `window_json` semantics
   defined and tested (deferred item from slice 1).
4. **Policy**: add `mission` to `RUNBOOK_ORIGINS` in `policy.py`; never in the unattended set, never in
   `LIMIT_EXEMPT_ORIGINS`; a mutating typed step from it needs a `mission_approvals` row for the mission's current grant
   hash. The §5 tests exactly as the contract lists them.
5. **Hook layer** (reference adapter, contracts §12): pre/post tool hooks call `mission.post_event` and may block.
   Blocked classes enforced even with an unrestricted runtime: reading secret stores and `.env` contents; destructive
   VCS in live trees; publishing; broad `rm`/`mkfs`/`dd`-class commands; `docker volume rm`, `prune -a`; edits to
   sudoers/daemon.json. Ask classes: crossing into a protected resource, irreversible-looking operations. Failure mode
   configurable (default fail closed for blocked, fail open + loud event otherwise); both behaviours tested. Hooks are
   a speed bump, not the boundary: tests assert safety holds when the hook is absent (tripwires/credential scoping).
6. **Worker boundary tests** (explicit): the worker socket's method allowlist never contains approval/decision/restore/
   checkpoint/standing-grant/config methods; a worker uid calling them gets "unknown method"; `rpc.sock` refuses the
   worker uid. `mission.request_decision` creates a card but cannot approve.

## Acceptance tests (write first)
- Approval binds to the hash: edit any grant field after approval => not approved; two missions cannot share one
  approval; stale approval after scope change; elevated approvals required where listed; forged actor in params ignored.
- Standing grants: in-scope incident-backed mission starts unattended; out-of-scope, expired, revoked, window-denied,
  Change/Critical, broad-sudo, no-backup each refuse and raise a decision card instead; versions and expiry honoured.
- Policy: `mission` origin accepted for typed steps only with an approval row for the current hash; never unattended; never
  limit-exempt; other origins unchanged.
- Hooks: each blocked class blocked; ask classes asked; fail-closed/fail-open behaviours; a hostile transcript cannot
  disable hooks; removal of hooks leaves tripwire/credential protections intact.
- End-to-end with fakes: approved Repair mission -> checkpoint -> session (fake) -> tripwire quiet -> session ends ->
  capture_post -> verify passes -> completed; verify fails -> needs_operator/rollback_pending; tripwire pause ->
  card -> rollback restores; crash at each phase -> classified, no replay.
- Worker allowlist/identity tests from item 6. Missions disabled => nothing constructed. Full suite green.

## Report
Files changed, test counts, deviations (each), open questions.
