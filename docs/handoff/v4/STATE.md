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
| 10 | Typed steps from `mission` origin | BUILT 2026-10-23 (`typed_steps.py`, `run_mission_runbook`); Claude `/code-review high` 9 findings fixed; Codex skipped (usage limit). NOT exposed to the worker (needs its own decision + review); see `S10-notes.md` |

## Next action
**Handoff 2026-10-22 (token budget).** All workers finished and merged into `v4-next` (62 commits ahead of origin, NOT pushed). Non-privileged suite 6262 passed. No background agents running.
Merged: S9 (release proof), S9b (rehearsal agent through the real Docker proxy, scenarios end cleanly), G4-F1 (protected-set discovery, fail closed), G4-F2 (8 safety fixes + 13 follow-up findings, Codex clean), G4-F3 (rows 11-21, Codex clean), G4-F4 (`container:<in-scope>` network mode + `GET /version` through the proxy; Codex 2 P1s fixed), plus the grant-budget default fix. G4 record: `G4-review.md` (F2 follow-up dispositions are in the F2 worker reports: all 13 fixed, none declined; F3 and F4 likewise).
**Do next, in order:**
1. Item 7 passed on PE_UBUNTU_TEST on 2026-10-23: namespace repair completed through the proxy (15 writes/3 creates), the failed canary rolled back, the service-scoped correction completed (5 writes/1 create), no proxy refusals, missions released and teardown empty. Record: `/tmp/planet-express-rehearsal-20261023-final.json` on the operator host. The run exposed and fixed Compose inspect-shaped create defaults, checkpoint comparison of generated labels, an in-flight Docker/firewall tripwire race, service-scoped project-network reattachment, and a stale harness baseline. `GET /info` was not needed.
2. Privileged suite re-run 2026-10-23 after the item 7 fixes: 259 passed, 5 skipped; the earlier fd-leak flake in `test_exactly_fds_0_1_2_3...[sudo]` did not recur.
3. Claude `/code-review high` on F3 and F4 ran 2026-10-23 (9 findings, all dispositioned; Codex had already reviewed them). Fixed: scans are serialised (`begin()` on the supervisor thread and the monitor thread shared one Scanner and could report each other as timeouts); `container:<target>` now also requires each network the target is attached to to pass the same scope rule as a direct attach; the start-deferral timer resets on a failed start and when a mission settles; `/version` clamps `Components[].Details.ApiVersion`; the brief's data-handling text has one source; a stale `baseline.prev.json` is cleared when there is nothing to set aside. Left, with reasons: start check on host/`container:` network mode applies only to containers carrying the proxy's CREATED_LABEL, so existing in-scope containers (Plex, gluetun-attached apps) are unaffected (finding was a false positive); `worker:decision` actor is deliberate (a worker's free-text notes must not forge cards) and v4 has no pre-upgrade cards; `_mission_locks` (one Lock per mission) and the 256-entry usage cache re-read from the log on a miss, so neither affects correctness; the launcher preflight on the poll thread is the existing design, re-proved at most every 600 s.
   Item 7 fixes got `/code-review high` on 2026-10-23 (Codex skipped, usage limit). Dispositions: tripwire deferral no longer skips the scan, it excuses only `firewall_changed` while a proxy write is unresolved or just finished, capped at 120 s of continuous cover (deliberate: a container stop takes ~10 s and Docker rewrites its rules throughout, and a 5 s cap failed the rehearsal); `docker.result` now closes on upstream failure (status 0); in-flight query bounded to the event tail; label filter shared (`comparable_labels`) and narrowed to Compose's per-recreate labels, `config-hash` included because it is measured unstable under `network_mode: service:` (rehearsal re-run proves it); snapshot failure is logged; stale/corrupt deferral state tolerated. Left as designed: project-network reattachment (needs the service in scope, the same project label, and prior membership at session start). Rehearsal passed again after the fixes (record `/tmp/planet-express-rehearsal-20261023-review-fixes.json`); privileged suite 259 passed, 5 skipped (the fd-leak flake showed once on [direct], 3 clean reruns).
4. S10 done (see table). Open decision: expose `run_mission_runbook` to the worker via an RPC method (allowlist change, second review), or leave it Core-only for v4.0.
5. Final G4 re-run on the finished branch (whole-branch Codex, `/code-review high`, fuller `/cso` with dependency CVE scan), then update `G4-review.md`.
   **Final G4 re-run, in progress 2026-10-23** (Claude `/code-review high` whole branch + `/cso`; Codex skipped, usage limit). `/cso`: nothing at the 8/10 bar in the v4 surface; report at the session scratchpad `cso-2026-10-23.json`. Inherited-from-v3 items: certifi stale CA bundle (bump `certifi>=2026.10.19`, check the live host venv), `deploy.sh:90` installs unhashed `>=` floors, CI actions tag-pinned and no CODEOWNERS. Below the bar: exec/attach/archive on an in-scope container do not apply `protected_mounts` (`policy.py:433`, design call), `_NAME` regexes use `$` with `.match` (`resources.py:18`, `policy.py:67`: use `fullmatch`), inspect redaction misses split Cmd args, PidsLimit/Memory/ShmSize uncapped.
   Code-review: FIXED (f505be5): scheme-less Docker TCP hosts, non-normalised grant paths, compose.dry_run fixed text (note: the broker already blanked non-2xx output, so the stderr claim was partly wrong; pass/fail stays a one-bit oracle). **STILL OPEN, in priority order:**
   a. `leases.py:65` stack lease never relates to path/mount leases under the stacks root (nobody passes `declared`): two missions can edit the same files. Plan: `MissionStore.start_session` and `Supervisor._acquire_early` pass `declared={stack: [path:<stacks_root>/<stack>]}` (stacks_root from the tripwire/assembly), test with a stack grant vs a path grant under it.
   b. `supervisor.py:588` PE_HOOK_POLICY can exceed the launcher's 4096-char env cap on hosts with many protected paths, so no Repair session starts. Plan: above ~3500 chars write the JSON into the Core-owned hook dir and pass `PE_HOOK_POLICY_FILE`; `hooks.main` reads it (bounded).
   c. `supervisor.py:984` `roll_back` does not catch `CheckpointNotFound`/`CheckpointCorrupt` from `restore()`, leaving the mission stuck in `rollback_pending`; route it through the `_rollback_failed` path the tripwire rollback uses.
   d. `tripwire/diff.py:190` a newly seen container mount on a docker named-volume path counts as `protected` (discovery marks every volume mountpoint protected) even when the proxy authorised the volume by compose label: a repair of a service that was down at baseline pauses falsely. Needs a decision on how the diff learns which volumes the proxy authorised.
   e. `supervisor.py:449` the checkpoint is created once and reused by every later start attempt, so a retry hours later can roll back the operator's later edits and verify against a stale baseline. Plan: re-take it (or refuse to reuse) when the start failed before any session launched and the host has changed.
   f. `recovery.py:128` `matches_checkpoint` calls the host unchanged when only compose and env files and container facts match, so edits to other in-scope files are missed and a cancel can close the mission as `cancelled`. Design limit of what the manifest captures; at minimum say so in the brief and the cancel text.
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
