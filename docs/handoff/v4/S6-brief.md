# S6 — Tripwires and grants (v4 slice 6) — SAFETY SURFACE (G1 + G3, one reviewer not Claude)

Read first: `docs/designs/v4-slice-0-contracts.md` §2.3 (resource ids and containment), §4 (grant schema), §8, §9, §15,
§16; `docs/designs/v4-mission-control.md` §5, §5.3, §6.2, §6.3, §6.8, §6.9; `docs/designs/v4-plan.md` slice 6;
the merged `planet_express/missions/` (`models.Grant`, `canonical.py`, `approvals.py`, `store.grant_resources`,
`supervisor.py` freeze/pause paths, `worker_rpc.py`, `launcher/` for how the session env is built).
Depends on slice 5 (`CheckpointProvider`, immutable baseline manifest). If S5 is not merged, build against its
Protocol with fakes and say so in the report. `git merge --ff-only v4-next` first; do not push; Co-Authored-By.

## Deliver
1. `planet_express/missions/grants.py`: grant validation per contracts §4 (scope subset of discovered inventory, no
   protected resource in scope unless tier=critical, checks exist, budgets within site maxima, backup floor),
   `grant_hash` (RFC 8785 canonical, already in `canonical.py`), and **grant-to-environment builder**:
   `build_session_env(grant, ...) -> SessionEnv` returning exactly the env vars, brokered tool names and files the
   session may see. Default deny: credentials not named in `grant.credentials` are absent from env, cwd tree and
   readable paths. A grant change after approval changes the hash and invalidates `mission_approvals` (test it).
2. Protected domains: site config `missions.protected` lists CONCRETE resources (contracts §2.3 ids); a built-in
   default set (ingress dynamic config, DNS config, credential files, firewall dump, backup config, sudoers,
   `daemon.json`, docker socket) cannot be emptied silently (removing a default needs an explicit recorded
   override). Setup proposes the list from discovery; the operator confirms. Matching uses §2.3 containment and
   **protected wins**.
3. `planet_express/missions/tripwire/`: `signals.py` (one collector per signal in §9: containers + per-container
   image/mounts/networks/labels hash, listening ports, mount table, protected file hashes, data-volume size and
   entry-count drops, git remote/push activity), `monitor.py`, `diff.py`.
   - Each signal read has a 3 s timeout, the scan a 10 s deadline, default interval 15 s (`tripwire_interval_s`);
     a failed, late or partial scan FAILS CLOSED: pause. Longer interval during `investigating`.
   - Baseline = the checkpoint's immutable manifest + a live state capture at `executing` start; never "whatever
     was seen last".
   - Diff verdict per change: `allowed`, `unexpected_in_scope` (counted against `effects.max_unexpected_in_scope`
     and per_resource budgets), `protected` (immediate pause).
   - Pause = freeze the session cgroup, revoke capability endpoints, durable write of `paused_tripwire` with the diff
     (redacted, bounded), operator card payload with options resume / roll back (calls S5 restore) / take over / cancel.
     Budget 30 s worst case change->recorded event; a test measures it with a fake clock and a real-time smoke.
   - The monitor runs while any mission is `executing`, independent of the Leela scan, in its own thread with its
     own failure isolation: a monitor crash pauses every executing mission (fail closed), never silently stops.
   - Hostile content: log lines, file contents and container labels are DATA; the hash of the grant never changes
     during a mission; tests plant instruction-like strings in logs/labels and assert no behaviour change.
4. Brokered tools (`tool.invoke`, contracts §8): registry `(name, params model, handler, risk, secret_ref)`,
   Worker RPC method `tool.invoke` on the worker socket only, per-tool scope fields checked against the grant,
   Core injects the secret, output redacted and bounded, every call recorded. Initial tools: `traefik.read`,
   `compose.dry_run`, `app.health_get`, `arr.queue_item` (read), `dns.rewrite` (write ones stay gated until slice 7).
   Secrets live only in Core's config; a scan of the session env, cwd, readable files and worker socket responses
   finds none.
5. Setup preflight (brief §6.8) as named launcher-style steps with probe/repair/undo, each FAILS CLOSED with a test:
   (a) worker identity distinct from Core and not in `docker` beyond tier; (b) no unauthenticated Docker TCP
   listener reachable (name the exact exposure; a socket proxy with allowlist is acceptable); (c) sudoers contains
   only generator-managed rules, unmanaged wildcard rules are reported and removed by `repair` only with approval.
   A Repair-tier start refuses unless all pass; Observe/Propose are unaffected.

## Acceptance tests (write first)
- Planted-change test per protected domain: ingress, DNS file, credential file, firewall dump, data-volume shrink
  (>10% size or >5% entries), backup config, sudoers, daemon.json: session frozen and mission `paused_tripwire`
  within one scan interval + deadline; rollback offered; the pause event carries the diff.
- Out-of-scope container created / mount added / image changed on a non-scope service => detected the same way.
- Failed scan, timed-out signal, collector exception and monitor crash each pause (fail closed); a paused mission
  stays paused after a Core restart (durable) and the freeze is re-applied.
- In-scope allowed change does not pause; `unexpected_in_scope` over budget pauses; protected-inside-stack case
  (a stack with an ingress container and a helper): recreating the helper is allowed, touching ingress pauses.
- Grant -> env: no DNS/backup/router credentials in env, files or reachable paths unless named; brokered tool call
  works and the key is never returned; a hostile log line cannot change the grant hash; changed grant invalidates the
  approval; standing-grant rules from `approvals.py` unchanged.
- Each preflight side door has a failing-host test and a passing-host test; the privileged subset runs in docker and
  on the MOS VM (`scripts/run_privileged_tests.sh`); sudoers repair needs approval and undo restores the file.
- Missions disabled => no monitor thread starts. Full suite green.

## Report
Files changed, test counts, deviations from the contract (list each), open questions, and the measured pause latency.
