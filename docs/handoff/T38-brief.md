# Task T38 — slice 5b-1a: schema v5, runbook model, runbook policy (no engine yet)

Branch `v2`. Python 3.11+, no new dependencies. **Read `docs/designs/slice-5b-multistep-execution.md`
(ACCEPTED, revision 3.1) in full first** — it is the spec; this brief scopes the first half of its
landing 5b-1. The second half (T39, 5b-1b) is the engine, re-routing existing actions, and the
dashboard controls. T38 is merged to `v2` but **not deployed on its own**; it ships with T39.

Nothing in the running system may call the new code yet except `Store.init()`'s migration and the
startup cutover wiring in §1. `CommandService`, Farnsworth's handlers, Zoidberg and Scruffy keep their
current behaviour. The second-review gate (CLAUDE.md) applies: this is the schema and policy that the
execution boundary will stand on.

## 1. Schema v5 and the migration (`planet_express/core/store.py`) — design §6

- Replace "re-run `CREATE … IF NOT EXISTS` and stamp the version" with an explicit, ordered migration
  step keyed on the current `user_version`:
  - a **fresh** database (no tables) gets the full v5 schema directly;
  - a **v4** database runs `_migrate_4_to_5`; versions below 4 run the existing idempotent create
    path first (keep today's behaviour for 0-3), then 4→5;
  - a database **newer than v5** is still refused by the existing preflight (`SchemaTooNewError`).
- `_migrate_4_to_5` in **one** `BEGIN IMMEDIATE` transaction:
  1. **Live-row cutover:** every `running`/`verifying` execution → `interrupted` (reason "interrupted
     by the v5 upgrade"), with an `execution.interrupted` event; every `pending` approval →
     `expired` with an `approval.expired` event (reason "superseded by the v5 upgrade; propose it
     again"). Collect both lists (ids, action, target, message_id) to return.
  2. Rebuild `executions` as `executions_new` with the v5 DDL (below), copy every row
     (`kind='run'`, `parent_execution_id` NULL), validate the copied count, drop the old table,
     rename, recreate any index that referenced it.
  3. `ALTER TABLE approvals ADD COLUMN plan_json TEXT`, `plan_sha256 TEXT`, `origin TEXT`
     (all nullable; pre-v5 rows stay NULL).
  4. Create `execution_steps`, `attempts`, `rollback_candidates` and the indexes below.
  5. `PRAGMA foreign_key_check` must return **no rows** (else raise → rollback). Note:
     `foreign_keys` must be OFF for the table rebuild inside the transaction and restored after;
     do it correctly (SQLite's documented 12-step table-rebuild procedure).
  6. Only then `PRAGMA user_version = 5`; `COMMIT`. Any exception → `ROLLBACK`, the database is left
     exactly at v4, and the error propagates (core refuses to start; the T23 snapshot is recovery).
- `Store.init()` returns (or exposes via a new method) the cutover lists so core startup can update
  the affected Telegram cards. Wire that in `casa_farnsworth.run_bot` next to
  `commands.reconcile_on_startup()`: for each expired approval with a `message_id`, edit its card to
  "⏻ Superseded by the Planet Express upgrade — propose it again."; for each interrupted execution,
  use the existing interrupted-execution message. Best-effort, never blocks startup, never logs
  exception text (bot token).
- v5 DDL (exactly; names matter to T39):

```sql
CREATE TABLE executions (
    id                  TEXT PRIMARY KEY,
    approval_id         TEXT NOT NULL REFERENCES approvals (id),
    kind                TEXT NOT NULL DEFAULT 'run' CHECK (kind IN ('run', 'rollback')),
    parent_execution_id TEXT REFERENCES executions (id),
    status              TEXT NOT NULL CHECK (status IN ('running', 'verifying', 'passed', 'failed',
                        'interrupted', 'aborted', 'rolled_back', 'rollback_failed')),
    started_at          REAL NOT NULL,
    finished_at         REAL,
    reason              TEXT,
    abort_requested_at  REAL,
    CHECK ((kind = 'run') = (parent_execution_id IS NULL))
);
CREATE UNIQUE INDEX executions_one_active_rollback ON executions (parent_execution_id)
    WHERE kind = 'rollback' AND status IN ('running', 'verifying');

CREATE TABLE execution_steps (
    execution_id   TEXT NOT NULL REFERENCES executions (id),
    n              INTEGER NOT NULL CHECK (n >= 1),
    type           TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    binding_json   TEXT NOT NULL,
    pre_state_json TEXT,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'passed', 'failed',
                   'skipped', 'aborted')),
    effect         TEXT CHECK (effect IN ('applied', 'not_applied', 'unknown')),
    output_json    TEXT,
    reason         TEXT,
    started_at     REAL,
    finished_at    REAL,
    PRIMARY KEY (execution_id, n)
);

CREATE TABLE attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions (id),
    step_n       INTEGER NOT NULL,
    step_type    TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('reserved', 'consumed', 'released')),
    reserved_at  REAL NOT NULL,
    settled_at   REAL,
    UNIQUE (execution_id, step_n)
);
CREATE INDEX attempts_pair_time ON attempts (step_type, target_key, reserved_at);

CREATE TABLE rollback_candidates (
    execution_id    TEXT NOT NULL REFERENCES executions (id),
    step_n          INTEGER NOT NULL,
    stack           TEXT NOT NULL,
    service         TEXT NOT NULL,
    image_reference TEXT NOT NULL,
    old_image_id    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    closed_at       REAL,
    PRIMARY KEY (execution_id, step_n)
);
```

- Existing store methods must keep working unchanged against v5 (`TERMINAL_EXECUTION_STATUSES` and
  `_EXECUTION_STATUSES` extended for the new statuses, `interrupt_unfinished`, `recent_attempts`,
  incident proposals, direct executions). `SCHEMA_VERSION = 5`.
- `scripts/state_snapshot.py` must still restore a pre-v5 snapshot (it is stdlib-only and schema
  agnostic — verify, don't assume).

## 2. Store APIs for T39 (no callers yet besides tests)

- Steps: `create_steps(execution_id, steps)` (from a validated runbook, all `pending`),
  `set_step_pre_state`, `mark_step_dispatched`, `finish_step(status, effect, output, reason)`,
  `list_steps(execution_id)`. Enforce legal transitions (`pending → dispatched → passed|failed`,
  `pending → skipped|aborted`, `dispatched → aborted` is illegal).
- Attempts (design §4.2 "Reservation is per runbook"): `reserve_runbook_attempts(execution_id,
  pairs, *, window_start, cooldown_start, max_per_day, now)` — **one** `BEGIN IMMEDIATE`: count
  `reserved`+`consumed` rows per `(step_type, target_key)` in the windows, decide for the full
  multiplicity as one decision, and either insert one `reserved` row per mutating step or insert
  nothing and return the refusal reason. `consume_attempt(execution_id, step_n)`,
  `release_attempts(execution_id, step_ns)`, and `reconcile_reserved_attempts()` for startup
  (reserved + step `dispatched` or uncertain → `consumed`; otherwise `released`).
- Rollback candidates: `open_rollback_candidate(...)`, `close_rollback_candidate(...)`,
  `any_open_rollback_candidate(now)` which **raises** on any read error (callers fail closed).
- Approvals: a variant of `propose`/`create_direct_execution` that stores `plan_json`,
  `plan_sha256`, `origin` (server-supplied argument, never read from the document). Existing
  methods unchanged.

## 3. Runbook model (`planet_express/execution/runbook.py`, new) — design §4.1-§4.3

- Pydantic models, `extra="forbid"` everywhere: `Runbook{kind="runbook", version=1, title, steps,
  artifacts}` and `Step{type, params, binding}`. **A document containing `origin` (or any unknown
  key) fails validation.**
- A `STEP_TYPES` registry in code. Each entry: params model, binding model, risk, rollback kind
  (`none` | `conditional` | `automatic`), output schema (names → pydantic types), and the set of
  param fields that accept a reference plus the output type each accepts. Register the 5b-1
  catalogue: `service.restart`, `service.start`, `service.stop`, `stack.up`, `stack.down`,
  `stack.up_all`, `stack.down_all`, `stack.down_ingress` (risks per D33/design table), `wait`
  (1-300s), `check.container`, `check.log_since_start`, `read.qbittorrent_session_port`,
  `unit.action`, `prune.safe`. Definitions only — no argv, no execution in T38.
- References `{"from_step": n, "output": name}`: legal only in declared fields, only to an earlier
  step, only to a declared output of matching type. Validate at load.
- `canonical_json(runbook)` (sorted keys, no whitespace variance, UTF-8) and `plan_sha256(runbook)`;
  loading a stored plan re-hashes and refuses a mismatch.
- `risk(runbook)` = max over steps. `mutating_pairs(runbook)` → list of `(step_type, target_key)`
  with multiplicity, R0 steps excluded.
- Artifacts: `artifacts` keys must equal the sha256 of their content; unknown/unused artifacts are
  refused.

## 4. Runbook policy (`planet_express/execution/policy.py`) — design §4.2

- `decide_runbook(runbook, origin, *, autonomy=None) -> RunbookDecision(allowed, needs_approval,
  automatic, risk, reason, pairs)`. `origin` is a server-side argument from a closed set
  (`telegram`, `telegram-direct`, `dashboard`, `dashboard-direct`, `incident`, `chat`, `planner`,
  `zoidberg`, `system`, `install`, `amy`); anything else is refused.
  - forbidden risks refuse; unknown step types refuse (already refused by the model, check again);
  - **automatic** only for `(origin == "zoidberg" and every step is update.canary)` (D34 — the type
    lands in 5b-3; until then no runbook can qualify) and `(origin == "system" and every step is
    prune.safe)` (D38). Everything else non-R0 needs approval (D23/D31 unchanged);
  - `direct_request_risks` applies to operator-direct origins exactly as `allows_direct_request`
    does today;
  - T24 limits apply to non-operator origins, evaluated by the store's reservation (pairs returned
    for the caller). Existing `decide`, `allows_direct_request`, `limit_refusal` stay as they are.

## Tests (new `tests/test_runbook.py`, `tests/test_store_v5.py`, additions to `test_policy.py`)
- Migration: fresh → v5; v4 fixture DB (build it with the **v4 DDL copied into the test**, with
  approvals pending/approved, executions running/passed, incident proposals, events) → v5 with every
  row preserved, running→interrupted, pending→expired, events written, FK check clean, the cutover
  lists returned; a forced failure mid-migration leaves a byte-for-byte v4 database (user_version 4,
  old `executions` intact); v6 is refused; re-running `init()` on v5 is a no-op.
- Existing store tests stay green untouched.
- Step transitions (legal and illegal), attempts: one-decision reservation for multiplicity 2 on a
  cooldown'd pair, refusal inserts nothing, consume/release, startup reconciliation rules; candidates
  fail-closed read.
- Runbook: `origin` key rejected, unknown step type, bad params, forward/self references, undeclared
  output, type mismatch, canonical hash stable across key order, tampered stored plan refused,
  artifact key/content mismatch, risk = max, pairs with multiplicity.
- Policy: each origin; automatic exceptions only for the exact (origin, type) combinations; a
  planner runbook containing only `prune.safe` is **not** automatic; forbidden risk; unknown origin.

## Verify
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` (1250 pass today; sandboxes that block
  AF_UNIX fail the RPC socket tests — report, never skip or fake)
- `.venv/bin/ruff check .`, `git diff --check`
- Report every file changed and every deviation from this brief or the design, with the reason.

## Must NOT change
`casa_bender.py`, legacy plan execution, `CommandService` behaviour, Zoidberg, Scruffy/dashboard,
existing policy functions' behaviour, auth.
