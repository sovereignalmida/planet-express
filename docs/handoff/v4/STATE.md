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
| 9 | Release proof, rehearsal harness, docs | Brief written (`S9-brief.md`); execution status unconfirmed. Real-host rehearsal done 2026-10-20 on MOS_TEST_2 + PE_UBUNTU_TEST |
| 10 | Typed steps from `mission` origin | Not started; carried items in `S10-notes.md` (execution-safety, second review required) |

## Next action
**Handoff 2026-10-22 (session limit).** G4-F2 REPORTED DONE (all 8 items + 5b, 6148 non-privileged tests pass, nothing declined) but NOT MERGED and NOT reviewed: its Codex round failed on the Codex usage limit (retry after 4:03 PM), `/code-review` not run. Branch (branch `worktree-agent-a67ad72f675cb4906`, worktree under `.claude/worktrees/`; brief `G4-brief-F2.md` + item 5b). On pick-up: check `git branch --list 'worktree-agent-*'` for its commits, run the non-privileged suite on it, `codex review --base v4-next`, merge --no-ff into v4-next, then rerun the item 7 rehearsal. Then F3 (G4-review.md rows 11-21), S10 (`S10-notes.md`), final G4 re-run. PE_UBUNTU_TEST (192.168.122.142) is left RUNNING and set up (compose-v2, `planetexpress` user, launcher repaired; no rehearsal containers left); shut it down with `virsh shutdown PE_UBUNTU_TEST` when done. v4-next is ~12 commits ahead of origin, NOT pushed. Estimate to ship: 2-4 working days.
(2026-10-22: continued on a second Claude account after account A hit its usage limit.)
1. **G4 release review**: record in `G4-review.md`. Codex C1 FIXED (discovery, merged 71cde47). code-review rows 1-8 = batch F2 (brief `G4-brief-F2.md`, worker running, plus 5b: side-door gate refuses symlinked docker.socket on stock systemd hosts, found by the real rehearsal); rows 11-21 = batch F3 (to launch after F2 merges: same files). Row 9 -> S10. Row 10 documented. /cso clean. Also fixed by orchestrator: default grant Budgets exceeded default site maxima (bare propose always refused).
1b. S9 MERGED. Real-VM results 2026-10-22 on PE_UBUNTU_TEST (docker-compose-v2 installed there; launcher repair+probe done with core user `planetexpress`, workspace /srv/pe-workspaces): privileged suite 258 passed / 6 skipped. Repair rehearsal (item 7) NOT yet passing: harness works against real Docker, blocked by 5b above; rerun after F2 merges (`PE_PYTHON=/opt/pe-venv/bin/python scripts/run_rehearsal.sh --vm 192.168.122.142 -- --core-user planetexpress`). Never set PE_REHEARSAL_SKIP_GATE (the permission classifier blocks it, rightly).
2. Open S7b items: token shim not started by the runtime (decision: relax proxy socket auth to SO_PEERCRED uid+cgroup binding, token as defence in depth, set DOCKER_HOST); read the S7b /code-review result.
3. Confirm/execute S9, then S10 (see `S10-notes.md`).
4. Operator-owned: live deploy of v3-production (PRs #14/#15 merged at 79648e6); push v4-next (3 commits ahead of origin on 2026-10-22).

Privileged tests on MOS: `PE_PYTHON=/mnt/data/pe/venv/bin/python scripts/run_privileged_tests.sh --vm 192.168.122.159`.
Test VMs: MOS_TEST_2 192.168.122.159 (direct strategy; /mnt/data chowned root:root 755, operator-approved 2026-10-21); PE_UBUNTU_TEST 192.168.122.142 (sudo strategy). launch.conf is in /etc/planetexpress-launch (MOS: <PE_HOME>/launch).
Known carry: the setup_steps CLI does not yet pass the MOS conf/helper-store paths.

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
