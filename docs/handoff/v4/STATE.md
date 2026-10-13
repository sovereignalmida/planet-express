# v4 STATE (update at every checkpoint, commit with it)

Branch: `v4-next` (cut from `v3-next` @ d861838). v3-production is live; no deploy without an
operator go. Protocol: `docs/designs/v4-orchestration.md`.

## Slice status
| # | Slice | Status |
|---|---|---|
| 0 | Contracts, threat model | not started (waiting on server-Claude export) |
| 1 | Mission store | not started |
| 2 | Ship's Memory | not started |
| 3 | Supervisor, adapter, worker | not started |
| 4 | Surfaces | not started |
| 5 | Checkpoint | not started |
| 6 | Tripwires and grants | not started |
| 7 | Approval, standing grants, privilege | not started |
| 8 | Verification, recovery | not started |
| 9 | First Repair, release | not started |

## Next action
Fold the server-Claude export (gap list, tool inventory, incident register, health checks) into a
Slice 0 spec; run G1 (Codex) on it; then start slices 1 and 2 in parallel worktrees.

## Decisions since the brief
- 2026-10-13: brief §14 decisions 1-8 recorded.

## Open
- Server-Claude export in progress (operator).
- Push of `v4-next` to origin: not yet authorised.
- MOS test VM (192.168.122.159) was unreachable on 2026-10-13; needed before slice 6.

## Usage / account
Primary orchestrator: Claude Code, account A. Budget notes: (none yet).

## Resume commands
- Tests: `pytest tests -x -q` (targeted first).
- Review: `codex review --base v4-next` / `--uncommitted`; Claude: `/code-review`.
