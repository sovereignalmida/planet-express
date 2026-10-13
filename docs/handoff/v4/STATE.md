# v4 STATE (update at every checkpoint, commit with it)

Branch: `v4-next` (cut from `v3-next` @ d861838). v3-production is live; no deploy without an
operator go. Protocol: `docs/designs/v4-orchestration.md`.

## Slice status
| # | Slice | Status |
|---|---|---|
| 0 | Contracts, threat model | DONE rev 0.4 (final G1 dispositioned); was: spec rev 0.3 (`v4-slice-0-contracts.md`); two Codex G1 rounds folded in (§19, §20); MOS cgroup freeze/kill verified; needs operator sign-off |
| 1 | Mission store | MERGED to v4-next (9870e21 + merge); Codex G3 done by worker (3 fixed); Claude /code-review round 1 done: 5 fixes merged |
| 2 | Ship's Memory | MERGED to v4-next (d744c2a + merge); Codex G3 x2 done (2 fixed); v6 DDL combined with slice 1 in core/store.py |
| 3 | Supervisor, adapter, worker | next |
| 4 | Surfaces | next (parallel with 3) |
| 5 | Checkpoint | not started |
| 6 | Tripwires and grants | not started |
| 7 | Approval, standing grants, privilege | not started |
| 8 | Verification, recovery | not started |
| 9 | First Repair, release | not started |

## Next action
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
