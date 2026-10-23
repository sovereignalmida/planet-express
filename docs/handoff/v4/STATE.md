# v4 STATE (update at every checkpoint, commit with it)

Branch: `v4-next` (cut from `v3-next` @ d861838). v3-production is live; no deploy without an
operator go. Protocol: `docs/designs/v4-orchestration.md`.

## Slice status
| # | Slice | Status |
|---|---|---|
| 0 | Contracts, threat model | DONE rev 0.4 (final G1 dispositioned); needs operator sign-off |
| 1 | Mission store | MERGED; Codex G3 + Claude review fixed |
| 2 | Ship's Memory | MERGED; Codex G3 x2 fixed |
| 3 | Supervisor, adapter, worker | MERGED (3a-3d); Codex ~9 rounds + Claude /code-review fixed. Privileged green docker + MOS + Ubuntu VM. Launcher steps wired into setup (missions answer, default off) |
| 4 | Surfaces | MERGED (rounds 1+2). Codex G3 on round 2 still pending from the 2026-10-13 usage limit; G4 whole-branch review covers it |
| 5 | Checkpoint | MERGED (3 Codex rounds + code-review). MOS parity not claimed until a real-compose conformance run |
| 6 | Tripwires and grants | MERGED (code-review 10 fixed, Codex 2 fixed) |
| 7a | Approval flow, daemon wiring, hooks | MERGED |
| 7b | Docker proxy, sudo generation | MERGED; review rounds + audit fixes (labels, host-root-equivalent mounts); privileged green on docker + both VMs |
| 8 | Verification, recovery | MERGED (Codex 2 rounds + code-review) |
| 9 | Release proof, rehearsal harness, docs | Ubuntu item 7 repair rehearsal passed 2026-10-23; MOS repair parity and open-side-door refusal remain unclaimed |
| 10 | Typed steps from `mission` origin | Not started; carried items in `S10-notes.md` (execution-safety, second review required) |

## Next action
**Handoff 2026-10-22 (token budget).** All workers finished and merged into `v4-next` (62 commits ahead of origin, NOT pushed). Non-privileged suite 6262 passed. No background agents running.
Merged: S9 (release proof), S9b (rehearsal agent through the real Docker proxy, scenarios end cleanly), G4-F1 (protected-set discovery, fail closed), G4-F2 (8 safety fixes + 13 follow-up findings, Codex clean), G4-F3 (rows 11-21, Codex clean), G4-F4 (`container:<in-scope>` network mode + `GET /version` through the proxy; Codex 2 P1s fixed), plus the grant-budget default fix. G4 record: `G4-review.md` (F2 follow-up dispositions are in the F2 worker reports: all 13 fixed, none declined; F3 and F4 likewise).
**Do next, in order:**
1. Item 7 passed on PE_UBUNTU_TEST on 2026-10-23: namespace repair completed through the proxy (15 writes/3 creates), the failed canary rolled back, the service-scoped correction completed (5 writes/1 create), no proxy refusals, missions released and teardown empty. Record: `/tmp/planet-express-rehearsal-20261023-final.json` on the operator host. The run exposed and fixed Compose inspect-shaped create defaults, checkpoint comparison of generated labels, an in-flight Docker/firewall tripwire race, service-scoped project-network reattachment, and a stale harness baseline. `GET /info` was not needed.
2. Privileged suite re-run 2026-10-23 after the item 7 fixes: 259 passed, 5 skipped; the earlier fd-leak flake in `test_exactly_fds_0_1_2_3...[sudo]` did not recur.
3. Claude `/code-review high` on F3 and F4 ran 2026-10-23 (9 findings, all dispositioned; Codex had already reviewed them). Fixed: scans are serialised (`begin()` on the supervisor thread and the monitor thread shared one Scanner and could report each other as timeouts); `container:<target>` now also requires each network the target is attached to to pass the same scope rule as a direct attach; the start-deferral timer resets on a failed start and when a mission settles; `/version` clamps `Components[].Details.ApiVersion`; the brief's data-handling text has one source; a stale `baseline.prev.json` is cleared when there is nothing to set aside. Left, with reasons: start check on host/`container:` network mode applies only to containers carrying the proxy's CREATED_LABEL, so existing in-scope containers (Plex, gluetun-attached apps) are unaffected (finding was a false positive); `worker:decision` actor is deliberate (a worker's free-text notes must not forge cards) and v4 has no pre-upgrade cards; `_mission_locks` (one Lock per mission) and the 256-entry usage cache re-read from the log on a miss, so neither affects correctness; the launcher preflight on the poll thread is the existing design, re-proved at most every 600 s.
   Item 7 fixes got `/code-review high` on 2026-10-23 (Codex skipped, usage limit). Dispositions: tripwire deferral no longer skips the scan, it excuses only `firewall_changed` while a proxy write is unresolved or just finished, capped at 120 s of continuous cover (deliberate: a container stop takes ~10 s and Docker rewrites its rules throughout, and a 5 s cap failed the rehearsal); `docker.result` now closes on upstream failure (status 0); in-flight query bounded to the event tail; label filter shared (`comparable_labels`) and narrowed to Compose's per-recreate labels, `config-hash` included because it is measured unstable under `network_mode: service:` (rehearsal re-run proves it); snapshot failure is logged; stale/corrupt deferral state tolerated. Left as designed: project-network reattachment (needs the service in scope, the same project label, and prior membership at session start). Rehearsal passed again after the fixes (record `/tmp/planet-express-rehearsal-20261023-review-fixes.json`); privileged suite 259 passed, 5 skipped (the fd-leak flake showed once on [direct], 3 clean reruns).
4. S10 (`S10-notes.md`, includes the policy network-guard gap for steps with empty params, G4-review row 9). Execution-safety: Codex + Claude review.
5. Final G4 re-run on the finished branch (whole-branch Codex, `/code-review high`, fuller `/cso` with dependency CVE scan), then update `G4-review.md`.
6. Operator: slice 0 sign-off; v3 carry-overs in `G4-review.md` (the setup-server exit-mid-install one is worst); push and deploy are yours. MOS repair rehearsal not claimed (no compose plugin). Existing installs with an explicit uid range 61000-61999 must follow INSTALL.md "Moving an existing uid range".
Shut the VM down when finished: `virsh shutdown PE_UBUNTU_TEST`. Estimate to ship: roughly 2-3 working days (S10 is the largest item).

## Decisions since the brief
- 2026-10-13: brief §14 decisions 1-8 recorded.

## Open
- Pushed `v4-next` to origin (public repo: keep infra detail out of tracked files; private inputs go in gitignored `docs/private/`).
- Operator-side findings from the export, separate from v4: legacy sudoers wildcard line, open Docker TCP socket, broken VPN port-forward cycle, leftover timers, AdGuard in paused_containers, two credentials to rotate.
- MOS test VM was unreachable on 2026-10-13; needed before slice 6.

## Usage / account
Primary orchestrator: Claude Code (account A until the 2026-10-22 usage limit; continued on a second account). Budget notes: (none yet).

## Resume commands
- Tests: `pytest tests -x -q` (targeted first).
- Review: `codex review --base v4-next` / `--uncommitted`; Claude: `/code-review`.

## Deferred findings (owned by a later slice)
- Slice 7: standing-grant `window_json` is stored but not evaluated; grant `protected`/`exclusions` not cross-checked against standing scope or plan (spec §5.2 needs a window format).
- Slice 3: supervisor must confirm session dead BEFORE requesting a terminal transition (terminal transition now frees leases/session in one txn).
- Existing v3 code review findings (not v4): quiet re-scan dropped when refused in a maintenance window; MOS `docker-compose config --format json` may fail on Compose v1; severity wobble re-planning. Unverified.

## Review debt
- Slice 4 round 2 Codex review (missed at the 2026-10-13 usage limit): folded into the G4 whole-branch review.
- 2026-10-23: Codex usage limit hit; item 7 rehearsal fixes got Claude `/code-review` only. Codex skipped per the CLAUDE.md rule (no re-queue).
