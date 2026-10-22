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
**Handoff 2026-10-22 (token budget).** All workers finished and merged into `v4-next` (62 commits ahead of origin, NOT pushed). Non-privileged suite 6262 passed. No background agents running.
Merged: S9 (release proof), S9b (rehearsal agent through the real Docker proxy, scenarios end cleanly), G4-F1 (protected-set discovery, fail closed), G4-F2 (8 safety fixes + 13 follow-up findings, Codex clean), G4-F3 (rows 11-21, Codex clean), G4-F4 (`container:<in-scope>` network mode + `GET /version` through the proxy; Codex 2 P1s fixed), plus the grant-budget default fix. G4 record: `G4-review.md` (F2 follow-up dispositions are in the F2 worker reports: all 13 fixed, none declined; F3 and F4 likewise).
**Do next, in order:**
1. Rerun the item 7 repair rehearsal on PE_UBUNTU_TEST (192.168.122.142, left running; launcher set up with core user `planetexpress`, workspace /srv/pe-workspaces, uid range 200000-200999): `PE_PYTHON=/opt/pe-venv/bin/python scripts/run_rehearsal.sh --vm 192.168.122.142 -- --core-user planetexpress > record.json`. Read `proxy.namespace.refused` / `proxy.canary.refused` in the record. Expect further real-host findings; F4 notes `GET /info` is NOT served (decision pending: add a filtered/fixed-field passthrough only if compose really needs it). Never set PE_REHEARSAL_SKIP_GATE (the permission classifier blocks it).
2. Re-run the privileged suite on the VM (last: 259 passed, 5 skipped; one unreproduced fd-leak flake in `test_exactly_fds_0_1_2_3...[sudo]`, watch for it): `PE_PYTHON=/opt/pe-venv/bin/python PE_VM_DIR=/opt/pe-privileged-tests scripts/run_privileged_tests.sh --vm 192.168.122.142`.
3. Claude `/code-review` on F3 and F4 was NOT run (Codex was). Run it before calling them done.
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
