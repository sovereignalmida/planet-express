# v4 Slice 0 — Contracts and threat model

Status: rev 0.4 (2026-10-13), G1 FINAL CHECK done and dispositioned (§21). Two G1 (Codex) rounds folded in; see §19 and §20. MOS cgroup
capability verified on the test VM (§20). Awaiting operator sign-off. Parent: `v4-mission-control.md` (v4.1) and
`v4-plan.md`. This slice produces no product code. Its output is the set of contracts below, which
slices 1-9 build against, plus the threat model they must defend. Where this document and the
brief disagree, fix the disagreement here before any slice starts.

Existing seams referenced (verified in the tree): store schema v5 with explicit migrations
(`planet_express/core/store.py`, `SCHEMA_VERSION = 5`); RPC server with uid allowlist and one handler
table (`planet_express/integrations/rpc.py`); host-control providers
(`planet_express/execution/host_control_{systemd,mos}.py`); typed engine and runbooks
(`planet_express/execution/{engine,runbook,policy}.py`); setup plan/apply/undo journal
(`planet_express/setup/`); redaction (`planet_express/core/redact.py`).

## 1. Principals and trust

| Principal | Identity | Trust |
|---|---|---|
| Operator | dashboard/Telegram session | Approves grants; elevation for Critical |
| Core | PE daemon user (today in `docker` group) | Owns store, approvals, verification, checkpoints, tripwires |
| Supervisor | thread/subprocess of Core | Spawns and watches sessions; holds no extra authority |
| Worker | dedicated Unix user `pe-worker` (never the Core user) | Untrusted-but-capable; privilege per tier |
| Runtime | agent CLI/process started by Supervisor as Worker | Same as Worker |
| Remote worker | another host, dedicated key, unix socket forwarded over SSH | Same as Worker, plus network exposure |

Invariant I1: nothing a Worker or Runtime says is evidence of success or permission to mutate.
Invariant I2: Core never runs agent-supplied text as a command; the Runtime does, as Worker.
Invariant I3: Worker reaches Core only through the worker socket (§6).
Invariant I4: **raw Docker access is host root.** A Worker never holds the raw Docker socket, the
`docker` group, or an unauthenticated Docker TCP port unless the mission is escalated to *broad*
(§4). The default Repair tier reaches Docker through the **scope-enforcing Docker API proxy** (§6.1).
Invariant I5: a Core-mediated call carries a fence; a Worker's native mutation does not and cannot.
So a lease is released or reclaimed only after the session is **provably dead** (§2.1).

## 2. Mission schema (store v6)

New tables, one forward migration v5→v6. `Store.init()` migrates directly today (snapshots are a deploy/setup
step), so slice 1 adds an upgrade guard: the daemon refuses to run the v5→v6 migration unless a
snapshot marker younger than 2 hours exists (written by `scripts/state_snapshot.py create`; a longer window because deploy snapshots before an interactive wizard). All times
UTC epoch seconds; all JSON columns validated by Pydantic models on read and write.

```sql
CREATE TABLE missions (
  id TEXT PRIMARY KEY,                    -- M-<monotonic>, never reused
  title TEXT NOT NULL, origin TEXT NOT NULL,   -- incident|operator|maintenance|system|import
  incident_id TEXT REFERENCES incidents(id),
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL,                    -- state machine §3
  tier TEXT NOT NULL,                      -- observe|propose|repair|change|critical
  goal TEXT NOT NULL, success_criteria TEXT NOT NULL,   -- bounded, redacted
  grant_json TEXT NOT NULL, grant_hash TEXT NOT NULL,
  standing_grant_id TEXT,                  -- set when start was unattended
  runtime TEXT NOT NULL, session_ref TEXT, -- adapter name; opaque handle
  cancel_requested_at INT, cancel_requested_by TEXT,
  plan_json TEXT, plan_hash TEXT,          -- change/critical: approved plan or diff
  checkpoint_id TEXT, verification_plan_json TEXT, verification_result_json TEXT,
  outcome TEXT, outcome_flags TEXT,        -- completed|failed|rolled_back|...; partially_applied
  created_at INT NOT NULL, updated_at INT NOT NULL, closed_at INT,
  schema_ver INT NOT NULL DEFAULT 1);
CREATE TABLE mission_events (             -- append-only; redacted before insert
  seq INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL REFERENCES missions(id), at INT NOT NULL,
  kind TEXT NOT NULL,                      -- state|tool|diff|tripwire|decision|artifact|note|takeover
  actor TEXT NOT NULL,                     -- core|supervisor|worker|operator:<id>
  body_json TEXT NOT NULL, redaction_applied INT NOT NULL DEFAULT 1);
CREATE TABLE mission_leases (
  resource TEXT PRIMARY KEY,               -- canonical id, see §2.3
  mission_id TEXT NOT NULL REFERENCES missions(id), fence INTEGER NOT NULL,   -- = resource_fences.fence at acquire
  acquired_at INT NOT NULL, expires_at INT NOT NULL, last_heartbeat INT NOT NULL);
CREATE TABLE standing_grants (
  id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, tier TEXT NOT NULL,
  window_json TEXT, created_by TEXT NOT NULL, created_at INT NOT NULL,
  expires_at INT NOT NULL, revoked_at INT, version INT NOT NULL);
CREATE TABLE mission_approvals (           -- the ONLY proof of approval; created+consumed atomically
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
  phase TEXT NOT NULL,                     -- grant | plan | critical:<n> | broad_sudo | no_backup
  bound_hash TEXT NOT NULL,                -- grant_hash or plan_hash this approval covers
  decision TEXT NOT NULL, operator TEXT NOT NULL, elevated INT NOT NULL,
  decided_at INT NOT NULL, UNIQUE (mission_id, phase, bound_hash));
CREATE TABLE mission_sessions (            -- one live session per mission
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
  token_hash TEXT NOT NULL, issued_at INT NOT NULL, expires_at INT NOT NULL, revoked_at INT,
  kind TEXT NOT NULL,                      -- local_cgroup | local_uid | remote_tunnel
  cgroup_path TEXT, leader_pid INT, session_uid INT UNIQUE,     -- uid from the reserved pool
  fence_json TEXT NOT NULL);               -- resource -> fence
CREATE UNIQUE INDEX one_live_session ON mission_sessions(mission_id) WHERE revoked_at IS NULL;
CREATE TABLE resource_fences (resource TEXT PRIMARY KEY, fence INTEGER NOT NULL);  -- permanent, never deleted
CREATE TABLE mission_artifacts (id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id), name TEXT,
  sha256 TEXT NOT NULL, bytes INT NOT NULL, path TEXT NOT NULL);   -- files under artifact dir
```

Rules: strict models (`extra=forbid`, bounded strings/lists, enums as `Literal`) live in
`planet_express/missions/models.py` (slice 1; G1-reviewed there). Canonical JSON for every hash is
RFC 8785 (JCS); duplicate keys, NaN/Infinity and non-UTF-8 are rejected before hashing.
Approval is proven only by a `mission_approvals` row with `decision='approve'`, the required `phase`,
`elevated=1` where the phase demands it (`broad_sudo`, `no_backup`, `critical:*`), and `bound_hash`
equal to the mission's *current* `grant_hash` (or `plan_hash`). **Every** issuance of a Repair-or-higher
session and every transition into `executing` re-checks this inside the transaction, unless a valid
unexpired, unrevoked standing grant covers the grant (then `standing_grant_id` is set and `broad_sudo`
/`no_backup` are refused). A changed grant gets a new hash, so old rows stop matching; `missions` carries no approval pointer. Events are never updated or deleted by mission code (retention job only, §2.2).
`fence` is taken by incrementing `resource_fences.fence` (upserted, never deleted) inside the same
`BEGIN IMMEDIATE` that inserts the lease, so a freed-then-reacquired resource never reuses a fence.
Starting a session is one transaction: mission status CAS, approval validation (below), lease
acquisition, session insert (the partial unique index makes a second live session fail). A mission row's `grant_json` is immutable
once any approving `mission_approvals` row exists for its hash; a change creates a new grant hash and requires re-approval.

### 2.1 Resume and cancel
- Resume reads `status`, the last event `seq`, the checkpoint and the lease; it never re-sends an
  accepted mutation. Cancel sets `cancel_requested`; Supervisor kills the session's process group,
  Core reads host state, and the mission lands in `cancelled` or `needs_operator`.
- A live session runs in a **delegated cgroup** (v2, `pids`/`freezer`); pause = freeze the cgroup,
  cancel/kill = freeze, `cgroup.kill`, confirm empty. Where cgroups are unavailable the host is proposal-only (decision 2026-10-13; the per-uid kill fallback described here is withdrawn; the unique uid per session in §22.1 remains for isolation, not as containment). Original text: a per-session Unix identity (`kind=local_uid`) taken from a reserved uid range that
  setup creates, allocated and reaped transactionally via `mission_sessions.session_uid` (unique),
  verified through `SO_PEERCRED`, with exclusivity checked before start; kill is `kill -9 -1` from a
  setuid helper running as that uid only; a host
  that can do neither is proposal-only for Repair.
- **A lease is never reclaimed on TTL alone.** Expiry marks it `suspect`; Core revokes the session
  token and capability endpoints, kills and confirms the cgroup/uid empty, classifies host state,
  and only then frees the lease. Core-mediated mutations (typed runbooks, tools, canary, prune)
  must present the current fence; v3 canary and prune acquire leases through the same manager.
- Heartbeat: Supervisor refreshes each held lease every `ttl/3`. A lease past `expires_at` is
  reclaimable; the old holder's fence is stale and Core rejects its calls.

### 2.3 Resource identity and overlap
Canonical ids are `host:<h>/stack:<s>`, `…/service:<s>/<svc>`, `…/mount:<path>`, `…/path:<abs>`,
`…/unit:<name>`. Overlap is defined by containment: `stack:s` contains every `service:s/*`, the
stack's compose paths, and its declared mounts; `mount:/a` contains `path:/a/**` and `mount:/a/b`.
Acquire expands a request to its containment set and takes all or none inside one
`BEGIN IMMEDIATE`; two requests conflict iff their expanded sets intersect. Slice 1 tests the
stack/service/mount/path overlap matrix.

### 2.2 Retention
Events and artifacts kept 90 days after `closed_at` by default (configurable); the mission row,
grant, outcome, checkpoint reference and verification result are kept until the operator purges.

## 3. State machine

```
draft ──► queued ──► investigating ──► awaiting_decision ──► executing ──► verifying
                         │                    │                   │            │
                         └────────────────────┴───────────────────┴────────────┴─► completed
                                                                          failed | rolled_back
 any non-terminal ──► cancelled | blocked | needs_operator | recovery_required
 executing ──► paused_tripwire ──► executing | rolled_back | needs_operator
 recovery_required ──► (investigating | executing | verifying | rolled_back | needs_operator)
                       chosen by Core's host-state classification, not by the runtime
```

Terminal: completed, failed, rolled_back, cancelled. **One execution predicate**, evaluated atomically at session issuance and at every transition into
`executing`: (a) the current-grant approval or a valid standing grant (§2 rule), AND (b) for Change/
Critical, an approving `plan` row bound to the current `plan_hash`, AND (c) one approving
`critical:<index>` row bound to the plan hash for **every** `protected_touches` entry in the plan,
AND (d) elevated `broad_sudo`/`no_backup` rows where the grant demands them. No `critical:*` row
substitutes for `plan` or for the grant approval. `blocked` = waiting on a lease or window;
`needs_operator` = Core cannot proceed without a human. Illegal transitions raise and are logged.
Observe/Propose missions never enter `executing`.

`blocked` exits: `blocked → queued` when the lease or window clears, `→ awaiting_decision` after
`blocked_max_s`, `→ cancelled`. `verifying` ends in `completed` (all checks pass), or in
`needs_operator` on any failed check, or in `rollback_pending` when the grant says
`on_verify_fail: rollback` (default `needs_operator`; rollback needs a checkpoint and an approved
tier). `rollback_pending` (from executing, paused_tripwire, verifying or recovery_required → rollback_pending → rolled_back
| needs_operator) is an explicit non-terminal state while a restore runs. Resumption after a
runtime outage or crash is **operator-mediated** (brief §14.8); the only automatic restarts are
Observe/Propose sessions (no mutation possible), within the retry budget.

`partially_applied` is a flag set when ≥1 mutation event was recorded and the end state is not
`completed`. It forces `needs_operator` or `rolled_back`.

## 4. Grant schema

```json
{
 "v": 1, "tier": "repair",
 "scope": {"stacks": ["media"], "services": ["media/qbittorrent"], "paths": ["~/stacks/media"],
           "mounts": [], "hosts": ["local"]},
 "protected": {"resources": [...]},                 // from site config, copied in at approval
 "exclusions": ["network", "persistent-data", "credentials", "publishing"],
 "window": {"deny": ["backup"], "allow": null},
 "sudo": {"mode": "scoped|broad", "rules": [{"unit": "x.service", "actions": ["restart"]}]},
 "credentials": ["tool:arr-api"],                   // brokered tools, never raw keys
 "budgets": {"wall_s": 3600, "tool_calls": 200, "output_bytes": 5000000, "retries": 2},
 "effects": {"max_unexpected_in_scope": 3, "per_resource": {"service:media/*": {"changes": 5}}},
 "on_verify_fail": "needs_operator|rollback",
 "docker": "proxy|raw",                           // raw only with sudo.mode = broad
 "verification": {"checks": ["<check ids from §10>"], "agreed_at": 0},
 "backup": {"covered": true, "recent_s": 86400, "acknowledged_no_backup": false}
}
```

- `grant_hash` = SHA-256 of the RFC 8785 canonical JSON. Approval binds to it via `mission_approvals`.
- `docker: raw` is permitted only with `sudo.mode = broad` and an elevated `broad_sudo` approval; the
  default Repair worker uses the proxy (§6.1).
- `sudo.mode = broad` requires an elevated per-mission approval, is rejected for standing grants and
  unattended starts, and is shown at the top of the approval card.
- Grant validation (Core): scope ⊆ discovered inventory; no protected resource inside scope unless
  tier = critical; checks exist; budgets within site maxima; backup floor satisfied or acknowledged.
- A standing grant stores a scope template; a mission starting under it gets a concrete grant
  whose hash is recorded with `standing_grant_id`. Standing grants never yield tier change/critical,
  broad sudo, or `acknowledged_no_backup`.

## 5. Policy hook into existing code

- `policy.py` change (slice 7): add `mission` to `RUNBOOK_ORIGINS` (today it is rejected as unknown); it
  is never in the unattended set, never in `LIMIT_EXEMPT_ORIGINS`, and a mutating typed step from it
  needs a `mission_approvals` row for the mission's current grant hash (it does not create an
  unrelated v3 approval). Tests cover each of those.
- New origin `mission` is **not** in the unattended set. Unattended start is
  decided solely by `standing_grants`, evaluated by `missions/` code, and recorded on the mission.
- The v3 typed engine and its attempt limits (T24) are unchanged. A mission may *request* a typed
  runbook for a known repair; that goes through `CommandService` as origin `mission` and obeys its
  limits and approvals.
- Mission mutation by the Worker itself (native tools) is not run by Core; it is bounded by the
  tier's OS privilege and watched by checkpoint/tripwires/verification (§7-§9).

## 6. Worker socket and RPC

- Separate socket `/run/planetexpress/worker.sock`, mode 0660, group `pe-worker`; the existing
  `rpc.sock` keeps its uid allowlist and is **never** reachable by `pe-worker`.
- A second `RpcServer` instance with its own handler dict, whose keys are an explicit allowlist:
  `mission.get_context`, `mission.post_event`, `mission.heartbeat`, `mission.request_decision`,
  `mission.submit_result`, `memory.search`, `query.container`, `logs.tail`, `incident.get`,
  `tool.invoke` (brokered integrations, §8).
- `SO_PEERCRED` gives uid, pid and gid; the server resolves the pid's cgroup and requires it to equal
  the `mission_sessions.cgroup_path` (or, in the per-uid fallback, the session's uid). The session
  token (256-bit, only its hash stored, delivered on an inherited file descriptor, never in argv or
  env) is checked as well. A same-uid process outside the session cgroup is refused. Handlers
  receive `(params, session)` so every call is scoped to one mission and its fences. `RpcServer`
  needs that extension (slice 2/3).
- Test: the worker uid calling `approval.decide`, `execution.*`, `config.*`, `canary.*`, `auth.*`
  gets "unknown method" on its socket and a refused connection on `rpc.sock`.
### 6.1 Scope-enforcing Docker API proxy
**Session binding.** The proxy serves each session on its **own Unix socket** (path unique per
session, created by Core at issuance, 0660 to the worker group, removed at revoke) and also requires
the session token on every connection (an `X-PE-Session` header injected by a Core-owned shim, or a
connection preface). Before forwarding any write the proxy loads the session row and checks: not
revoked, cgroup/uid binding, grant hash still approved, and the target's resource id against the
session's **effective scope** (§6.2) and current fences. A same-uid process in another session cannot
use this session's socket because the socket path is only inherited by that session.

Deny-by-default and schema-aware: only listed API versions and endpoints are served, and for each
write endpoint only listed request fields are accepted; anything else is refused and logged.

Served for the default Repair worker: container list/inspect/logs/stats, container create/start/
stop/restart/kill/rename/update/remove, exec create/start/inspect, container archive (get/put),
image list/inspect, network and volume list/inspect, network connect/disconnect (scoped), events.
Refused outright: swarm, plugins, secrets/configs, system/daemon, volume **create/prune** and any
volume driver or `driver_opts`, image build, image import/load, `prune` endpoints, and
`remote=`/URL parameters anywhere.

Container-create/update field rules (refused unless explicitly satisfied): no `Privileged`;
`CapAdd` limited to a short list; `PidMode/IpcMode/NetworkMode/UsernsMode/CgroupnsMode/UTSMode`
not `host`; no `Devices`, `Sysctls`, `Tmpfs`, `SecurityOpt` (other than a fixed allowlist),
`GroupAdd` to a protected gid, `VolumesFrom` to an out-of-scope container; every `Mounts/Binds`
entry must be (a) an existing grant-scoped named volume with a validated local driver and no bind
options, or (b) a bind whose host path is inside the grant's `paths`/`mounts`, and never the Docker
socket; named volumes are **never created** by the worker (a volume the mission needs is created by
Core from the approved plan); image pulls happen only through Core from the operator's registry
allowlist when a plan names the image. Container name/labels/networks must resolve (§2.3) inside the
grant scope. Each accepted write is recorded as an event before it is forwarded.

Responses: `inspect` output has environment values and secret-looking labels redacted by the
proxy. `exec` and `archive` into an in-scope container are allowed (diagnosis needs them); the
credentials *that service itself holds* are therefore in scope by definition. What stays out of
reach is credentials for other systems (brokered tools) and every protected resource.
Accepted residual: a worker can change data on a protected-data volume that an in-scope container
already mounts read-write via exec; tripwires (size/entry-count/mtime) catch bulk change, not
subtle edits, so the grant must not scope such a service unless the operator accepts that.
Proxy rules are a reviewed capability, and the discovery/Unraid consumer uses a separate read-only
proxy (brief §14.1 item 9).

### 6.2 Change missions: how new resources get created (design intent; normative in slice 10)
Change missions are Phase 3, owned by **slice 10** (plan in `v4-plan.md`), whose own G1 makes this
section normative. Phase 2 (Repair) uses none of it. The intent, fixed now so Repair does not
foreclose it, with the G1-final constraints already adopted:

- The worker never creates volumes, networks or images. **Core is the sole creator and remover**: it
  pulls images by digest from the registry allowlist, creates volumes and networks (validated local
  driver, no bind options), applies brokered DNS/route/backup writes, requires each name to be
  **absent or already owned by this mission** (a pre-existing same-name object aborts the plan), and
  records actual ids and ownership in a Core-created-resource journal (events plus a table in slice 10).
  The proxy never serves network or volume create/remove to a worker.
- The plan is a strict, hashed model (slice 10): file operations, images each bound to an approved
  digest and to the service that uses it, volumes, networks, binds, services, dns, routes, backups,
  tiles, `verification_plan`, rollback, `protected_touches`. Files are changed by **Core applying the
  approved patch** (source sha256 precondition and resulting sha256 recorded) or by a Core-scoped
  writer that enforces the same preconditions; the native worker does not write Change files
  directly, so "a changed file after approval is refused" is testable.
- Created resources get canonical ids under §2.3 (`…/volume:<n>`, `…/network:<n>`, `…/route:<host>`,
  `…/dns:<name>`). The **effective scope** = `grant.scope ∪ plan resource ids`, persisted on the
  session row as `effective_scope_json` with its sha256, extended only by Core at the moment it
  applies the plan, and used by the proxy, leases and tripwires. The grant hash does not change.
- Execution predicate is the §3 one. Rollback removes Core-created resources in reverse order and
  restores the checkpoint; data volumes are kept unless the operator says otherwise.
- Builds stay refused; a service needing a local build needs a digest-pinned prebuilt image or an
  escalated `docker: raw` mission.
Acceptance (slice 10): add then remove a synthetic service on the test homelab, reversible, plan
hash enforced, pre-existing-name collision aborts, a worker edit to a Change file is refused.

- Remote worker: identity is **the per-session forwarded sockets**, not `SO_PEERCRED` (a forwarded
  socket's peer is the local ssh process). For each remote session Core creates unique worker-socket
  and proxy-socket paths, owned by the forwarder's uid, bound to the session row (`kind=
  remote_tunnel`) plus the token; the remote process holds no other credential and no route to this
  host, so containment is the host boundary. Kill = revoke the token, close both sockets, drain
  in-flight proxy requests, then classify host state; that is "provably dead" for a remote session.
  The same socket forwarded by SSH (`ssh -L`/`-R` with `StreamLocalBindUnlink`),
  dedicated key restricted with `restrict,permitlisten=...,command=` options. No TCP listener.

## 7. Checkpoint contract

`checkpoint.create(mission_id, scope) -> checkpoint_id`; `checkpoint.restore(id)`.

Captured: in-scope Compose files **and `.env` files**, as full copies under
`<state>/checkpoints/<id>/` (mode 0600, Core-owned, never readable by the worker; they hold secrets
at rest on the same host and trust level as the originals) with a manifest of sha256+mode+owner; container
inspect JSON (image id, mounts, networks, labels, restart policy, health); mount state; listening
ports; in-scope unit file hashes. Not captured: data volumes (that is the backup's job; §4 floor).
**Secrets at rest:** the checkpoint root is a 0700 Core-owned directory on the data pool (MOS: under
`/mnt/data`), excluded from off-host backup by path, purged at `closed_at + retention`, and a file
the Core cannot already read is **refused** at scope validation (a mission cannot be granted what
Core cannot restore). Encryption with a separately held restore key is a documented later hardening.
**Post-change manifest** is captured by Core when the session ends or `verifying` starts (and at each
tripwire pause). Restore: write back files (compare-and-swap: current hash must equal the post-change
manifest hash, else refuse, state `needs_operator`, and report the drift), then ask the provider to recreate affected services. Restore is a recorded
mission event and a verified one (the checks re-run).
**Restorable (exact comparison):** captured files (bytes, mode, owner) and, after recreate, each
in-scope service's image id, command, env hash, mounts, networks, labels, restart policy and
health config. **Observational only:** ports, mount table, unit state, volume contents, runtime
health. Provider seam: `HostControlProvider` covers only units, logs and metrics, so slice 5
publishes a versioned `CheckpointProvider` protocol (compose-file discovery, container inspect,
recreate) built on the existing stack orchestrators, with systemd and MOS implementations. MOS
parity is claimed only once that implementation passes its tests.
Backup freshness: Core queries the backup integration for last success time and coverage for each
in-scope path; result stored in the grant (`backup`).

## 8. Brokered integrations (`tool.invoke`)

A tool is `(name, params model, handler, risk, secret_ref)` registered in Core; the Worker passes
params, Core injects the secret and calls the application API, returns redacted output. Initial
tools: `traefik.read`, `compose.dry_run`, `dns.rewrite` (both servers), `arr.queue_item` (read,
remove with flags), `app.health_get`. Each tool declares scope fields checked against the grant.
Tools are the **only** way a Worker reaches application credentials. Adding a tool is a
reviewed capability (listed in `CLAUDE.md`'s v4 surface).

## 9. Tripwires

- Monitor thread in Core scans every `tripwire_interval_s` (default 15) with a hard scan deadline of
  10 s and a per-signal timeout of 3 s. A failed or late scan **fails closed immediately**: pause.
  The pause SLA (the release-proof number) is `interval + deadline + freeze + durable write`, budgeted
  as 30 s worst case from the change to the recorded `paused_tripwire` event, and tested as such. Defaults for diffs: data-volume shrink >10% size or >5% entries;
  `effects.max_unexpected_in_scope` as granted. Baselines are the checkpoint's immutable manifest.
  Matching: a signal's resource id is tested against the grant `scope` and `protected.resources` by
  the §2.3 containment rules (protected wins). Monitor while any mission is
  `executing` (and a longer interval during `investigating`), independent of the Leela scan.
- Signals: container set and per-container image/mounts/networks/labels hash; listening ports
  (`ss`); mount table; protected-resource file hashes (ingress dynamic config, DNS config,
  credential files, firewall rule dump hash, backup job config, sudoers, daemon.json); data
  volume size and entry-count drops beyond a threshold; remote config of named cross-host systems
  where readable; git remote/push activity in live trees (reflog of tracked trees).
- Diff rule: each signal is `allowed` (inside grant scope), `unexpected-in-scope`, or `protected`.
  `protected` pauses; `unexpected-in-scope` is logged and pauses if it exceeds the declared change
  budget. Pause = freeze the session cgroup (and revoke capability endpoints on kill), mission `paused_tripwire`, operator card
  with the diff and options: resume, roll back to checkpoint, take over, cancel.
- Tripwires are advisory against a root-capable agent that actively evades (accepted, brief §6.9);
  they are the control against mistakes, injected instructions and runaway scope.

## 10. Verification catalogue (typed, Core-executed)

A mission's `verification_plan_json` is one canonical model
`{catalogue_v, checks: [{id, check, params}]}` (strict, RFC 8785). The grant's
`verification.checks` and a Change plan's `checks` are **hash-referenced** to this plan: the grant
stores `verification.plan_sha256`, so the exact checks and parameters (route, probe argv, project
target) are fixed by the approved hash and replayable by the release-proof tests. It cannot change
after approval.

| Check | Params | Passes when |
|---|---|---|
| `http` | url or route+resolve override, expect status/regex, timeout | matches, N consecutive times |
| `exec_probe` | container, argv (allowlisted binaries), expect | exit 0 and output matches |
| `container_state` | container, expect running + health | matches AND not only health string |
| `mount_readable` | container or host, path | `stat`/`ls` succeeds inside the consumer |
| `project_complete` | compose project/dir | every defined service exists and is running |
| `log_absent` | container, pattern, window_s | no match in window |
| `file_state` | path, sha256 or mode | matches |
| `query` | bounded read-only query via brokered tool | matches |

Each check has a strict params model (slice 8, G1-reviewed) with bounded timeout (≤60 s), bounded
retries, and an operator-owned allowlist of binaries for `exec_probe` in Core config. A baseline
scan is taken at checkpoint; "no new finding in scope" means no finding absent from the baseline
whose resource id is contained in the grant scope (§2.3). The set is versioned (`catalogue_v`). A mission's verification plan lists check ids and params,
is agreed in the grant before mutation, and cannot be changed after approval. Completion requires
all checks pass **and** a fresh Leela scan has no new finding in scope.

## 11. Supervisor and adapter lifecycle

```
start(mission) → lease acquire → checkpoint (repair+) → spawn session as worker with env from grant
   → heartbeat/stream events (redact → persist) → on result/exit → Core verify → close
```
- Adapter protocol as in the brief §7. `status()` is advisory; Core's mission row is truth.
- The session environment is built by Core from the grant: minimal env, brokered tool endpoint, no
  secret values. Working dir = a per-mission workspace owned by the worker.
- Crash classification (Core, from host state, never from the runtime): compare checkpoint vs. current;
  events show accepted mutations; if state == checkpoint → `needs_operator` offering resume (or
  automatic restart for Observe/Propose only); if state matches the end expectation → verifying;
  else `recovery_required` → `needs_operator` or `rollback_pending`.

## 11.1 External-session import
An imported session was not started under the worker's identity, cgroup, proxy or token, so it is
**evidence-only**: its record becomes events and artifacts on a mission. It may inform a mission but
never moves it to `executing` or `completed`, and a Repair mission still requires verification. A
session that must act is relaunched through the Supervisor.

## 12. Hook layer

The reference adapter installs the runtime's pre/post tool hooks, which call
`mission.post_event` and may block. Blocked classes (enforced even with an unrestricted runtime):
reading secret stores and `.env` contents; destructive VCS in live trees; publishing; broad
`rm`/`mkfs`/`dd`-class commands; `docker volume rm`, `prune -a`; edits to sudoers/daemon.json.
Ask classes: crossing into a protected resource, irreversible-looking operations. Hook failure mode
is configurable (default: fail closed for blocked classes, fail open with a loud event otherwise).

## 13. Ship's Memory

Schema (`memory_cards`, `memory_fts`, `memory_edges`, `memory_history`): DDL, strict card models and
FTS triggers are slice 2's deliverable (G1-reviewed there); rules fixed here: FTS5 external-content
table over title+statement+tags kept in sync by triggers; retrieval orders by scope match, then
confirmed recency, then BM25, capped at N cards and a byte budget; edit history is append-only;
every transition records the actor.
scope filter on JSON tags. Status machine: candidate → confirmed → deprecated; only an operator
action moves a procedure/constraint to confirmed. Retrieval for a session returns **confirmed**
cards only, rendered as quoted data with ids; candidates appear in the operator UI only.
Import format and rules as in `v4-server-memory-export.md`. Importer is idempotent by `id`, never
overwrites a confirmed card (creates a conflict candidate instead), and rejects secret-shaped
content using redaction's detector as a pre-check.

## 14. Egress policy

Allowed to leave: briefs, redacted logs, redacted config excerpts, confirmed memory cards, to the
configured runtime/vendor only. Never: `.env` contents, credential stores, brokered secrets,
private keys, tokens (detected by redaction + path denylist). Local runtimes unrestricted. Egress
is recorded as an event (bytes, destination label). Redaction is layer one of three (the others:
path denylist at the hook layer, credential scoping).

## 15. Setup, undo and uninstall (what slices must add)

Every one of these is a setup step with an undo-journal entry and a repair path: `pe-worker` user and
group; worker home/workspace/checkpoint dirs (on the data pool for MOS); worker socket; generated
sudoers file for the worker tier (exact rules, `visudo -c`, shown to operator); docker socket proxy
and removal of unauthenticated TCP; systemd unit or init script for the supervisor if split out;
SSH forced-command key for a remote worker. Preflight (brief §6.8) runs in `setup repair` and before
each Repair start.

## 16. Threat model

| Threat | Vector | Control | Residual |
|---|---|---|---|
| Prompt injection from logs/web | hostile text in a container log | data-not-instructions framing; tripwires on effect; hooks; protected resources; no standing grant for change/critical | Medium: agent may act within scope on bad advice |
| Confused deputy via Core | worker calls Core with attacker aim | worker socket allowlist, session token, scope check on every tool | Low |
| Worker escapes to root | docker group, open TCP socket, sudo wildcard, unmanaged rules | §6.8 preflight; separate identity; generated sudo; no docker TCP | Medium on a compromised kernel/runtime |
| Secret exfiltration | logs/configs sent to vendor; agent reads `.env` | egress policy; path denylist; brokered tools; redaction; no key in env | Medium: redaction has known gaps |
| Memory poisoning | runtime proposes a malicious "procedure" | quarantine; operator confirm; confirmed-only retrieval | Low |
| Runaway cost/loop | agent loops | budgets; kill; spend best-effort | Low |
| Destructive VCS/publish | `reset --hard`, push to public remote | hook blocks; protected domain; tripwire on reflog/remote | Low-Medium |
| Crash replay | supervisor/host dies mid-mutation | event journal before dispatch; Core classifies from host state; no replay | Low |
| Stale lease | holder dies | TTL + fencing; Core rejects stale fence | Low |
| Approval bypass | edit grant after approval | immutable grant, hash binding | Low |
| Downgrade | v3 reads v6 store | v3 refuses newer schema; documented snapshot restore | Operator action |
| MOS gaps | systemd-only code paths | provider-aware reads; proposal-only until parity | Documented |

## 17. Test and rollout strategy

- Unit and fixture tests per slice; the shared fixtures live in `tests/fixtures/v4/` (synthetic;
  anonymised mirrors of the private export's incident shapes).
- A **hostile-fixture suite** (planted secret, hostile log line trying to widen the grant,
  protected-resource change, stale lease, crash at each phase, forged session token) runs in CI
  from slice 3 onward.
- Missions are off by default behind `missions.enabled`. Each slice leaves `v4-next` releasable as
  v3. Release proof is the brief §11.3 list; each item gets a named test or a rehearsal record in
  `docs/handoff/v4/`.
- Rehearsal environments: the repo's test homelab (`tests/homelab`), the Ubuntu dev host, and the
  MOS VM.

## 18. Decisions this spec closes, and what it leaves open

Closed: store layout (§2), state machine (§3), grant schema and hashing (§4), worker socket (§6),
checkpoint (§7), tripwire loop (§9), check catalogue (§10), memory rules (§13), egress (§14).

Open for G1 review: (a) one process or two for Core + Supervisor (default: Supervisor is a thread
pool inside Core with the session as a child process; revisit if crash isolation demands more);
(b) which runtime is the reference adapter and how its hooks are installed; (c) the exact default
thresholds for tripwire diffs (data-volume shrink, change budget); (d) per-OS mechanism to keep the
checkpoint store unreadable by the worker on MOS (no ACLs: groups and modes), **and whether MOS can
delegate a cgroup per session** (§2.1); if not, the per-uid fallback or proposal-only applies.

## 19. G1 review record (Codex, 2026-10-13) and disposition

All findings accepted; rev 0.2 changes in brackets.
- Raw Docker is host root, contradicting "worker cannot reach root" [I4, §4 `docker`, §6.1 proxy,
  brief tier table and release proofs 2-3].
- Fencing cannot bind native mutations [I5, §2.1 provably-dead reclaim, cgroup/uid fallback].
- Lease overlap [§2.3]. Stop/kill only the process group [cgroup freeze/kill].
- Approval bound to hash had no storage [`mission_approvals`]. Change/Critical second approval
  [`plan_json/plan_hash`, §3]. `rollback_pending` mismatch [§3]. Runtime-outage resume vs. brief [§3, §11].
- Session token storage and same-uid impersonation [`mission_sessions`, §6 cgroup binding, fd delivery].
- Checkpoint `.env` restore and missing post-change manifest [§7]. Provider protocol gap [§7
  `CheckpointProvider`]. Untestable "exactly" [§7 restorable vs observational].
- Missing: cancel columns/FKs [§2]; models/canonicalization [§2 RFC 8785, strict models in slice 1];
  change budget and matching [§4 `effects`, §9]; check params and scope predicate [§10]; memory DDL
  [§13, slice 2]; scan deadline and thresholds [§9].
- Snapshot-then-migrate claim [§2 upgrade guard]. `mission` origin rejected by `RUNBOOK_ORIGINS` [§5].

## 20. G1 round 2 (Codex) and MOS verification

Round 2 accepted in full [rev 0.3]: named-volume bind bypass and the wider Docker API surface
[§6.1 rewritten: deny-by-default, field rules, no volume create, no build/import, pulls only via
Core]; Repair entering `executing` without an affirmative approval [approval rule]; fence reuse after
release [`resource_fences`]; remote-worker identity [§6 remote]; uid-fallback representability
[`session_uid`, range, helper]; double live session [partial unique index, single transaction]; `.env`
secrets at rest [§7]; SLA arithmetic [§9; brief proof 3 aligned]; `blocked`/verify-fail dead ends
[§3, `on_verify_fail`]; external-session containment [§11.1]. One finding is **accepted as residual**:
exec/archive into an in-scope container exposes that service's own credentials, and subtle edits to
mounted protected data evade size-based tripwires (§6.1, stated, not hidden).

MOS capability check (test VM, kernel 6.18, 2026-10-13): unified cgroup v2 is mounted; a child
cgroup can be created, populated, **frozen** (`cgroup.freeze`) and **killed** (`cgroup.kill`) and
confirmed empty (`cgroup.events populated 0`); `useradd`, `setpriv`, `unshare` and `runuser` exist.
So the cgroup path is available on MOS. Not yet proven: delegation of a cgroup subtree to the
non-root Core user (the cgroup tree is RAM-backed, so a boot-time init script must create and chown
it), and the setup-created `pe-worker` account surviving reboot (users are recreated each boot).
Both are slice 3 and slice 6 setup work with MOS tests.

## 21. G1 final check (Codex, 2026-10-13) and disposition

Seven blockers, all accepted, one follow-up. Resolved in rev 0.4: effective scope without a model,
network create conflict, diff hash not binding the result, local proxy session binding, approval
predicate conflict, verification-plan parameters, and Change having no owning slice. The first three
and the owning-slice item are handled by moving Change to **slice 10** with the constraints written
into §6.2 (they affect only Phase 3); the proxy binding, execution predicate and verification-plan
model are normative now (§3, §6.1, §10). The `approval_id` wording follow-up is fixed in §2.
Remaining work is owned by slices, not by slice 0.

## 22. Session identity and launcher (slice 3d; rev 2 after G1 review; OPERATOR-APPROVED 2026-10-13: unique uid per session, DirectLauncher on MOS, proposal-only when the preflight fails)

Findings that shaped this section (Codex G1, 2026-10-13): Core is unprivileged on Ubuntu so it cannot
spawn as another uid or move that process into a cgroup; MOS has **no sudo** and PE runs as root there
(`setup/plan.py`); a shared worker uid lets two sessions ptrace each other and defeat the per-cgroup RPC
binding; the helper's own install path, GID, and delegated file ownership were unspecified.

### 22.1 Identity: one unique uid per live session (all hosts)
- Setup reserves a **uid range** (e.g. 61000-61999) and creates, per uid, a private group with the same
  gid and **no supplementary groups**. No `pe-worker` shared account. `mission_sessions.session_uid` is set
  for every session kind and is unique (already indexed); allocation is transactional in `start_session`.
- A session's workspace is owned by its uid, mode 0700; uid, gid and workspace are released at reap. A
  reaped uid is not reused until its cgroup is gone and no process runs as it (`kill -9 -1` as that uid via
  the launcher, then verify none remain).
- The RPC binding (§6) is cgroup AND uid AND token; a cross-session ptrace/`/proc/<pid>/fd` attempt must
  fail by uid separation (test it). `kernel.yama.ptrace_scope` is not relied on.
- `worker_uid` must never equal the Core uid or 0 (reject in config validation and at spawn); the gid must
  equal the uid's private gid exactly; any other pair is refused.

### 22.2 Two launcher strategies, chosen by host capability, never silently downgraded
- `DirectLauncher`: Core already has the privilege (**MOS: Core runs as root**; also a Core granted
  `CAP_SETUID/SETGID/CHOWN/DAC_OVERRIDE` by a systemd unit if the operator chooses). It performs the §22.3
  steps in-process. MOS needs no sudoers artifact and no helper.
- `SudoLauncher` (Ubuntu with unprivileged Core): `sudo -n /usr/libexec/planetexpress/pe-launch` with the
  sudoers rule `<core-user> ALL=(root) NOPASSWD: /usr/libexec/planetexpress/pe-launch ""` (the empty
  string argument forbids any argv; verified by a test that `sudo -n pe-launch --x` is refused). Input is
  one JSON spec on stdin: `{session_id, worker_uid, cwd, argv, env, token}`; **the gid, cgroup root,
  uid range and workspace root come from a root-owned config the helper loads itself, never the spec.**
- Preflight (setup, repair, and before each Repair start) picks the strategy and fails closed with the
  exact missing prerequisite; a host that can do neither is proposal-only.
- Install hardening (both): the helper and its config live at fixed root-owned paths whose every ancestor
  directory is root-owned, not group/world writable, and not a symlink (`/usr/libexec/planetexpress/`,
  `/etc/planetexpress/launch.conf` 0600 root); the helper opens paths with `O_NOFOLLOW`/`openat2
  RESOLVE_NO_SYMLINKS`, checks the cgroup root with `statfs` for `CGROUP2_SUPER_MAGIC`, and installation
  tests cover writable and symlinked ancestors. It is never installed under a Core-writable path.

### 22.3 Launch steps (in the helper or in-process)
1. Validate session id (`^[A-Za-z0-9_-]{1,64}$`), uid in range and not Core/0, cwd under the workspace root,
   owned by that uid, not a symlink, `argv[0]` absolute, bounds on argv/env/token, no NUL.
2. **The launcher owns cgroup creation** (`create()` and spawn become one `launch(session_id, spec) ->
   ContainedSession`, returning only after success; on any failure it removes what it made). It makes
   `<cgroup_root>/<session_id>` (must not exist) and sets ownership: directory and `cgroup.procs`,
   `cgroup.freeze`, `cgroup.kill` owned by the Core uid mode 0644/0755 (so Core can freeze/kill/read
   emptiness unprivileged); `cgroup.events` world-readable. The worker uid owns nothing in it.
3. Pipe with the token to the child as fd 3; close every other fd above 2.
4. Join the cgroup, `setgroups([])`, `setgid(gid)`, `setuid(uid)`, verify, `no_new_privs`, `setsid`, reset
   rlimits, `chdir`, `execve` with exactly the spec env. The exec'd pid is not tracked; the cgroup is.
5. Tests prove: Core's write to `cgroup.freeze`/`cgroup.kill` succeeds and the worker's gets EACCES; the
   token is absent from `/proc/<pid>/cmdline` and `environ`; no stray fds; double-fork escapers stay in the
   cgroup; spec fields cannot select another gid, root, or path.

### 22.4 cgroup layout and capability gates
- Ubuntu/systemd: `casa-planetexpress.service` has `Delegate=yes`; at start Core moves itself into a
  `<delegated>/core` leaf, then session cgroups are siblings under `<delegated>/sessions` (an empty
  parent, so domain controllers such as `pids` can be enabled in `subtree_control` and `pids.max` set per
  session). MOS: the same tree created by a boot script (RAM-backed cgroupfs) owned as above.
- Preflight performs real operations on a throwaway cgroup: create, move, freeze **and confirm
  `cgroup.events frozen 1`**, kill, confirm `populated 0`, rmdir, and checks `cgroup.kill` exists
  (kernel >= 5.14). `freeze()` must poll `frozen 1` before reporting paused. A host that fails any preflight step is **proposal-only** for
  missions: there is no weaker fallback containment (decided 2026-10-13).

### 22.5 Setup, repair, undo (named steps, each with probe, repair and undo)
`session-uid-range`, `launcher-helper` (file, config, ancestors check), `launcher-sudoers` (Ubuntu only;
fragment `/etc/sudoers.d/planetexpress-launch`, `visudo -c`, and a probe that `sudo -n pe-launch` works and
`pe-launch --x` is refused; must not be skipped when the legacy sudoers file is preserved),
`cgroup-tree` (systemd unit settings or MOS boot script), `workspace-root`. MOS: all state on the data pool,
recreated by the boot hook.

### 22.6 Stated assumption (not a conclusion)
On Ubuntu, Core is also in the `docker` group today, so a compromised Core is already root-equivalent and
the helper adds no authority Core lacks. This is an assumption about the current deployment, stated per
host in the threat model; if the docker-group assumption is removed the helper's checks are the boundary
and must be reviewed on their own. The release proof checks the observable prerequisites.

### 22.7 No `preexec_fn` in a multithreaded Core (from the slice 3 review)
`Popen(preexec_fn=...)` from the threaded Core daemon can deadlock between fork and exec, and the hang
would hold `Supervisor` state. Both launchers therefore spawn through the **same exec helper**: Core
starts `pe-launch` as a plain subprocess (via `sudo -n` on Ubuntu, directly when Core is root on MOS)
and all cgroup join, uid/gid drop, `no_new_privs`, fd closing and `execve` happen inside that
single-threaded helper. Core never runs Python between fork and exec. `DirectLauncher` is the same
helper without `sudo`, so there is one launch implementation to review and test.

### 22.8 Normative clarifications (rev 3, from the final Codex re-check; these supersede any conflicting wording above)
1. **Sockets.** No shared `pe-worker` group exists. Per-session worker and proxy sockets live in a
   per-session directory `<sock_root>/<session_id>/` created by the launcher owned by the Core uid with
   the session's private gid and mode 0750; Core creates the two sockets inside it and the launcher
   (root helper) then chowns exactly those paths to `session_uid:session_gid`, mode 0600, before
   exec. `<sock_root>` is root/Core-owned mode 0711 so a session can traverse to its own directory only.
   The old `0660 group pe-worker` wording in §6/§6.1 is withdrawn.
2. **One helper, no in-process path.** Both `DirectLauncher` and `SudoLauncher` invoke the same helper
   as a plain subprocess (the first only skips `sudo`). §22.2's "in-process" wording is withdrawn.
3. **UID allocation.** `start_session` is the sole allocator. It commits a session row in state
   `launching` holding the uid (unique index) **before** the helper runs; the helper only validates the
   uid it is given. The row becomes `live` after the helper's status line. A uid is released only after
   the session cgroup is removed and no process runs as that uid. A crash between commit and launch leaves
   a `launching` row that recovery kills (cgroup + uid sweep) before reuse.
4. **Interpreter and sudo environment.** The sudo target is a root-owned fixed entry point (a native
   wrapper, or `/usr/bin/python3 -I -S pe-launch.py` invoked by it). The sudoers fragment sets
   `Defaults!<path> env_reset, !setenv, !env_editor`; the helper builds its own minimal environment
   before loading config or code. Tests run it with a hostile Core environment (`PYTHONPATH`,
   `PYTHONHOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH`) and prove it is ignored.
5. **FD map and status protocol.** The child gets exactly: fd 0 `/dev/null`; fd 1 and 2 one merged output
   pipe whose read end Core holds (the helper's own stdout, so it flows through sudo); fd 3 the token pipe
   read end. Nothing else. Before `execve` the helper writes one framed line
   `PE-LAUNCH-STATUS {"ok":true,"session_id":...,"uid":...,"cgroup":...}` (or `{"ok":false,"error":...}`
   with non-zero exit) as the first line of the merged output; Core reads and strips it, then returns the
   `ContainedSession`. Failure cleanup removes the cgroup and session dir. "No stray fds" is tested by
   listing `/proc/<child>/fd` and requiring exactly `0,1,2,3`.
6. **Preflight extended.** Besides create/move/freeze(+confirm)/kill/empty, it enables the `pids`
   controller on the sessions parent, sets and reads back `pids.max` on a throwaway child, and performs the
   freeze/kill writes as the **actual Core identity** and the denied writes as a **throwaway session uid**
   (via the helper), failing the host (proposal-only) if any expectation differs.
7. **Test wording.** Assert the session uid gets EACCES on *writes* to `cgroup.procs`, `cgroup.freeze`,
   `cgroup.kill` (and on ancestors' `cgroup.procs`); assert `cgroup.events` readability separately as intended.
