# S4 — Mission and memory operator surfaces (v4 slice 4)

Read first: `docs/designs/v4-slice-0-contracts.md` §3, §6, §13; `docs/designs/v4-mission-control.md` §4, §8;
`planet_express/integrations/rpc.py` (handler table, auth, how the dashboard calls core), `casa_scruffy.py` +
`templates/` + `static/` (existing dashboard patterns and auth), `casa_farnsworth.py` `handle_message()` (Telegram
command pattern; touch minimally), the merged `planet_express/missions/` and `planet_express/memory/`.
Run `git merge --ff-only v4-next` first; do not push; Co-Authored-By trailer.

## Deliver (OPERATOR surfaces only; no worker-facing code, no mutation capability)
1. Operator RPC methods on the EXISTING socket (uid allowlist unchanged): `mission.list`, `mission.get`,
   `mission.events` (paged), `mission.create` (Observe/Propose only; origin operator/incident; refuses tiers
   above propose), `mission.cancel` (sets `cancel_requested`), `memory.candidates`, `memory.confirm`,
   `memory.deprecate`, `memory.search`. All return redacted, bounded data. `memory.confirm/deprecate` act as
   an `operator:<id>` actor taken from the authenticated dashboard/Telegram identity (never from params).
   Gated by `missions.enabled`; when off they return a clear "disabled" error.
2. Dashboard: a Missions page (board by status, mission detail with event stream, artifacts, result) and a
   Memory page (candidates queue with confirm/deprecate, confirmed cards, review-due). Follow existing
   templates/CSS/auth/CSRF patterns and the repo's dashboard tests; mobile-width friendly.
3. Telegram: `/missions` (list), `/mission <id>` (summary), `/cancel_mission <id>`; reuse existing
   authorisation of the operator chat. Keep changes in `casa_farnsworth.py` to a small dispatch hook plus a
   new module `planet_express/application/mission_service.py` holding the logic.
4. `incident → mission`: a "Investigate" action on an incident creates a Propose mission seeded with that
   incident's evidence.
5. NOT in scope: approvals of Repair+, attach/takeover, the worker socket, anything touching `policy.py`/
   `engine.py`/the mutation lock.

## Acceptance tests (first)
- Each RPC: happy path, disabled flag, bad params, tier above propose refused, pagination bounds, output
  redaction (planted secret absent), actor derived from the auth identity not params.
- `memory.confirm` of a procedure/constraint works for an operator actor and is refused for any other;
  candidates never appear in `memory.search` results.
- Dashboard pages render with fixtures, require auth, are CSRF-protected on POSTs, and show disabled state.
- Telegram commands authorised only for the configured chat; unknown id handled.
- Full suite green; no change in behaviour with missions disabled.
