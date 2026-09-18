# Task T33 — incident policy, typed proposals and dashboard context

Branch `v2`. Python 3.11+, SQLite through `planet_express.core.store.Store`, no new dependencies.
T32 already supplies deterministic incident identity, lifecycle events and transactional reconciliation
receipts. T33 is the first consumer of that foundation.

## Why

Open incidents are durable but invisible outside the database. They cannot yet explain which scan
certified them, whether a typed remediation exists, or why policy will allow or refuse that action.
T33 exposes that context through core RPC and Scruffy, and permits one narrow incident-driven action:
an approval-gated restart of a currently failing Compose-managed container.

The incident is evidence for a proposal, never authority to mutate the host. The existing action
registry, target resolver, policy engine, approval card and execution path remain the authority.

## Locked safety contract

### Current-source gate

Before creating an incident-driven proposal, core must first take the existing `PipelineState`
host-mutation lock with an incident-check owner. A running scan therefore makes the proposal return
busy, and a new scan cannot start until the check and proposal transaction finish. While holding that
lock, core must:

1. read and validate the saved `config.STATE_MONITOR` as a `MonitorSnapshot` in `full` mode;
2. calculate its canonical T32 `scan_id`;
3. require the latest durable reconciliation receipt to have exactly that `scan_id`;
4. require the selected incident to be open, failing, and last observed in exactly that scan.

Missing, malformed, partial-mode, unreconciled or superseded snapshots fail closed. An incident omitted
from a newer full scan is stale even when it remains open. Reads may still display stale incidents,
but their hint must explain that a fresh reconciled full scan is required and no proposal button may
be offered.

Release the lock after the proposal row/card handoff completes, including every refusal/error path.
This deliberately reuses the lock that already serializes scan start and typed mutations: no second
lock may create an ordering gap with `try_start_run()`.

On authorization, first acquire the existing host-mutation lock exactly as ordinary typed actions do,
then run the same gate while still holding it and immediately before the approval/execution
transaction. A scan cannot start between this recheck and worker handoff. If the incident resolved,
was omitted, or its source scan ceased to be the current saved reconciled scan, close the pending
approval as denied by `policy`, record an
`approval.refused_stale_incident` event, update its approval card, and run nothing. This second check
is required because approvals can remain pending for an hour and survive core restarts.

### Typed remediation mapping

Only an open, failing `container_health` incident has a T33 remediation. Add a strict read adapter
beside `actions.container_compose_labels()` that returns a target only when Docker inspect succeeds
and both the Compose project and service labels are present and valid. Keep the existing fallback
helper unchanged for Amy's display/investigation callers. An inspect failure or either missing label
is informational only and cannot be inferred from the container name. The resulting proposal is:

```text
action         docker.restart_service
stack/service  values from current Docker Compose labels
requested_via  incident
requested_by   authenticated dashboard operator
```

Then call the existing typed proposal path. Target resolution and current policy decide whether the
action is registered, forbidden, rate-limited, paused, network-guarded or otherwise invalid. T33 must
not build argv, execute directly, bypass approval, or infer another action. Unknown-condition
incidents and every other incident family remain informational until a typed action is explicitly
registered for them in a later task.

### Durable association and schema compatibility

Bump `SCHEMA_VERSION` from 3 to 4 and add:

```text
incident_proposals
  approval_id  primary key, foreign key to approvals
  incident_id  foreign key to incidents
  scan_id      foreign key to incident_reconciliations
```

Create the association in the same `BEGIN IMMEDIATE` transaction as a newly inserted approval. If an
existing pending proposal for the same action and target is returned, attach it only when it is
already associated with this exact incident and scan; otherwise refuse the incident proposal rather
than silently relabel another caller's approval. Generic proposals retain their existing behavior.

`Store.propose()` accepts optional incident provenance and, while holding its write transaction,
rechecks the latest receipt and incident predicates before insert. This database check complements
the `PipelineState` critical section; the lock makes the snapshot file and reconciliation receipt a
stable pair, while the transaction makes the incident association atomic. It returns a typed
stale-source refusal without creating either row. Provide a read for approval provenance so
`CommandService.decide()` can run the current-source gate for incident-linked approvals after taking
the same lock. The new table avoids altering existing
approval rows; schema 3 upgrades to 4 in the existing atomic initialization transaction. Older code
must refuse schema 4, so the live deployment requires the standard pre-upgrade snapshot.

No incident row or lifecycle event is deleted when an approval expires, is denied, fails, or passes.

## Application and RPC contract

Add a small incident application service (or equivalent cohesive helper) that owns snapshot
validation, currentness decoration, deterministic hints and the typed mapping. It may depend on the
store and action adapters, but must not ask an LLM.

Incident list decoration must not inspect Docker once per row. Read and validate the snapshot once,
read the receipt once, and resolve all eligible container resources with one batched Docker inspect
adapter under a single `RPC_DOCKER_TIMEOUT_SECONDS` deadline. `incident.get` and
`incident.propose` use the remaining part of the same per-request deadline for their one target.
If the shared label lookup times out or fails, return the incidents with a non-actionable
`target_unavailable` hint; do not hold an RPC worker beyond its normal deadline and do not expose a
proposal button. The dashboard requests at most 20 open plus 10 resolved incidents per poll even
though the RPC validation supports a caller-supplied maximum of 100.

Expose authenticated core RPC methods:

- `incident.list {status, limit}`: bounded open/resolved/all rows, newest first, decorated with
  `source_current`, a deterministic `hint`, and linked proposal summary when present;
- `incident.get {incident_id}`: one decorated incident plus ordered lifecycle events and linked
  proposals;
- `incident.propose {incident_id, operator}`: validate the operator, enforce the current-source gate,
  map the typed action, and return the normal `ProposeResult` shape.

IDs are exactly 12 lowercase hexadecimal characters. Status is `open`, `resolved`, or `all`; limit is
an integer from 1 to 100. Invalid inputs are `bad_request`, missing IDs are `not_found`, and a valid
incident with no current typed remediation returns a successful RPC result with `ok=false` and a
plain reason. Docker timeouts/refusals use the existing proposal result semantics and never become a
500 response. RPC responses must not expose Telegram message IDs or raw snapshot contents.

The hint is code-owned data with `agent`, `state`, and `message`:

- current actionable container: Farnsworth / `proposal_available`;
- stale or unreconciled source: Leela / `fresh_scan_required`;
- unknown observation: Amy / `investigation_required`;
- current incident without a registered mapping: Amy / `no_typed_remediation`;
- eligible container whose current Compose identity cannot be established: Farnsworth /
  `target_unavailable`;
- resolved incident: Leela / `resolved`.

Messages report only persisted facts and registered capabilities. They do not claim a diagnosis,
root cause, or completed investigation.

## Dashboard contract

Add authenticated Scruffy routes:

- `GET /api/incidents?status=open|resolved|all&limit=N`;
- `GET /api/incidents/<incident_id>`;
- `POST /api/incidents/<incident_id>/propose` with CSRF and the authenticated device identity.

Map core `bad_request`/`not_found` to stable JSON 400/404 responses and other failures to 503, in the
same style as approvals. Treat `/api/incidents` as JSON for authentication and CSRF handling.

On the Actions tab, add an Incident Console above Flight Authorisation. It shows open incidents first
and a compact recent-resolved group, including severity, summary, resource, occurrence count,
first/last seen, current/stale source state, and the deterministic crew hint. Only
`proposal_available` rows receive a **PROPOSE RESTART** button. Posting it must disable repeat
submission, display the returned reason, and refresh both incidents and approvals so the resulting
approval card is visible. Build all dynamic text with DOM text nodes; do not inject response strings
as HTML. Poll while visible and preserve in-flight UI across the dashboard's snapshot refresh, as the
approval panel already does.

Incident API and UI require the existing Airlock session. The public health widget remains unchanged.

## Tests

### Store and application

- schema 3 upgrades to 4 with all prior rows intact, and schema-3 code refuses the upgraded database;
- a proposal and its incident/scan association commit atomically;
- stale receipt, omitted incident, resolved incident, unknown condition and mismatched scan all write
  nothing;
- a scan racing proposal creation is either already running (proposal is busy) or starts only after
  the guarded proposal finishes; a scan racing authorization cannot start before worker handoff;
- generic pending dedup cannot be adopted as incident provenance, while an exact incident retry is
  idempotent and sends no duplicate card;
- only a current failing Compose container maps to `docker.restart_service`;
- list decoration performs one bounded batch lookup, not one Docker call per incident, and a timeout
  returns non-actionable rows within the RPC budget;
- inspect failure, either missing Compose label, invalid target, target timeout, policy refusal,
  cooldown and daily limits fail
  without execution;
- approval of an incident proposal takes the mutation lock, then revalidates currentness before
  consume/worker handoff; stale approval closes as policy-denied and creates no execution;
- ordinary proposal and decision behavior is unchanged;
- hint states are deterministic for actionable, stale, unknown, unsupported and resolved incidents.

### RPC and dashboard

- strict method parameter and ID validation, bounded list inputs, missing-item 404 and timeout paths;
- list/get responses contain context, events and proposal summaries without `message_id`;
- incident APIs require authentication; POST requires CSRF and uses the session operator, ignoring
  any submitted identity;
- Actions markup loads the incident client and exposes the expected panel/API hooks;
- client rendering uses text content, suppresses proposal controls for every non-actionable state,
  prevents double submission and refreshes approvals after a successful proposal.

## Verification

- Focused store, command service, RPC, route and browser-client tests.
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` outside the socket-restricted sandbox.
- `.venv/bin/ruff check .`, JavaScript syntax check, and `git diff --check`.
- Mandatory `codex review --uncommitted`; fix or explicitly record every finding.

## VM rehearsal

Use the homelab VM and its real core database:

1. take the required pre-upgrade state snapshot, deploy the candidate and confirm schema 4 and both
   units active;
2. run a full scan with a failing Compose container, confirm the incident is current in RPC/Scruffy,
   and propose its restart without authorizing it;
3. confirm one Telegram/dashboard approval exists and repeated clicks do not duplicate it;
4. run a newer full scan that resolves or omits the source, then attempt authorization and confirm
   policy closes it without an execution;
5. recreate the failure, run a full scan, propose and authorize; confirm the typed restart and
   verification pass, with incident provenance still visible;
6. confirm a non-container incident renders its Amy hint without a proposal control;
7. restore fixtures and VM state and confirm no service errors.

## Must not change

Incident identity/extraction/lifecycle rules, Hermes prompts or finding IDs, legacy Farnsworth plans,
action registry semantics, policy thresholds, direct dashboard restart behavior, Telegram callback
format, authentication model, automatic remediation, quarantine, or shell-plan handling. T33 adds a
guarded incident source to the existing typed approval path and displays its evidence.

## Brief review record

Three independent `codex review` rounds found five contract gaps, all fixed here: proposal and
authorization checks could race snapshot publication/reconciliation; the existing Compose-label
fallback could masquerade as a complete identity; per-row Docker inspection could exhaust RPC
workers; and one acceptance-test line reversed the required authorization ordering. The final
contract shares `PipelineState` serialization with scans and mutations, uses strict complete labels,
batches list decoration under one RPC deadline, and revalidates only after lock acquisition. The
round-three wording correction was not re-reviewed because the review loop is capped at three.
