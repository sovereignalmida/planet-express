# S1 — Mission store (v4 slice 1)

Branch: worktree off `v4-next`. Python 3.11+, **no new dependencies**. Read first: `docs/designs/v4-slice-0-contracts.md`
§1-§5, §2.1-§2.3, §3, §4 (rev 0.4); `planet_express/core/store.py` (how v4→v5 migrates, `SCHEMA_VERSION`,
`_write`, `BEGIN IMMEDIATE` patterns); `tests/` store tests for style. Missions are OFF by default.

## Deliver
1. `planet_express/missions/models.py`: strict Pydantic models (`extra="forbid"`, bounded strings/lists,
   `Literal` enums): `Grant` (§4), `VerificationPlan` (§10 header), `Mission`, `MissionEvent`, `Lease`,
   `StandingGrant`, `Approval`. **No floats in any hashed model** (ints and strings only) so canonical
   JSON is unambiguous.
2. `planet_express/missions/canonical.py`: RFC 8785-style canonical JSON for the types you allow
   (sorted keys, no whitespace, UTF-8, reject duplicate keys, NaN/Inf, floats), `sha256_hex()`.
3. `planet_express/missions/schema.py`: `DDL_V6` string with every table in contract §2 (missions,
   mission_events, mission_leases, resource_fences, standing_grants, mission_approvals, mission_sessions
   incl. the partial unique index, mission_artifacts). S2 supplies its own DDL separately; the
   orchestrator merges both into the v6 migration.
4. v5→v6 migration in `core/store.py` using `DDL_V6`, **plus the upgrade guard**: refuse to migrate
   unless a snapshot marker younger than 10 minutes exists (path in config/state dir; `deploy`/`setup`
   writing it is out of scope, but add a documented helper `write_snapshot_marker()`). Keep the existing
   refusal of newer-than-code schemas. `SCHEMA_VERSION = 6`.
5. `planet_express/missions/state.py`: transition table from contract §3 (all states incl.
   `paused_tripwire`, `rollback_pending`, `recovery_required`, `blocked` exits), `transition(mission,
   to, actor)` raising on illegal edges, writing a `state` event in the same transaction.
6. `planet_express/missions/leases.py`: resource ids and containment (§2.3), `acquire(resources,
   mission, ttl)` expanding to the containment set, all-or-none in one `BEGIN IMMEDIATE`, fence from
   `resource_fences` upsert (never reused), `heartbeat`, `release`, `mark_suspect` (expiry does NOT free
   a lease; only `release_after_proven_dead` does).
7. `planet_express/missions/approvals.py`: `record_approval`, `execution_predicate(mission)` exactly as
   contract §3 (grant-or-standing, plan, every critical:<i>, broad_sudo/no_backup elevated); a denial row
   never satisfies it; a changed grant hash invalidates old rows.
8. `planet_express/missions/store.py`: CRUD, `append_event` (applies `core.redact.redact()` BEFORE insert),
   `start_session(mission)` as ONE transaction (status CAS, approval predicate, lease acquire, session
   insert) — second live session must fail via the partial unique index.
9. Config: `missions.enabled` (default false) in `config_schema.py`; nothing in the existing daemon
   behaves differently when false.

## Acceptance tests (write first; file names `tests/test_missions_*.py`)
- Every legal edge passes, every illegal edge raises and logs; observe/propose never reach `executing`.
- v5→v6 from a real v5 DB fixture: idempotent, guard refuses without snapshot marker, newer schema refused.
- Canonical hash: key order and whitespace independent; floats, NaN, duplicate keys rejected; stable vectors.
- Events: planted secret (use a key shape `redact.py` catches) never present in the table; events are
  never updated or deleted by the module (assert API surface).
- Leases: stack vs service vs mount vs path overlap matrix; concurrent acquire from two threads, exactly
  one wins; fence strictly increasing across release/reacquire; TTL expiry marks suspect and does NOT free;
  stale fence rejected by `check_fence`.
- Approvals: approve row satisfies, deny row does not, wrong phase/hash does not, non-elevated `broad_sudo`
  does not, standing grant path refuses broad_sudo/no_backup; grant change invalidates.
- `start_session`: two concurrent starts → one session; missing approval → refused with no lease left behind.
- Full existing suite stays green with `missions.enabled` false (run it once at the end).

## Constraints
- Touch `core/store.py`, `config_schema.py` and new `planet_express/missions/` only. Do not edit
  `casa_farnsworth.py`, `rpc.py`, `policy.py`.
- No shell, no network. Match surrounding style (comment density, naming).
- Report: files changed, tests added + results (targeted and full), deviations from the contract and why,
  open questions. Stop and report if the contract is ambiguous or contradicts the code; do not widen scope.
