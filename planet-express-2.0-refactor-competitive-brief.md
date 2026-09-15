# Planet Express 2.0 Refactor & Competitive Architecture Brief

**Project:** Planet Express  
**Repository:** https://github.com/sovereignalmida/planet-express  
**Purpose:** Refactor guidance and competitive architecture brief for the next major development cycle.  
**Date:** 2026-09-13

---

## Executive Summary

Planet Express sits in an emerging category of self-hosted AI infrastructure management tools, but it still has a distinct and defensible position.

The closest current projects are:

- **Steward** — broad autonomous IT/network operations platform
- **homelab-ai** — AI control plane for Docker/homelab applications
- **OnCallMate** — Docker + Telegram + AI incident investigation
- **SRE Agent** — agentic SRE workflow with explicit human approval
- **ARGOS** — broad autonomous homelab infrastructure manager
- **WAGMIOS** — secure Docker control plane designed for AI agents
- **HolmesGPT / K8sGPT** — mature AI-SRE tooling, primarily Kubernetes-centric

None is simply "Planet Express already built under another name."

The strongest strategic position for Planet Express is:

> **A safety-bounded autonomous sysadmin for Docker Compose. It watches your server, diagnoses failures, proposes fixes, asks before making changes, executes through an independent safety layer, verifies the result, remembers what happened, and updates containers one at a time with automatic rollback.**

The critical design principle to preserve and strengthen is:

> **AI proposes intent. Deterministic software decides whether and how that intent becomes a host mutation.**

The refactor should make Planet Express **more opinionated, more deterministic, and safer**, not broader.

---

# 1. Competitive Landscape

## 1.1 Steward

Repository:

https://github.com/braedonsaunders/steward

### Why it matters

Steward is the closest direct architectural competitor found.

It independently converges on several concepts also present in Planet Express:

- Telegram approvals
- risk-gated remediation
- rollback
- Docker/systemd recovery
- persistent investigations
- local-first operation
- incident tracking
- recommendations and approvals
- automation tiers
- quarantine after repeated failures

Its conceptual loop is roughly:

```text
discover
  ↓
understand
  ↓
act
  ↓
learn
```

Steward goes much further into a general IT control plane:

- network discovery
- SSH
- SNMP
- WinRM
- NAS management
- topology mapping
- missions
- subagents
- encrypted credentials
- remote desktop
- enterprise-style policy
- generalized adapters and packs

### What Planet Express should borrow

- persistent incidents
- durable investigations
- policy-based execution
- action-risk classification
- repeated-failure quarantine
- verification as part of remediation
- execution history

### What Planet Express should reject

Do not follow Steward into:

- network autodiscovery
- SNMP management
- Windows management
- remote desktop
- browser automation
- LDAP/OIDC enterprise IAM
- graph databases
- generic workflow automation
- arbitrary subagents
- general remote terminal functionality
- plugin marketplace complexity

Steward is solving the "autonomous IT management platform" problem.

Planet Express should solve a narrower problem exceptionally well.

---

## 1.2 homelab-ai

Repository:

https://github.com/JeremiahM37/homelab-ai

### Why it matters

This is probably the closest competitor on the actual Docker homelab axis.

Its model is essentially:

> Make every homelab application AI-callable.

It supports:

- service health monitoring
- automatic repair
- application plugins
- MCP
- AI chat
- local models
- persistent failure memory
- multiple remediation tiers
- direct tooling for homelab services

Its extension interface is intentionally simple, centered around ideas like:

```text
health()
restart()
tools()
```

### Strengths worth borrowing

- simple adapter/plugin contracts
- MCP support
- OpenAI-compatible/local model support
- basic persistent failure memory
- straightforward homelab UX

### What Planet Express should NOT copy

Do not become an app-control assistant such as:

```text
"Search Sonarr for a movie and send it to qBittorrent."
```

That is a different product.

Also avoid the model of letting a powerful LLM freely edit files merely because backups are taken first.

Planet Express's current pattern is stronger:

1. AI proposes the change.
2. The real file remains untouched.
3. Human approval is obtained.
4. Path containment and safety are checked again at execution time.
5. Only then is the mutation applied.

That philosophy should be preserved.

---

## 1.3 OnCallMate

Repository:

https://github.com/ismailperim/oncallmate

### Relevant overlap

- Docker-focused
- Telegram
- AI investigation
- Docker socket proxy
- audit trail
- security-conscious design

Approval workflows have historically been less mature than Planet Express's current architecture.

Planet Express should remain ahead here by treating approvals and the safety boundary as core architecture rather than an optional feature.

---

## 1.4 SRE Agent

Repository:

https://github.com/alparn/sre-agent

### Relevant overlap

Its agentic state model is roughly:

```text
OBSERVE
  ↓
REASON
  ↓
ACT
  ↓
LEARN
```

It includes explicit human approval gates.

However, it primarily targets:

- Prometheus
- Grafana
- Kubernetes
- production SRE environments

Planet Express targets ordinary self-hosted Docker Compose infrastructure.

That remains a meaningful niche.

---

## 1.5 ARGOS

Repository:

https://github.com/DarkAngel-agents/argos

### Relevant overlap

ARGOS aims at autonomous management across:

- Docker / Swarm
- NixOS
- PostgreSQL HA
- Proxmox
- UniFi
- broader homelab infrastructure

It is strategically interesting, but much broader than Planet Express.

Avoid competing on infrastructure breadth.

---

## 1.6 WAGMIOS

Repository:

https://github.com/mentholmike/wagmios

### Relevant overlap

WAGMIOS is a secure Docker control plane intended to be operated by AI agents.

The key overlap is the concern that AI should not get unrestricted Docker access.

Its granular API scope concept supports Planet Express's direction toward typed actions and controlled execution.

However, WAGMIOS is not primarily an autonomous:

```text
monitor
  ↓
diagnose
  ↓
propose
  ↓
approve
  ↓
remediate
  ↓
verify
```

system.

---

## 1.7 HolmesGPT / K8sGPT

HolmesGPT:

https://github.com/HolmesGPT/holmesgpt

K8sGPT:

https://github.com/k8sgpt-ai/k8sgpt

These validate the broader AI-SRE category.

They are not direct replacements because their strongest orientation is Kubernetes and production cluster operations.

The category is established.

The Docker Compose homelab implementation space is still comparatively open.

---

# 2. Product Positioning

Do not position Planet Express simply as:

> An AI homelab agent.

That phrase is becoming crowded.

Prefer something closer to:

> **A safety-bounded autonomous sysadmin for Docker Compose.**

Expanded positioning:

> Planet Express watches the host, detects infrastructure problems, diagnoses failures, proposes remediation, requests approval when policy requires it, executes through an independent safety boundary, verifies recovery, remembers incident history, and safely updates containers with automatic rollback.

Another useful statement:

> **Planet Express is not an LLM with a Docker socket.**

Its value comes from:

- deterministic monitoring
- controlled planning
- explicit policy
- independent execution safety
- verification
- rollback
- durable incident memory

---

# 3. Key Competitive Advantage

## Bender is the security boundary, not the LLM

This is one of Planet Express's strongest architectural choices.

The desired model is:

```text
LLM
  ↓
proposes intent
  ↓
Policy Engine
  ↓
Bender
  ↓
constructs and executes approved operation
```

Not:

```text
LLM
  ↓
generates arbitrary shell
  ↓
hope validation catches everything
```

The executor should never "trust" the planner.

This distinction should become even stronger during the refactor.

---

# 4. Current Refactor Pressure

The current architecture shows clear signs of responsibility accumulation.

At the time of review:

- `casa_farnsworth.py` is roughly 1,600+ lines
- `casa_bender.py` is roughly 800+ lines

Farnsworth has accumulated responsibilities including:

- orchestration
- planning
- Telegram interaction
- state transitions
- operational commands
- approval flow management

Bender has accumulated:

- shell inspection
- safety policy
- sudo checks
- path validation
- forbidden-command detection
- systemd restrictions
- execution

The refactor should restore strong role boundaries.

Conceptually:

> **Leela monitors. Farnsworth plans. Bender executes.**

They should not become miscellaneous utility buckets.

---

# 5. Target Architecture

The current conceptual pipeline is roughly:

```text
snapshot
  ↓
findings
  ↓
plan
  ↓
approval
  ↓
commands
```

The recommended target is:

```text
Observation
    ↓
Finding
    ↓
Incident
    ↓
Remediation Plan
    ↓
Policy Decision
    ↓
Approval if required
    ↓
Typed Actions
    ↓
Execution
    ↓
Verification
    ↓
Resolved / Failed / Quarantined
```

The most important new concepts are:

- **Incident**
- **Policy**
- **Typed Action**
- **Verification**
- **Quarantine**

---

# 6. Incident Lifecycle

Recommended incident lifecycle:

```text
OPEN
  ↓
DIAGNOSING
  ↓
PLANNED
  ↓
AWAITING_APPROVAL
  ↓
EXECUTING
  ↓
VERIFYING
  ├── RESOLVED
  ├── FAILED
  └── QUARANTINED
```

This is much more useful than treating each monitor pass as an isolated event.

The UI should eventually show:

```text
Incident #42

Service:
  sonarr

First seen:
  2026-09-11 14:03

Occurrences:
  4

Previous remediation:
  restart             SUCCESS
  restart             FAILED
  recreate            SUCCESS

Current state:
  OPEN

Current diagnosis:
  healthcheck failing after dependency timeout
```

---

# 7. P0 — Durable Incident Memory

Introduce SQLite.

Do **not** remove the JSON state files.

Keep files such as:

```text
latest_monitor.json
latest_findings.json
run_status.json
```

They remain useful as:

- inspectable snapshots
- debugging artifacts
- exports
- latest-state caches

Add:

```text
planetexpress.db
```

Suggested initial tables:

```text
incidents
findings
plans
approvals
action_runs
verification_runs
events
```

Possible additional tables later:

```text
diagnostic_runs
rollback_runs
service_state_history
update_runs
```

## Incident fingerprinting

Recurring failures should map back to the same incident where appropriate.

Possible fingerprint:

```text
stack
+
service
+
finding_type
+
normalized_error_signature
```

For example:

```text
media + sonarr + unhealthy + dependency_timeout
```

Instead of creating dozens of unrelated alerts, Planet Express should know:

```text
Incident #42

First seen: Sep 11
Occurrences: 4
Last seen: Sep 13
```

This persistent operational memory is more immediately useful than adding a generic vector/RAG system.

---

# 8. P0 — Typed Actions

This should be one of the highest-priority changes.

## Problem

The current basic model is still conceptually:

```text
LLM generates shell command
        ↓
Bender tries to determine whether it is safe
```

Even with strong defensive validation, arbitrary shell is a huge semantic surface.

## Recommended model

Farnsworth emits typed intent:

```json
{
  "action": "docker.restart_service",
  "stack": "media",
  "service": "sonarr"
}
```

Not:

```bash
docker compose -f /some/path/docker-compose.yml restart sonarr
```

Bender receives the typed action and constructs the command itself.

## Initial typed actions

Possible action registry:

```text
docker.restart_service
docker.stop_service
docker.start_service
docker.recreate_service

docker.compose_up
docker.compose_down_safe
docker.pull_service

systemd.restart
systemd.start
systemd.stop_safe

mount.restart
mount.verify

compose.apply_diff

docker.image_prune_safe

borg.run_backup
borg.verify_backup
```

The exact list should stay intentionally small.

## Safety impact

This changes the trust model from:

```text
parse arbitrary shell
```

to:

```text
validate structured intent
```

That is a major security improvement.

## Arbitrary shell

If shell access remains available for diagnostics, it should be:

- read-only
- explicitly scoped
- heavily constrained
- primarily owned by Amy
- unavailable for normal mutating remediation

Long term, mutating arbitrary `shell.exec` should be eliminated.

---

# 9. P0 — Policy Engine

Separate policy from execution.

Bender should execute.

A policy engine should answer:

> Is this action allowed, denied, automatic, or approval-gated?

Suggested risk classes:

```text
R0 — Observation only
     automatically allowed

R1 — Bounded and reversible
     optionally automatic

R2 — Service-impacting
     approval normally required

R3 — Configuration/filesystem mutation
     approval + backup/diff + rollback required

R4 — Destructive/cross-boundary/root-risk
     forbidden
```

Examples:

```text
inspect logs               R0
inspect container state    R0

restart container          R1

recreate service           R2
restart systemd service    R2

apply compose diff         R3
modify managed config      R3

rm -rf /
disk partition mutation
unknown sudo command       R4
```

User autonomy policy could look like:

```yaml
autonomy:
  r0: auto
  r1: auto
  r2: approve
  r3: approve
  r4: deny
```

This makes autonomy explicit and user-configurable.

---

# 10. P0/P1 — Failure Quarantine

Repeated failed remediation must stop escalating automatically.

Example rule:

```text
3 failed remediation attempts
within 60 minutes
```

causes:

```text
Incident → QUARANTINED
```

After quarantine:

- automatic mutation stops
- Amy may continue diagnosis
- read-only observation continues
- user is notified
- future mutations require manual intervention or explicit override

This prevents loops such as:

```text
restart
restart
restart
restart
restart
```

and provides a proper fail-safe behavior.

---

# 11. P0 — Verification Engine

An action is not successful merely because:

```text
exit code == 0
```

An action is successful because the service returned to its desired state.

Every remediation plan should include:

```text
action
expected_result
verification
rollback
```

Example:

```yaml
action:
  type: docker.restart_service
  stack: media
  service: sonarr

verify:
  - container_running
  - docker_health == healthy
  - http_status == 200

timeout: 90

rollback:
  type: optional_previous_state
```

Potential verification checks:

```text
container_running
container_health
http_status
tcp_connect
systemd_active
mount_present
filesystem_writable
dependency_reachable
log_absence
process_running
```

Planet Express already has an important proof of concept here:

> Zoidberg already follows a canary + stabilization + rollback philosophy.

Generalize that behavior across normal remediation.

---

# 12. Zoidberg as a Strategic Differentiator

Zoidberg is more important than it may initially appear.

The one-service-at-a-time update pattern:

```text
pull/update one service
      ↓
watch health
      ↓
verify stabilization
      ↓
continue
```

with:

```text
automatic rollback on failure
```

is a strong differentiator.

Do not turn updates into:

```text
docker compose pull
docker compose up -d
```

for the entire stack at once.

Preserve the canary philosophy.

Longer term, reuse the same verification engine for:

- remediation
- updates
- config changes
- dependency restarts

---

# 13. P1 — Adapter Interface

Borrow the simplicity of homelab-ai's plugin model, but keep the adapter layer infrastructure-oriented.

Suggested interface:

```python
class Adapter:
    def observe(self):
        ...

    def diagnose(self):
        ...

    def verify(self):
        ...

    def actions(self):
        ...
```

Potential built-ins:

```text
DockerAdapter
ComposeAdapter
SystemdAdapter
FilesystemAdapter
MountAdapter
BorgAdapter
TraefikAdapter
AdGuardAdapter
NFSAdapter
```

Future optional adapters:

```text
ProxmoxAdapter
UnraidAdapter
ZFSAdapter
ResticAdapter
NUTAdapter
SMARTAdapter
```

## Important boundary

Do not turn this into application automation for every self-hosted app.

Avoid first-class operational adapters like:

```text
SonarrTool
RadarrTool
PlexTool
ImmichTool
```

unless they exist strictly for health/diagnostics.

Planet Express is an infrastructure sysadmin, not a media-management assistant.

---

# 14. P1 — MCP

MCP is worth adding, but only after the safety model is clean.

Useful read-first MCP operations:

```text
get_status
list_incidents
get_incident
diagnose_incident
list_plans
get_plan
get_update_history
get_backup_status
get_recent_events
```

Mutation should NOT directly expose:

```text
restart_container()
```

Instead expose:

```text
propose_restart()
```

or:

```text
request_remediation()
```

which returns:

```text
plan_id
risk_class
approval_required
status
```

The path remains:

```text
MCP
 ↓
Farnsworth
 ↓
Policy
 ↓
Approval if required
 ↓
Bender
 ↓
Verification
```

There must be no alternate privileged path.

A strong product principle is:

> **Every interface uses the same safety boundary.**

This includes:

- Telegram
- Scruffy web UI
- MCP
- CLI
- future API clients

---

# 15. P1 — LLM Provider Abstraction

Current provider support should evolve toward three logical provider types:

```text
openai
anthropic
openai-compatible
```

`openai-compatible` effectively unlocks:

- Ollama
- LiteLLM
- OpenRouter
- vLLM
- LM Studio
- Groq-compatible endpoints
- many future local or hosted providers

Do not build many bespoke provider integrations when a compatible API abstraction covers them.

Provider capabilities should be explicit.

Example:

```python
capabilities = {
    "tool_use": True,
    "web_search": False,
    "vision": False,
    "structured_output": True
}
```

Amy's advanced diagnostic tooling can select based on capabilities rather than provider name.

---

# 16. P1 — Integration-Test Homelab

Current logic tests are useful, but the project increasingly needs real integration coverage.

Create a disposable Compose test environment containing intentionally broken services.

Suggested fixtures:

```text
healthy-service

crash-loop-service

unhealthy-healthcheck

dependency-failure

bad-mount-simulator

slow-start-service

update-breaks-health

bad-config-service

network-timeout-service
```

Integration tests should exercise:

```text
observe
 ↓
detect
 ↓
incident creation
 ↓
diagnose
 ↓
plan
 ↓
policy
 ↓
approve
 ↓
execute
 ↓
verify
 ↓
resolve / fail / rollback / quarantine
```

This becomes critical after typed actions are introduced.

---

# 17. Recommended Package Structure

A possible post-refactor layout:

```text
planet_express/
│
├── core/
│   ├── models.py
│   ├── incidents.py
│   ├── events.py
│   ├── state.py
│   └── database.py
│
├── monitoring/
│   └── leela.py
│
├── analysis/
│   └── hermes.py
│
├── planning/
│   ├── farnsworth.py
│   └── planner.py
│
├── execution/
│   ├── bender.py
│   ├── actions.py
│   ├── policy.py
│   ├── verifier.py
│   └── rollback.py
│
├── diagnostics/
│   └── amy.py
│
├── updates/
│   └── zoidberg.py
│
├── adapters/
│   ├── base.py
│   ├── docker.py
│   ├── compose.py
│   ├── systemd.py
│   ├── mounts.py
│   └── borg.py
│
├── integrations/
│   ├── telegram.py
│   ├── mcp.py
│   └── notifier.py
│
├── web/
│   └── scruffy.py
│
└── llm/
    ├── base.py
    ├── openai.py
    ├── anthropic.py
    └── openai_compatible.py
```

This is a direction, not a requirement that every file be created immediately.

The key is responsibility separation.

---

# 18. Suggested Core Models

A refactor should centralize domain types instead of passing loose dictionaries everywhere.

Possible conceptual models:

```python
Observation
Finding
Incident
Diagnosis
RemediationPlan
Action
PolicyDecision
Approval
ExecutionResult
VerificationResult
RollbackResult
Event
```

Example:

```python
@dataclass
class Action:
    action_type: str
    target: dict
    parameters: dict
    risk_class: str | None = None
```

Example:

```python
@dataclass
class RemediationPlan:
    incident_id: int
    summary: str
    actions: list[Action]
    verification: list[VerificationCheck]
    rollback: list[Action]
    expires_at: datetime
```

Avoid making the LLM's response format the internal domain model.

Parse LLM output into validated domain objects first.

---

# 19. Event Model

A durable event stream can make the entire system easier to reason about.

Example event types:

```text
observation.created
finding.created

incident.opened
incident.updated
incident.quarantined
incident.resolved

diagnosis.started
diagnosis.completed

plan.created
plan.expired

approval.requested
approval.granted
approval.denied

action.started
action.completed
action.failed

verification.started
verification.passed
verification.failed

rollback.started
rollback.completed
rollback.failed

update.started
update.rolled_back
update.completed
```

This allows:

- history
- audit trail
- Scruffy timelines
- Telegram summaries
- debugging
- future analytics

without requiring a complicated event-sourcing architecture.

It can simply be an append-only SQLite table initially.

---

# 20. Approval Model

Approval should be explicit and durable.

Possible fields:

```text
approval_id
plan_id
incident_id
risk_class
requested_at
expires_at
requested_via
decision
decision_at
decided_by
```

Approval expiration should remain a core safety behavior.

A stale approval must not authorize a changed system state indefinitely.

For higher-risk actions, consider requiring that the action still matches the current preconditions when approval is consumed.

---

# 21. Preconditions

Typed actions allow explicit preconditions.

Example:

```yaml
action:
  type: docker.restart_service
  stack: media
  service: sonarr

preconditions:
  - service_exists
  - stack_path_matches_registered_stack
  - container_id == abc123
```

This protects against TOCTOU-style state changes:

> The system that gets modified should still be the system the user approved.

Compose-file mutation already follows a similar philosophy with execution-time path revalidation.

Generalize it.

---

# 22. Read vs Write Capabilities

A useful architectural split:

```text
OBSERVATION CAPABILITIES
------------------------
inspect container
read logs
inspect compose config
inspect systemd state
inspect mounts
read journal
inspect filesystem metadata
check HTTP
check TCP
inspect backup state

MUTATION CAPABILITIES
---------------------
restart service
recreate service
restart systemd unit
apply approved config diff
rollback config
perform canary update
rollback image
```

Read capabilities can be substantially broader.

Write capabilities should remain small and typed.

---

# 23. Amy's Role

Amy should become the deeper diagnostic investigator.

Possible responsibilities:

```text
collect read-only evidence
correlate logs
perform targeted probes
search documentation if allowed
generate hypotheses
rank hypotheses
recommend next diagnostic probe
```

Amy should NOT bypass Bender for mutation.

Amy can say:

```text
Hypothesis:
NFS mount disappeared before Sonarr started.

Recommended remediation:
restart mount unit, verify mount, then restart Sonarr.
```

Farnsworth converts that into a typed plan.

Policy evaluates it.

Bender executes.

---

# 24. Hermes's Role

Hermes should focus on deterministic classification and signal reduction.

Examples:

```text
container unhealthy
container crash-loop
mount missing
disk pressure
backup stale
systemd failed
dependency unavailable
TLS endpoint failed
network unreachable
```

Avoid forcing an LLM to identify problems that deterministic rules can classify reliably.

LLMs should be used where ambiguity and cross-signal reasoning provide value.

---

# 25. Farnsworth's Role

Farnsworth should be the planner/orchestrator.

Responsibilities:

```text
take incident context
request deeper diagnosis if needed
construct remediation proposal
convert proposal into typed actions
attach verification
attach rollback
request policy decision
request approval where needed
coordinate execution lifecycle
```

Farnsworth should not directly:

- run arbitrary shell
- own Telegram protocol mechanics
- parse every infrastructure subsystem
- implement low-level Docker behavior
- implement filesystem safety

---

# 26. Bender's Role

Bender should be deliberately boring.

That is a compliment.

Bender should:

```text
receive validated typed action
re-check target
re-check preconditions
construct deterministic command/API call
execute
capture result
emit event
```

Bender should not interpret broad natural-language intent.

The less creative Bender is, the safer Planet Express is.

---

# 27. Scruffy's Role

Scruffy should evolve from latest-state display toward an operational timeline.

High-value views:

```text
Current health
Open incidents
Awaiting approval
Quarantined incidents
Recent resolved incidents
Update history
Backup status
```

Incident detail page:

```text
What happened
When first detected
Evidence
Diagnosis
Plan
Risk classification
Approval
Actions executed
Verification result
Rollback result
Previous occurrences
```

This turns the UI into an explainable operational console.

---

# 28. Things Explicitly Out of Scope

To protect product focus, the following should remain outside Planet Express unless the product strategy materially changes:

```text
network-wide device autodiscovery
generic SNMP management
Windows administration
remote desktop
general browser automation
enterprise IAM
generic workflow automation
graph-database topology engines
unrestricted agent subprocess trees
arbitrary remote shell product
application-content automation
media library management
download automation
general chatbot assistant
```

The product is:

> self-hosted infrastructure reliability and remediation

not:

> universal homelab automation.

---

# 29. Refactor Phases

## Phase 1 — Package Restructure

Goal:

- separate modules
- preserve existing behavior
- establish clean boundaries
- avoid simultaneously rewriting logic

Tasks:

```text
move shared models
split Telegram integration
split execution helpers
split state handling
introduce package namespaces
maintain compatibility shims if needed
```

No major behavior changes yet.

---

## Phase 2 — Typed Actions

Goal:

Remove arbitrary mutating shell from the normal remediation path.

Tasks:

```text
define Action model
create ActionRegistry
convert current common remediation operations
move command construction into Bender
validate parameters strictly
add action serialization
add action tests
```

Start with only existing known operations.

Do not over-design a universal action system.

---

## Phase 3 — Policy Engine

Goal:

Make autonomy explicit.

Tasks:

```text
define R0-R4
classify every action
add configurable autonomy policy
add deny rules
preserve forbidden-target concepts
attach policy decision to plans
```

---

## Phase 4 — SQLite + Incidents

Goal:

Introduce durable operational memory.

Tasks:

```text
database migration framework
incidents table
events table
plans table
approvals table
action runs
verification runs
incident fingerprinting
```

Continue generating existing JSON snapshots.

---

## Phase 5 — Verification

Goal:

Prove recovery instead of assuming command success.

Tasks:

```text
verification interface
Docker checks
HTTP checks
systemd checks
mount checks
timeouts
failure reporting
events
```

Then integrate rollback.

---

## Phase 6 — Failure Quarantine

Goal:

Prevent remediation loops.

Tasks:

```text
failure counters
time-window policy
automatic quarantine
manual release
Telegram notice
Scruffy indication
```

---

## Phase 7 — Adapter Interface

Goal:

Make infrastructure support extensible without bloating core logic.

Tasks:

```text
Adapter base contract
Docker adapter
Compose adapter
Systemd adapter
Mount adapter
Borg adapter
```

Migrate existing logic gradually.

---

## Phase 8 — Scruffy Incident Timeline

Goal:

Expose durable operational state.

Tasks:

```text
open incidents
incident detail
approval state
execution timeline
verification result
quarantine state
historical incidents
```

---

## Phase 9 — OpenAI-Compatible Provider

Goal:

Support local and alternate hosted models with minimal bespoke code.

Tasks:

```text
provider capability model
openai-compatible base
Ollama validation
LiteLLM validation
OpenRouter validation
structured output compatibility
```

---

## Phase 10 — MCP

Goal:

Expose Planet Express safely to external AI clients.

Start read-first.

Tasks:

```text
status resources
incidents
plans
history
diagnosis requests
```

Then add mutation proposal endpoints which always route through policy + approval + Bender.

---

# 30. Recommended Priority Matrix

## P0 — Do during the refactor

```text
package decomposition
typed actions
policy engine
incident model
SQLite event/history store
verification
basic quarantine
```

## P1 — Immediately after core refactor

```text
adapter interface
Scruffy incident timeline
openai-compatible providers
integration-test homelab
MCP read surface
```

## P2 — Later

```text
additional infrastructure adapters
advanced incident correlation
local knowledge/runbook enrichment
metrics/analytics
optional autonomous R1 policies
```

## Avoid

```text
Steward-style infrastructure sprawl
general purpose automation
direct app content manipulation
arbitrary mutating LLM shell
parallel privileged APIs bypassing Bender
```

---

# 31. Build / Borrow / Reject Summary

## BUILD

Planet Express should own:

```text
Compose-first autonomous remediation
safety-bounded action execution
explicit approval flow
typed infrastructure actions
incident history
verification
quarantine
canary updates
automatic rollback
human-readable audit history
```

## BORROW

From Steward:

```text
durable incidents
investigation lifecycle
risk classes
policy engine
quarantine
execution history
verification discipline
```

From homelab-ai:

```text
simple extension contract
MCP support
OpenAI-compatible/local model support
lightweight persistent memory
```

From SRE Agent:

```text
explicit lifecycle/state-machine thinking
human gates
observe → reason → act → learn discipline
```

From WAGMIOS:

```text
least-privilege action surfaces
avoid raw Docker socket-style agent access
```

## REJECT

```text
generic AI root shell
unrestricted config editing
network-management platform expansion
enterprise IT management scope
media/application assistant functionality
unbounded subagent architecture
```

---

# 32. Security Principles

The refactor should preserve these as hard rules.

## 32.1 LLM output is untrusted

Treat LLM output exactly like external input.

It must be:

```text
parsed
validated
normalized
policy checked
target checked
precondition checked
```

before execution.

## 32.2 Approval is not execution permission forever

Approval should be:

- scoped to a plan
- scoped to specific actions
- time-limited
- invalidated if materially changed

## 32.3 Bender owns command construction

The planner chooses:

```text
docker.restart_service
```

Bender chooses:

```text
exact executable
exact argv
exact compose path
exact timeout
exact environment
```

## 32.4 Path containment must be checked at execution time

Especially for:

- Compose files
- configuration files
- mount targets
- rollback files

## 32.5 High-risk actions fail closed

Unknown action:

```text
DENY
```

Unknown target:

```text
DENY
```

Unknown sudo requirement:

```text
DENY
```

Unknown risk classification:

```text
DENY
```

---

# 33. Possible Action Schema

Example:

```json
{
  "version": 1,
  "action_id": "act_123",
  "type": "docker.restart_service",
  "target": {
    "stack": "media",
    "service": "sonarr"
  },
  "parameters": {},
  "preconditions": [
    {
      "type": "service_exists"
    },
    {
      "type": "stack_registered"
    }
  ]
}
```

Policy may enrich it:

```json
{
  "risk_class": "R1",
  "decision": "AUTO",
  "reason": "bounded reversible container restart"
}
```

Execution result:

```json
{
  "action_id": "act_123",
  "status": "completed",
  "exit_code": 0,
  "started_at": "...",
  "completed_at": "..."
}
```

Verification result:

```json
{
  "action_id": "act_123",
  "status": "passed",
  "checks": [
    {
      "type": "container_running",
      "passed": true
    },
    {
      "type": "docker_health",
      "passed": true
    }
  ]
}
```

---

# 34. Suggested Incident Schema

Conceptual fields:

```text
id
fingerprint
status
severity
stack
service
finding_type

title
summary

first_seen
last_seen
occurrence_count

current_diagnosis
current_plan_id

quarantined_at
resolved_at

created_at
updated_at
```

Do not over-normalize early.

SQLite should improve durability, not create a giant enterprise database project.

---

# 35. Suggested Event Schema

```text
id
timestamp
event_type

incident_id
plan_id
action_id

source
payload_json
```

This alone can support a large amount of future functionality.

---

# 36. Possible Policy Configuration

Example:

```yaml
policy:
  autonomy:
    R0: auto
    R1: auto
    R2: approve
    R3: approve
    R4: deny

  quarantine:
    max_failed_actions: 3
    window_minutes: 60

  approval:
    ttl_minutes: 30

  forbidden:
    stacks:
      - critical_database

    systemd_units:
      - ssh.service

    paths:
      - /
      - /boot
      - /etc/ssh
```

This is illustrative.

Policy should remain human-readable.

---

# 37. Canary Update Model

Recommended generalized update lifecycle:

```text
DISCOVER UPDATE
      ↓
CREATE UPDATE PLAN
      ↓
PULL IMAGE
      ↓
UPDATE ONE SERVICE
      ↓
VERIFY STARTUP
      ↓
STABILIZATION WINDOW
      ↓
VERIFY HEALTH
      ├── PASS → persist success
      └── FAIL → rollback previous image
                    ↓
                  verify rollback
```

Record:

```text
old image
new image
digest
timestamps
health result
rollback result
```

This can later feed Scruffy history.

---

# 38. Explainability

A high-value Planet Express output should read like:

```text
Incident #42

Problem:
Sonarr became unhealthy at 14:03.

Likely cause:
Its NFS media mount disappeared 37 seconds earlier.

Evidence:
- mount unit inactive
- /casamedia unavailable
- Sonarr log reports path access failures

Recommended remediation:
1. Restart casamedia mount.
2. Verify filesystem availability.
3. Restart Sonarr.
4. Verify container health and HTTP response.

Risk:
R2 — service-impacting.

Approval:
Required.

Rollback:
No persistent configuration change is involved.
If the mount cannot recover, Sonarr will not be repeatedly restarted.

Previous history:
A similar incident occurred twice in the last 30 days.
```

That is substantially more useful than:

```text
Container unhealthy. Restart?
```

---

# 39. Strategic Product Boundary

Planet Express should be strongest when the user says:

```text
Something on my server is broken.
Figure out why.
Tell me what you want to change.
Don't do anything dangerous.
Fix it safely.
Confirm it actually worked.
Remember what happened.
```

That is the product.

---

# 40. Recommended 2.0 Definition

Planet Express 2.0 should represent an architectural change:

## Planet Express 1.x

Conceptually:

```text
AI-assisted monitoring and remediation system
```

## Planet Express 2.0

Conceptually:

> **A persistent infrastructure remediation system with an AI planner.**

This distinction matters.

The deterministic system owns:

- state
- policy
- execution
- verification
- rollback
- audit history

The AI owns:

- diagnosis
- hypothesis generation
- remediation intent
- explanation

---

# 41. Core Principle for the Coding Session

When evaluating any refactor or new feature, ask:

> Does this increase deterministic control over infrastructure state, or does it merely give the LLM more power?

Prefer the former.

Another useful test:

> Can this capability be expressed as a small typed action with deterministic execution and verification?

If yes, it probably belongs in Planet Express.

If it requires:

```text
"Let the agent run arbitrary commands until it works"
```

it probably does not.

---

# 42. Immediate Refactor Checklist

Use this as the first-pass coding checklist.

```text
[ ] Create package/module structure
[ ] Move shared domain models out of large agent files
[ ] Define Incident model
[ ] Define Action model
[ ] Define PolicyDecision model
[ ] Define VerificationResult model
[ ] Introduce ActionRegistry
[ ] Move mutating command construction into Bender
[ ] Preserve existing read-only diagnostics
[ ] Define R0-R4 policy
[ ] Add SQLite database layer
[ ] Add append-only events table
[ ] Persist incidents
[ ] Persist plans
[ ] Persist approvals
[ ] Persist action runs
[ ] Implement incident fingerprint
[ ] Add first verification checks
[ ] Add quarantine counter
[ ] Preserve existing JSON outputs
[ ] Add migration tests
[ ] Add typed-action tests
[ ] Add policy tests
[ ] Add Bender safety tests
[ ] Add first Docker integration fixture
```

---

# 43. Final Direction

Do not broaden Planet Express merely because competitors support more systems.

The most defensible architecture is narrower:

```text
Docker Compose
Linux host
real incidents
typed remediation
explicit safety policy
human approval
verification
rollback
history
```

Steward demonstrates where the product could become too broad.

homelab-ai demonstrates where it could become too application-centric.

Planet Express should stay in the middle:

> **deep infrastructure reliability for self-hosters without enterprise sprawl or unrestricted AI control.**

The strongest long-term product statement remains:

> **AI is allowed to propose intent. Deterministic software decides whether and how that intent becomes a host mutation.**

That principle should drive the refactor.
