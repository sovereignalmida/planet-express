# Slice 5b — multi-step typed execution, and retiring legacy shell plans

Status: **Revision 3** (2026-09-22) — operator decisions D34-D38 taken; outside-voice rounds 1
(17 findings) and 2 (12 findings) folded in (§10, §11). Parent plan: `docs/designs/planet-express-2-0-slices.md` (slice 5,
D25). Slice 5a (T37, `v2.0.0-7`) is live.

## 1. Why this slice exists

`CommandService` runs one execution shape: resolve → one argv → verify. That covers restart and
(since 5a) stack up/down. Four behaviours still live outside it, and legacy plans are the last
**unenforced safety boundary** in the project:

| Flow | Today | Safety model |
| --- | --- | --- |
| Legacy LLM plans (Hermes finding → Farnsworth plan → Telegram ✅ → Bender) | free-text **shell** steps via `bender._run_command` (`shell=True`) | pattern blocklist (`_safety_check`), forbidden-stack regex, sudo allowlist, network-guard flag. Default-allow. |
| `/rollback <plan_id>` | runs the plan's LLM-written `rollback` steps, same shell path | same |
| Canary updates (`/patchnow`, weekly Sun 05:00 unattended) | Zoidberg: discovery and `pull` via its own `_run` (`shlex.split`, no shell, **no** `_safety_check`); the `up -d` recreate and the retag-based rollback via `_safety_check` + `_run_command` (`shell=True`) | mutation lock; automatic by design |
| `/install <url> <domain>` and Amy's compose edits | Fry/Amy compose text → `propose_compose_diff` → Telegram diff approve → `apply_pending_diff` writes the file; starting it is a *separate* legacy plan | human approves the diff; path/symlink/absent-vs-present checks at apply (no stale-content check) |
| Safe prune (automatic during a scan) | `run_safe_prune` via `_run_command` when root disk ≥ 80%, all containers healthy, no update rollback window open | fixed command list; automatic by design |

5b moves all of these onto one **multi-step typed execution engine**, then deletes `_run_command`,
`shell=True`, `_safety_check`'s pattern list and the LLM plan format.

## 2. Evidence: what the legacy paths actually do (live host, 2026-04 → 2026-09)

From Bender's redacted step logs (`logs/*.log`) on the live host:

- **66 legacy plan runs, 343 steps.** `docker restart <ctr>` 66 steps in 39 runs; wait-then-check
  (`sleep N && docker inspect/logs …`) ~64 steps in ~45 runs; read-only evidence (`docker logs`,
  `inspect`, `ps`, `systemctl status|list-*`, `journalctl`, `ls`, `mount`, `du`, `free`) ~100 steps;
  the gluetun/qBittorrent verifier 10 steps in 9 runs (3 failed); image/network prune 5 runs each;
  `docker start` 3; `systemctl restart` 1; `docker exec … sh` 3 (2 failed); a few ad-hoc
  `cd …/stacks/<x> && …` / `find ~/stacks …` loops.
- **Zoidberg: 200 updates, 198 clean, 2 automatically rolled back** (519 logged steps).
- **Compose diffs:** rare (`pending_diffs.json` empty today).

A small typed catalogue covers what plans did; the rest is evidence gathering, which the T17
read-only diagnostic loop already does *before* planning.

## 3. Goals and non-goals

Goals
1. One engine for every host mutation: typed steps, argv only, through the **bounded** runner
   (`bender.run_argv_bounded`: byte and time caps; redaction before any storage or egress).
2. **The approved artifact is exactly what runs**: step list, per-step target bindings and any file
   content, hashed; nothing an approval covers can change after it is taken (§4.3 defines the one
   runtime-output mechanism and how it is bound).
3. Per-step progress, persisted and visible (dashboard rail, Telegram card).
4. Honest recovery: rollback only where a step has a *real* inverse and only for what the step
   actually changed; unknown outcomes are reported as unknown, never guessed.
5. Verification from host state, never from exit codes alone.
6. Retire `_run_command`, `shell=True`, `_safety_check`'s pattern list, `PlanSet`/`pending_plan.json`
   and the LLM `rollback` field.

Non-goals (slice 6+): quarantine, P5 adapters, MCP surface, openai-compatible provider,
user-authored runbooks, dashboard buttons for install/update.

## 4. The model

### 4.1 Step catalogue

Every step is `{"type", "params", "binding"}` (binding in §4.2). Params are validated by a per-type
pydantic model with `extra="forbid"`. Types are code; adding one is a reviewed change.

| Step type | Risk | Effect (argv, no shell) | Rollback | Verifier |
| --- | --- | --- | --- | --- |
| `service.restart` | R1 | `compose restart <svc>` | **none** | `verify_after_restart` |
| `service.start` | R1 | `compose start <svc>` | conditional: stop, **only if the step's recorded pre-state was stopped** | running + healthy/none |
| `service.stop` | R2 | `compose stop <svc>` | conditional: start, only if pre-state was running | not running |
| `stack.up` / `stack.down` | as D33 | as T37 | **none** (down can destroy container state; up does not recreate the old containers) | as T37; `stack.up` outputs `{project, service, container_name, container_id}` for every approved service |
| `wait` | R0 | sleep ≤ 300s, abortable | n/a | n/a |
| `check.container` | R0 | inspect a **bound** container | n/a | expect running/healthy/stopped; fails the run if unmet |
| `read.qbittorrent_session_port` | R0 | `docker exec <bound qbit> …` reading **only** the `Session\Port=` line (bounded; never the whole file) | n/a | output: `port` (int 1–65535) |
| `check.log_since_start` | R0 | `docker logs --since <StartedAt of a bound container>` of a bound container, fixed-string match | n/a | fails if absent |
| `update.canary` | R2 (D34 automatic) | two-phase, §4.5 | **automatic** inverse: previous image | canary watch |
| `prune.safe` | R2 (D38 automatic in-scan) | existing fixed prune list | n/a | n/a |
| `unit.action` | R3 | `sudo systemctl <action> <unit>` only if `_check_sudo_allowlist` grants it | conditional start↔stop by recorded pre-state; restart none | `systemctl is-active` |
| `compose.write` | R3 | compare-and-swap write of approved content, §4.6 | restore the recorded backup (edit) / remove the created file (new) and the directory **only if this step created it, it is the same directory (inode) and it is empty**; only if the file still holds the approved content | file sha = approved sha |

**Recipes** are fixed step lists owned in code, callable by the planner by name with typed params.
First: `vpn.resync_port_forward(mode = dead_forward | sync_mismatch)` — gluetun → wait 20s → GSP →
qBit (or GSP → qBit), then a `check.log_since_start` of GSP for `New : <port>` where `<port>` comes
from the `read.qbittorrent_session_port` step's validated output.

### 4.2 Runbook document, bindings and approval

```jsonc
{ "kind": "runbook", "version": 1,
  "title": "Resync VPN port forward",                  // cards only; never executed
  "steps": [
    { "type": "service.restart",
      "params":  { "stack": "network", "service": "gluetun" },
      "binding": { "compose_path": "/…/stacks/network/docker-compose.yml",
                   "compose_sha256": "…", "project": "network",
                   "container": "CASA_GLUETON", "container_id": "…" } } ],
  "artifacts": { "<sha256>": "<compose text>" } }
```

- **Bindings** are resolved at proposal time and are part of the hashed document: compose path and
  file sha, project, service, container name *and id* where one exists, and for all-stack steps the
  ordered stack set (T37). `check.*` steps may only name containers that resolve to a compose
  identity at proposal time. Before each step the engine re-resolves and **fails the step without
  running it** if anything drifted (compose file edited, container replaced, stack set changed).
- **Staged bindings** for new stacks: a `compose.write(new)` step binds an *expected-absent* path;
  later steps on that stack bind to the write step's output (`"from_step": n`) and to the service
  names parsed from the approved compose artifact at proposal time. At execution they resolve only
  after the write step has passed and the file's sha equals the artifact's. Container identity for
  later `check.*` steps comes from the `stack.up` step's outputs (§4.3), which fail on a missing,
  duplicate or replaced container.
- **Origin is never part of the document.** It is assigned by trusted server code from the entry
  point that created the runbook (planner, Telegram/dashboard operator, Zoidberg's scheduler, the
  scan's prune gate, `/install`, Amy) and stored in a separate column; a submitted document carrying
  an `origin` field is rejected. The D34/D38 automatic exceptions key on that server-side origin
  **and** on the step type (`update.canary` / `prune.safe` only), never on anything the model wrote.
- Stored as `approvals.plan_json` + `plan_sha256` (schema v5). Risk = max over steps, computed; the
  model never supplies risk, rollback or verifiers.
- **Policy for multi-target runbooks:** `policy.decide_runbook(steps, origin)` checks forbidden
  risks and direct-request rules against the computed risk, and T24 limits for every mutating
  `(step_type, target_key)` pair **with its multiplicity in this runbook aggregated** (a runbook that
  restarts the same service twice counts twice). Checked at proposal, at approval and before
  execution. An **attempt is a dispatch**: immediately before a mutating step's argv, the engine
  transactionally re-checks and reserves that pair (`reserved`), marking it `consumed` once
  dispatched or `released` if the step is skipped, aborted or fails its pre-dispatch checks; startup
  reconciliation settles any `reserved` rows. Steps that never run are never charged. Existing single-action paths become
  one-step runbooks through the same function (behaviour unchanged).
- The card lists every step in plain words, the risk, and which steps have a rollback.

### 4.3 Runtime outputs (the one exception to "fully known at approval")

- Each step type declares, **in code**, its output schema (names, types, validators) and which of
  its own param/binding fields may accept a reference, and of what output type.
- A reference is `{"from_step": n, "output": "<name>"}`; it is legal only in such a typed field and
  only to an **earlier** step. Proposal-time validation checks the step exists, precedes the
  consumer, declares that output, and that the types match; the reference itself is hashed.
- When the producing step finishes, its outputs are validated against the schema and **persisted
  before any consumer runs**; an invalid or missing output fails the producer. Immediately before a
  consumer builds its argv, each reference is re-read from the store, re-validated and substituted
  canonically (no string templating). Nothing else in an approved runbook can change.

### 4.4 Engine and step state machine

- Holds the mutation lock for the whole run. Steps run in order. Per step, persisted *before* the
  argv is spawned: `pre_state` (what the rollback will need), then `dispatched`; after:
  `passed | failed | skipped | aborted`, with `effect ∈ {applied, not_applied, unknown}` from the
  step's postcondition check (not the exit code).
- On a core restart mid-step the step is reconciled at startup against its postcondition →
  `applied | not_applied | unknown`; the execution becomes `interrupted`. No auto-resume.
- **Abort** (dashboard + Telegram): honoured between steps and during `wait`; a dispatched argv is
  never killed. Terminal state `aborted`.
- **Rollback** (dashboard + Telegram, `/rollback <execution>`): a child execution of the same
  approval (`parent_execution_id`, `kind = rollback`) that runs, in reverse order, the conditional
  inverse of every step whose effect is **`applied`**. Steps without an inverse are listed as "not
  reversible". A step whose effect is `unknown` is first re-reconciled against fresh bound state; if
  it is still indeterminate it is **excluded** from rollback and reported, and the only way to act on
  it is a separately displayed, separately approved recovery runbook with its exact mutations shown.
  One active rollback per execution; idempotent controls. Terminal: `rolled_back | rollback_failed`.
- **After failure:** a post-failure hook hands Amy the redacted, bounded step outputs, bindings,
  verifier evidence and rollback outcome (today's `_investigate_failure` contract). Any compose edit
  Amy proposes is a new `compose.write` runbook with its own approval.

### 4.5 Canary updates (`update.canary`, D34)

Eligibility: the service's Compose image must be a canonical mutable `name:tag` reference;
digest-pinned (`repo@sha256:…`) and build-only services are ineligible and the refusal is recorded.
Phase 1: persist `pre_state` (compose file sha, exact reference, the running container's image id,
the image id the reference currently points to) and a **rollback-candidate row** (§6), then mark the
sub-state `pull_dispatched` and run `compose pull <svc>`; persist the newly resolved image id
immediately after. No change → the step passes as `not_applied`. A crash after `pull_dispatched`
reconciles by inspecting both the running container and what the reference now points to.
Phase 2 (deploy): tag the new image id to the exact reference, `compose up -d --pull never <svc>`,
then inspect the container and require its image id to equal the new one; canary watch as today.
Automatic inverse on failure: retag the recorded **old** image id to the same exact reference,
`up -d --pull never`, verify the container's image id equals the old one. Refuses to run if the
compose file sha or the reference drifted since phase 1. Safe prune refuses to run while any
rollback-candidate row is open **or the candidate table cannot be read** (fail closed; today a
malformed candidates file lets prune proceed).

### 4.6 `compose.write`

Params: stack, `expected_old_sha256` **or** `expected_absent`, `content_sha256`; binding: resolved
path under `stacks_root` (no symlinks anywhere on the path). Immediately before writing: re-check the
expectation (compare-and-swap), write a temp file, fsync, preserve mode/owner/ACL of the replaced
file, rename, fsync the directory, and record the backup path and sha as the step's `pre_state`
first. `.env` files are never written (secrets stay human-only). LAN-only domain and forbidden-stack
rules from `/install` are checked at proposal time on the parsed artifact. `pre_state` also records
whether the stack directory existed and, if this step creates it, its inode — the inverse uses that.

## 5. Mapping the flows

- **Legacy plans → planner runbooks.** Same pipeline and diagnostic loop; the planner must emit a
  runbook (catalogue steps or recipes) that validates before any card is sent. Invalid → Amy's
  diagnosis only (D37); every refusal is logged with its validation error. Cards move into the store
  (dashboard-visible). `/skip` → DENY. `pending_plan.json`, `PlanSet` and the `awaiting_approval`
  pipeline state go away (a pending plan is just a pending approval).
- **Canary** → Zoidberg builds one `update.canary` runbook per eligible service, origin `zoidberg`,
  automatic under D34 with T24 limits per service; weekly window and `/patchnow` unchanged.
- **Safe prune** → `prune.safe` runbook, origin `system`, automatic in-scan under D38; unchanged
  gates.
- **`/install` and Amy edits** → `[compose.write, stack.up, check.container…]` runbooks; one
  approval covers write + start (D35: new LAN-only stacks only for `/install`).
- **`/rollback <plan_id>`** → alias for the ROLL BACK control of an execution.

## 6. Schema v5 and the v4 → v5 migration

**Migration.** `Store.init()` today only re-runs `CREATE … IF NOT EXISTS` and stamps `user_version`;
v5 needs a real, transactional migration step keyed on the current version: `BEGIN IMMEDIATE`; create
`executions_new` with the full DDL; copy and validate every row; drop and rename; add the new
columns, tables and indexes; `PRAGMA foreign_key_check` must return nothing; only then set
`user_version = 5`; `COMMIT`. Any failure rolls back and core refuses to start (T23 snapshot is the
recovery path). **Cutover of live v4 rows**, done by the same migration: unfinished v4 executions
are reconciled to `interrupted` first (as startup does today); pending v4 approvals are expired
with their cards updated ("superseded by an upgrade; propose again"); after v5, execution of any
approval without a verified `plan_sha256` is refused.

**Target shape:**

- `approvals`: `plan_json TEXT`, `plan_sha256 TEXT` (nullable for pre-v5 rows; new rows always set).
- `executions`: `kind TEXT NOT NULL DEFAULT 'run' CHECK (kind IN ('run','rollback'))`,
  `parent_execution_id TEXT REFERENCES executions(id)`, status adds `aborted`, `rolled_back`,
  `rollback_failed`; `abort_requested_at REAL`; a partial unique index allowing one active rollback
  per parent. Child executions keep the parent's `approval_id` (the authority is the original
  approval; the rollback operator is recorded in `events`).
- `execution_steps(execution_id, n, type, params_json, binding_json, pre_state_json, status, effect,
  output_json, reason, started_at, finished_at)`, PK `(execution_id, n)`.
- `attempts(id, execution_id, step_n, step_type, target_key, state CHECK (state IN
  ('reserved','consumed','released')), reserved_at, settled_at)`, index `(step_type, target_key,
  reserved_at)`; T24 counts `reserved` + `consumed` rows in the window.
- `rollback_candidates(execution_id, step_n, stack, service, image_reference, old_image_id,
  created_at, expires_at, closed_at)`, PK `(execution_id, step_n)`; "open" = `closed_at IS NULL AND
  expires_at > now`.
- Active-rollback uniqueness: `CREATE UNIQUE INDEX executions_one_active_rollback ON executions
  (parent_execution_id) WHERE kind = 'rollback' AND status IN ('running', 'verifying')`.
- `approvals.origin TEXT` (server-assigned, §4.2).
- T23: pre-upgrade snapshot and refusal of newer schemas already cover rolling back a deploy.

## 7. Landings

| # | Scope | Gate |
| --- | --- | --- |
| **5b-1** | Schema v5 with the transactional v4→v5 migration and live-row cutover (§6); engine with the step state machine, bindings, bounded runner, abort, **rollback (conditional inverses, child executions, `unknown` confirmation)**, post-failure Amy hook; `policy.decide_runbook` + per-pair T24; catalogue: restart/start/stop/stack up-down/wait/check.*/`unit.action`/`prune.safe`; existing restart + stack actions become one-step runbooks (behaviour unchanged); dashboard rail per step + ABORT/ROLL BACK controls | Codex + VM |
| **5b-2** | Planner emits runbooks; `vpn.resync_port_forward` recipe + `read.qbittorrent_session_port`; refuse-to-card + refusal log; legacy plans **off by default** behind `legacy_plans_enabled` — an **accepted capability reduction** under D36/D37 (plans that needed `docker exec`, ad-hoc shell loops or anything outside the catalogue become diagnosis-only), not a parity claim; `pending_plan.json` retired for new plans | Codex + VM (gluetun/GSP/qBit mimic fixture) |
| **5b-3** | `update.canary` two-phase on the engine; Zoidberg and safe prune (D38) routed through it; rollback candidates in the store | Codex + VM (local registry serving a good and a crashing tag) |
| **5b-4** | `compose.write` with staged bindings; `/install` and Amy edits | Codex + VM |
| **5b-5** | Delete `_run_command`, `shell=True`, `_safety_check` patterns, `PlanSet`, the legacy switch and `pending_diffs.json`; CLAUDE.md's "unenforced boundary" paragraph rewritten | Codex + VM + live soak (≥ 1 weekly canary window) |

Every landing leaves `v2` deployable; each gets a live deploy. `v2.0.0` = 5b-5 live.

## 8. Decisions (operator, 2026-09-22)

- **D34** — unattended weekly canary updates stay automatic, as a documented exception to D31,
  limited to `update.canary` (pinned image, automatic verified inverse, per-service T24 limits,
  existing exclusions).
- **D35** — `/install` writes new stacks only, LAN-only domain, as today.
- **D36** — legacy plans off by default from 5b-2 behind a config switch removed in 5b-5.
- **D37** — no typed remediation → diagnosis only; no paste-these-commands cards.
- **D38** — safe prune keeps running automatically during scans under its existing gates (disk ≥ 80%,
  every container healthy, no canary rollback window open); the second documented exception to D31.
  Recorded by the coordinator as "behaviour unchanged"; the operator can revert it to approval-gated.

## 9. Risks

- **Planner regression** under a catalogue constraint: the 5b-2 refusal log is reviewed before 5b-5.
- **Canary fidelity**: the reference/image-id binding and `--pull never` must be proven on a
  crashing-tag fixture before the live weekly window runs on it (5b-3 soak).
- **Schema v5**: the largest migration yet; rehearsed restore of a pre-v5 snapshot on the VM.
- **Size**: five landings, each deployable.

## 10. Outside-voice review, round 1 (Codex, 2026-09-22) — resolution

| # | Finding | Resolution |
| --- | --- | --- |
| 1 | Canary automation contradicts D31 | Explicit operator decision D34; narrow `update.canary` capability, not general R2 automation (§4.1, §8) |
| 2 | Safe prune is also automatic | D38 recorded; routed through the engine unchanged (§5, §8) |
| 3 | start/stop, up/down, restart "inverses" aren't rollbacks | Conditional inverses by recorded pre-state; up/down/restart not reversible (§4.1) |
| 4 | Rollback ignored partially applied steps | Step state machine with `pre_state`/`dispatched`/`effect`; `unknown` included with confirmation (§4.4) |
| 5 | Canary digest unknown at approval | Typed, hashed runtime-output placeholders (§4.3) and two-phase canary (§4.5) |
| 6 | `compose up` doesn't pin the image | Retag exact reference + `--pull never` + verify container image id (§4.5) |
| 7 | Canary rollback lacked artifacts | Phase 1 records compose sha, exact reference, old image id; drift refuses (§4.5) |
| 8 | No proposal-time target snapshot | Hashed per-step bindings, re-resolved before each step (§4.2) |
| 9 | New stacks can't resolve at proposal | Staged bindings to the write step's output (§4.2) |
| 10 | `compose.write` lost-update + metadata | Compare-and-swap on `expected_old_sha256`/`expected_absent`, metadata preserved, fsyncs (§4.6) |
| 11 | Policy/T24 not defined for runbooks | `policy.decide_runbook`, per-pair limits recorded atomically (§4.2, §6) |
| 12 | Schema couldn't hold rollback/abort | Full v5 schema (§6) |
| 13 | Rollback landed after its consumers | Rollback + abort in 5b-1, before any producer (§7) |
| 14 | Legacy off before catalogue parity | All catalogue types except canary/compose.write land in 5b-1; legacy defaults off only in 5b-2 (§7) |
| 15 | Amy investigation dropped | Post-failure hook (§4.4) |
| 16 | `run_argv` is unbounded | Engine uses `run_argv_bounded` everywhere (§3) |
| 17 | Zoidberg current-state description wrong | Corrected (§1) |

## 11. Outside-voice review, round 2 (Codex, 2026-09-22) — resolution

| # | Finding | Resolution |
| --- | --- | --- |
| A1 | v5 was a target shape, not a migration | Transactional table-rebuild migration with FK check before the version stamp (§6) |
| A2 | `unknown` effects still rolled back on a guess | Excluded; re-reconciled, otherwise only a separately approved recovery runbook (§4.4) |
| A3 | Runtime outputs not tight enough | Per-type output/field schemas in code, earlier-step-only references, persist-before-consume, re-validate before argv; missing `read.qbittorrent_session_port` added (§4.1, §4.3) |
| A4 | Staged bindings couldn't identify new containers | `stack.up` outputs container identities; checks bind to them (§4.1, §4.2) |
| A5 | T24 per-pair accounting ambiguous | Attempt = dispatch; aggregated multiplicity; reserve/consume/release with reconciliation (§4.2, §6) |
| A6 | "Parity" wasn't a gate | Renamed an accepted capability reduction under D36/D37 (§7) |
| B1 | `origin` forgeable inside the document | Removed from the document; server-assigned column; exceptions key on origin + step type (§4.2) |
| B2 | Canary "discovery before mutation" false; crash gap | `pre_state` + candidate row + `pull_dispatched` before pull; reconcile tag and container (§4.5) |
| B3 | Rollback candidates had no table | `rollback_candidates` table; prune fails closed on read errors (§4.5, §6) |
| B4 | New-stack rollback could delete a pre-existing dir | Directory removed only if created by the step, same inode, empty (§4.1, §4.6) |
| B5 | Live v4 approvals/executions unhandled | Migration reconciles/expires them; no execution without a verified plan hash (§6) |
| B6 | Digest-pinned references aren't taggable | Canary requires a mutable `name:tag`; others ineligible and recorded (§4.5) |
