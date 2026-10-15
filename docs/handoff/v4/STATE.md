# v4 STATE (update at every checkpoint, commit with it)

Branch: `v4-next` (cut from `v3-next` @ d861838). v3-production is live; no deploy without an
operator go. Protocol: `docs/designs/v4-orchestration.md`.

## Slice status
| # | Slice | Status |
|---|---|---|
| 0 | Contracts, threat model | DONE rev 0.4 (final G1 dispositioned); was: spec rev 0.3 (`v4-slice-0-contracts.md`); two Codex G1 rounds folded in (§19, §20); MOS cgroup freeze/kill verified; needs operator sign-off |
| 1 | Mission store | MERGED to v4-next (9870e21 + merge); Codex G3 done by worker (3 fixed); Claude /code-review round 1 done: 5 fixes merged |
| 2 | Ship's Memory | MERGED to v4-next (d744c2a + merge); Codex G3 x2 done (2 fixed); v6 DDL combined with slice 1 in core/store.py |
| 3 | Supervisor, adapter, worker | 3a+3b+3c MERGED incl. review round 2 (a4de086). Codex G3 on merged slice 3 done: 4 findings carried into S3d-brief.md. 3d MERGED (7d15f93 + fixes): launcher built; privileged tests GREEN in docker (247 pass) and on MOS VM direct strategy (175 pass; sudo tests skipped, no sudo on MOS). Codex 3d gate: ~9 rounds fixed; last P1 (default workspace under 0700 state dir) fixed via preflight 'workspace root reachable' check. Claude /code-review of 3d still TO DO. Not wired into setup plan/wizard (standalone `python -m planet_express.missions.launcher.setup_steps probe|repair|undo`); MOS post-start.sh boot hook still to add. Deviations from §22/22.8 listed in the S3d agent report: Core pre-creates sockets, per-session workspace 02770, missions.worker_user retired, PE_SESSION_TOKEN_FD always 3 |
| 4 | Surfaces | MERGED (round 1 + round 2 fixes, 10 Claude findings). Codex G3 on round 2 PENDING (usage limit until 21:05 WEST 2026-10-13): run `codex review --base 44c38e5` on the slice-4 diff |
| 5 | Checkpoint | not started |
| 6 | Tripwires and grants | not started |
| 7 | Approval, standing grants, privilege | not started |
| 8 | Verification, recovery | not started |
| 9 | First Repair, release | not started |

## Next action
3d Claude /code-review DONE (5 fixed, rest logged: result.json readability, repair_sessions_table w/o snapshot, sweep_uids stale rows). S5-brief.md and S6-brief.md written and Codex-G1'd (3 P1s fixed). S5 MERGED (checkpoint; 3 Codex rounds + code-review fixed; privileged green docker+MOS; MOS parity NOT claimed until a real-compose conformance run; accepted deviation: compose config output is bounded-by-memory not kill-on-exceed; carried to S7: actor from authenticated identity, supervisor calls capture_post after session dead). NEXT: S6 dispatch (builds on S5 Protocol); wire setup steps + MOS boot hook; Ubuntu (sudo strategy) rehearsal VM; Phase-1 exit rehearsal (real incident -> mission -> contextualised session survives restart); then slice 5 (checkpoint) and 6 (tripwires+grants) specs + Codex G1 BEFORE any code. Run privileged tests on MOS with: PE_PYTHON=/mnt/data/pe/venv/bin/python scripts/run_privileged_tests.sh --vm 192.168.122.159. Previous text follows.

Operator signs off slice 0 (`docs/designs/v4-slice-0-contracts.md` rev 0.3); then slices 1 and 2 in parallel worktrees (briefs:
`docs/handoff/v4/S1-brief.md`, `S2-brief.md`, not yet written).

## Decisions since the brief
- 2026-10-13: brief §14 decisions 1-8 recorded.

## Open
- Pushed `v4-next` to origin (public repo: keep infra detail out of tracked files; private inputs go in gitignored `docs/private/`).
- Operator-side findings from the export, separate from v4: legacy sudoers wildcard line, open Docker TCP socket, broken VPN port-forward cycle, leftover timers, AdGuard in paused_containers, two credentials to rotate.
- MOS test VM was unreachable on 2026-10-13; needed before slice 6.

## Usage / account
Primary orchestrator: Claude Code, account A. Budget notes: (none yet).

## Resume commands
- Tests: `pytest tests -x -q` (targeted first).
- Review: `codex review --base v4-next` / `--uncommitted`; Claude: `/code-review`.

## Deferred findings (owned by a later slice)
- Slice 7: standing-grant `window_json` is stored but not evaluated; grant `protected`/`exclusions` not cross-checked against standing scope or plan (spec §5.2 needs a window format).
- Slice 3: supervisor must confirm session dead BEFORE requesting a terminal transition (terminal transition now frees leases/session in one txn).
- Existing v3 code review findings (not v4): quiet re-scan dropped when refused in a maintenance window; MOS `docker-compose config --format json` may fail on Compose v1; severity wobble re-planning. Unverified.

## Review debt
- Codex usage limit hit 2026-10-13 ~20:40 WEST (resets 21:05). Anything reviewed only by Claude until then must get `codex review` afterward: slice 4 round 2.
