# v4 STATE (update at every checkpoint, commit with it)

Branch: `v4-next` (cut from `v3-next` @ d861838). v3-production is live; no deploy without an
operator go. Protocol: `docs/designs/v4-orchestration.md`.

## Slice status
| # | Slice | Status |
|---|---|---|
| 0 | Contracts, threat model | spec rev 0.3 (`v4-slice-0-contracts.md`); two Codex G1 rounds folded in (§19, §20); MOS cgroup freeze/kill verified; needs operator sign-off |
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
