# Planet Express v4 — Mission Control and the Ship's Memory

**Status:** proposal for architectural review  
**Audience:** the operator, the Planet Express maintainers, and the server-side
remediation agent  
**Decision sought:** whether this is the right product direction and what must change
before implementation begins

## 1. Why this exists

Planet Express v3 is a capable monitoring and *bounded automation* system for a Docker
Compose homelab. It discovers incidents, records evidence, presents a dashboard and
Telegram interface, and can perform a deliberately small catalogue of verified,
approval-gated actions.

That is valuable infrastructure, but it does not meet the product's original purpose.
When a real incident, configuration change, installation, removal, or improvement is
needed, the operator currently leaves Planet Express and opens a Claude Code session
with the server skill pack. That session has the context, tools, judgement, and
flexibility to actually do the work. Planet Express has mostly become an attractive
reporting tool that hands off at the point where it should become most useful.

The target is not an unrestricted autonomous root agent. It is a persistent operations
system that can delegate meaningful work to capable agents while retaining the server's
context, the operator's intent, a clear authority boundary, a durable audit trail, and
real verification.

> **Farnsworth's job:** keep the server healthy, useful, secure, recoverable, and
> steadily improving, subject to the operator's priorities and constraints.

## 2. Product thesis

Planet Express should be the durable control plane for a crew of specialised agents. It
should work in a world where Claude Code, OpenCode, Hermes, and future agent runtimes
are useful tools, rather than pretending a fixed action catalogue can replace them.

The persistent Python service coordinates the work; it does not need to be the only
reasoning runtime. A runtime can investigate and remediate through the operator's
server skill pack, while Planet Express provides:

- continuously collected evidence and health baselines;
- a durable mission queue, priorities, and operator intent;
- environment-specific institutional memory;
- declared authority and approval boundaries;
- tool, command, patch, and decision records;
- post-change verification and re-scanning;
- a dashboard and Telegram record that survives any one agent session.

The product must get more capable over time because it learns the environment and
captures proven local procedures—not because it silently retrains an LLM or treats
unverified chat output as fact.

## 3. Keep the v3 foundation

This is an extension, not a rewrite. Python remains the control plane because it already
contains the expensive, host-specific work that should not be discarded:

- Ubuntu/systemd and MOS/sysvinit support;
- setup wizard, repair, uninstall, undo, boot restoration, and deployment knowledge;
- host-control and stack-orchestration provider seams;
- monitoring, incident state, SQLite persistence, scheduling, locking, Telegram,
  dashboard authentication, and local RPC;
- current typed runbooks, target binding, verification, canary updates, and safe pruning.

Typed runbooks remain the preferred mechanism for narrow, repetitive, proven automation.
They must not remain the boundary of all possible remediation. A mission is the broader
unit of work for changes that cannot honestly be reduced to a small, predeclared step
catalogue.

No agent runtime is mandatory for a supported Planet Express installation. The core must
continue to work on both Ubuntu and MOS without Node, Docker-in-Docker, a vendor SDK, or
cloud access. Runtimes are optional capabilities, configured separately.

The operator's present Claude Code workflow and its casaserver-specific skill pack are
**development and rehearsal inputs**, not a Planet Express product dependency. They may be
used to discover useful capabilities, procedures, fixtures, and edge cases while v4 is built,
but no released v4 contract, installation path, adapter API, or acceptance test may assume
their presence. A site that has no comparable skill pack must still be able to install the
core, configure a compatible worker or external session, and understand its limits.

## 4. The crew as real responsibilities

The crew names should correspond to distinct responsibilities and feedback loops, not
merely stages in a single scripted pipeline.

| Crew member | Responsibility | Durable output |
|---|---|---|
| Leela | Observe host, stack, dependency, backup, and performance state; establish baselines. | Evidence and signals |
| Hermes | Turn evidence into incidents, maintenance debt, risks, and improvement opportunities; rank the work. | Operations backlog |
| Farnsworth | Own goals, priorities, delegation, authority checks, escalations, and mission closure. | Missions and decisions |
| Amy | Investigate unfamiliar failures; research documentation and alternatives. | Diagnosis and options |
| Bender | Perform audited host/tool operations and collect post-change evidence. | Change record and verification |
| Zoidberg | Manage update, recovery, and rollback experiments. | Update/recovery outcomes |
| Fry | Translate requests to add, remove, or reshape services into implementation missions. | Change proposals |
| Scruffy | Preserve, version, retrieve, and present operational state and knowledge. | Ship's Memory and audit history |

One agent need not equal one process or one model. The important rule is that each role has
an explicit responsibility, relevant tools/evidence, a durable output, and a way to learn
from the result.

## 5. The mission model

An incident, an operator request, routine maintenance, or an improvement idea becomes a
**mission**. It is the durable unit of delegation and accountability.

### 5.1 Mission fields

```text
id, title, origin, priority, status
goal and success criteria
scope: host / mount / stack / service / paths / network dependencies
current evidence and referenced snapshots
relevant Ship's Memory cards
constraints and maintenance window
authority level and approval record
assigned crew role and runtime adapter
live event stream, artifacts, commands, diffs, and decisions
verification plan and final verification evidence
outcome, rollback/next steps, and memory candidates
```

Suggested state machine:

```text
draft -> queued -> investigating -> awaiting_decision -> executing -> verifying
      -> completed | failed | cancelled | blocked | needs_operator
```

Missions are resumable. A runtime crash or a CLI session ending must not erase the goal,
the evidence already gathered, the last safe state, or the operator's decision.

### 5.2 Examples

- **Incident:** "Restore Paperless after its database dependency stopped. Do not touch
  public ingress or delete data."
- **Operator request:** "Add Paperless to the services stack, with backups, a healthcheck,
  route, and a clean rollback plan."
- **Improvement:** "Investigate why media indexing regularly saturates the NAS and propose
  an improvement that preserves backup windows."
- **Maintenance:** "Validate a Borg restore path that has not been tested in 90 days."

## 6. Authority and approvals

The core decision is to approve useful scopes of work rather than requiring the operator to
babysit every command. Every mission has one of these authorities:

| Authority | What a runtime may do | Approval |
|---|---|---|
| Observe | Inspect bounded host state, logs, config, and documentation. | Automatic |
| Propose | Produce diagnosis, command proposals, patches, diffs, and rollback plan. | Automatic |
| Repair | Make bounded repairs in the explicitly declared scope. | One mission-level approval |
| Change | Add/remove services or make configuration/Compose changes. | Approve reviewed plan/diff |
| Critical | Touch ingress/DNS, credentials, identities, storage, destructive operations, or other protected domains. | Explicit per-change approval |

**Recommendation:** a Repair mission gets a single explicit approval for all mutations
inside its declared scope. Change and Critical work retain stronger checkpoints. This keeps
the operator in control without recreating the current handoff and command-by-command
friction.

The authority grant must name its limits in human language and structured form, for example:

> Restore the `media` stack. You may restart services and edit its Compose files. Do not
> affect `network`, credentials, public routes, or persistent data; do not run during the
> backup window.

## 7. Agent runtimes and the tool gateway

### 7.1 Runtime adapters

Farnsworth dispatches to an adapter rather than embedding a specific vendor or CLI.

```python
class AgentRuntime(Protocol):
    def start(self, mission: Mission, context: MissionContext, authority: Authority) -> RunHandle: ...
    def status(self, handle: RunHandle) -> AgentUpdate: ...
    def cancel(self, handle: RunHandle) -> None: ...
    def collect(self, handle: RunHandle) -> AgentResult: ...
```

The first adapter should support the operator's existing Claude Code + server skill-pack
workflow. It must receive a generated mission brief, not a blank prompt. OpenCode, Hermes,
or a remote worker can implement the same contract later.

A runtime is a specialist, not the source of truth. Planet Express owns mission state,
approval state, audit records, and verification.

### 7.2 Tool gateway

Agents must be able to do real work, but Planet Express should broker the access. The tool
gateway records identity, mission, authority, inputs, outputs, and relevant pre/post state.

Initial capabilities:

- read Docker/Compose/service/mount/network/backup state;
- bounded logs and configuration reads;
- documentation and web research where configured;
- patch/diff operations on mission-authorised files;
- Compose and service actions;
- an explicit, audited `argv` execution capability within mission scope;
- post-change scans and verification requests.

Known typed actions should still take the typed engine path. The gateway supports the novel
work that typed actions cannot express.

This intentionally retires the absolute claim that "there is no shell anywhere." That claim
was correct for an LLM writing shell commands directly inside the core daemon. It is not a
realistic description of a capable remediation system using coding-agent runtimes. The new
guarantee is stronger and more honest:

> Planet Express never silently grants an agent authority. Every tool use belongs to a known
> mission, identity, scope, approval, audit record, and verification outcome.

## 8. The Ship's Memory

The missing component is durable, evidence-backed institutional knowledge of this particular
environment. Without it, an agent resolves an NFS failure today and rediscovers it from zero
next month, or proposes an attractive redesign that conflicts with an intentional local
constraint.

### 8.1 Memory types

| Type | Purpose | Example |
|---|---|---|
| Fact | A discovered or declared environmental truth. | `/mnt/media` backs the media stack. |
| Fragility | A known failure mode or limitation. | NFS can become stale while container health is green. |
| Procedure | A local, proven method. | Recovery order for stale `/mnt/media`. |
| Decision | A deliberate trade-off or deferred work. | Keep NFS until the storage migration project. |
| Preference | Operator intent and working style. | Prefer maintenance windows; do not babysit routine repairs. |
| Lesson | What a mission established or invalidated. | A version upgrade changed the recovery order. |

Memory is not an unbounded chat transcript or an unreviewed vector-store. It is a versioned,
inspectable set of structured cards with bounded text and evidence references.

### 8.2 Memory card contract

```text
id, title, type, scope/tags
statement and recommended procedure/constraint
evidence links: missions, snapshots, configuration or documents
source: observed | operator-stated | researched | inferred
confidence: candidate | corroborated | verified | deprecated
created_at, last_verified_at, review_after
supersedes / conflicts_with
owner and edit history
```

Example:

```text
title: NFS stale-handle recovery for /mnt/media
type: fragility + procedure
scope: mount:/mnt/media, stacks:media,services
statement: File access can fail while Docker health remains green.
procedure: stop affected services, restore the mount, then restart in the recorded order.
constraint: do not remount during an active backup.
evidence: incident M-41, incident M-67
confidence: verified
review_after: next storage migration or 2027-01-13
```

Current host observations always outrank memory. A memory card supplies context and a
hypothesis; it is never standalone permission to mutate a host.

### 8.3 Learning loop

At mission close, the assigned runtime proposes structured memory candidates:

1. What was observed?
2. What changed?
3. Did verification prove success or failure?
4. What environment fact, fragility, procedure, decision, or preference was learned?
5. Does it revise or invalidate existing knowledge?

Farnsworth/Scruffy validate provenance and conflicts. Low-risk observations can be stored as
candidate facts. A procedure becomes corroborated only after repeat successful use or operator
confirmation. Operator decisions and preferences are always explicitly attributed to the
operator. Stale cards should trigger review rather than becoming silent folklore.

For the first version, use the existing SQLite store plus full-text search and structured scope
filters. Do not add a vector database until ordinary retrieval has demonstrably failed. Semantic
retrieval can be added as an optional index later; it must not become the operational source of
truth.

## 9. Delivery plan

### Phase 0 — Contract and threat-model design

Write and review the following before code changes:

- mission schema, state machine, resume/cancel semantics, and retention;
- authority scopes, protected domains, approval UX, and escalation rules;
- runtime-adapter protocol and lifecycle;
- tool-gateway API, identity propagation, audit records, and error semantics;
- Ship's Memory schema, retrieval, conflict, expiry, and promotion rules;
- local/remote worker model for Ubuntu and MOS;
- test and deployment strategy.

**Exit criterion:** a reviewed design makes it possible to implement the first slice without
choosing a vendor-specific runtime or weakening current host support.

### Phase 1 — Ship's Memory and read-only/proposal missions

Implement the store, mission board, Telegram status, full-text retrieval, and a generic
Observe/Propose runtime-adapter path. The first *reference* adapter may target a configured
agent CLI, but it must receive its capabilities and mission context through the published
adapter contract rather than assume a casaserver-specific skill pack.

The adapter receives an incident/operator request, live evidence, relevant memory, constraints,
and success criteria. It returns a diagnosis, evidence, suggested action, verification plan,
and memory candidates. It changes nothing. During development, existing privileged sessions
may be attached manually to missions to seed fixtures and memory; that transition practice is
not a v4 feature claim.

**Exit criterion:** a real server incident can become a persistent mission whose contextualised
Claude session is recoverable after a restart and whose results are visible in PE.

### Phase 2 — Scoped Repair missions

Implement the tool gateway and one-approval Repair authority. Begin with bounded repair scopes:

- restore a failed Compose service;
- correct a configuration issue inside one named stack;
- remediate disk pressure;
- resolve a mount/dependency failure;
- correct a backup job configuration.

Every mission must trigger a fresh Leela scan and record the verification result. Failure does
not become a green card because an agent said it was fixed.

**Exit criterion:** the operator can approve a real repair mission once, leave it alone, and see
evidence that it succeeded, failed safely, or needs a decision.

### Phase 3 — Change missions

Support operator requests to add, remove, or reshape services. Fry produces an implementation
plan; the runtime investigates and prepares a reviewed diff; Bender applies and verifies it.

**Exit criterion:** one non-trivial service can be added and one removed through an audited,
reversible mission on a test host, then rehearsed on the live fleet.

### Phase 4 — Proactive operations and improvement

Hermes maintains a ranked, deduplicated backlog across incidents, reliability debt, restore
confidence, security/configuration drift, capacity/waste, obsolete services, and improvement
opportunities. Farnsworth presents a useful queue of missions rather than a stream of alerts.

**Exit criterion:** PE can recommend meaningful, environment-aware work and explain why it
matters, while respecting explicit operator priorities and maintenance windows.

## 10. Definition of done: shippable v4

v4 is shippable only when it delivers a portable **Mission Control** product, not merely a
record of one host's transition from manual Claude sessions. It includes Phases 0 through 2;
proactive improvement (Phase 4), cross-host mutations, and a collection of independent crew
processes are later releases.

### 10.1 Product promise

An operator can create or accept an incident-backed mission, grant one bounded Repair approval,
and leave Planet Express to complete a repair inside one Compose-stack/service scope. PE records
what happened, runs the verification checks agreed before the mutation, and returns an evidence-
backed outcome: completed, failed safely, rolled back, or needs operator input.

v4 does **not** promise autonomous arbitrary host administration. It promises a durable mission
system, evidence-backed local memory, pluggable agent delegation, and contained execution of
bounded repairs.

### 10.2 Required capabilities

- A versioned, migrated Mission store with the full state model, including lease expiry,
  cancellation, `partially_applied`, `rollback_pending`, and `recovery_required` states.
- A canonical Ship's Memory store with provenance, scopes, expiry/review dates, optional
  read-only validity probes, conflict/supersession handling, and a safe import path for an
  operator's existing notes. No automatic bidirectional sync with arbitrary notes or agent
  memories.
- Mission creation, review, approval, cancellation, status, artifacts, and outcomes in both
  the dashboard and Telegram control surface.
- A public runtime-adapter contract, an external/manual-session import path, and at least one
  documented reference agent-worker adapter. The adapter must not require a bundled skill pack
  or a particular LLM vendor.
- A contained local-worker mode: dedicated Unix identity; no Docker socket/group; no sudo;
  no direct write access to managed host resources; gateway-only mutation access. Its install is
  optional, but its security model is mandatory whenever it is enabled.
- A tool gateway with audited, scope-checked repair primitives for one stack/service: selected
  Compose operations, guarded file patches in authorised paths, and approved service operations.
  It must not expose an unbounded privileged shell under Repair authority.
- Pre-approved, versioned verification checks. A mission cannot be marked completed solely from
  an agent assertion or a generic scan.
- Mission budgets for wall time, tool calls, output/artifact size, retries, and provider usage
  where supplied by the runtime. Redaction applies before persistence to mission records,
  runtime output, artifacts, and memory candidates.
- Resource-scoped mutation leases. Read-only monitoring continues while a mission runs; a
  conflicting canary, prune, or mutation is refused/deferred with a clear reason.

### 10.3 Release proof

The release gate requires all of the following:

1. Existing v3 behaviour and existing installations pass their relevant regression suites.
2. A worker process is proven unable to access Docker or sudo directly, or write a protected
   managed path, outside the tool gateway.
3. The tool gateway rejects attempts outside the mission scope and records every accepted
   mutation before dispatch.
4. A planted secret is absent from mission context, streamed output, saved artifacts, events,
   and Ship's Memory.
5. A process crash during investigation, execution, and verification produces an honest
   resumable/recovery state without replaying an unknown mutation.
6. Fixture-based evaluations for three historical incident shapes demonstrate relevant-memory
   retrieval, correct fact/unknown separation, valid structured output, and no mutation from
   Observe/Propose authority. Exact LLM phrasing is not an acceptance criterion.
7. A real or homelab-rehearsed single-service repair mission succeeds: a failed canary has been
   rolled back, the mission applies an approved bounded Compose/configuration correction,
   recreates only that service, and passes the pre-approved application-level check.
8. The contained-worker and repair flows are rehearsed on both Ubuntu/systemd and MOS/sysvinit,
   using a local compatible worker or the documented remote-worker path. Neither host type may
   silently degrade to a privileged, unmanaged session.
9. Setup, repair, undo, uninstall, upgrade, and documentation cover every optional worker,
   account, socket, secret, grant, and boot-time component introduced by v4.

### 10.4 Explicitly out of scope for v4

- A bundled casaserver skill pack, private incident register, or migration of one operator's
  agent memories as product data.
- An unrestricted root/Bash agent operating under Repair authority.
- Cross-host mutations and distributed transactions; multi-host missions are Observe/Propose
  only until their own design and rehearsal work is complete.
- Fully autonomous novel remediation. All novel Repair work remains mission-approved; only
  existing narrowly defined v3 automation stays automatic.
- Separate always-running Amy/Fry/Scruffy agent processes, broad proactive optimisation, or an
  agent swarm. These may follow once missions demonstrate a real need.
- A mandatory local LLM CLI, Node runtime, vendor account, internet connection, or vector
  database.

## 11. Portability and deployment requirements

- Existing v3 installations continue to function with all mission features disabled by default.
- Ubuntu/systemd and MOS/sysvinit remain first-class test targets.
- A local agent runtime is optional. A MOS host can use a remote worker or proposal-only mode.
- No runtime may require an unreviewed broad sudo grant just to be installed.
- Runtime configuration/secrets remain separate from topology and are redacted from records.
- Every new daemon, socket, account, grant, package, and boot hook is represented in the setup
  plan, repair path, uninstall path, and undo journal.

## 12. What not to do

- Do not rewrite the Python service or discard the installer/provider work.
- Do not implement a theatrical multi-agent swarm whose roles have no durable ownership.
- Do not make a specific LLM vendor, Node, or cloud account a mandatory dependency.
- Do not put raw transcripts into permanent memory and call that learning.
- Do not grant agents silent root access or hide mutation behind optimistic labels.
- Do not require approval for each routine command inside an already-approved Repair mission.
- Do not let UI/dashboard work outrun the mission, memory, and execution contracts.

## 13. Measures of success

The product is succeeding when:

- a real incident becomes a mission rather than a separate, forgotten terminal conversation;
- the agent begins with relevant local constraints and prior successful remedies;
- an approved repair completes without command-by-command operator babysitting;
- PE independently proves whether the repair worked;
- repeated incidents produce better procedures, not repeated rediscovery;
- an operator can ask to add/remove/improve something and receive a durable, auditable outcome;
- MOS and Ubuntu installations remain supportable and reversible.

## 14. Questions for review

The following questions need explicit answers before implementation:

1. Is one scoped approval the correct default for Repair missions? What must always escalate?
2. What is the initial trust boundary for a local Claude Code worker: dedicated Unix user,
   remote worker, or both?
3. Which protected domains are non-negotiable on this fleet (network/ingress, data volumes,
   backups, credentials, package management, host reboots, firewall, identity)?
4. What exact skills/tools does the existing server-side Claude workflow rely on, and which
   should move behind the PE tool gateway first?
5. What are the first three real missions that would prove the system useful?
6. Which existing fixes/workarounds should seed the Ship's Memory—especially NFS, backup,
   boot, network, and service-order knowledge?
7. How should a remote worker authenticate, receive bounded context, and return artifacts
   without exposing host secrets?
8. What should happen when a runtime is unavailable mid-mission, or its recommendation is
   inconsistent with live evidence or existing memory?

## 15. Requested reviewer response

Review this as a skeptical principal engineer and experienced homelab operator. Do not merely
summarise it. Identify:

1. Incorrect assumptions about the existing Planet Express architecture or MOS/Ubuntu deployment.
2. Missing security, reliability, privacy, cost, or operational constraints.
3. Places where the plan creates agent-theatre rather than practical capability.
4. The smallest credible Phase 1 and its concrete acceptance tests.
5. The first genuinely useful Phase 2 Repair mission to implement.
6. Recommended changes to the mission, tool-gateway, and Ship's Memory contracts.
7. Any reason Python should *not* remain the control plane.

Give recommendations in priority order and distinguish blockers from follow-up improvements.
