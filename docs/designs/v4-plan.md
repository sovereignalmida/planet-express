# Planet Express v4 — Execution plan

Plans against `v4-mission-control.md` (v4.1). Nothing here deploys: `v3-production` stays live on
the production host and v4 work happens on a new branch with no deploy without an explicit go.

## Working agreements

- **Branch:** `v4-next`, cut from `v3-next`. v3 fixes continue on `v3-next` and are merged in.
  Missions are off by default, so `v4-next` stays releasable as v3 at every slice boundary.
- **Slice = one spec, one PR-sized change.** Each starts with `/spec` (acceptance tests written
  first), ends with tests green, Claude `/code-review`, and `codex review` per `CLAUDE.md`.
  Every finding is fixed or explicitly declined in the slice notes.
- **Code lives in `planet_express/missions/`** (and `planet_express/memory/`), not in
  `casa_farnsworth.py`. Existing modules are touched only through narrow seams (store migration,
  RPC registration, setup steps, dashboard/Telegram views).
- **Schema:** one forward migration per slice that needs it (store is at v5; v4 starts at v6), each
  with a pre-upgrade snapshot step and a documented downgrade (restore snapshot).
- **Test hosts:** Ubuntu/systemd dev box; the libvirt MOS VM (currently unreachable, bring it up
  before slice 6); a homelab rehearsal stack for the Repair proof.
- **Rehearsal inputs:** the server-Claude export (`v4-server-memory-export.md`) feeds fixtures
  and the Phase 0 decisions. Fixtures are anonymised and synthetic equivalents must exist so no
  acceptance test needs the export.

## Slice map

| # | Slice | Depends on | Runs in parallel with |
|---|---|---|---|
| 0 | Contracts, threat model, `CLAUDE.md` rewrite, operator decisions | server export (gap list, tool inventory) | — |
| 1 | Mission store, state machine, events, leases + fencing | 0 | 2 |
| 2 | Ship's Memory store, FTS, import format and importer, quarantine | 0 | 1 |
| 3 | Session supervisor, adapter protocol, reference adapter, contained worker, brief generator | 1, 2 | 4 |
| 4 | Mission and memory surfaces: dashboard, Telegram, RPC (worker socket allowlist) | 1, 2 | 3 |
| 5 | Checkpoint/restore service and backup-freshness check | 1 | 6 |
| 6 | Tripwire monitor, protected domains, grant schema, grant → credential/env builder | 1, 5 | 5 (design), 7 after |
| 7 | Approval, standing grants, privileged worker tier, hooks, ask-flow | 3, 4, 6 | — |
| 8 | Verification catalogue, pre-agreed checks, crash classification and recovery | 1, 3, 5 | 6, 7 |
| 9 | First Repair mission end to end, Ubuntu then MOS, release proof, setup/undo/uninstall/docs | 7, 8 | — |
| 10 | Change missions (Phase 3): plan model, Core-applied resources, effective scope, add/remove a service | 9 | — |

Phase 1 is slices 0–4 (plus the Observe/Propose path through 3). Phase 2 is 5–9; Phase 3 is slice 10.

## Slice detail and acceptance tests

### Slice 0 — Contracts (gate)
Deliverables: mission and grant schemas; state machine incl. `paused_tripwire`,
`recovery_required`; protected-domain list; standing-grant rules; checkpoint and tripwire design;
worker identity, privilege tiers and per-OS credential building; adapter/supervisor lifecycle;
memory schema, quarantine, import format; egress policy; verification-check catalogue; threat model
(prompt injection, exfiltration, memory poisoning, confused deputy, runaway cost); test and
rollback strategy. `CLAUDE.md` safety text already updated.
Exit: operator decisions in §14 of the brief are recorded (done 2026-10-13); the server export's gap list is folded in; design reviewed by Codex; no open blocker.

### Slice 1 — Mission store
- Strict models + RFC 8785 hashing; approval proven only by `mission_approvals`; resource-containment lease matrix; upgrade guard refuses unsnapshotted migration.
- Create/transition only along legal edges; illegal transitions rejected and logged.
- Migration v5→v6 from a real v5 database, idempotent, with a snapshot and a refusal on newer
  schemas preserved.
- Event stream is append-only; redaction runs before write (planted secret never persisted).
- Leases: two missions on the same stack are refused/deferred; a stale fencing token is rejected;
  an expired lease frees the resource; scans and monitoring are never blocked.
- Missions disabled by default: the full existing suite passes unchanged.

### Slice 2 — Ship's Memory
- Cards round-trip through the generic import format; malformed cards rejected with clear errors.
- Imported/proposed cards land as `candidate`; candidates are excluded from retrieval for sessions.
- Procedures/constraints change status only by an operator-attributed action.
- FTS plus scope filters return the right cards for three fixture incidents; supersession and
  conflict handled; stale cards raise review items.
- An import from plain Markdown notes alone works (no skill pack, no export).

### Slice 3 — Supervisor, adapter, worker (Observe/Propose)
- Mission → generated brief containing evidence, confirmed memory only, constraints, success
  criteria, output schema.
- Session starts under the worker identity with grant-derived environment; read-only worker cannot
  mutate the host and cannot reach approval/decision RPC.
- Daemon restart mid-investigation: session is detected, mission resumes or goes `recovery_required`
  honestly.
- Budgets (wall, tool calls, output) enforced; cancel kills the whole process group.
- Structured result validates; fixture evaluations for three incident shapes; no mutation occurs.
- External-session import attaches a record without PE having launched it.
- Works with two different configured runtimes behind the same protocol (second may be a stub).

### Slice 4 — Surfaces
- Dashboard board and Telegram: create, review, approve/cancel placeholders, status, artifacts,
  live event view; auth and elevation reuse existing flows.
- Worker socket exposes only its method allowlist; calling `decide`, `execution_*` from the worker
  uid fails.
- Attach/takeover is available for a live session and is recorded in the event stream.

### Slice 5 — Checkpoint
- Checkpoint and unit-state reads go through the host-control provider (systemd and MOS), not direct `systemctl`.
- Snapshot of in-scope Compose/config files, container and mount state; restore reproduces them
  byte-for-byte including permissions.
- Refusal when in-scope backups are stale or unverifiable unless explicitly acknowledged.
- Restore after a mid-mission crash; checkpoint store survives reboot on Ubuntu and on the MOS pool.

### Slice 6 — Tripwires and grants
- Setup preflight (brief §6.8): worker identity distinct from core; no unauthenticated Docker TCP reachable; only generator-managed sudoers rules. Each fails closed, with a test per side door.
- Brokered integration tools hold app API keys server-side; a session env/file scan finds none.
- Each protected domain has a planted-change test (ingress, DNS file, credential file, firewall,
  data-volume shrink, backup config): session paused within one scan interval, mission
  `paused_tripwire`, rollback offered.
- Out-of-scope container/mount change detected the same way.
- Grant → environment: a session without DNS/backup credentials has none in its env, files or
  reachable paths; a hostile log line cannot alter the grant (hash unchanged).
- Grant hash binds approval; a changed grant invalidates it.

### Slice 7 — Approval, standing grants, privilege
- Docker API proxy (slice-0 spec §6.1): each refusal class has a test (privileged, host mount, socket mount, out-of-scope object, host namespaces); every accepted write is recorded before forwarding.
- `mission` origin added to policy with the §5 tests.
- Hook layer enforces the guards an interactive classifier would: refuse secret-store reads, destructive VCS in live trees, publishing.
- One approval covers all mutations inside scope; anything outside raises a decision card.
- Standing grants: scoped, expiring, listed, revocable; only incident-backed; never Change/Critical;
  expiry and revoke stop an unattended start.
- Repair worker has docker and generated scoped sudo; setup shows the generated sudo scope;
  undo/uninstall remove it.
- Hooks record every tool call and ask on irreversible-looking commands; the hook failing open or
  closed is a configured, tested behaviour.

### Slice 8 — Verification and recovery
- Catalogue includes in-container probe, route probe with resolve override, log-absence window, and compose-project completeness; a Docker `healthy` alone never satisfies a check.
- A verification plan must exist before any mutation; completion needs the checks plus a fresh
  scan to pass; an agent claim alone never completes a mission.
- Crash at investigation, execution and verification yields the right state, with no replay.
- `failed`, `rolled_back` and `needs_operator` outcomes are produced from real failure cases.

### Slice 9 — First Repair and release
- Rehearsal: canary rolled back, approved bounded correction of one service, recreate only it,
  pre-agreed check passes. Run on Ubuntu, then MOS (local worker or documented remote path).
- Release proof items 1–10 from the brief, each with a named test or rehearsal record.
- Setup, repair, undo, uninstall, upgrade, downgrade documentation; INSTALL.md and CHANGELOG.
- Final full-branch `codex review --base v3-next` and `/code-review` before any deploy conversation.

### Slice 10 — Change missions (Phase 3)
Owns slice-0 spec §6.2 (made normative by its own G1): plan model and hash, Core-created-resource
journal, Core-applied file patches with source/result sha256 preconditions, effective-scope
persistence, protected-touch approvals, add/remove a synthetic service (reversible), collision abort,
refusal of direct worker edits to Change files.

## Coordination

- **Owner role:** one integration owner (the main session) holds the slice map, merges, and runs
  the review gates. Parallel slices (1∥2, 3∥4, 5∥6, 8∥6/7) can be built by separate agents in
  isolated git worktrees, each from the slice's spec, reporting back with test results.
- **Hand-offs between slices are contracts, not code:** schema, protocol and RPC definitions land
  first and are reviewed before dependent slices start.
- **Definition of done per slice:** acceptance tests written first and passing; existing suite
  green; `/code-review` and `codex review` findings resolved or declined in writing; docs and
  setup/undo coverage for anything that adds an account, socket, secret or boot hook.
- **Risk register kept live:** worker containment on MOS, redaction gaps, runtime CLI behaviour
  changes, cost, MOS VM availability.

## Immediate next steps

1. Operator finishes the server-Claude export (prompt in `v4-server-memory-export.md`); the §14
   decisions are already recorded.
2. Cut `v4-next` from `v3-next`; commit the brief, plan, export spec and `CLAUDE.md` change.
3. Slice 0 spec; Codex review of it; then slices 1 and 2 in parallel.
