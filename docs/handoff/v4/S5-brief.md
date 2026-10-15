# S5 — Checkpoint (v4 slice 5) — SAFETY SURFACE (G1 + G3, one reviewer not Claude)

Read first: `docs/designs/v4-slice-0-contracts.md` §2.3, §3, §7, §15, §22.8; `docs/designs/v4-mission-control.md`
§6.1, §6.9; `docs/designs/v4-plan.md` slice 5; `planet_express/execution/compose_files.py` (CAS write/restore
already exists: reuse its path checks and permission/ACL copy), `systemd_stack_orchestrator.py`,
`mos_stack_orchestrator.py`, `host_control_*.py`; `planet_express/missions/{store,models,state,schema}.py`
(`checkpoint_id` column and `rollback_pending`/`rolled_back` states already exist).
Run `git merge --ff-only v4-next` first; do not push; Co-Authored-By trailer. Missions stay OFF by default.

## Goal
Before a Repair mission's first mutation Core snapshots the in-scope files and records host state; after a crash or
a tripwire the operator can restore it, and restore refuses to overwrite anything that drifted. Slice 5 builds the
service and its provider seam. It does NOT start Repair sessions (slice 7), does not run tripwires (slice 6) and
does not decide approval. Its entry points are called by later slices and by tests.

## Deliver
1. `planet_express/missions/checkpoint/` package:
   - `provider.py`: versioned `CheckpointProvider` Protocol (v1): `discover_compose(stack) -> list[ComposeFile]`
     (compose + `.env` + overrides, resolved real paths, inside the stacks root only), `inspect(service) ->
     ContainerFacts` (image id, command, env hash, mounts, networks, labels, restart policy, health config),
     `listening_ports()`, `mount_table()`, `unit_hashes(units)`, `recreate(services) -> RecreateResult`.
     Implementations `SystemdCheckpointProvider` and `MosCheckpointProvider` built on the EXISTING stack
     orchestrators/docker access (no direct `systemctl`/shell strings, argv only). Docker inspection uses the
     existing docker access path; if only the raw socket exists, read-only calls only.
   - `store.py`: checkpoint root `<state>/checkpoints/<id>/` (dir 0700 Core-owned, files 0600), manifest
     (sha256, mode, owner uid/gid, size, real path, symlink flag) written atomically, fsynced, then a `COMMITTED`
     marker file written last; a directory without the marker is garbage and never restorable.
   - `service.py`: `create(mission_id, scope) -> checkpoint_id`, `capture_post(checkpoint_id)`,
     `restore(checkpoint_id, *, ack_drift=False) -> RestoreReport`, `purge(checkpoint_id)`.
   - `backup.py`: `BackupFreshness` protocol + a config-driven implementation (last-success time and covered paths
     per in-scope path, from the configured backup integration; if none configured the answer is "unverifiable").
2. Scope validation (Core, before any capture): every in-scope file must be readable by Core and inside the stacks
   root after `realpath`; symlinks leaving the root, devices, FIFOs, sockets and files over `checkpoint.max_file_bytes`
   (default 1 MiB) are REFUSED, not skipped. A file Core cannot read refuses the grant ("cannot grant what Core cannot restore").
3. Backup floor: `create` refuses (typed error) when any in-scope data path has no verified backup within
   `grant.backup.recent_s`, unless `grant.backup.acknowledged_no_backup` is true; when acknowledged, the checkpoint
   records `no_backup=true` and the mission is classed Critical by the caller (return the flag, do not change tier here).
4. Restore (CAS): for each captured file, current sha256 must equal the post-change manifest hash (or the original
   hash if no post manifest exists because the session never started). Any mismatch => nothing is written, mission
   -> `needs_operator`, report lists drifted paths. `ack_drift=True` (operator decision, recorded with actor) restores
   over drift after saving the drifted bytes under `<id>/drift/`. Writes are atomic (temp + fsync + rename), restore
   bytes, mode and owner. Then `provider.recreate()` for affected services and compare each service's restorable
   facts (image id, command, env hash, mounts, networks, labels, restart policy, health) with the captured ones;
   differences are reported as `unrestored`, never silently accepted. Ports/mount table/unit state/volume contents
   are observational only and reported, not asserted.
5. Durability and lifecycle: store survives reboot (state dir on the data pool for MOS, `/mnt/data/...`); startup
   scan quarantines uncommitted directories; `purge` at `closed_at + retention` (default 90 d, mission row keeps the
   reference); purge refuses while a mission still references the checkpoint in a non-terminal state. Disk guard:
   refuse a capture that would exceed `checkpoint.max_total_bytes` (default 256 MiB per checkpoint, 2 GiB store).
6. Store/schema: only if needed, a `checkpoints` table (id, mission_id, created_at, committed_at, post_at, no_backup,
   state). If the table is added it ships with the migration path and snapshot guard from slice 1; schema version bump.
7. Mission events: `checkpoint.created`, `checkpoint.post_captured`, `checkpoint.restore_started|refused|done`,
   all via the existing event path with redaction. Never put file CONTENTS in an event or log; only paths+hashes.
8. Operator RPC additions are NOT in scope (slice 7 wires approval); expose service calls for the supervisor only.

## Acceptance tests (write first; each side door has one)
- Byte-for-byte round trip: tree with files of modes 0600/0640/0755, a file with an ACL, a `.env`; capture, mutate,
  restore => identical bytes/mode/owner (owner part skipped when not root, asserted when privileged).
- Crash safety: kill between manifest write and marker => directory is quarantined at next start and never restored;
  kill mid-restore (fault-inject after N files) => re-running restore completes and ends identical (idempotent).
- CAS: a post-change file edited by someone else => restore refuses, writes nothing, reports the path, mission
  `needs_operator`; `ack_drift` restores and keeps the drifted bytes.
- Refusals: symlink leaving the root, symlink swapped for a path after validation (TOCTOU: open with `O_NOFOLLOW`
  on a dirfd-relative walk and verify `fstat` matches the manifest), unreadable file, FIFO, oversize file, path
  outside the stacks root, `..` components. No partial checkpoint remains after any refusal.
- Backup floor: stale, absent and unverifiable backups each refuse; acknowledged flag lets it through and records
  `no_backup`; the flag never defaults to true.
- Secrets: checkpoint dir and files have the exact modes; the worker identity (a different uid in the privileged
  tests) cannot list or read them; no checkpoint content appears in events, logs, or error strings.
- Provider: both providers pass the same conformance suite against fakes; MOS parity claimed only after its
  conformance run passes. Run the privileged subset via `scripts/run_privileged_tests.sh` (docker; MOS VM).
- Restore recreates a service and a deliberately drifted label/env is detected as `unrestored`.
- Retention: purge removes the dir and keeps the DB reference; purge refused for a non-terminal mission.
- Missions disabled => no checkpoint code path runs. Full suite green.

## Report
Files changed, test counts, deviations from the contract (list each), open questions. Do not widen scope.
