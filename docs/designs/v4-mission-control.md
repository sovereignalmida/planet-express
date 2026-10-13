# Planet Express v4 — Mission Control and the Ship's Memory

**Status:** v4.1, revised after architectural review (supersedes the v4.0 draft kept beside it as
`v4-mission-control-v4.0-draft.md`)
**Audience:** the operator, the Planet Express maintainers
**Decision sought:** approve the safety-model change in §6 and the Phase 0 open decisions in §14,
then plan against this document.

## 0. What changed from the v4.0 draft

The draft kept v3's safety model (typed steps, no shell, a gateway that brokers every command) and
bolted missions onto it. Review found that a command-level gate cannot make a capable agent safe
and also cannot let it do what the operator's own Claude session does. v4.1 changes the model:

| v4.0 draft | v4.1 |
|---|---|
| Tool gateway brokers every mutation; no unbounded shell under Repair | PE **supervises the real agent session** with its native tools; control is by consequence |
| Safety = what the agent is allowed to type | Safety = checkpoint, tripwires, credential scoping, independent verification |
| Worker never has docker/sudo | Privilege follows authority tier; Repair workers have docker and scoped sudo |
| Per-mission approval for novel work | Per-mission approval **or standing grants** for unattended incident repair |
| Global mutation lock | Per-resource leases; monitoring never blocks |
| "No shell" invariant retained | Retired deliberately (§6.7); typed runbooks stay as the fast path, not the boundary |

Unchanged: Python control plane, v3 foundation, Ship's Memory, optional runtimes, portability
(Ubuntu/systemd and MOS/sysvinit), independent verification.

## 1. Why this exists

Planet Express v3 monitors a Docker Compose homelab, records evidence, and can perform a small
catalogue of verified, approval-gated actions. When a real incident, change, install, removal or
improvement is needed, the operator leaves PE and opens a Claude Code session on the server. That
session has the context, tools and judgement to do the work. PE hands off at the point where it
should become most useful.

> **Farnsworth's job:** keep the server healthy, useful, secure, recoverable and steadily
> improving, subject to the operator's priorities and constraints.

The goal of v4 is for PE to do what the operator's server session does, durably and with the
operator's authority boundaries intact, instead of being the thing the operator works around.

## 2. Product thesis

PE is the **durable control plane and supervisor** for agent sessions that do real work. It owns:

- continuously collected evidence and baselines;
- a durable mission queue and the operator's declared intent;
- environment-specific institutional memory (the Ship's Memory);
- authority grants, protected domains, checkpoints and tripwires;
- a full record of what the agent did, and independent proof of whether it worked;
- the dashboard/Telegram surface, and the ability for the operator to attach to a live session.

The agent runtime is a replaceable specialist. PE is the source of truth for mission state,
approval state, audit and verification. The product gets better by learning the environment and
capturing proven local procedures, not by treating unverified chat output as fact.

## 3. Foundation kept

Python stays the control plane. The expensive host-specific work is kept: systemd and MOS support,
setup/repair/uninstall/undo, host-control and stack-orchestration providers, monitoring, incident
state, SQLite, scheduling, Telegram, dashboard auth, local RPC, canary updates, safe pruning.

Typed runbooks remain the preferred mechanism for narrow, repetitive, proven automation. A mission
is the unit of work for everything a small catalogue cannot honestly express. A repair that
succeeds repeatedly as a mission is a candidate to **graduate** into a typed runbook.

No agent runtime is mandatory. The core must install and run on Ubuntu and MOS without Node, a
vendor SDK, or cloud access. Runtimes are optional capabilities configured separately.

The operator's Claude Code workflow and server skill pack are **development and rehearsal inputs**
(§9), never a product dependency. No released contract, install path, adapter API or acceptance
test may assume them.

## 4. The mission model

An incident, operator request, maintenance task or improvement idea becomes a **mission**, the
durable unit of delegation and accountability.

### 4.1 Fields

```text
id, title, origin, priority, status
goal and success criteria
scope: host / mount / stack / service / paths / network dependencies
evidence and referenced snapshots; relevant Ship's Memory cards
constraints and maintenance window
grant (structured) and its hash; approval record
runtime adapter and session reference
event stream: transcript, tool calls, diffs, tripwire events, decisions
checkpoint reference; verification plan (agreed before mutation) and result
outcome, rollback/next steps, memory candidates
```

### 4.2 State machine

```text
draft -> queued -> investigating -> awaiting_decision -> executing -> verifying
      -> completed | failed | rolled_back | cancelled | blocked | needs_operator
executing -> paused_tripwire -> (resumed | rolled_back | needs_operator)
any live state -> recovery_required   (supervisor or host crashed; state unproven)
```

`partially_applied` is a recorded outcome flag, not a resting state: it forces `needs_operator`
or `rollback_pending`. Missions are resumable: a runtime crash or session end must not erase the
goal, gathered evidence, checkpoint, or the operator's decision.

### 4.3 Examples

- **Incident:** "Restore Paperless after its database dependency stopped. Do not touch ingress or
  delete data."
- **Request:** "Add Paperless to the services stack with backups, a healthcheck, a route and a
  rollback plan."
- **Improvement:** "Find out why media indexing saturates the NAS; propose a fix that preserves
  backup windows."
- **Maintenance:** "Validate the Borg restore path, untested in 90 days."

## 5. Authority

Authority is a structured **grant**, with a human-readable rendering. The structured grant is the
enforceable object; approval binds to its hash.

| Tier | What the session may do | Worker privilege | Approval |
|---|---|---|---|
| Observe | Inspect bounded host state, logs, config, docs | Contained, read-only | Automatic |
| Propose | Produce diagnosis, diffs, plans, rollback plan | Contained, read-only | Automatic |
| Repair | Act inside the declared scope: restart, recreate, edit config/Compose, run commands | Docker **through a scope-enforcing API proxy** + scoped sudo, brokered credentials | One mission approval, or a **standing grant** |
| Change | Add/remove/reshape services | As Repair | Approve plan/diff, then execute |
| Critical | Protected domains (below) or irreversible effects | As Repair | Explicit per-change approval |

### 5.1 Grant contents

Scope (stacks, services, paths, mounts), permitted tier, protected-domain exclusions, maintenance
window rules, budgets (wall time, tool calls, output size, retries, spend where reported), the
verification plan, and the credentials the session will receive.

### 5.2 Standing grants

A standing grant pre-approves a class of incident-backed repair so the operator can leave PE
alone, for example: *"For the `media` stack, PE may restart services and edit its Compose files,
outside the backup window, without asking."* Rules:

- A standing grant covers only incident-backed missions whose scope fits inside it.
- It is explicit, listed, revocable, versioned, and expires unless renewed.
- It never covers Change or Critical work.
- Anything outside it becomes `awaiting_decision` and a Telegram/dashboard card.

### 5.3 Protected domains (default: always escalate)

Network/ingress/DNS, firewall, credentials and identities, persistent data volumes and storage,
backup configuration, package management, host reboot, **publishing** (pushing to any remote,
exposing a service publicly), **destructive version-control operations in a live working tree**
(`reset --hard`, `checkout --`, `stash`, trial `revert`, force-push), sudoers and the Docker daemon
configuration, and any cross-host system the operator names (NAS exports, edge proxy, router, DNS
provider). The operator may refine the list; it cannot be emptied silently. A tripwire (§6.2)
watches each.

Protected domains are defined per site as **concrete resources** (containers, networks, paths,
units, hosts, remotes) in configuration, not as stack names: a stack can contain both a protected
ingress container and an ordinary helper that a Repair legitimately recreates. Setup proposes the
list from discovery and the operator confirms it.

## 6. Safety model: control by consequence

PE does not try to predict what an agent will type. It makes the agent's work **recoverable,
observable and bounded by what it holds**.

### 6.1 Checkpoint before the first mutation

PE snapshots the in-scope Compose and config files into a versioned store, records container and
mount state, and confirms backups of in-scope data are fresh. A Repair mission is refused on a
stack with no verified recent backup unless the operator acknowledges that explicitly. Rollback is
"restore the checkpoint", which works for edits the agent invented. Where something is not
restorable (e.g. deleted data without a backup), the mission is classed Critical up front.

### 6.2 Tripwires: continuous state diff against the grant

PE keeps scanning while a mission runs. It diffs host state against the grant: containers,
mounts, listening ports, ingress and DNS config, firewall, credential files, backup jobs,
data-volume size/mtime. A protected-domain change or out-of-scope change **pauses** the session
and asks the operator, with the option to roll back to the checkpoint. This is detect-and-revert
rather than prevent-by-grammar, and it is also the defence against injected instructions: the
effect shows up in the diff however the instruction was phrased.

### 6.3 Credential scoping

The session environment is built from the grant. Credentials for protected domains (DNS provider,
router, backup repository, identity provider) are not provided to a session that is not granted
them. This is the one control that holds even against a capable root-level agent.

**Brokered integrations.** Application API keys (media-manager, DNS-rewrite, reverse-proxy,
photo-library APIs and similar) are exposed to the session as PE-brokered tools that hold the key
server-side, not as environment variables or readable files. A session that needs to fix a
phantom queue entry or a DNS rewrite calls the tool; it never holds the key. This is also how
"secret files are never sent" (§14.4) stays true when the work needs those APIs.

### 6.4 Tool hooks: a speed bump, not the boundary

The reference adapter installs the runtime's pre-/post-tool hooks to record every call and to ask
the operator when the agent crosses into a protected domain or an irreversible-looking command
(`docker volume rm`, `prune -a`, recursive deletes, database drops). Hooks give audit and UX.
Safety does not depend on them stopping a determined agent. The hook layer must also supply the
guards an interactive runtime's permission classifier would otherwise provide, because a headless
worker may run under different settings: refuse reading raw secret stores and `.env` contents,
refuse destructive version-control operations in live trees, and refuse publishing. Whatever PE
expects refused, PE enforces itself.

### 6.5 Independent verification and crash honesty

- A verification plan is agreed **before** mutation (typed, core-executed checks: HTTP probe
  through the real route, container health, in-container probe (an `exec` that exercises the
  dependency, not a `stat` healthcheck), mount readable inside the consumer, file stat, bounded
  query, a log-absence check over a window, and **compose-project completeness**: every service
  the project defines exists after a recreate). Green-but-broken health status has recurred often
  enough that no Docker health state alone may satisfy a check. The mission completes only when PE
  runs those checks plus a fresh Leela scan and they pass. An agent assertion is never sufficient.
- After any crash PE reads host state to classify the mission; it never replays an unknown
  mutation. Intent is journaled before the session is started and at each checkpoint/phase change.

### 6.6 Secrets and egress

Redaction is applied before persistence to mission records, transcripts, artifacts and memory
candidates. Redaction has known gaps, so it is one layer: the other layers are credential scoping
(§6.3) and a Phase 0 egress policy stating what may leave the host (logs, configs, memory) and to
which runtime. Content from logs and the web is passed to the session as data, not instructions.

### 6.7 What is retired from v3, and what is not

Retired: the "no shell anywhere" invariant; the rule that all host changes are typed steps; the
global mutation lock as the concurrency model for missions; the typed catalogue as the boundary.

Kept: the engine and typed runbooks as the fast path for known repairs; their verification and
crash-settlement logic; target-binding discipline for typed steps; unattended origins for typed
automation stay limited to `zoidberg`/`update.canary` and `system`/`prune.safe`. Mission unattended
start is governed solely by standing grants (§5.2).

### 6.8 Preconditions: no side door around the worker boundary

A worker boundary is meaningless if the host has another path to root. Setup preflight checks, and
a mission refuses to start a Repair-tier worker unless these hold:

- The worker's Unix identity is **not** the PE core's identity and not any identity that is in the
  `docker` group beyond what its tier grants.
- The Docker daemon has **no unauthenticated TCP listener** reachable by the worker (a TCP
  `0.0.0.0` socket is root for anyone who can connect, including a worker with no docker group).
  The operator may keep a socket proxy with an allowlist instead; the preflight names the exact
  exposure it found.
- Sudoers contains only rules managed by PE's generator; unmanaged broader rules (for example a
  literal wildcard that sudoers matches across spaces) are reported and, with approval, removed by
  `repair`.

### 6.9 Accepted residual risk

An agent with root can in principle defeat any in-host tripwire. That is already true of the
operator's own session today. v4 makes damage detectable and recoverable and keeps protected
credentials out of reach; the off-host backup requirement is the real floor.

## 7. Runtimes and session supervision

```python
class AgentRuntime(Protocol):
    def start(self, mission, context, grant) -> RunHandle: ...
    def status(self, handle) -> AgentUpdate: ...
    def cancel(self, handle) -> None: ...
    def collect(self, handle) -> AgentResult: ...
```

The supervisor (not the adapter) owns: spawning the session under the worker identity with the
grant-derived environment, the generated mission brief, transcript capture, redaction before
persistence, budgets, pause/resume/kill, lease heartbeat, and crash detection.

**Attach and takeover.** The operator can attach to a live mission session (terminal multiplexer
semantics), observe, interject, or take over. A takeover is recorded; the mission continues as
operator-driven and PE still verifies the outcome. This is what makes a mission resemble the
operator's existing workflow.

**Reference adapter.** Runs a configured agent CLI headlessly with its native tools and the
grant-derived hooks and environment. It receives its context through the published adapter
contract. A manual/external-session import path attaches an existing human or agent session's
record to a mission without PE having launched it.

**Concurrency.** Leases are per resource (stack/service/mount) with a TTL and fencing token.
Read-only monitoring and scans never block. A conflicting canary, prune or other mission is
refused or deferred with a clear reason.

## 8. The Ship's Memory

Durable, evidence-backed knowledge of this environment, so an agent does not rediscover an NFS
failure from zero each month or propose a redesign that conflicts with an intentional constraint.

### 8.1 Cards

```text
id, title, type: fact | procedure | constraint | decision
scope/tags; statement; procedure steps if any
evidence links (missions, snapshots, docs)
source: observed | operator-stated | researched | inferred
status: candidate | confirmed | deprecated
created_at, last_verified_at, review_after; supersedes / conflicts_with; edit history
```

Four types in v4; fragility, preference and lesson are expressed as tags or constraint/decision
cards until retrieval proves a finer split is needed. Cards are bounded text, not transcripts.

### 8.2 Rules

- Current host observations always outrank memory. A card is context and a hypothesis, never
  permission.
- **Constraints compile into the grant** where they are expressible ("do not remount during a
  backup", "do not touch network") so they are enforced by tripwires and the maintenance-window
  logic, not left as advice the agent may ignore.
- **Quarantine.** Runtime-proposed candidates are stored as `candidate`, shown to the operator as
  untrusted, and are **not injected into later sessions** until confirmed. Procedures and
  constraints are confirmed by the operator only; there is no automatic promotion by repeat
  success. Operator-stated cards are attributed to the operator.
- Stale cards raise review items, not silent folklore.
- Retrieval is SQLite full-text search plus scope filters. No vector database until ordinary
  retrieval has demonstrably failed. Validity probes on cards are deferred to a later release
  (they are command execution and need their own review).

### 8.3 Learning loop

At mission close the runtime proposes structured candidates: what was observed, what changed,
what verification proved, what was learned, what it invalidates. PE validates provenance and
conflicts and stores candidates for operator review.

## 9. Knowledge bootstrapping: development phase versus product

There are two separate things, and the product must never conflate them.

**Development input (this operator, this fleet only).** The operator's server-side Claude session
holds incident history, procedures, tool use and environment notes. These seed fixtures,
evaluation cases, gap analysis and test memory during the v4 build. The export is specified in
`v4-server-memory-export.md`. None of it ships, and no release contract depends on its presence.

**The canonical store.** Ship's Memory is the single canonical store. The operator's existing
skill file, memory notes and exports are inputs to it, imported one way, and are not kept in sync.
Where they disagree, the card records the conflict for the operator.

**Product (any site).** A generic **import format** (Markdown-with-front-matter / JSONL cards,
documented and versioned) lets any operator load existing notes. PE also generates its own
**discovery candidates** from scans (mounts, dependencies, start order, health baselines) and from
missions it runs, so a site with no notes and no skill pack still builds memory. The importer
treats every imported item as `candidate` / `operator-stated` per §8.2. No automatic bidirectional
sync with arbitrary notes or agent memories.

## 10. Delivery plan

**Phase 0 — Contracts and threat model.** Mission schema/state machine/resume/cancel/retention;
grant schema, protected domains, standing-grant rules; checkpoint and tripwire design; worker
identity, privilege tiers and credential building per OS; adapter and supervisor lifecycle;
Ship's Memory schema, quarantine and import format; egress policy; verification-check catalogue;
test and deployment strategy; rewrite of the `CLAUDE.md` safety section. Exit: a reviewed design
from which slice 1 can start without choosing a vendor runtime or weakening Ubuntu/MOS support.

**Phase 1 — Memory, missions, Observe/Propose.** Mission store, memory store and import,
supervisor and reference adapter in a contained read-only worker, mission board in dashboard and
Telegram. Exit: a real incident becomes a persistent mission whose contextualised session survives
a restart and whose result is visible in PE.

**Phase 2 — Scoped Repair.** Checkpoint, tripwires, grant/approval/standing grants, privileged
worker tier, hooks, independent verification, crash recovery. First mission: recreate the
dependents of a recreated network-namespace provider (the incident the operator's server session
fixed by hand; see the private export), then disk pressure with held-rollback settlement; the
canary-rollback config correction follows (§11 proof 7). Exit: the operator approves once (or has a standing grant), leaves it alone,
and sees evidence of success, safe failure, rollback, or a decision request.

**Phase 3 — Change missions.** Add/remove/reshape services with plan/diff checkpoints.

**Phase 4 — Proactive operations.** Ranked backlog, improvement missions, environment-aware
recommendations.

## 11. Definition of done: shippable v4 (Phases 0–2)

### 11.1 Promise

An operator can create or accept an incident-backed mission, grant one bounded Repair approval (or
rely on a standing grant), and leave PE to complete a repair inside one Compose-stack/service
scope. PE records what happened, runs the agreed verification, and returns an evidence-backed
outcome: completed, failed safely, rolled back, or needs operator input. v4 does not promise
unbounded autonomy.

### 11.2 Required capabilities

- Versioned, migrated mission store with the §4.2 states, leases with fencing, cancellation.
- Ship's Memory store with provenance, quarantine, review dates, supersession, and a generic
  import path.
- Mission creation, review, approval, cancellation, status, attach/takeover, artifacts and
  outcomes in the dashboard and Telegram.
- Public runtime-adapter contract, external-session import, and one documented reference adapter
  with no bundled skill pack and no required LLM vendor.
- Checkpoint/restore; Docker API proxy; tripwire monitor with pause/rollback; grant → credential/environment
  builder; standing grants; privileged worker tier with a dedicated Unix identity; hooks.
- Verification catalogue with pre-agreed checks; crash classification.
- Budgets (wall time, tool calls, output size, retries); redaction before persistence.

### 11.3 Release proof

1. v3 behaviour and existing installations pass their regression suites; missions disabled by
   default.
2. Checkpoint restores in-scope files and state exactly; refusal when backups are stale. The
   worker cannot reach Docker or root by any path (group, TCP socket, unmanaged sudo rule); the
   §6.8 preflight fails closed on each.
3. A planted protected-domain change (ingress, DNS file, credential file, firewall, data-volume
   shrink) during a mission pauses the session within one scan interval and offers rollback.
4. A session without protected-domain credentials cannot alter those domains.
5. A planted secret is absent from mission context, transcript, artifacts, events and memory; a
   hostile log line that tries to widen the grant has no effect on the grant.
6. A crash during investigation, execution and verification yields an honest resumable or
   recovery state with no replay of an unknown mutation.
7. A homelab-rehearsed repair: **a VPN-gateway recreate orphans the containers that join its
   network namespace; the mission recreates its dependents in dependency order** (a plain restart
   does not rebind the namespace), then passes an in-namespace application check and a
   compose-project completeness check. A second rehearsal: a failed canary rolled back, then an
   approved bounded config correction recreating only that service.
8. Fixture evaluations of three historical incident shapes: relevant memory retrieved, fact and
   unknown separated, valid structured output, no mutation under Observe/Propose.
9. Worker and repair flows rehearsed on Ubuntu/systemd and on MOS/sysvinit (local worker or the
   documented remote-worker path); neither silently degrades to an unmanaged session.
10. Setup, repair, undo, uninstall, upgrade and docs cover every worker, account, socket, secret,
    grant and boot-time component v4 adds. A v4-to-v3 downgrade is documented (snapshot restore).

### 11.4 Out of scope for v4

A bundled skill pack, private incident register, or one operator's memories as product data;
cross-host mutation (multi-host missions are Observe/Propose); fully autonomous novel Change or
Critical work; separate always-running Amy/Fry/Scruffy agent processes or an agent swarm; a
mandatory local LLM CLI, Node, vendor account, internet connection or vector database.

## 12. Crew roles (vocabulary, not architecture)

Leela observes; Hermes ranks work; Farnsworth owns goals, grants and closure; Amy investigates;
Bender executes and records change; Zoidberg owns update/recovery; Fry shapes change requests;
Scruffy owns memory and history. In v4 these are responsibilities inside the core and the mission
flow. They are not separate processes and not a mission field.

## 13. Portability and deployment

- Existing v3 installs keep working with missions off by default.
- Ubuntu/systemd and MOS/sysvinit are first-class. MOS root is RAM and PE lives on the data pool,
  so the worker account's home, credentials and checkpoint store must live on the pool and be
  recreated by setup. MOS has no `setfacl`; permissions use groups and plain bits.
- The remote-worker path uses a unix socket reached over SSH, not a new TCP listener.
- The worker must not reach PE's approval/decision RPC methods; it uses a separate socket with a
  per-principal method allowlist.
- No runtime installation requires an unreviewed broad sudo grant: the Repair-tier sudo scope is
  generated from the grant and shown to the operator at setup.
- Every new daemon, socket, account, grant, package and boot hook is in the setup plan, repair,
  uninstall and undo journal.

## 14. Operator decisions (recorded 2026-10-13)

Resolved; these are inputs to Phase 0, not open questions.

1. **Protected domains:** the default list (network/ingress/DNS, firewall, credentials and
   identities, persistent data volumes and storage, backup configuration, package management, host
   reboot). None may be touched under a Repair grant; each needs explicit per-change approval.
2. **Repair privilege:** raw Docker access is host root, so the default Repair worker reaches Docker
   only through a **scope-enforcing API proxy** (slice-0 spec §6.1) plus sudo generated from the grant (exact units and
   commands, shown at setup). A mission may be **escalated to broad sudo and raw Docker** only by an explicit
   per-mission approval, recorded in the audit trail. Broad sudo is never covered by a standing
   grant and never applies to an unattended start.
3. **Standing grants:** ship in v4.0, **off by default**. Never cover Change, Critical, broad sudo,
   or a backup-floor override.
4. **Egress:** redacted logs and configs may go to the operator's configured vendor. Secret files,
   `.env` contents and credential stores are never sent. Local-model runtimes are unrestricted.
5. **Backup floor:** a Repair on a stack with no verified recent backup is refused unless the
   operator records an explicit "no backup" acknowledgement for that mission. A standing grant
   cannot override it.
6. **First missions:** (a) canary-rollback config fix (the Repair proof); (b) stale NFS/mount
   diagnosis, propose-only; (c) backup restore validation, propose-only; (d) add a new service
   (Change; a Phase 3 preview, not a v4.0 release gate). The server export's gap list may refine
   the set.
7. **MOS and remote workers:** local dedicated-user worker on both Ubuntu and MOS, with the
   worker home, credentials and checkpoints on the data pool for MOS. Remote worker = unix socket
   forwarded over SSH under a dedicated key, no new TCP listener. If MOS cannot meet containment,
   fall back to remote worker or proposal-only; never to an unmanaged session.
8. **Runtime failure or contradiction:** a runtime outage mid-mission preserves state and the
   checkpoint and moves the mission to `needs_operator`, with no retry beyond the retry budget. A
   recommendation that contradicts live evidence or confirmed memory does not proceed: live
   evidence wins, the conflict is flagged on the mission, and the memory card is marked for
   review.

### 14.1 Open items raised by the server export (decide in Phase 0)

9. **Unauthenticated Docker TCP socket** (decided 2026-10-13): replace it with an **allowlisting
   socket proxy** that exposes only the read/discovery endpoints dashboard discovery and
   multi-host need; the raw daemon listens on the Unix socket only. The §6.8 preflight fails
   closed on any other TCP exposure.
10. **Backup floor definition** (decided 2026-10-13): the backup tool covers only the primary
    host's local application data and Docker data root. **It does not back up the NAS.** So the
    floor is scope-specific: a scope whose data is covered and recent passes; a scope whose data
    lives on the NAS has no coverage, so a Repair touching it is refused unless the operator
    records the "no backup" acknowledgement (decision 5). The first definition is *covered and
    recent*; restore-tested is shown on the card and is earned by the restore-validation mission.
    NAS backup is an operator project outside v4, but missions surface it as a standing finding.
11. **MOS parity.** Checkpoint and unit-state reads use systemd properties and fail on MOS. The
    checkpoint service must be provider-aware; until then MOS is proposal-only (decision 7's
    fallback applies).

## 15. What not to do

Do not rewrite the Python service. Do not create separate agent processes without a durable
output each. Do not make a vendor, Node or a cloud account mandatory. Do not store raw
transcripts as memory. Do not promote agent-proposed procedures to trusted memory without the
operator. Do not hide mutations behind optimistic labels or mark a mission complete on an agent's
word. Do not let dashboard work outrun the mission, memory and safety contracts.
