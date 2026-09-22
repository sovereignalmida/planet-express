# Slice 5b — multi-step typed execution, and retiring legacy shell plans

Status: **DRAFT for review** (2026-09-22). Parent plan: `docs/designs/planet-express-2-0-slices.md`
(slice 5, D25). Slice 5a (T37, `v2.0.0-7`) is live.

## 1. Why this slice exists

`CommandService` runs exactly one execution shape: resolve → one argv → verify. That covers restart
and (since 5a) stack up/down. Four behaviours still live outside it, and one of them is the last
**unenforced safety boundary** in the project:

| Flow | Today | Safety model |
| --- | --- | --- |
| Legacy LLM plans (Hermes finding → Farnsworth plan → Telegram ✅ → Bender) | free-text **shell** steps, `subprocess.run(shell=True)` | pattern blocklist (`_safety_check`), forbidden-stack regex, sudo allowlist, network-guard flag. Default-allow. |
| `/rollback <plan_id>` | runs the plan's LLM-written `rollback` steps, same shell path | same |
| Canary updates (`/patchnow`, weekly Sun 05:00 unattended) | Zoidberg: `docker compose pull` → `up -d` → watch → auto-retag + `up -d` on failure; strings through `_run_command` | `_safety_check` per command; mutation lock; automatic by design |
| `/install <url> <domain>` and Amy's compose edits | Fry/Amy produce compose text → `propose_compose_diff` → Telegram diff approve → `apply_pending_diff` writes the file; restart is a *separate* legacy plan | human approves the diff; path/symlink/new-vs-existing checks at apply |

5b replaces all four with one **multi-step typed execution engine**, then deletes `_run_command`,
`shell=True` and the LLM plan format.

## 2. Evidence: what the legacy paths actually do (live host, 2026-04 → 2026-09)

Classified from Bender's redacted step logs (`logs/*.log`) on the live host:

- **66 legacy plan runs, 343 steps.** By command shape:
  - `docker restart <container>` — 66 steps in 39 runs (2 failed)
  - wait-then-check (`sleep 20|30|60|90 && docker inspect/logs …`) — ~64 steps in ~45 runs
  - read-only evidence (`docker logs --tail`, `docker inspect --format`, `docker ps`, `systemctl
    status|list-timers|list-units`, `journalctl`, `ls`, `mount`, `du`, `free`) — ~100 steps
  - the gluetun/qBittorrent port-forward recipe's typed-in verifier (`SINCE=$(docker inspect …) && …
    grep -F "New : $PORT"`) — 10 steps in 9 runs (3 failed)
  - `docker image prune -a` / `docker network prune -f` — 5 runs each
  - `docker start <container>` — 3; `systemctl restart` — 1; `docker exec … sh` — 3 (2 failed);
    ad-hoc `cd …/stacks/<x> && …` / `find ~/stacks …` loops — a handful
- **Zoidberg: 519 logged steps; 200 updates: 198 clean, 2 automatically rolled back.** Runs
  unattended every Sunday 05:00 and on `/patchnow`.
- **Compose diffs:** rare (`pending_diffs.json` is empty today).

Conclusion: a small typed catalogue covers essentially everything plans did. The mutations are
restart, start, stop, wait, pull/recreate/retag, prune, and a few allowlisted `systemctl` actions; the
rest is evidence gathering, which the T17 read-only diagnostic loop already does *before* planning.

## 3. Goals and non-goals

Goals
1. One engine for every host mutation: typed steps, argv only (`bender.run_argv`), minimal env.
2. The **approved artifact is exactly what runs**: the approval stores the full step list with typed
   params (and any file content) plus its SHA-256; nothing can change after approval.
3. Per-step progress, persisted: the dashboard rail and Telegram card show real steps.
4. Real recovery semantics per step: `abortable` between steps, `rollbackable` where a step has an
   inverse, never "resume" automatically after a core restart (D8-style interrupted state stays).
5. Verification from host state, never from exit codes alone (as today).
6. Retire `_run_command`, `shell=True`, `_safety_check`'s pattern list and the LLM `rollback` field.

Non-goals (slice 6 or later): quarantine, P5 adapters, an MCP surface, an openai-compatible
provider, arbitrary user-authored runbooks, dashboard buttons for install/update.

## 4. The model

### 4.1 Step catalogue (typed, versioned)

Every step is `{"type": <name>, "params": {...}}`, validated by a per-type pydantic model with
`extra="forbid"`. Types are code, not data; adding one is a reviewed change.

| Step type | Params | Argv / effect | Inverse (rollback) | Verifier |
| --- | --- | --- | --- | --- |
| `service.restart` | stack, service | `compose restart <svc>` | none | existing `verify_after_restart` |
| `service.start` / `service.stop` | stack, service | `compose start|stop <svc>` | each other | running+healthy / not running |
| `stack.up` / `stack.down` | stack | as T37 | each other | as T37 |
| `wait` | seconds (1–300) | sleep, interruptible by abort | n/a | n/a |
| `check.container` | container, expect ∈ {running, healthy, stopped} | read-only inspect | n/a | fails the plan if unmet |
| `check.log_since_start` | container, fixed_string, since_container | `docker logs --since <StartedAt of since_container>` + fixed-string match (no regex, no shell) | n/a | fails if absent |
| `image.pull` | stack, service | `compose pull <svc>`; records new digest | n/a (pull is harmless) | digest resolved |
| `service.recreate` | stack, service, image_digest (pinned) | `compose up -d <svc>` after verifying the local image digest equals the pinned one | recreate with the **previous** digest (retag + `up -d`) | canary watch (as Zoidberg) |
| `prune.safe` | — | existing `run_safe_prune` | n/a | n/a |
| `unit.action` | unit, action | `sudo systemctl <action> <unit>` **only** if `_check_sudo_allowlist` grants it | start↔stop | `systemctl is-active` |
| `compose.write` | stack, content_sha256, is_new | atomic write of the approved content (content stored with the approval) under `stacks_root`, backup of the old file | restore the backup / remove the new dir | file sha equals approved sha |

Named **recipes** are fixed step lists with parameters, owned in code, used by the planner instead of
free text. First recipe: `vpn.resync_port_forward(mode = dead_forward | sync_mismatch)` → the exact
gluetun/GSP/qBit order, waits and the `check.log_since_start` verifier the planner prompt spells out
today. Recipes are how today's hand-written prompt knowledge becomes enforced code.

### 4.2 Plan document and approval

```jsonc
{ "kind": "runbook", "version": 1,
  "title": "Resync VPN port forward",            // for cards; never executed
  "origin": "planner" | "operator" | "zoidberg" | "install" | "amy",
  "risk": "R2",                                   // max over steps, computed, not model-supplied
  "steps": [ {"type": "service.restart", "params": {"stack": "network", "service": "gluetun"}}, … ],
  "artifacts": { "<sha256>": "<compose text>" }   // only for compose.write
}
```

- Stored on the approval (`target_json` → a new `plan_json` column + `plan_sha256`; schema **v5**).
- Risk = max(step risks). Step risks: checks/wait R0, restart/start/stop/up R1, down/pull/recreate/
  prune R2, `unit.action` and `compose.write` R3, anything touching an ingress stack R3.
  Policy (`forbidden_risks`, `direct_request_risks`, D23/D31, T24 limits) applies to the **plan's**
  risk unchanged.
- Every target in every step is resolved at proposal time (as T37 does) and **re-resolved before
  each step**; drift fails the step before it runs.
- The card shows every step in plain words, the risk, and which steps can be rolled back.

### 4.3 Engine

- Holds the mutation lock for the whole run (as today). Steps run sequentially; each persists
  `pending → running → passed|failed|skipped|rolled_back` in a new `execution_steps` table.
- **Abort** (new, dashboard + Telegram): honoured between steps and during `wait`; the running argv
  is never killed mid-flight (a half-applied `compose up` is worse than finishing it).
- **On failure:** stop. Completed steps with inverses are listed; the operator may **ROLL BACK**,
  which runs the inverses in reverse order as a new execution (its own approval-free child, since it
  only reverses what was approved). Not automatic — except the canary case (§5.2).
- **On core restart mid-run:** `interrupted`, as today; no auto-resume. `resumable` stays false for
  every type in 5b.
- Every step's argv goes through `run_argv`; output is redacted and bounded before storage/egress.

### 4.4 Planner

Farnsworth's planner (and its diagnostic loop, unchanged) must emit a runbook: steps from the
catalogue or a recipe call, validated before a card is ever sent. A plan that doesn't validate is
**not** sent for approval; the operator gets Amy's diagnosis plus "no typed remediation — needs a
human", which is also the honest answer for what the 3 `docker exec … sh` / ad-hoc `cd` plans tried.
The LLM never supplies a risk, a rollback or a verifier: those come from the step types.

## 5. Mapping the four flows

### 5.1 Legacy plans → planner runbooks
Same pipeline (Leela → Hermes → diagnostics → Farnsworth), new output format, new executor. Cards
move from `pending_plan.json` to the store's approvals (dashboard-visible, T31 cards). `/skip` becomes
DENY. The `pending_plan.json`/`PlanSet` files and the `awaiting_approval` pipeline state go away
(a pending plan becomes just a pending approval), which also removes the "config apply blocked by a
plan awaiting approval" friction seen in T36's rehearsal.

### 5.2 Canary updates → `update.canary` runbook per service
`image.pull` → (if the digest changed) `service.recreate(pinned new digest)` → canary watch → on
failure the engine **automatically** runs the inverse (recreate previous digest) and reports.
This is the one automatic mutation in the system, and it conflicts with D23/D31 as written
(non-R0 never automatic). See decision **5b-D1**.

### 5.3 `/install` and Amy's compose edits → `compose.write` runbooks
Fry/Amy output → validated compose text → runbook `[compose.write(new), stack.up, check…]` with the
content in `artifacts`. One approval covers write + start (today it's two separate approvals).
Existing guards move into `compose.write`: stays under `stacks_root`, no symlinks, new-vs-existing
must match proposal time, LAN-only domain rule, forbidden stacks, backup of any replaced file.

### 5.4 `/rollback <plan_id>` → the ROLL BACK control on an execution
Rollback stops being LLM-authored text; it's the inverses of the steps that ran. `/rollback <id>`
keeps working as a Telegram alias for "roll back execution <id>" while legacy ids exist.

## 6. Landings

| # | Scope | Gate |
| --- | --- | --- |
| **5b-1** | schema v5 (`plan_json`, `plan_sha256`, `execution_steps`), engine, step types: restart/start/stop/up/down/wait/check.*, card + dashboard rail per step, abort. Existing restart/stack actions become single-step runbooks (behaviour unchanged). | Codex + VM |
| **5b-2** | planner emits runbooks; `vpn.resync_port_forward` recipe; validation → refuse-to-card; legacy plan path kept **behind a config switch, default off** for the transition (see 5b-D3) | Codex + VM (fixture that mimics gluetun/GSP/qBit) |
| **5b-3** | `image.pull` / `service.recreate` / canary on the engine; Zoidberg calls it; weekly window unchanged; rollback candidates move into the store | Codex + VM (local registry fixture that serves a good and a crashing tag) |
| **5b-4** | `compose.write`, `/install`, Amy diffs; one approval for write+start | Codex + VM |
| **5b-5** | ROLL BACK control, `/rollback` alias, `unit.action`, `prune.safe` | Codex + VM |
| **5b-6** | delete `_run_command`, `shell=True`, `_safety_check` patterns, `PlanSet`/`pending_plan.json`, the switch; CLAUDE.md's "unenforced boundary" paragraph rewritten | Codex + VM + live soak |

`v2.0.0` = 5b-6 live.

## 7. Decisions needed from the operator

- **5b-D1 — Unattended canary updates.** Today Zoidberg updates ~every service weekly with no approval
  (200 updates, 2 auto-rollbacks). Options: (a) keep it automatic as an explicit, documented
  exception to D31, limited to `update.canary` (pinned digest, automatic inverse, T24 limits apply);
  (b) weekly pass *proposes* one batched approval card instead; (c) automatic only for services you
  mark, approval for the rest. Recommendation: **(a)**; it has a real inverse and a track record, and
  (b) would turn a silent weekly chore into a weekly card you have to tap.
- **5b-D2 — What `/install` may write.** Today: new stack dir only, LAN-only domain, operator approves
  the diff. Keep exactly that (recommended), or also allow edits to existing stacks from `/install`?
- **5b-D3 — Legacy plan cutover.** (a) switch-guarded transition: 5b-2 turns legacy plans off by
  default but keeps a config switch until 5b-6 deletes them (recommended); (b) hard cutover at 5b-2
  with no way back; (c) run both in parallel until 5b-6.
- **5b-D4 — Remediations the catalogue can't express.** When the planner has no typed fix, Planet
  Express only reports a diagnosis (recommended; matches D31's spirit). The alternative, a
  "manual steps" card with commands for you to paste, is out of scope unless you want it.

## 8. Risks

- **Planner regression:** a model constrained to a catalogue may produce fewer plans. Mitigation: 5b-2
  logs every refused plan with its validation error for a few weeks before 5b-6.
- **Canary fidelity:** Zoidberg's retag-based rollback is subtle (tag vs digest); 5b-3 must reproduce
  it exactly and prove it on a crashing-tag fixture before touching the live weekly window.
- **Schema v5:** first migration since incidents; T23 snapshots and refusal of newer schemas already
  cover rollback of a deploy.
- **Size:** six landings. Each must leave `v2` deployable.
