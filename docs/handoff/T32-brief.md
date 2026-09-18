# Task T32 — deterministic incident identity and durable lifecycle

Branch `v2`. Python 3.11+, SQLite through `planet_express.core.store.Store`, no new dependencies.
This is the incident **foundation** only. T33 will connect open incidents to policy, typed proposals,
RPC and the dashboard.

## Why

Every full scan is currently isolated: Leela writes a raw snapshot, Hermes invents sequential finding
IDs (`f1`, `f2`, ...), and Farnsworth plans from that one report. The same unhealthy container on two
scans has no durable identity, occurrence count or resolution history. Policy already has persistent
per-target action limits, but there is no incident record to drive it.

Incident identity must not depend on Hermes's model-written description, severity, ordering or
suggested action. Those values can vary between equivalent scans. T32 derives identity from Leela's
structured facts before Hermes runs.

## Read first

- `casa_leela.py`: `run_full()` and every check represented in the extraction matrix below.
- `casa_farnsworth.py`: `run_pipeline()` and `run_bot()`; incident reconciliation belongs after the
  full monitor snapshot is saved and before Hermes analyzes it.
- `planet_express/core/store.py`: schema compatibility, `_write()`, event conventions and startup
  initialization.
- `planet_express/core/redact.py`: persisted free text must use the existing redaction boundary.
- `tests/test_casa_leela.py`, `tests/test_store.py` and the pipeline tests.
- T23 in `docs/designs/planet-express-2-0-slices.md`: a schema bump is rollback-sensitive and must be
  covered by the pre-upgrade snapshot workflow.

## Locked decisions

### Identity

An observation has these normalized fields:

```text
kind        stable check family, e.g. container_health or nfs_mount
resource    stable subject within that family, e.g. CASA_RADARR or CASA_TA:/youtube
condition   healthy | failing | unknown
severity    CRITICAL | HIGH | MEDIUM | LOW | null
summary     bounded, redacted operator text
details     bounded JSON containing only allowlisted structured fields
```

The fingerprint is SHA-256 over canonical JSON containing exactly:

```json
{"version":1,"kind":"<kind>","resource":"<resource>"}
```

Store the full lowercase hex digest and `fingerprint_version=1`. Do not include severity, transient
status text, timestamps, counts, errors, model output or suggested remediation. Changing those fields
updates one incident instead of creating another.

Reject empty or overlong identity fields rather than silently hashing malformed data. `kind` is a
small code-owned enum; `resource` is bounded to 512 characters.

### Resolution

- `failing` opens or updates an incident.
- `unknown` with a severity opens or updates an incident (for example, a failed discovery). An
  unalerted `unknown` such as a healthcheck still inside its declared startup grace does not create a
  new incident, but it updates an already-open matching incident without resolving it.
- `healthy` resolves the matching open incident. A healthy observation does not create a row.
- A resolved fingerprint seen failing again reopens the same row and preserves `first_seen`.
- Missing subjects never resolve incidents. This prevents a failed discovery, removed list entry or
  partial snapshot from being mistaken for recovery.
- A full scan reconciles each fingerprint at most once. Duplicate observations are a producer error;
  fail that reconciliation without partially updating the database.
- Status and updates-only scans do not reconcile incidents.

This deliberately leaves an incident open when a subject disappears permanently. T33 or the later
incident UI can add an explicit operator close/archive operation; T32 must prefer stale-open over a
false recovery.

### Extraction matrix

Build a pure `observations_from_snapshot(snapshot) -> list[Observation]` in
`planet_express/core/incidents.py`. It imports no LLM or execution code.

| Leela field | `kind` | Stable `resource` | Condition |
|---|---|---|---|
| `containers[]` | `container_health` | container `name` | failing for `issue` or `crash_looping`; unknown for `health=starting`; otherwise healthy |
| `stack_completeness[]` | `stack_completeness` | `stack` | unknown for `status=unknown` or any service observation `status=unknown`; failing for `alert`; otherwise healthy |
| `disk[]` | `disk_usage` | mount path | failing for `alert`; otherwise healthy |
| `mounts` | `configured_mounts` | literal `host-mounts` | failing when `missing` is non-empty; otherwise healthy |
| `unraid_exports` | `unraid_exports` | literal `unraid` | unknown when unreachable; failing for duplicates/alert; otherwise healthy |
| `nfs_mount_health[]` | `nfs_mount` | `container:path` | failing for stale/timeout/error; healthy for ok; omit unavailable |
| global NFS discovery failure (`container` and `path` null) | `nfs_discovery` | literal `mount-discovery` | unknown with the existing MEDIUM alert; it never resolves missing per-mount subjects |
| `vpn_port_forwarding` | `vpn_port_forwarding` | literal `gluetun-qbittorrent` | failing for `alert`; otherwise healthy |
| `backups` | `backup_job` | configured job key | failing when result is not success or last run is absent; otherwise healthy |
| `services` | `systemd_service` | unit key | failing unless active; otherwise healthy |
| `certs[]` | `tls_certificate` | `resolver` (the certificate source-file stem, present in readable and unreadable states) | failing for unreadable, expired, expiring or renew-soon; otherwise healthy |

Severity is code-owned and deterministic:

| Observation | Severity |
|---|---|
| container crash-looping | HIGH |
| container unhealthy or non-zero exit, with image containing postgres/redis/elasticsearch/mariadb/mysql/valkey/mongo | HIGH |
| other container issue, including starting beyond its computed grace | MEDIUM |
| container starting inside grace | null (`unknown`, preserves an existing incident but opens none) |
| stack incomplete/unknown | its existing `alert` (unknown is already MEDIUM) |
| disk pressure | its existing HIGH/CRITICAL `alert` |
| configured mount missing | HIGH |
| Unraid unreachable | MEDIUM (`unknown`) |
| duplicate Unraid fsid | HIGH |
| NFS stale/timeout/error or discovery unknown | its existing HIGH/MEDIUM `alert` |
| VPN port-forwarding alert | HIGH |
| backup result not success or missing last run | HIGH |
| inactive systemd service | MEDIUM |
| unreadable certificate | HIGH |
| expired / expiring / renew-soon certificate | CRITICAL / HIGH / MEDIUM |
| every healthy observation | null |

These rules intentionally mirror Leela/Hermes's current deterministic rules where they exist and
make the remaining pre-Hermes classifications explicit. T32 must not ask an LLM to fill a severity.

Do not create incidents for image update candidates, aggregate journal counts, memory/uptime, or
Docker reclaimable-space rows in T32. Image updates have their own canary workflow; the remaining
facts lack a stable subject or an existing deterministic severity contract.

For every free-text input (`issue`, `error`, status text), redact it before persistence and cap each
value at 1 KiB. Details must never contain environment values, container configuration, command
output or an entire Leela row. Tests plant `API_KEY=incident-secret` and assert it cannot be found in
the database bytes or returned incident dictionaries.

### Durable model

Bump `SCHEMA_VERSION` from 2 to 3 and add:

```text
incidents
  id                   12 lowercase hex chars, primary key
  fingerprint          64 lowercase hex chars, unique
  fingerprint_version  integer
  kind                 text
  resource             text
  status               open | resolved
  condition            failing | unknown
  severity             nullable severity enum
  summary              text
  details_json         text
  last_observed_scan_id  64 lowercase hex chars, foreign key to incident_reconciliations
  first_seen           real
  last_seen            real
  resolved_at          nullable real
  occurrences          positive integer

incident_events
  id          integer primary key
  incident_id foreign key to incidents
  scan_id     foreign key to incident_reconciliations
  ts          real
  kind        opened | observed | resolved | reopened
  payload     bounded JSON

incident_reconciliations
  scan_id             64 lowercase hex chars, primary key
  snapshot_timestamp  text
  reconciled_at       real
  observation_count   non-negative integer
```

Define `incident_reconciliations` before the incident tables in the schema so their scan references
are valid foreign keys. `scan_id` is SHA-256 over the canonical JSON of the validated
`MonitorSnapshot` object. The timestamp therefore participates in identity, so two scans with equal
host facts remain distinct. Index incident status/last-seen, incident-event lookup and reconciliation
time. `Store.init()` upgrades an existing schema 2 database by creating the new tables and stamping
version 3 in the same initialization transaction; an older core must continue refusing the newer
database through the existing compatibility gate.

`Store.reconcile_incidents(scan_id, snapshot_timestamp, observations)` runs in one `BEGIN IMMEDIATE`
transaction and returns the changed/current rows. It must:

- insert `opened` with occurrence 1;
- update an open incident and increment occurrences once per failing/unknown full scan (`observed`);
- resolve only from the matching healthy observation (`resolved`);
- reopen the same row, clear `resolved_at`, increment occurrences and record `reopened`;
- preserve `first_seen`, reject duplicate fingerprints and leave the database byte-for-byte
  logically unchanged on validation failure;
- insert the reconciliation receipt in the same transaction as lifecycle changes, including a scan
  with zero failing observations or zero lifecycle changes;
- stamp every observed incident and lifecycle event with that receipt's `scan_id`; a subject omitted
  from a later scan stays open but keeps its older `last_observed_scan_id`;
- treat an already-recorded `scan_id` as an idempotent retry and never increment occurrences twice;
- provide `get_incident(id)` and bounded `list_incidents(status=None, limit=...)` reads for T33.

Also provide `latest_incident_reconciliation()`. T33 must hash the current saved monitor snapshot,
require an exact receipt match, and require the selected incident's `last_observed_scan_id` to equal
that same current `scan_id` before creating any incident-driven proposal. If extraction fails, the
database write fails, the incident was omitted, or the process dies between saving the snapshot and
reconciliation, those checks fail closed. A successful receipt for a newer partial scan cannot
certify an incident last observed by an older scan.

Incident events are permanent lifecycle history and are not folded into the existing 90-day prune of
unlinked general events.

### Pipeline integration

Construct the incident recorder from the already initialized core `Store` in `run_bot()` and pass it
to every full-pipeline entry point. After `STATE_MONITOR` is written:

1. extract deterministic observations;
2. calculate the canonical `scan_id` and reconcile them in SQLite;
3. continue to Hermes and the existing report/planning path.

An extraction or database error is logged with a stack trace and sends one concise operator
notification, but does not discard the saved monitor snapshot or prevent the existing Hermes report.
T32 has no automatic actions, so continuing is safe. Do not claim incidents are current when
reconciliation failed. The absent/mismatched durable receipt is the machine-readable proof T33 uses
to refuse incident-driven proposals, including after a process restart.

Startup does not resolve or increment anything. Reconciliation happens exactly once per completed
full scan.

## Tests

### Pure extraction

- Every matrix row produces the stable kind/resource and expected condition/severity.
- Reordering snapshot lists produces the same sorted observations and fingerprints.
- Changing descriptions, severity or transient details does not change a fingerprint.
- Malformed entries are refused or omitted explicitly; none can collapse to an empty shared subject.
- Secrets in free text are redacted and bounded before reaching the store.

### Store

- schema 2 upgrades to 3 with existing approvals/executions/events intact;
- open → observed → resolved → reopened preserves ID/first_seen and records exact occurrence counts;
- alerted unknown opens/updates; unalerted unknown creates nothing, and neither form resolves;
- healthy-only input creates no incident;
- missing observation leaves an open incident unchanged;
- duplicate fingerprint or invalid batch rolls the whole transaction back;
- concurrent reconciliation produces one incident row and monotonic occurrence counts;
- retrying one `scan_id` is idempotent; a different timestamp produces a distinct receipt and one
  additional observation;
- a successful no-change/healthy scan still writes a receipt, while a rolled-back transaction does
  not;
- an open subject omitted by a newer successful scan retains its older `last_observed_scan_id`, so it
  cannot qualify for T33 proposals;
- reads enforce status/limit bounds and decode `details_json`;
- a version-3 database is still refused by schema-2 code in the existing downgrade rehearsal.

### Pipeline

- full mode reconciles after the snapshot write and before Hermes;
- status/updates modes never reconcile;
- reconciliation failure logs/notifies and the existing Hermes path continues;
- one full scan calls reconciliation once, including when there are no failing observations.

## Verification

- Focused extraction, store and pipeline tests.
- `CASA_CONFIG=config.example.yaml .venv/bin/pytest -q` outside the socket-restricted sandbox.
- `.venv/bin/ruff check .` and `git diff --check`.
- Mandatory `codex review --uncommitted`; fix or explicitly record every finding.

## VM rehearsal

Use the test homelab and the real core database:

1. Take the required pre-upgrade snapshot, push the code and restart core; confirm schema 3 and both
   units active.
2. Run a full scan with the unhealthy and crash-loop fixtures; record their incident IDs and
   occurrence counts.
3. Run the same scan again; confirm the same IDs and one increment each, with no duplicate rows.
4. Recover one fixture, run a full scan, and confirm its incident resolves while the still-failing
   fixture remains open.
5. Break or fake one discovery as unknown and confirm it does not resolve the prior incident.
6. Restore the fixtures and VM state; confirm no service errors.

## Must not change

Hermes finding identity or prompts, Farnsworth planning semantics, policy decisions, approval or
execution behavior, dashboard/RPC routes, Telegram command behavior, config ownership, automatic
remediation, quarantine, or legacy shell-plan handling. T32 records incidents; T33 acts on and
displays them.

## Brief review record

Three independent `codex review` rounds found six contract gaps, all fixed here: startup health could
falsely resolve container/stack incidents; certificate identity changed after repair; global NFS
discovery had no valid subject; several families lacked deterministic severity; a global successful
receipt could not prove reconciliation after failure; and a newer receipt could certify an incident
omitted from that scan. The final contract uses unalerted unknown observations to preserve without
opening, stable certificate/NFS identities, a complete severity table, transactional scan receipts,
and per-incident `last_observed_scan_id`. The round-three scan-association fix was not re-reviewed
because the review loop is capped at three rounds; its required stale-incident regression test is
listed above.
